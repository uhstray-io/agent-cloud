"""Streamed calls are charged to the per-key budget: inference-gateway-agentgateway task 2.3a.

agentgateway v1.5.0 adds stream_options.include_usage only when a streamed chat request has no
stream_options at all (crates/agentgateway/src/llm/mod.rs:1549-1560 at tag v1.5.0), and it settles
a budget from the last usage it parsed (crates/llm/src/conversion/completions.rs:1207-1219,
crates/agentgateway/src/telemetry/log.rs:1279-1280). Two bypasses followed: a client sending
{"include_usage": false}, and a client disconnecting after the answer but before vLLM's separate
final usage chunk. config.yaml.j2 closes both with a per-model CEL body `transformation` that sends
every streamed chat request with include_usage and continuous_usage_stats (usage on every chunk),
and leaves Responses requests alone. The render test pins the expression; the live tests run it on
the pinned image when podman has it.
"""

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml
from fake_http import DrainingHandler
from test_agw_listener_tls import _render

IMAGE = "cr.agentgateway.dev/agentgateway:v1.5.0"
EXPR = ('has(llmRequest.messages) && has(llmRequest.stream) && llmRequest.stream == true '
        '? (has(llmRequest.stream_options) && type(llmRequest.stream_options) == map '
        '? llmRequest.stream_options : {}).merge({"include_usage": true, "continuous_usage_stats": true}) '
        ': llmRequest.stream_options')
FORCED = {"include_usage": True, "continuous_usage_stats": True}
KEY = "synthetic-budget-key-5e1d"
USAGE = {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}


def test_every_model_forces_usage_on_streams(tmp_path):
    cfg = _render(tmp_path, agw_models=[{"name": "a"}, {"name": "b", "upstream_model": "org/b"}])
    models = cfg["llm"]["models"]
    assert [m["name"] for m in models] == ["a", "b"]
    for m in models:
        assert m["transformation"] == {"stream_options": EXPR}, m["name"]


