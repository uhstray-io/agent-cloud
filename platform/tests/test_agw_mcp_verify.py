"""The deploy's MCP verify (agentgateway-mcp task 3.4), run against a fake gateway.

The real tasks of deploy-agentgateway.yml's verify play, from "MCP probes: pick the identity and the
requests" to the report, run on localhost through the shared probe path (tasks/agw-probe.yml) against a
local HTTP server that answers the way agentgateway v1.5.0 was measured to (design.md, spike 1.1 to
1.7): 401 for a missing or unknown key, an `initialize` and a `tools/list` answered as a one-frame event
stream, a gateway-raised error as a JSON body, and /v1's route answering any path 400 when MCP is off.
Every key and every body here is synthetic.

What is pinned: each verdict reads the body (an empty tool list and a rate-limit refusal are HTTP 200);
an unproven check fails unless accepted; the keyless probe carries no credential and both `Accept`
values; the key never reaches the task output; `mcp-self` is never the identity; and with MCP off a
keyed `initialize` that comes back as an MCP reply fails the deploy.
"""

import json
import sys
import threading
from http.server import ThreadingHTTPServer

import harness_sandbox
import playbook_yaml
import pytest
import yaml
from fake_http import DrainingHandler

REPO = playbook_yaml.REPO
DEPLOY = REPO / "platform/playbooks/deploy-agentgateway.yml"
PROBE = REPO / "platform/playbooks/tasks/agw-probe.yml"
KEY = "SYNTHETIC-MCP-KEY-0001"
OTHER = "SYNTHETIC-MCP-KEY-0002"
SVC = "SYNTHETIC-MCP-KEY-SERVICE"
FIRST, LAST = "MCP probes: pick the identity and the requests", "Report the MCP checks"
GATHER = "Gather this host's service-deploy result"
PHASE3 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 3"))
NAMES = [t["name"] for t in PHASE3["tasks"]]
TOOLS = [{"name": "chat_completions", "description": "d", "inputSchema": {"type": "object"}}]
COMPLETION = {
    "content": [{"type": "text", "text": "{}"}],
    "structuredContent": {"choices": [{"message": {"content": "pong"}}], "usage": {"total_tokens": 3}},
    "isError": False,
}
MARKER = "SYNTHETIC-LOG-LINE-MARKER"
LOOPBACK_LINE = (
    "2026-10-14T00:00:01Z info request gateway=default/default listener=loopback route=internal/llm:request "
    'http.method=POST http.status=200 protocol=llm identity="mcp-self" http.path="/v1/chat/completions" ' + MARKER
)
COLOURED = LOOPBACK_LINE.replace("listener=loopback", "\x1b[3mlistener\x1b[0m\x1b[2m=\x1b[0mloopback").replace(
    'identity="mcp-self"', '\x1b[3midentity\x1b[0m\x1b[2m=\x1b[0m"mcp-self"'
)
INIT_REPLY = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "protocolVersion": "2025-11-25",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "rmcp", "version": "x"},
    },
}


def _frame(obj) -> bytes:
    return ("event: message\ndata: " + json.dumps(obj) + "\n\n").encode()


