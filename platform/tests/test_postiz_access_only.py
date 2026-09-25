"""Run both seed playbooks' read-only access checks against a synthetic OpenBao.

The check must never write, must not pass on a GET 404 alone (OpenBao answers 404 for a
missing path AND for one the token cannot see), and must require the capability the real
seed uses: `create` to POST a new path, `patch` to PATCH an existing one (reviews of
PR #205). tasks/assert-bao-seed-access.yml is the shared implementation.
"""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

PLAYBOOKS = {
    "postiz": ("platform/playbooks/seed-postiz-secrets.yml", "services/postiz",
               {"postiz_verify_access_only": True}, "Read-only Postiz access verified", "SEED_X_API_KEY"),
    "openbao-key": ("platform/playbooks/seed-openbao-key.yml", "services/agentgateway",
                    {"bao_verify_access_only": True, "bao_path": "services/agentgateway",
                     "bao_key": "vllm_api_key"}, "Read-only access to secret/services/agentgateway verified",
                    "BAO_VALUE"),
}
# The capability matrix tests ONE shared task (tasks/assert-bao-seed-access.yml), so it runs
# against one playbook; the other keeps one pass and one refusal to prove its wiring.
MATRIX = "openbao-key"
WIRING = "postiz"


def writes(requests):
    return [r for r in requests if r[0] in ("PATCH", "PUT") or (r[0] == "POST" and r[1].startswith("/v1/secret/"))]


