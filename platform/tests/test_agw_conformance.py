"""Gateway conformance against direct vLLM (change inference-gateway-agentgateway, tasks 2.1, 2.2).

conformance.sh runs for real against two stub OpenAI-compatible servers on loopback, one standing
in for the gateway and one for vLLM, both speaking JSON and SSE. The stubs answer every case the
script sends, can be told to misbehave the ways a gateway could (strip a field, buffer a stream,
not route the Responses API), and record every request so the test can see what arrived and which
key it carried. A curl shim on PATH records every argv, so "no key on a command line" is asserted
on what was executed, not on source text.

run-agw-conformance.yml runs for real too, on localhost through harness_sandbox, against the same
stubs and a synthetic OpenBao. All values are synthetic.
"""

import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import seed_harness
import yaml
from fake_http import DrainingHandler

REPO = playbook_yaml.REPO
SCRIPT = REPO / "platform/services/agentgateway/deployment/tests/conformance.sh"
PLAYBOOK = REPO / "platform/playbooks/run-agw-conformance.yml"
GW_KEY = "synthetic-gateway-client-key-7f3a"
UP_KEY = "synthetic-vllm-upstream-key-91c2"
EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
CASES = ["models", "chat-thinking-off", *[f"effort-{e}" for e in EFFORTS], "chat-template-kwargs",
         "tool-call", "stream-xhigh", "responses"]
REAL_CURL = shutil.which("curl")