class Gateway(ThreadingHTTPServer):
    """mode: normal | open | empty | ratelimit | stream-error | init-500, or with MCP off:
    off | off-served | off-200 | off-result-in-400."""

    def __init__(self, mode, call="ok"):
        """call (the model call): ok | tool-error | no-choices | ratelimit | no-chat (the tool is not listed)."""
        self.mode, self.call, self.seen = mode, call, []
        super().__init__(("127.0.0.1", 0), self._handler())

    def _handler(self):
        server = self

        class H(DrainingHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0)) or 0) or b"{}")
                auth = self.headers.get("authorization", "")
                server.seen.append(
                    {
                        "path": self.path,
                        "auth": auth,
                        "accept": self.headers.get("accept", ""),
                        "method": body.get("method"),
                        "params": body.get("params"),
                    }
                )
                keyed = auth in (f"Bearer {KEY}", f"Bearer {OTHER}")
                mode = server.mode
                if mode in ("off", "off-served", "off-200", "off-result-in-400"):
                    if not keyed:
                        return self._send(401, b"api key authentication failure: no API Key found", "text/plain")
                    if mode == "off-200":  # not an MCP reply, but not an error status either
                        return self._send(200, b"ok", "text/plain")
                    if mode == "off-result-in-400":  # an MCP reply carried by an error status
                        return self._send(400, json.dumps(INIT_REPLY).encode(), "application/json")
                    if mode == "off":
                        err = {
                            "error": {
                                "message": "LLM request body is missing string field 'model'",
                                "type": "invalid_request_error",
                                "code": "missing_model",
                            }
                        }
                        return self._send(400, json.dumps(err).encode(), "application/json")
                elif mode != "open" and not keyed:
                    return self._send(401, b"api key authentication failure: no API Key found", "text/plain")
                if mode == "ratelimit":
                    err = {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": {"code": -32003, "message": "rate limit exceeded"},
                    }
                    return self._send(200, json.dumps(err).encode(), "application/json")
                if body.get("method") == "initialize":
                    return self._send(500 if mode == "init-500" else 200, _frame(INIT_REPLY), "text/event-stream")
                if body.get("method") == "tools/list":
                    tools = [] if mode == "empty" else ([{"name": "other"}] if server.call == "no-chat" else TOOLS)
                    if mode == "stream-error":
                        return self._send(
                            200,
                            _frame({"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "Unknown tool"}}),
                            "text/event-stream",
                        )
                    return self._send(
                        200, _frame({"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}), "text/event-stream"
                    )
                if body.get("method") == "tools/call":
                    if server.call == "ratelimit":
                        err = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32003, "message": "rate limit exceeded"}}
                        return self._send(200, json.dumps(err).encode(), "application/json")
                    result = {
                        "tool-error": {**COMPLETION, "isError": True},
                        "no-choices": {**COMPLETION, "structuredContent": {}},
                    }
                    return self._send(
                        200,
                        _frame({"jsonrpc": "2.0", "id": 3, "result": result.get(server.call, COMPLETION)}),
                        "text/event-stream",
                    )
                return self._send(400, b"{}", "application/json")

        return H


@pytest.fixture
def gateway():
    made = []

    def start(mode, call="ok"):
        gw = Gateway(mode, call)
        threading.Thread(target=gw.serve_forever, daemon=True).start()
        made.append(gw)
        return gw

    yield start
    for gw in made:
        gw.shutdown()
        gw.server_close()


def _tasks() -> list:
    out = []
    for t in PHASE3["tasks"][NAMES.index(FIRST) : NAMES.index(LAST) + 1]:
        t = json.loads(json.dumps(t).replace("tasks/agw-probe.yml", str(PROBE)))
        out.append(t)
    return out


def _engine(tmp_path, log: str):
    """A stand-in container engine: `exec ... date` prints a fixed time, `logs` prints `log` and records its argv."""
    (tmp_path / "gateway.log").write_text(log)
    stub = tmp_path / "engine"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{tmp_path}/engine.calls"\n'
        'case "$1" in\n'
        "  exec) echo 2026-10-14T00:00:00Z ;;\n"
        f'  logs) printf "%b" "$(cat "{tmp_path}/gateway.log")" ;;\n'
        "esac\n"
    )
    stub.chmod(0o755)
    return str(stub)


def _run(
    tmp_path,
    gw,
    enabled=True,
    identities=("alice", "mcp-self"),
    extra=None,
    check=False,
    admins=None,
    log=LOOPBACK_LINE,
):
    """`admins` are the identities derived from platform_admins (the only ones /mcp admits); by default every
    identity but the internal one and the service client `svc`."""
    if admins is None:
        admins = [i for i in identities if i not in ("mcp-self", "svc")]
    hv = {
        "_agwp_base": f"http://127.0.0.1:{gw.server_port}",
        "_resolved": {"client_alice": KEY, "client_bob": OTHER, "client_mcp-self": "SYNTHETIC-SELF", "client_svc": SVC},
        "agw_identities": list(identities),
        "_agw_admin_identities": [{"key": a, "name": a} for a in admins],
        "_verify_default": identities[0] if identities else "",
        "agw_mcp_enabled": enabled,
        "_engine": _engine(tmp_path, log),
        "agw_models": [{"name": "m1"}, {"name": "m2"}],
        "_mcp_log_hits": PHASE3["vars"]["_mcp_log_hits"],
        **(extra or {}),
    }
    inv = {
        "all": {"hosts": {"gw": {"ansible_connection": "local", "ansible_python_interpreter": sys.executable, **hv}}}
    }
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    (tmp_path / "play.yml").write_text(yaml.safe_dump([{"hosts": "all", "gather_facts": False, "tasks": _tasks()}]))
    args = ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml")] + (
        ["--check"] if check else []
    )
    r = harness_sandbox.run(
        args,
        tmp_path,
        cwd=REPO,
        env={**harness_sandbox.env_for(tmp_path), "ANSIBLE_STDOUT_CALLBACK": "default"},
        timeout=180,
    )
    out = r.stdout + r.stderr
    assert (
        MARKER not in out and KEY not in out and OTHER not in out and SVC not in out and "SYNTHETIC-SELF" not in out
    ), "a key reached the task output"
    return r.returncode, out