def _image_present() -> bool:
    if not shutil.which("podman"):
        return False
    return subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True).returncode == 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream:
    """Records every request body. A chat stream behaves like vLLM: the answer chunk carries
    usage only under continuous_usage_stats, then after `tail_delay` comes the separate final
    usage chunk (only under include_usage) and [DONE]."""

    def __init__(self, tail_delay=0.0):
        self.seen = []
        self.tail_delay = tail_delay
        up = self

        class H(DrainingHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                up.seen.append({"path": self.path, "body": body})
                if not body.get("stream"):
                    data = json.dumps({"id": "x", "object": "chat.completion", "created": 1,
                                       "model": body["model"], "usage": USAGE, "choices": [
                                           {"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": "hi"}}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                so = body.get("stream_options") or {}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                answer = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                          "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]}
                if so.get("include_usage") and so.get("continuous_usage_stats"):
                    answer["usage"] = USAGE
                try:
                    self.wfile.write(f"data: {json.dumps(answer)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(up.tail_delay)
                    if so.get("include_usage"):
                        final = {**answer, "choices": [], "usage": USAGE}
                        self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    pass  # the gateway closed the upstream after the client left

        self.server = ThreadingHTTPServer(("0.0.0.0", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def _live_config(tmp_path, upstream_url, transformation, budget):
    rendered = _render(tmp_path, agw_upstream_base_url=upstream_url)["llm"]["models"][0]
    model = {k: v for k, v in rendered.items() if k != "transformation"}
    model["params"] = {"baseUrl": upstream_url}
    if transformation:
        model["transformation"] = rendered["transformation"]
    llm = {"gateways": ["default"], "models": [model]}
    config = {"adminAddr": "127.0.0.1:15000"}
    if budget:
        config["database"] = {"url": "sqlite:///tmp/budget.db"}
        llm["policies"] = {"apiKey": {"mode": "strict",
                                      "location": {"header": {"name": "authorization", "prefix": "Bearer "}},
                                      "keys": [{"key": KEY, "metadata": {"name": "c1"}, "budgets": [
                                          {"name": "hourly-tokens", "limit": {"unit": "Tokens", "amount": 10},
                                           "window": {"rolling": "1h"}, "onBudgetExceeded": "Block"}]}]}}
    return {"config": config, "gateways": {"default": {"port": 4000}}, "llm": llm}


@pytest.fixture
def upstream():
    made = []

    def make(**kw):
        u = Upstream(**kw)
        made.append(u)
        return u

    try:
        yield make
    finally:
        for u in made:
            u.close()


@pytest.fixture
def gateway(tmp_path):
    """gateway(upstream, transformation=True, budget=False) -> port of a running v1.5.0 gateway."""
    started = []

    def start(up, transformation=True, budget=False):
        url = f"http://host.containers.internal:{up.server.server_port}/v1"
        path = tmp_path / f"live-{len(started)}.yaml"
        path.write_text(yaml.safe_dump(_live_config(tmp_path, url, transformation, budget)))
        port, name = _free_port(), f"agw-stream-usage-{_free_port()}"
        started.append(name)
        subprocess.run(["podman", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:4000",
                        "-v", f"{path}:/config.yaml:ro", IMAGE, "-f", "/config.yaml"],
                       check=True, capture_output=True)
        deadline = time.time() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
                break
            except urllib.error.HTTPError:
                break  # the listener answers
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        return port

    try:
        yield start
    finally:
        for name in started:
            subprocess.run(["podman", "rm", "-f", name], capture_output=True)


def _post(port, path, body, key=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _chat(**extra):
    return {"model": "m", "messages": [{"role": "user", "content": "x"}], **extra}


live = pytest.mark.skipif(not _image_present(), reason=f"podman or {IMAGE} not available")


@live
def test_the_transformation_on_the_pinned_image(gateway, upstream):
    up = upstream()
    port = gateway(up)
    for body in (_chat(stream=True), _chat(stream=True, stream_options={}),
                 _chat(stream=True, stream_options={"include_usage": False, "x_keep": True}),
                 _chat(stream=True, stream_options=None),
                 _chat(stream=False, stream_options={"include_usage": False}), _chat()):
        assert _post(port, "/v1/chat/completions", body) == 200, body
    assert [s["body"].get("stream_options") for s in up.seen] == [
        FORCED, FORCED, {**FORCED, "x_keep": True}, FORCED, {"include_usage": False}, None], up.seen


@live
def test_a_responses_stream_keeps_its_stream_options(gateway, upstream):
    up = upstream()
    port = gateway(up)
    _post(port, "/v1/responses", {"model": "m", "input": "x", "stream": True,
                                  "stream_options": {"include_obfuscation": False}})
    assert [(s["path"], s["body"].get("stream_options")) for s in up.seen] == [
        ("/v1/responses", {"include_obfuscation": False})], up.seen


def _stream_then_disconnect(port):
    """Opt out of usage, read until the answer arrives, then drop the connection before vLLM's
    final usage chunk (the upstream holds it back for 3 s)."""
    body = json.dumps(_chat(stream=True, stream_options={"include_usage": False})).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: gw\r\nContent-Type: application/json\r\n"
                  + f"Authorization: Bearer {KEY}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        buf = b""
        while b'"hi"' not in buf:
            chunk = s.recv(4096)
            assert chunk, buf
            buf += chunk
    time.sleep(1.5)  # the gateway notices the close and settles the request


@live
@pytest.mark.parametrize("transformation, charged", [(True, True), (False, False)])
def test_a_stream_dropped_after_the_answer_is_still_charged(gateway, upstream, transformation, charged):
    # Budget 10 tokens; the stream costs 50. Charged -> the next request is blocked (429). Without
    # the transformation the same disconnect leaves the budget untouched: the bypass is real.
    up = upstream(tail_delay=3.0)
    port = gateway(up, transformation=transformation, budget=True)
    _stream_then_disconnect(port)
    status = _post(port, "/v1/chat/completions", _chat(), key=KEY)
    assert status == (429 if charged else 200), status