def _reorder(value):
    """Reverse every object's key order: a gateway that re-serialises JSON."""
    if isinstance(value, dict):
        return {k: _reorder(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reorder(v) for v in value]
    return value


class Stub:
    """One OpenAI-compatible server. Options:
    key           the Bearer value it requires; None requires no Authorization header at all
    drop_reasoning  strip `reasoning` from completions (a gateway losing a field)
    buffer_stream   hold every stream chunk until the end (a buffering gateway)
    responses_404   no route for the Responses API
    reorder         reverse JSON key order
    first_delay / gap  stream timing: seconds to the first token, seconds between chunks
    models          the ids its models list returns (default ["m"])
    cut_stream      end the stream cleanly right after the first token: no finish, no [DONE]
    mutate          a function applied to every 2xx JSON body and every stream chunk before it is
                    sent (a gateway adding, dropping or retyping fields)
    """

    def __init__(self, key=None, drop_reasoning=False, buffer_stream=False, responses_404=False,
                 reorder=False, first_delay=0.4, gap=0.1, models=("m",), cut_stream=False,
                 mutate=None):
        self.key, self.drop_reasoning, self.buffer_stream = key, drop_reasoning, buffer_stream
        self.responses_404, self.reorder = responses_404, reorder
        self.first_delay, self.gap = first_delay, gap
        self.models = list(models)
        self.cut_stream = cut_stream
        self.mutate = mutate
        self.seen = []
        stub = self

        class Handler(DrainingHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def do_GET(self):
                stub.handle(self, "GET", None)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                stub.handle(self, "POST", json.loads(raw or b"{}"))

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 64

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.port = self.server.server_port

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ── responses ──
    def send(self, h, status, body):
        if self.mutate and 200 <= status < 300:
            body = self.mutate(body)
        data = json.dumps(_reorder(body) if self.reorder else body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def handle(self, h, method, body):
        auth = h.headers.get("Authorization")
        self.seen.append({"method": method, "path": h.path, "auth": auth, "body": body})
        if (self.key is None and auth is not None) or (self.key is not None and auth != f"Bearer {self.key}"):
            return self.send(h, 401, {"error": {"type": "invalid_request_error", "code": "invalid_api_key",
                                                "message": "bad key"}})
        rid = f"{random.randrange(10**9)}"
        now = int(time.time()) + random.randrange(1000)
        if method == "GET" and h.path == "/v1/models":
            return self.send(h, 200, {"object": "list", "data": [
                {"id": i, "object": "model", "created": now, "owned_by": "vllm", "root": i} for i in self.models]})
        if method == "POST" and h.path == "/v1/responses":
            if self.responses_404:
                return self.send(h, 404, {"detail": "Not Found"})
            return self.send(h, 200, {"id": "resp_" + rid, "object": "response", "created_at": now,
                                      "status": "completed", "model": body["model"], "output": [
                                          {"id": "rs_" + rid, "type": "reasoning", "summary": []},
                                          {"id": "msg_" + rid, "type": "message", "role": "assistant",
                                           "content": [{"type": "output_text", "text": "204"}]}]})
        if method == "POST" and h.path == "/v1/chat/completions":
            thinking = body.get("reasoning_effort") != "none" and \
                (body.get("chat_template_kwargs") or {}).get("enable_thinking", True)
            if body.get("stream"):
                return self.stream(h, body, rid, now, thinking)
            msg = {"role": "assistant", "content": "204", "reasoning": "12*17 is 204" if thinking else None}
            finish = "stop"
            if body.get("tools"):
                msg["content"] = None
                msg["tool_calls"] = [{"id": "call_" + rid, "type": "function", "function": {
                    "name": "get_weather", "arguments": json.dumps({"city": "Newark, NJ"})}}]
                finish = "tool_calls"
            if self.drop_reasoning:
                msg.pop("reasoning")
            return self.send(h, 200, {"id": "chatcmpl-" + rid, "object": "chat.completion", "created": now,
                                      "model": body["model"], "choices": [
                                          {"index": 0, "message": msg, "finish_reason": finish}],
                                      "usage": {"prompt_tokens": 9, "completion_tokens": 7,
                                                "completion_tokens_details": {
                                                    "reasoning_tokens": 5 if thinking else 0}}})
        self.send(h, 404, {"detail": "Not Found"})

    def stream(self, h, body, rid, now, thinking):
        def chunk(delta, finish=None):
            event = {"id": "chatcmpl-" + rid, "object": "chat.completion.chunk", "created": now,
                     "model": body["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            return ("data: " + json.dumps(self.mutate(event) if self.mutate else event) + "\n\n").encode()

        parts = [(0.0, chunk({"role": "assistant", "content": ""})), (0.0, b": keep-alive\n\n")]
        tokens = [{"reasoning": "think"}] * 3 if thinking else []
        tokens += [{"content": "2"}, {"content": "04"}]
        for i, delta in enumerate(tokens):
            if self.drop_reasoning:
                delta = {k: v for k, v in delta.items() if k != "reasoning"} or {"content": ""}
            parts.append((self.first_delay if i == 0 else self.gap, chunk(delta)))
        if self.cut_stream:
            parts = parts[:3]  # role chunk, keep-alive, first token
        else:
            parts.append((self.gap, chunk({}, "stop")))
            parts.append((0.0, b"data: [DONE]\n\n"))
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Connection", "close")
        h.end_headers()
        if self.buffer_stream:
            time.sleep(sum(d for d, _ in parts))
            h.wfile.write(b"".join(p for _, p in parts))
            h.wfile.flush()
            return
        for delay, part in parts:
            time.sleep(delay)
            h.wfile.write(part)
            h.wfile.flush()


@pytest.fixture
def stubs():
    made = []

    def make(**kw):
        s = Stub(**kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.stop()


def _shim(tmp: Path, exit_code: int | None = None) -> Path:
    """A curl on PATH that records each argv (one argument per line, `--` between calls), then
    runs the real curl, or exits with `exit_code` (a connection failure) without running it."""
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    tail = f"exit {exit_code}" if exit_code is not None else f'exec "{REAL_CURL}" "$@"'
    (bindir / "curl").write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do printf "%s\\n" "$a" >> "$SHIM_LOG"; done\n'
        'printf -- "--\\n" >> "$SHIM_LOG"\n' + tail + "\n")
    (bindir / "curl").chmod(0o755)
    return bindir


def _run(tmp: Path, gw: Stub, direct: Stub, *, direct_key=True, env=None, shim_exit=None, path_dirs=()):
    out = tmp / "out"
    out.mkdir(exist_ok=True)
    (tmp / "gw.key").write_text(GW_KEY)
    (tmp / "up.key").write_text(UP_KEY + "\n")
    for f in ("gw.key", "up.key"):
        (tmp / f).chmod(0o600)
    bindir = _shim(tmp, shim_exit)
    full_env = {
        "PATH": os.pathsep.join([*map(str, path_dirs), str(bindir), os.environ["PATH"]]),
        "SHIM_LOG": str(tmp / "argv.log"),
        "AGW_CONF_OUT": str(out),
        "AGW_CONF_GATEWAY_URL": gw.url,
        "AGW_CONF_GATEWAY_KEY_FILE": str(tmp / "gw.key"),
        "AGW_CONF_DIRECT_URL": direct.url,
        "AGW_CONF_DIRECT_KEY_FILE": str(tmp / "up.key") if direct_key else "",
        "AGW_CONF_MODEL": "m",
        "AGW_CONF_TIMEOUT": "30",
        **(env or {}),
    }
    r = subprocess.run(["bash", str(SCRIPT), "run"], env=full_env, capture_output=True, text=True, timeout=180)
    results = out / "results.jsonl"
    lines = [json.loads(x) for x in results.read_text().splitlines()] if results.exists() else []
    return r, lines


def _diff(tmp: Path, model_map: dict | None = None) -> dict:
    extra = []
    if model_map is not None:
        (tmp / "model-map.json").write_text(json.dumps(model_map))
        extra = [str(tmp / "model-map.json")]
    r = subprocess.run(["bash", str(SCRIPT), "diff", str(tmp / "out" / "results.jsonl"), *extra],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _case(report, name):
    return next(c for c in report["cases"] if c["case"] == name)


def _no_key_anywhere(tmp: Path, r: subprocess.CompletedProcess):
    argv = (tmp / "argv.log").read_text()
    for key in (GW_KEY, UP_KEY):
        assert key not in argv, "a key reached curl's argv"
        assert key not in r.stdout + r.stderr
        assert key not in (tmp / "out" / "results.jsonl").read_text()


# ── 2.1: the cases, the keys, the output ──────────────────────────────────────

def test_identical_targets_match_every_case_and_ids_timestamps_and_key_order_are_ignored(tmp_path, stubs):
    # Both stubs mint fresh ids and timestamps per response, and the "gateway" reverses key order.
    gw, direct = stubs(key=GW_KEY, reorder=True), stubs(key=UP_KEY)
    r, lines = _run(tmp_path, gw, direct)
    assert r.returncode == 0, r.stderr
    assert [(x["case"], x["target"]) for x in lines] == [(c, t) for c in CASES for t in ("gateway", "direct")]
    assert all(x["status"] == 200 and x["curl_exit"] == 0 for x in lines), lines
    report = _diff(tmp_path)
    assert report["verdict"] == "pass" and report["matched"] == report["total"] == len(CASES), report
    assert all(c["exact_body_match"] for c in report["cases"]), report
    _no_key_anywhere(tmp_path, r)
    # The output carries no body text: neither the stub's completion nor its reasoning.
    text = (tmp_path / "out" / "results.jsonl").read_text()
    assert "12*17 is 204" not in text and '"204"' not in text and "Newark" not in text


def test_each_target_receives_its_own_key_from_a_file_never_from_argv(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    r, _ = _run(tmp_path, gw, direct)
    assert r.returncode == 0, r.stderr
    assert {s["auth"] for s in gw.seen} == {f"Bearer {GW_KEY}"}
    assert {s["auth"] for s in direct.seen} == {f"Bearer {UP_KEY}"}  # the file's trailing newline is dropped
    assert len(gw.seen) == len(direct.seen) == len(CASES)
    _no_key_anywhere(tmp_path, r)


def test_an_upstream_without_a_key_gets_no_authorization_header(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY), stubs(key=None)
    r, lines = _run(tmp_path, gw, direct, direct_key=False)
    assert r.returncode == 0, r.stderr
    assert {s["auth"] for s in direct.seen} == {None}
    assert all(x["status"] == 200 for x in lines)


def test_the_requests_carry_every_effort_the_kwargs_tools_stream_and_responses_shape(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    r, _ = _run(tmp_path, gw, direct)
    assert r.returncode == 0, r.stderr
    bodies = [s["body"] for s in direct.seen if s["path"] == "/v1/chat/completions"]
    efforts = [b["reasoning_effort"] for b in bodies if "reasoning_effort" in b and not b.get("stream")
               and not b.get("tools")]
    assert efforts == EFFORTS
    assert {"enable_thinking": False} in [b.get("chat_template_kwargs") for b in bodies]
    assert {"reasoning_effort": "low"} in [b.get("chat_template_kwargs") for b in bodies]
    assert any(b.get("tools") and b["tools"][0]["function"]["name"] == "get_weather" for b in bodies)
    streamed = [b for b in bodies if b.get("stream")]
    assert len(streamed) == 1 and streamed[0]["reasoning_effort"] == "xhigh"
    responses = [s["body"] for s in direct.seen if s["path"] == "/v1/responses"]
    assert len(responses) == 1 and responses[0]["reasoning"] == {"effort": "low"}
    assert "reasoning_effort" not in responses[0]
    # The gateway and vLLM got the same request bodies.
    assert [s["body"] for s in gw.seen] == [s["body"] for s in direct.seen]


def test_the_gateway_and_vllm_model_names_can_differ(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    r, _ = _run(tmp_path, gw, direct, env={"AGW_CONF_DIRECT_MODEL": "upstream-id"})
    assert r.returncode == 0, r.stderr
    posts = lambda s: {x["body"]["model"] for x in s.seen if x["method"] == "POST"}  # noqa: E731
    assert posts(gw) == {"m"} and posts(direct) == {"upstream-id"}


def test_stream_timing_measures_first_token_not_first_byte_and_the_chunk_gaps(tmp_path, stubs):
    # The role chunk and a keep-alive arrive at once; the first token 0.4 s later; then 0.1 s gaps.
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY, first_delay=0.4, gap=0.1)
    r, lines = _run(tmp_path, gw, direct)
    assert r.returncode == 0, r.stderr
    d = next(x for x in lines if x["case"] == "stream-xhigh" and x["target"] == "direct")
    assert 0.35 <= d["timing"]["ttft_s"] < 1.5, d
    assert d["timing"]["keepalives"] == 1
    gaps = d["timing"]["gaps"]
    assert gaps["count"] == 5 and 0.07 <= gaps["p50_s"] <= 0.3, gaps
    assert d["semantic"] == {"finish_reason": "stop", "has_content": True, "has_reasoning": True, "done": True,
                             "error_event": False}


def test_a_buffering_gateway_shows_in_the_timing_deltas(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY, buffer_stream=True), stubs(key=UP_KEY)
    r, lines = _run(tmp_path, gw, direct)
    assert r.returncode == 0, r.stderr
    g = next(x for x in lines if x["case"] == "stream-xhigh" and x["target"] == "gateway")
    assert g["timing"]["gaps"]["max_s"] < 0.05, g  # every chunk arrived in one burst
    delta = _case(_diff(tmp_path), "stream-xhigh")["timing_delta"]
    assert delta["ttft_s"] > 0.3 and delta["gap_max_s"] < 0, delta


# ── 2.2: the comparison ───────────────────────────────────────────────────────

def test_a_gateway_that_strips_reasoning_fails_and_the_report_names_the_field(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY, drop_reasoning=True), stubs(key=UP_KEY)
    r, _ = _run(tmp_path, gw, direct)
    assert r.returncode == 0, r.stderr
    report = _diff(tmp_path)
    assert report["verdict"] == "fail"
    high = _case(report, "effort-high")
    assert high["verdict"] == "differ" and high["status_match"] and not high["shape_match"]
    assert {"key": "has_reasoning", "gateway": False, "direct": True} in high["semantic_diff"]
    # Thinking off: no reasoning either way, so stripping it changes nothing the client sees.
    assert _case(report, "effort-none")["semantic_match"]
    assert "effort-high" in report["not_matched"] and "models" not in report["not_matched"]


def test_a_gateway_without_the_responses_route_fails_on_status(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY, responses_404=True), stubs(key=UP_KEY)
    r, _ = _run(tmp_path, gw, direct)
    assert r.returncode == 0, r.stderr
    report = _diff(tmp_path)
    c = _case(report, "responses")
    assert c["status"] == {"gateway": 404, "direct": 200} and c["verdict"] == "error"
    assert c["failure"] == "gateway HTTP 404, direct HTTP 200"
    assert report["not_matched"] == ["responses"]
    assert report["failures"] == ["responses: gateway HTTP 404, direct HTTP 200"]


def test_identical_http_failures_on_both_sides_fail(tmp_path, stubs):
    # Both targets refuse the key alike: the bodies match, the run must not (PR 409 review).
    gw, direct = stubs(key="some-other-key"), stubs(key="some-other-key")
    r, lines = _run(tmp_path, gw, direct)
    assert r.returncode == 0, r.stderr
    assert {x["status"] for x in lines} == {401}
    report = _diff(tmp_path)
    assert report["verdict"] == "fail" and report["matched"] == 0
    for c in report["cases"]:
        assert c["status_match"] and c["semantic_match"] and c["verdict"] == "error", c
        assert c["failure"] == "gateway HTTP 401, direct HTTP 401"


def _stream(lines, target):
    return next(x for x in lines if x["case"] == "stream-xhigh" and x["target"] == target)


def test_a_stream_the_gateway_ends_early_but_cleanly_fails_and_the_run_continues(tmp_path, stubs):
    # curl sees a clean close (exit 0) and a 200: only the missing [DONE] tells. Under pipefail the
    # run must neither abort on it nor let it pass.
    gw, direct = stubs(key=GW_KEY, cut_stream=True), stubs(key=UP_KEY)
    r, lines = _run(tmp_path, gw, direct)
    assert r.returncode == 0, r.stderr
    g = _stream(lines, "gateway")
    assert g["curl_exit"] == 0 and g["status"] == 200 and g["semantic"]["done"] is False, g
    assert lines[-1]["case"] == "responses"  # the cases after the stream still ran
    c = _case(_diff(tmp_path), "stream-xhigh")
    assert c["verdict"] == "error" and c["failure"] == "gateway HTTP 200, stream ended without [DONE], direct HTTP 200"


def test_a_stream_cut_by_the_timeout_is_recorded_and_fails_without_aborting_the_run(tmp_path, stubs):
    # Every stub answers its plain cases at once; only the stream waits 3 s for its first token,
    # past the 1 s ceiling, so curl ends it with exit 28 mid-pipeline.
    gw, direct = stubs(key=GW_KEY, first_delay=3), stubs(key=UP_KEY, first_delay=3)
    r, lines = _run(tmp_path, gw, direct, env={"AGW_CONF_TIMEOUT": "1"})
    assert r.returncode == 0, r.stderr
    for t in ("gateway", "direct"):
        assert _stream(lines, t)["curl_exit"] == 28, _stream(lines, t)
    assert lines[-1]["case"] == "responses"
    c = _case(_diff(tmp_path), "stream-xhigh")
    assert c["verdict"] == "error" and c["failure"] == "gateway curl exit 28, direct curl exit 28"


def test_a_failure_on_one_side_only_is_an_error(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    base = {"case": "x", "status": 200, "curl_exit": 0, "body_sha256": "a", "shape_sha256": "s",
            "semantic": {}, "timing": {}}
    (out / "results.jsonl").write_text(
        json.dumps({**base, "target": "gateway", "status": 0, "curl_exit": 28}) + "\n"
        + json.dumps({**base, "target": "direct"}) + "\n")
    c = _case(_diff(tmp_path), "x")
    assert c["verdict"] == "error" and c["failure"] == "gateway curl exit 28, direct HTTP 200"


REMAP = {"gpt-oss-20b": "openai/gpt-oss-20b"}


def _remap_run(tmp_path, stubs, gateway_models):
    # The gateway serves its declared name; vLLM serves the upstream id and one more model the
    # gateway does not expose. Each stub echoes the model it was asked for, as both do.
    gw = stubs(key=GW_KEY, models=gateway_models)
    direct = stubs(key=UP_KEY, models=["openai/gpt-oss-20b", "undeclared-extra"])
    r, _ = _run(tmp_path, gw, direct,
                env={"AGW_CONF_MODEL": "gpt-oss-20b", "AGW_CONF_DIRECT_MODEL": "openai/gpt-oss-20b"})
    assert r.returncode == 0, r.stderr


def test_a_declared_model_remap_is_not_a_difference(tmp_path, stubs):
    _remap_run(tmp_path, stubs, ["gpt-oss-20b"])
    report = _diff(tmp_path, REMAP)
    assert report["verdict"] == "pass", report


def test_without_the_mapping_the_remap_shows_as_a_difference(tmp_path, stubs):
    _remap_run(tmp_path, stubs, ["gpt-oss-20b"])
    report = _diff(tmp_path)
    assert report["verdict"] == "fail"
    assert {"key": "model", "gateway": "gpt-oss-20b", "direct": "openai/gpt-oss-20b"} in \
        _case(report, "effort-low")["semantic_diff"]


@pytest.mark.parametrize("gateway_models, mapping", [
    (["gpt-oss-20b", "not-declared"], REMAP),            # the gateway exposes an undeclared model
    (["gpt-oss-20b"], {"gpt-oss-20b": "openai/other"}),  # the declared upstream is not what vLLM serves
])
def test_a_real_model_mismatch_still_fails(tmp_path, stubs, gateway_models, mapping):
    _remap_run(tmp_path, stubs, gateway_models)
    report = _diff(tmp_path, mapping)
    assert report["verdict"] == "fail" and "models" in report["not_matched"], report


def test_a_malformed_model_map_is_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "results.jsonl").write_text("")
    (tmp_path / "bad.json").write_text('["not", "an", "object"]')
    r = subprocess.run(["bash", str(SCRIPT), "diff", str(out / "results.jsonl"), str(tmp_path / "bad.json")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "must be a JSON object of strings" in r.stderr


def test_a_shape_difference_alone_fails_the_case(tmp_path):
    # Same status and semantics, different shape (a field added or removed that the semantic
    # summary does not look at): still a difference.
    base = {"case": "x", "status": 200, "curl_exit": 0, "body_sha256": "a", "shape_sha256": "s1",
            "semantic": {"k": 1}, "timing": {"total_s": 1.0, "gaps": {}}}
    out = tmp_path / "out"
    out.mkdir()
    (out / "results.jsonl").write_text(
        json.dumps({**base, "target": "gateway"}) + "\n"
        + json.dumps({**base, "target": "direct", "shape_sha256": "s2"}) + "\n")
    c = _case(_diff(tmp_path), "x")
    assert c["semantic_match"] and c["status_match"] and not c["shape_match"] and c["verdict"] == "differ"


def test_a_case_missing_a_target_is_incomplete_and_fails(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "results.jsonl").write_text(json.dumps(
        {"case": "models", "target": "gateway", "status": 200, "curl_exit": 0, "body_sha256": "a",
         "shape_sha256": "b", "semantic": {}, "timing": {}}) + "\n")
    report = _diff(tmp_path)
    assert report["verdict"] == "fail" and report["cases"] == [{"case": "models", "verdict": "incomplete"}]


def test_unreachable_targets_are_an_error_even_when_both_fail_alike(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    r, lines = _run(tmp_path, gw, direct, shim_exit=7)
    assert r.returncode == 0, r.stderr
    assert lines and all(x["status"] == 0 and x["curl_exit"] == 7 for x in lines)
    report = _diff(tmp_path)
    assert report["verdict"] == "fail" and {c["verdict"] for c in report["cases"]} == {"error"}
    assert {c["failure"] for c in report["cases"]} == {"gateway curl exit 7, direct curl exit 7"}


# ── shape differences named, allowlist ────────────────────────────────────────
# Values the stubs put in fields: none of them may appear in any output, only the key paths.
SECRET = "SYNTHETIC-SECRET-CONTENT-5d1e"
ALLOW = REPO / "platform/services/agentgateway/deployment/tests/conformance-shape-allow.json"


def _adds_route(body):
    """The gateway adds one object to every body and chunk; its value is a secret-looking string."""
    return {**body, "x_route": {"trace": SECRET}}


def _drops_prompt_tokens(body):
    """The gateway drops usage.prompt_tokens from completions (no semantic field reads it)."""
    if "usage" in body:
        body = {**body, "usage": {k: v for k, v in body["usage"].items() if k != "prompt_tokens"}}
    return body


def _retypes_prompt_tokens(body):
    if "usage" in body:
        body = {**body, "usage": {**body["usage"], "prompt_tokens": str(body["usage"]["prompt_tokens"])}}
    return body


def _diff_allow(tmp: Path, allow: dict | None):
    args = ["bash", str(SCRIPT), "diff", str(tmp / "out" / "results.jsonl"), ""]
    if allow is not None:
        # A complete file: the lists the test does not name are present and empty.
        (tmp / "allow.json").write_text(json.dumps({**FULL_ALLOW, **allow}))
        args.append(str(tmp / "allow.json"))
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert SECRET not in r.stdout
    return json.loads(r.stdout)


def test_a_shape_only_difference_is_named_by_path_and_no_value_is_printed(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY, mutate=_adds_route), stubs(key=UP_KEY)
    r, lines = _run(tmp_path, gw, direct)
    assert r.returncode == 0, r.stderr
    assert SECRET not in (tmp_path / "out" / "results.jsonl").read_text() + r.stdout + r.stderr
    report = _diff_allow(tmp_path, None)
    assert report["verdict"] == "fail" and report["matched"] == 0
    for c in report["cases"]:
        assert c["status_match"] and c["semantic_match"] and not c["shape_match"], c
        assert c["shape_diff"] == {"only_gateway": ["x_route.trace"], "only_direct": [], "type_changed": []}, c
        assert c["shape_unaccepted"] == c["shape_diff"] and c["verdict"] == "differ"
    assert set(report["shape_differences"]) == set(CASES)
    # Each result line carries the shape as paths and types, and nothing else of the body.
    g = next(x for x in lines if x["case"] == "effort-low" and x["target"] == "gateway")
    assert "x_route.trace:string" in g["shape"] and "choices.[].message.content:string" in g["shape"]
    assert all(":" in e for e in g["shape"])


def test_an_allowlisted_addition_passes_and_is_reported_as_allowed(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY, mutate=_adds_route), stubs(key=UP_KEY)
    _run(tmp_path, gw, direct)
    report = _diff_allow(tmp_path, {"gateway_may_add": ["x_route.trace"]})
    assert report["verdict"] == "pass", report
    c = _case(report, "stream-xhigh")
    assert not c["shape_match"] and c["shape_accepted"] and c["shape_allowed"]["only_gateway"] == ["x_route.trace"]


def test_an_allowlisted_addition_does_not_excuse_an_unlisted_drop(tmp_path, stubs):
    gw = stubs(key=GW_KEY, mutate=lambda b: _drops_prompt_tokens(_adds_route(b)))
    direct = stubs(key=UP_KEY)
    _run(tmp_path, gw, direct)
    report = _diff_allow(tmp_path, {"gateway_may_add": ["x_route.trace"]})
    assert report["verdict"] == "fail"
    c = _case(report, "effort-low")
    assert c["shape_unaccepted"] == {"only_gateway": [], "only_direct": ["usage.prompt_tokens"], "type_changed": []}
    assert c["verdict"] == "differ" and _case(report, "models")["verdict"] == "match"
    assert sorted(report["not_matched"]) == sorted(c for c in CASES if c.startswith(("effort-", "chat-", "tool-")))


def test_a_type_change_is_named_and_only_its_own_allowlist_entry_accepts_it(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY, mutate=_retypes_prompt_tokens), stubs(key=UP_KEY)
    _run(tmp_path, gw, direct)
    c = _case(_diff_allow(tmp_path, {"gateway_may_add": ["usage.prompt_tokens"]}), "effort-low")
    assert c["shape_diff"]["type_changed"] == [{"path": "usage.prompt_tokens", "gateway": ["string"],
                                                "direct": ["number"]}]
    assert c["verdict"] == "differ"
    retype_ok = _diff_allow(tmp_path, {"gateway_may_retype": ["usage.prompt_tokens"]})
    assert _case(retype_ok, "effort-low")["verdict"] == "match"


FULL_ALLOW = {"gateway_may_add": [], "gateway_may_drop": [], "gateway_may_retype": []}


@pytest.mark.parametrize("allow", [
    {**FULL_ALLOW, "gateway_may_ad": ["x"]},           # a misspelt extra key
    {**FULL_ALLOW, "gateway_may_add": "x_route"},      # not a list
    {**FULL_ALLOW, "gateway_may_drop": [1]},           # not a list of strings
    ["x_route.trace"],                                 # not an object
    {},                                                # empty: every list missing
    {"_comment": "only a comment"},                    # comment only
    {"gateway_may_add": ["x"], "gateway_may_drop": []},  # partial: gateway_may_retype missing
    {**FULL_ALLOW, "_comment": ["not", "a", "string"]},  # comment not a string
])
def test_a_malformed_shape_allowlist_is_refused(tmp_path, allow):
    out = tmp_path / "out"
    out.mkdir()
    (out / "results.jsonl").write_text("")
    (tmp_path / "allow.json").write_text(json.dumps(allow))
    r = subprocess.run(["bash", str(SCRIPT), "diff", str(out / "results.jsonl"), "", str(tmp_path / "allow.json")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "shape allowlist" in r.stderr, r.stderr


def test_a_complete_empty_allowlist_with_a_comment_is_accepted(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "results.jsonl").write_text("")
    (tmp_path / "allow.json").write_text(json.dumps({**FULL_ALLOW, "_comment": "why"}))
    r = subprocess.run(["bash", str(SCRIPT), "diff", str(out / "results.jsonl"), "", str(tmp_path / "allow.json")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_the_committed_shape_allowlist_is_empty():
    # Accepting a difference is the operator's decision after reading a run's shape_diff.
    allow = json.loads(ALLOW.read_text())
    assert {k: v for k, v in allow.items() if k != "_comment"} == FULL_ALLOW


# ── listener TLS options and input checks ─────────────────────────────────────

def test_listener_tls_options_go_to_the_gateway_only(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    tls = {"AGW_CONF_GATEWAY_RESOLVE": "gateway.dc1.example.internal:4000:127.0.0.1",
           "AGW_CONF_GATEWAY_CERT": "/leaf/cert.pem", "AGW_CONF_GATEWAY_CERT_KEY": "/leaf/key.pem",
           "AGW_CONF_GATEWAY_CA": "/ca/bundle.crt"}
    r, _ = _run(tmp_path, gw, direct, env=tls, shim_exit=7)
    assert r.returncode == 0, r.stderr
    calls = [c.strip("\n").split("\n") for c in (tmp_path / "argv.log").read_text().split("--\n") if c.strip()]
    gw_calls = [c for c in calls if any(a.startswith(gw.url) for a in c)]
    direct_calls = [c for c in calls if any(a.startswith(direct.url) for a in c)]
    assert len(gw_calls) == len(direct_calls) == len(CASES)
    for c in gw_calls:
        for flag, value in (("--resolve", tls["AGW_CONF_GATEWAY_RESOLVE"]), ("--cert", "/leaf/cert.pem"),
                            ("--key", "/leaf/key.pem"), ("--cacert", "/ca/bundle.crt")):
            assert c[c.index(flag) + 1] == value
    for c in direct_calls:
        assert not {"--resolve", "--cert", "--key", "--cacert"} & set(c)


@pytest.mark.parametrize("env, message", [
    ({"AGW_CONF_GATEWAY_CERT": "/c.pem"}, "go together"),
    ({"AGW_CONF_TIMEOUT": "x"}, "AGW_CONF_TIMEOUT must be a positive integer"),
    ({"AGW_CONF_GATEWAY_KEY_FILE": "/nonexistent/key"}, "is not readable"),
])
def test_bad_inputs_stop_before_any_request(tmp_path, stubs, env, message):
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    r, lines = _run(tmp_path, gw, direct, env=env)
    assert r.returncode == 2 and message in r.stderr, r.stderr
    assert gw.seen == [] and direct.seen == [] and lines == []


def test_a_failure_mid_run_removes_the_bodies(tmp_path, stubs):
    # A jq that dies while reading the stream: the script stops there, and the working directory
    # holding every request and response body is gone, while the results so far remain.
    real_jq = shutil.which("jq")
    jqdir = tmp_path / "jqbin"
    jqdir.mkdir()
    (jqdir / "jq").write_text(
        '#!/bin/sh\nfor a in "$@"; do [ "$a" = --unbuffered ] && { cat >/dev/null; exit 5; }; done\n'
        f'exec "{real_jq}" "$@"\n')
    (jqdir / "jq").chmod(0o755)
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    r, lines = _run(tmp_path, gw, direct, path_dirs=[jqdir])
    assert r.returncode == 2 and "jq failed while reading the gateway stream" in r.stderr, r.stderr
    assert not list((tmp_path / "out").glob("work.*"))
    assert lines and lines[-1]["case"] == "tool-call"


def test_the_seven_efforts_match_the_dgx_spark_endpoint_contract():
    # The seven values vLLM's request schema advertises, as dgx-spark records them
    # (dgx-spark plans/development/openspec/specs/inference-endpoint/spec.md and
    # vllm/public_probe.py EFFORTS). The script must send exactly these.
    text = SCRIPT.read_text()
    assert 'EFFORTS="none minimal low medium high xhigh max"' in text


# ── run-agw-conformance.yml, for real ─────────────────────────────────────────

class Bao(seed_harness.FakeBao):
    store: dict = {}

    def do_POST(self):
        self.record("POST")
        if self.path == "/v1/auth/approle/login":
            return self.reply({"auth": {"client_token": seed_harness.LOGIN}})
        self.reply({}, 404)

    def do_GET(self):
        self.record("GET")
        if self.path == "/v1/secret/data/services/agentgateway":
            return self.reply({"data": {"data": type(self).store, "metadata": {"version": 1}}})
        self.reply({}, 404)


def _playbook(tmp: Path, gw: Stub, direct: Stub, extra=None, check=False, host_vars=None):
    Bao.requests = []
    Bao.store = {"client_stray": GW_KEY, "vllm_api_key": UP_KEY, "agw_db_password": "synthetic-db"}
    with seed_harness.serve(Bao) as address:
        host = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                "service_name": "agentgateway", "agw_clients": ["stray"], "agw_models": [{"name": "m"}],
                "agw_upstream_base_url": direct.url, "agw_bind": "127.0.0.1", "agw_port": gw.port,
                **(host_vars or {})}
        inv = {"all": {"vars": {"openbao_addr": address},
                       "children": {"agentgateway_svc": {"hosts": {"gw": host}}}}}
        (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
        cmd = ["ansible-playbook", "-v", "-i", str(tmp / "inv.yml"), str(PLAYBOOK),
               "-e", json.dumps({**seed_harness.ROLE, **(extra or {})}), *(["--check"] if check else [])]
        env = {**harness_sandbox.env_for(tmp), "ANSIBLE_STDOUT_CALLBACK": "default"}
        r = harness_sandbox.run(cmd, tmp, cwd=REPO, env=env, timeout=300)
    out = r.stdout + r.stderr
    for value in (GW_KEY, UP_KEY, "synthetic-db", *seed_harness.NEVER_PRINTED):
        assert value not in out, f"{value} printed"
    # -v prints the tempfile result; every directory it made must be gone after the run.
    made = re.findall(r'"path": "([^"]*agw-conformance\.[^"]*)"', out)
    return r.returncode, out, made


def _left_behind(made):
    return [p for p in dict.fromkeys(made) if os.path.exists(p)]


def test_the_playbook_runs_reports_and_removes_its_directory(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    rc, out, made = _playbook(tmp_path, gw, direct)
    assert rc == 0, out
    assert f"Conformance as stray for m: PASS, {len(CASES)}/{len(CASES)} cases match." in out
    assert '"agw_conformance"' in out or "agw_conformance" in out  # CUSTOM STATS
    assert len(gw.seen) == len(direct.seen) == len(CASES)
    assert made and not _left_behind(made), "working directory left behind"


def test_a_failing_run_still_removes_the_keys(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    rc, out, made = _playbook(tmp_path, gw, direct, extra={"agw_conformance_timeout": "x"})
    assert rc != 0 and "AGW_CONF_TIMEOUT must be a positive integer" in out, out
    assert made and not _left_behind(made), "working directory (with keys) left behind"


def test_a_difference_fails_the_playbook_with_the_case_named(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY, responses_404=True), stubs(key=UP_KEY)
    rc, out, made = _playbook(tmp_path, gw, direct)
    assert rc != 0 and "The gateway did not match direct vLLM on: responses." in out, out
    assert made and not _left_behind(made)


def test_the_playbook_hands_the_declared_model_remap_to_the_comparison(tmp_path, stubs):
    gw = stubs(key=GW_KEY, models=["gpt-oss-20b"])
    direct = stubs(key=UP_KEY, models=["openai/gpt-oss-20b", "undeclared-extra"])
    rc, out, made = _playbook(tmp_path, gw, direct, host_vars={
        "agw_models": [{"name": "gpt-oss-20b", "upstream_model": "openai/gpt-oss-20b"}]})
    assert rc == 0 and f"PASS, {len(CASES)}/{len(CASES)} cases match" in out, out
    assert {x["body"]["model"] for x in direct.seen if x["method"] == "POST"} == {"openai/gpt-oss-20b"}
    assert made and not _left_behind(made)


@pytest.mark.parametrize("name", ["victim", "agw-conformance.evil"])
def test_an_extra_var_cannot_redirect_the_key_files_or_the_delete(tmp_path, stubs, name):
    # An extra var outranks the registered tempfile result. Aim it at a directory holding a canary:
    # nothing is written into it and it is not deleted, whether or not its name has the prefix
    # (the second is not directly under the temp root).
    target = tmp_path / name
    target.mkdir()
    (target / "canary").write_text("keep")
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    rc, out, _ = _playbook(tmp_path, gw, direct, extra={"_agwc_tmpdir": {"path": str(target)}})
    assert rc != 0 and "Do not pass _agwc_tmpdir as an extra var" in out, out
    assert sorted(p.name for p in target.iterdir()) == ["canary"]
    assert gw.seen == [] and direct.seen == []


def test_the_playbook_prints_the_shape_differences_by_path_only(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY, mutate=_adds_route), stubs(key=UP_KEY)
    rc, out, made = _playbook(tmp_path, gw, direct)
    assert rc != 0 and "shape differences (paths only)" in out, out
    assert '"x_route.trace"' in out and SECRET not in out
    assert made and not _left_behind(made)


def test_a_dry_run_sends_nothing(tmp_path, stubs):
    gw, direct = stubs(key=GW_KEY), stubs(key=UP_KEY)
    rc, out, _ = _playbook(tmp_path, gw, direct, check=True)
    assert rc == 0 and "nothing was sent" in out, out
    assert gw.seen == [] and direct.seen == [] and Bao.requests == []


def test_the_key_files_are_0600_in_a_private_directory():
    plays = playbook_yaml.load(PLAYBOOK)
    tasks = json.dumps(plays[1]["tasks"])
    assert '"dest": "{{ _agwc_tmpdir.path }}/gateway.key", "mode": "0600"' in tasks
    assert '"dest": "{{ _agwc_tmpdir.path }}/direct.key", "mode": "0600"' in tasks


# ── wiring ────────────────────────────────────────────────────────────────────

def test_the_semaphore_template_follows_manage_client_key():
    names = [t["name"] for t in yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]]
    i = names.index("Manage agentgateway Client Key")
    assert names[i + 1] == "Run agentgateway Conformance"
    t = yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"][i + 1]
    assert t["playbook"] == "platform/playbooks/run-agw-conformance.yml"


def test_agents_md_lists_the_workflow():
    assert "| Run agentgateway Conformance | `run-agw-conformance.yml` |" in (REPO / "AGENTS.md").read_text()


def test_script_is_executable():
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