# ── MCP on ────────────────────────────────────────────────────────────────────


def test_a_correct_gateway_passes_and_is_probed_keyless_then_keyed(tmp_path, gateway):
    gw = gateway("normal")
    rc, out = _run(tmp_path, gw)
    assert rc == 0, out
    assert [(s["method"], bool(s["auth"])) for s in gw.seen] == [
        ("initialize", False),
        ("initialize", True),
        ("tools/list", True),
        ("tools/call", True),
    ]
    call = gw.seen[-1]["params"]
    assert call["name"] == "chat_completions"
    assert call["arguments"]["body"]["max_tokens"] == 16 and call["arguments"]["body"]["model"] == "m1"
    assert {s["path"] for s in gw.seen} == {"/mcp"}
    assert all("application/json" in s["accept"] and "text/event-stream" in s["accept"] for s in gw.seen)
    assert {s["auth"] for s in gw.seen if s["auth"]} == {f"Bearer {KEY}"}
    assert "chat_completions" in out


def test_an_open_mcp_endpoint_fails_the_deploy_and_names_the_check(tmp_path, gateway):
    rc, out = _run(tmp_path, gateway("open"))
    assert rc != 0 and "Keyless POST /mcp" in out and "answered 200" in out, out


def test_an_empty_tool_listing_fails_even_though_the_status_is_200(tmp_path, gateway):
    rc, out = _run(tmp_path, gateway("empty"))
    assert rc != 0 and "listed no tool" in out, out


def test_a_gateway_raised_error_in_the_stream_fails_the_listing(tmp_path, gateway):
    rc, out = _run(tmp_path, gateway("stream-error"))
    assert rc != 0 and "listed no tool" in out, out


def test_an_initialize_answered_with_an_error_status_fails_even_with_a_valid_body(tmp_path, gateway):
    rc, out = _run(tmp_path, gateway("init-500"))
    assert rc != 0 and "must get an MCP session" in out and "HTTP\n" not in out, out


def test_the_chat_tool_must_be_listed(tmp_path, gateway):
    rc, out = _run(tmp_path, gateway("normal", call="no-chat"))
    assert rc != 0 and "does not show the chat tool" in out, out


@pytest.mark.parametrize("call", ["tool-error", "no-choices"])
def test_a_model_call_that_returns_no_completion_fails_the_deploy(tmp_path, gateway, call):
    """A self-call refusal is HTTP 200 with the error in the result (spike 1.5): the verdict reads the body."""
    rc, out = _run(tmp_path, gateway("normal", call=call))
    assert rc != 0 and "returned no completion" in out, out