def run_access_check(tmp_path, which, capabilities, provider="", exists=False, foreign=None,
                     override=None, declare=True, env_addr=False, inject=None, declared_value=None):
    playbook, path, extra_vars, _, own_input = PLAYBOOKS[which]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, body, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_POST(self):  # noqa: N802
            requests.append(("POST", self.path))
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            if self.path == "/v1/sys/capabilities-self":
                assert json.loads(body) == {"paths": [f"secret/data/{path}"]}
                return self.reply({"capabilities": capabilities, f"secret/data/{path}": capabilities})
            return self.reply({}, 403)

        def do_GET(self):  # noqa: N802
            requests.append(("GET", self.path))
            if exists:
                return self.reply({"data": {"data": {"unrelated": "x"}, "metadata": {"version": 1}}})
            return self.reply({}, 404)  # a missing path, or one this token cannot see

        def do_PATCH(self):  # noqa: N802
            requests.append(("PATCH", self.path))
            return self.reply({}, 403)

    # Default listen backlog is 5; a playbook's request bursts on a loaded machine got
    # "Connection reset by peer" (see test_scoped_publication.py).
    class Server(ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The address comes from the inventory's all.vars, as in production; an extra var
        # overriding it is what the seed refuses (tasks/assert-bao-addr-declared.yml).
        inventory = tmp_path / "inventory.yml"
        declared = {"all": {"vars": {"openbao_addr": declared_value or f"http://127.0.0.1:{server.server_port}",
                                     # A templated declaration resolves to the synthetic store.
                                     "openbao_host": f"127.0.0.1:{server.server_port}"}}}
        inventory.write_text(json.dumps(declared if declare else {"all": {"hosts": {}}}))
        extra = {"bao_role_id": "synthetic-role", "bao_secret_id": "synthetic-role-secret", **extra_vars}
        extra.update({"openbao_addr": override} if override else {})
        extra.update(inject or {})
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("SEED_") and key != "BAO_VALUE"}
        # Only this template's declared input: the seed refuses any other (a leftover).
        env.update({own_input: provider} if provider else {})
        env.update({foreign: "synthetic-leftover-value"} if foreign else {})
        # Stock output on purpose: stricter than production's redact_requests (MISTAKES 4.6).
        env.update(ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
        env.pop("OPENBAO_ADDR", None)
        env.update({"OPENBAO_ADDR": f"http://127.0.0.1:{server.server_port}"} if env_addr else {})
        result = subprocess.run(
            ["ansible-playbook", "-v", "-i", str(inventory), "-c", "local", playbook, "-e", json.dumps(extra)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=90,
        )
        output = result.stdout + result.stderr
        for value in [provider, "synthetic-role", "synthetic-role-secret", "synthetic-login-value",
                      "synthetic-leftover-value"]:
            if value:
                assert value not in output
        return result.returncode, output, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


PASSING = [(False, ["read", "create"]), (True, ["read", "patch"]), (False, ["root"]), (True, ["root"])]


@pytest.mark.parametrize("which,exists,capabilities",
                         [(MATRIX, *case) for case in PASSING] + [(WIRING, *PASSING[0])])
def test_access_check_passes_with_the_capability_the_seed_uses(tmp_path, which, exists, capabilities):
    code, output, requests = run_access_check(tmp_path, which, capabilities, exists=exists)
    assert code == 0, output
    assert PLAYBOOKS[which][3] in output
    assert ("POST", "/v1/sys/capabilities-self") in requests
    assert writes(requests) == []


@pytest.mark.parametrize("which", sorted(PLAYBOOKS))
def test_access_check_never_writes_even_with_a_staged_value(tmp_path, which):
    code, output, requests = run_access_check(tmp_path, which, ["read", "create"], "synthetic-provider-value")
    assert code == 0, output
    assert writes(requests) == []


REFUSED = [
    (False, ["deny"]), (False, ["read"]),
    (False, ["read", "update"]), (False, ["read", "patch"]),   # new path is POSTed: needs create
    (True, ["read", "create"]), (True, ["read", "update"]),    # existing path is PATCHed: needs patch
    (False, ["create"]),                                        # the seed reads first
]


@pytest.mark.parametrize("which,exists,capabilities",
                         [(MATRIX, *case) for case in REFUSED] + [(WIRING, *REFUSED[0])])
def test_access_check_refuses_a_token_the_real_seed_would_be_denied(tmp_path, which, exists, capabilities):
    code, output, requests = run_access_check(tmp_path, which, capabilities, exists=exists)
    assert code != 0, output
    assert PLAYBOOKS[which][3] not in output
    assert "seeding needs read plus" in output
    assert writes(requests) == []


@pytest.mark.parametrize("which,foreign", [("postiz", "BAO_VALUE"), ("openbao-key", "SEED_X_API_KEY")])
def test_a_leftover_input_of_another_seed_is_refused_before_the_login(tmp_path, which, foreign):
    # The isolated environment is checked when bound; the run re-checks what actually arrived.
    code, output, requests = run_access_check(tmp_path, which, ["read", "create"], foreign=foreign)
    assert code != 0, output
    assert f"does not declare: {foreign}" in output
    assert requests == []  # not even the AppRole login


@pytest.mark.parametrize("which", sorted(PLAYBOOKS))
def test_an_address_other_than_the_inventorys_is_refused_before_the_login(tmp_path, which):
    # An environment edited after binding (or a -e) overrides the inventory's address.
    code, output, requests = run_access_check(tmp_path, which, ["read", "create"],
                                              override="http://127.0.0.1:9")
    assert code != 0, output
    assert "An extra var overrode it" in output
    assert requests == []  # not even the AppRole login


def test_an_inventory_without_the_address_is_refused_before_the_login(tmp_path):
    # The playbooks fall back to the controller's OPENBAO_ADDR; unverifiable, so refused.
    code, output, requests = run_access_check(tmp_path, WIRING, ["read", "create"], declare=False, env_addr=True)
    assert code != 0, output
    assert "declares no single all.vars.openbao_addr" in output
    assert requests == []


EVIL = "http://127.0.0.1:9"


@pytest.mark.parametrize("inject", [
    # Codex review of PR #256: forge the check's own inputs alongside the override.
    {"openbao_addr": EVIL, "_ba_declared": [EVIL], "_ba_url": EVIL, "_ba_seen": [EVIL]},
    {"_bao_url": EVIL},             # the login URL itself
    {"_bm_url": EVIL},              # the merge target, set later by the playbook
    {"_sa_url": EVIL},              # the access-check target
], ids=["forged-check-inputs", "login-url", "merge-url", "access-url"])
def test_injected_extra_vars_cannot_move_the_login_or_the_write(tmp_path, inject):
    code, output, requests = run_access_check(tmp_path, "openbao-key", ["read", "create"], inject=inject)
    assert code != 0, output
    # The login task must never RUN: a run whose login went to the injected address also shows
    # no request here and fails (nothing listens there), so an empty log alone proves nothing.
    assert "TASK [Refuse an OpenBao address that is not the inventory's]" in output
    assert "TASK [Authenticate to OpenBao (AppRole)]" not in output
    assert requests == []


def test_a_templated_declaration_is_refused_with_its_own_reason(tmp_path):
    code, output, requests = run_access_check(tmp_path, WIRING, ["read", "create"],
                                              declared_value="http://{{ openbao_host }}")
    assert code != 0, output
    assert "declares a templated all.vars.openbao_addr" in output
    assert requests == []
