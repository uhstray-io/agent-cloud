"""Streamed calls are charged to the per-key budget: inference-gateway-agentgateway task 2.3a.

agentgateway v1.5.0 adds stream_options.include_usage only when a streamed chat request has no
stream_options at all (crates/agentgateway/src/llm/mod.rs:1549-1560), so a client sending
{"include_usage": false} streamed with no usage chunk and was charged nothing. config.yaml.j2 closes
that with a per-model CEL body `transformation`. The render test pins the expression; the live test
runs it on the pinned image against a recording upstream when podman and the image are present.
"""

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml
from fake_http import DrainingHandler
from test_agw_listener_tls import _render

IMAGE = "cr.agentgateway.dev/agentgateway:v1.5.0"
EXPR = ('has(llmRequest.stream) && llmRequest.stream == true && has(llmRequest.stream_options) '
        '? llmRequest.stream_options.merge({"include_usage": true}) : llmRequest.stream_options')


def test_every_model_forces_usage_on_streams(tmp_path):
    cfg = _render(tmp_path, agw_models=[{"name": "a"}, {"name": "b", "upstream_model": "org/b"}])
    models = cfg["llm"]["models"]
    assert [m["name"] for m in models] == ["a", "b"]
    for m in models:
        assert m["transformation"] == {"stream_options": EXPR}, m["name"]


def _image_present() -> bool:
    if not shutil.which("podman"):
        return False
    r = subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True)
    return r.returncode == 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not _image_present(), reason=f"podman or {IMAGE} not available")
def test_the_transformation_on_the_pinned_image(tmp_path):
    seen = []

    class Up(DrainingHandler):
        def log_message(self, *_a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            data = json.dumps({"id": "x", "object": "chat.completion", "created": 1, "model": body["model"],
                               "choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": "hi"}}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: [DONE]\n\n")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data.encode())

    up = ThreadingHTTPServer(("0.0.0.0", 0), Up)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    cfg = _render(tmp_path, agw_upstream_base_url=f"http://host.containers.internal:{up.server_port}/v1")
    model = cfg["llm"]["models"][0]
    live = {"config": {"adminAddr": "127.0.0.1:15000"}, "gateways": {"default": {"port": 4000}},
            "llm": {"gateways": ["default"], "models": [{
                "name": "m", "provider": model["provider"],
                "params": {"baseUrl": model["params"]["baseUrl"]},
                "transformation": model["transformation"]}]}}
    (tmp_path / "live.yaml").write_text(yaml.safe_dump(live))
    port, name = _free_port(), f"agw-stream-usage-{_free_port()}"
    subprocess.run(["podman", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:4000",
                    "-v", f"{tmp_path / 'live.yaml'}:/config.yaml:ro", IMAGE, "-f", "/config.yaml"],
                   check=True, capture_output=True)
    try:
        def post(extra):
            body = {"model": "m", "messages": [{"role": "user", "content": "x"}], **extra}
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                         json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()

        deadline = time.time() + 30
        while True:
            try:
                post({})
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        seen.clear()
        post({"stream": True})
        post({"stream": True, "stream_options": {}})
        post({"stream": True, "stream_options": {"include_usage": False, "continuous_usage_stats": True}})
        post({"stream": False, "stream_options": {"include_usage": False}})
        post({})
        assert [b.get("stream_options") for b in seen] == [
            {"include_usage": True},
            {"include_usage": True},
            {"include_usage": True, "continuous_usage_stats": True},
            {"include_usage": False},
            None,
        ], seen
    finally:
        subprocess.run(["podman", "rm", "-f", name], capture_output=True)
        up.shutdown()
