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
FIRST, LAST = "MCP probes: pick the identity and the requests", "Report the MCP checks"
GATHER = "Gather this host's service-deploy result"
PHASE3 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 3"))
NAMES = [t["name"] for t in PHASE3["tasks"]]
TOOLS = [{"name": "chat_completions", "description": "d", "inputSchema": {"type": "object"}}]
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

    def __init__(self, mode):
        self.mode, self.seen = mode, []
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
                    tools = [] if mode == "empty" else TOOLS
                    if mode == "stream-error":
                        return self._send(
                            200,
                            _frame({"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "Unknown tool"}}),
                            "text/event-stream",
                        )
                    return self._send(
                        200, _frame({"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}), "text/event-stream"
                    )
                return self._send(400, b"{}", "application/json")

        return H


@pytest.fixture
def gateway():
    made = []

    def start(mode):
        gw = Gateway(mode)
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


def _run(tmp_path, gw, enabled=True, identities=("alice", "mcp-self"), extra=None, check=False):
    hv = {
        "_agwp_base": f"http://127.0.0.1:{gw.server_port}",
        "_resolved": {"client_alice": KEY, "client_bob": OTHER, "client_mcp-self": "SYNTHETIC-SELF"},
        "agw_identities": list(identities),
        "_verify_default": identities[0] if identities else "",
        "agw_mcp_enabled": enabled,
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
    assert KEY not in out and OTHER not in out and "SYNTHETIC-SELF" not in out, "a key reached the task output"
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
    ]
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
        "MCP: with MCP disabled a keyed initialize is not an MCP reply",
    ]
    assert all(str(PROBE) in t["ansible.builtin.include_tasks"] and "no_log" not in t for t in keyed)