def test_a_rate_limited_model_call_is_unproven_not_passed(tmp_path, gateway):
    rc, out = _run(tmp_path, gateway("normal", call="ratelimit"))
    assert rc != 0 and "were not proven" in out, out
    rc, out = _run(tmp_path, gateway("normal", call="ratelimit"), extra={"agw_verify_allow_unproven": True})
    assert rc == 0 and "NOT proven" in out, out


@pytest.mark.parametrize("log", [LOOPBACK_LINE, COLOURED], ids=["plain", "coloured"])
def test_the_calls_v1_request_must_be_logged_as_mcp_self_on_the_loopback_listener(tmp_path, gateway, log):
    rc, out = _run(tmp_path, gateway("normal"), log=log)
    assert rc == 0, out
    calls = (tmp_path / "engine.calls").read_text()
    assert "logs --since 2026-10-14T00:00:00Z agentgateway" in calls
    assert "logged as mcp-self on the loopback listener" in out


@pytest.mark.parametrize(
    "log",
    [
        pytest.param("", id="no-log-line"),
        pytest.param(LOOPBACK_LINE.replace('identity="mcp-self"', 'identity="alice"'), id="another-identity"),
        pytest.param(LOOPBACK_LINE.replace("listener=loopback", "listener=default"), id="another-listener"),
        pytest.param(LOOPBACK_LINE.replace("/v1/chat/completions", "/mcp"), id="another-path"),
    ],
)
def test_a_missing_or_wrong_access_log_line_fails_and_no_log_line_is_printed(tmp_path, gateway, log):
    rc, out = _run(tmp_path, gateway("normal"), log=log)
    assert rc != 0 and "attributed to the internal identity" in out, out
    assert MARKER not in out  # _run asserts it too; the line never reaches the output


def test_a_rate_limit_refusal_is_unproven_not_passed(tmp_path, gateway):
    """-32003 comes back as HTTP 200 with the error in the body (spike 1.7)."""
    rc, out = _run(tmp_path, gateway("ratelimit"))
    assert rc != 0 and "were not proven" in out, out
    rc, out = _run(tmp_path, gateway("ratelimit"), extra={"agw_verify_allow_unproven": True})
    assert rc == 0 and "NOT proven" in out, out


def test_with_no_identity_that_may_call_mcp_the_checks_are_unproven(tmp_path, gateway):
    gw = gateway("normal")
    rc, out = _run(tmp_path, gw, identities=("mcp-self",))
    assert rc != 0 and "were not proven" in out, out
    assert [(s["method"], bool(s["auth"])) for s in gw.seen] == [("initialize", False)]
    rc, out = _run(tmp_path, gw, identities=("mcp-self",), extra={"agw_verify_allow_unproven": True})
    assert rc == 0, out


@pytest.mark.parametrize(
    "identities, verify_client, expected",
    [
        pytest.param(("mcp-self", "alice"), None, KEY, id="a-default-of-mcp-self-is-skipped"),
        pytest.param(("alice", "bob"), "mcp-self", KEY, id="a-named-mcp-self-is-skipped"),
        pytest.param(("alice", "bob"), "bob", OTHER, id="a-named-identity-is-used"),
        pytest.param(("svc", "alice"), None, KEY, id="a-service-client-default-falls-back-to-an-admin"),
        pytest.param(("svc", "alice"), "svc", KEY, id="a-named-service-client-falls-back-to-an-admin"),
    ],
)
def test_the_probing_identity_is_never_the_internal_one(tmp_path, gateway, identities, verify_client, expected):
    gw = gateway("normal")
    extra = {"agw_verify_client": verify_client} if verify_client else {}
    rc, out = _run(tmp_path, gw, identities=identities, extra=extra)
    assert rc == 0, out
    assert {s["auth"] for s in gw.seen if s["auth"]} == {f"Bearer {expected}"}


def test_a_dry_run_sends_nothing(tmp_path, gateway):
    gw = gateway("normal")
    rc, out = _run(tmp_path, gw, check=True)
    assert rc == 0 and gw.seen == [], out
    assert "Check mode" in out


# ── MCP off ───────────────────────────────────────────────────────────────────


def test_with_mcp_off_a_keyed_initialize_that_is_not_an_mcp_reply_passes(tmp_path, gateway):
    gw = gateway("off")
    rc, out = _run(tmp_path, gw, enabled=False)
    assert rc == 0, out
    assert [(s["method"], bool(s["auth"])) for s in gw.seen] == [
        ("initialize", True)
    ]  # keyless proves nothing, so it is not sent
    assert "MCP is disabled" in out


def test_with_mcp_off_an_mcp_reply_fails_the_deploy(tmp_path, gateway):
    rc, out = _run(tmp_path, gateway("off-served"), enabled=False)
    assert rc != 0 and "must not be served" in out, out


def test_with_mcp_off_a_200_that_is_not_an_mcp_reply_still_fails(tmp_path, gateway):
    """The status alone says the endpoint is served: nothing but an error status is acceptable."""
    rc, out = _run(tmp_path, gateway("off-200"), enabled=False)
    assert rc != 0 and "must not be served" in out, out


def test_with_mcp_off_an_mcp_reply_in_an_error_status_still_fails(tmp_path, gateway):
    """The body says it is served even when the status does not: the verdict reads the body."""
    rc, out = _run(tmp_path, gateway("off-result-in-400"), enabled=False)
    assert rc != 0 and "must not be served" in out, out


def test_with_mcp_off_any_non_internal_identity_may_probe(tmp_path, gateway):
    """Nothing is admitted on /mcp then, so a service client is as good a prober as an admin."""
    gw = gateway("off")
    rc, out = _run(tmp_path, gw, enabled=False, identities=("svc", "mcp-self"))
    assert rc == 0, out
    assert {s["auth"] for s in gw.seen if s["auth"]} == {f"Bearer {SVC}"}


def test_with_mcp_off_and_no_identity_the_probe_is_not_run_and_nothing_fails(tmp_path, gateway):
    gw = gateway("off")
    rc, out = _run(tmp_path, gw, enabled=False, identities=())
    assert rc == 0 and gw.seen == [] and "no enrolled identity" in out, out


# ── what the deploy reports upward ────────────────────────────────────────────


@pytest.mark.parametrize("unproven, verified", [(True, False), (False, True), (None, True)])
def test_an_accepted_unproven_mcp_check_makes_the_deploy_result_unverified(tmp_path, unproven, verified):
    gather = next(t for t in PHASE3["tasks"] if t["name"] == GATHER)
    hv = {"_deploy": {"changed": True}, **({"_mcp_unproven": unproven} if unproven is not None else {})}
    inv = {"all": {"hosts": {"gw": {"ansible_connection": "local", **hv}}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    (tmp_path / "play.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "all",
                    "gather_facts": False,
                    "tasks": [
                        gather,
                        {"ansible.builtin.debug": {"msg": "verified={{ _service_deploy_result.verified }}"}},
                    ],
                }
            ]
        )
    )
    r = harness_sandbox.run(
        ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml")],
        tmp_path,
        cwd=REPO,
        env={**harness_sandbox.env_for(tmp_path), "ANSIBLE_STDOUT_CALLBACK": "default"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"verified={verified}" in r.stdout


def test_every_task_that_carries_the_mcp_key_is_a_probe_include_with_the_key_in_the_probe_only():
    keyed = [
        t
        for t in _tasks()
        if "_resolved['client_" in json.dumps({k: v for k, v in t.items() if k not in ("name", "when")})
    ]
    assert [t["name"] for t in keyed] == [
        "MCP: an enrolled identity initialises a session",
        "MCP: an enrolled identity lists the tools",
        "MCP: an enrolled identity makes one model call through the chat tool",
        "MCP: with MCP disabled a keyed initialize is not an MCP reply",
    ]
    assert all(str(PROBE) in t["ansible.builtin.include_tasks"] and "no_log" not in t for t in keyed)
