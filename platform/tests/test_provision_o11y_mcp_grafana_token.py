"""MCP Grafana account and staged-token reconciliation against fake APIs."""

import json
import os
import subprocess
from urllib.parse import urlsplit

import pytest
from seed_harness import ROLE, ROOT, FakeBao, serve

PLAYBOOK = "platform/playbooks/provision-o11y-mcp-grafana-token.yml"
STORED = "synthetic-stored-token"
CANDIDATE = "synthetic-candidate-token"
RACED_CANDIDATE = "synthetic-raced-candidate-token"
OTHER_ACCOUNT_TOKEN = "synthetic-other-account-token"
ADMIN = "synthetic-admin-password"
ENGINE = f"""#!/bin/sh
shift            # exec
[ "$1" = "-i" ] && shift
shift            # container name
GF_SECURITY_ADMIN_PASSWORD={ADMIN} exec "$@"
"""


def make_handler(stored=STORED, candidate=None, valid=(STORED,), health=200, account=None, rejection=401,
                 race_on_merge=False, identities=None):
    """`account` is the exact account's {role, isDisabled}, or None when absent."""

    class Handler(FakeBao):
        requests = []
        store = {
            "grafana_admin_password": "synthetic-gf",
            **({"mcp_grafana_token": stored} if stored else {}),
            **({"mcp_grafana_token_candidate": candidate} if candidate else {}),
        }
        accepted = set(valid)
        identity_by_token = {STORED: "service-account:7", CANDIDATE: "service-account:7"}
        identity_by_token.update(identities or {})
        sa = dict(account, id=7, name="mcp-grafana") if account else None
        version = 1
        inject_race = race_on_merge

        def _drain(self):
            return self.body() if int(self.headers.get("Content-Length", 0)) else {}

        def do_POST(self):
            self.record("POST")
            body = self._drain()
            cls = type(self)
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            if self.path == "/v1/secret/data/services/o11y":
                if cls.inject_race:
                    # A second seed wins after the helper's fresh read but before its CAS.
                    cls.inject_race = False
                    cls.store["mcp_grafana_token_candidate"] = RACED_CANDIDATE
                    cls.version += 1
                if body.get("options", {}).get("cas") != cls.version:
                    return self.reply({"errors": ["check-and-set parameter did not match current version"]}, 400)
                cls.store = body["data"]
                cls.version += 1
                return self.reply({"data": {"version": cls.version}})
            if self.path == "/api/serviceaccounts":
                cls.sa = {"id": 7, "name": "mcp-grafana", "role": "Editor", "isDisabled": False}
                return self.reply(cls.sa, 201)
            return self.reply({}, 404)

        def do_PATCH(self):
            self.record("PATCH")
            patch = self._drain().get("data", {})
            cls = type(self)
            for key, value in patch.items():
                if value is None:
                    cls.store.pop(key, None)
                else:
                    cls.store[key] = value
            cls.version += 1
            return self.reply({"data": {"version": 2}})

        def do_GET(self):
            self.record("GET")
            cls = type(self)
            path = urlsplit(self.path).path
            if path == "/v1/secret/data/services/o11y":
                return self.reply({"data": {"data": cls.store, "metadata": {"version": cls.version}}})
            if path == "/api/user":
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if token not in cls.accepted:
                    return self.reply({"message": "invalid API key"}, rejection)
                return self.reply({"uid": cls.identity_by_token.get(token, "")}, health)
            if path == "/api/serviceaccounts/search":
                return self.reply({"serviceAccounts": [cls.sa] if cls.sa else []})
            if path == "/api/serviceaccounts/7":
                return self.reply(cls.sa) if cls.sa else self.reply({}, 404)
            return self.reply({}, 404)

    return Handler


def run(tmp_path, handler, *, check=False):
    engine = tmp_path / "engine"
    engine.write_text(ENGINE)
    engine.chmod(0o755)
    with serve(handler) as addr:
        inventory = tmp_path / "inventory.yml"
        inventory.write_text(json.dumps({"all": {
            "vars": {"openbao_addr": addr},
            "children": {"o11y_svc": {"hosts": {"localhost": {
                "ansible_connection": "local", "container_engine": str(engine),
                "o11y_grafana_local_url": addr}}}},
        }}))
        env = {k: v for k, v in os.environ.items() if k not in ("OPENBAO_ADDR", "BAO_ROLE_ID", "BAO_SECRET_ID")}
        env.update(ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
        result = subprocess.run(
            ["ansible-playbook", "-v", *( ["--check"] if check else []), "-i", str(inventory), PLAYBOOK,
             "-e", json.dumps(ROLE)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=180, stdin=subprocess.DEVNULL,
        )
    out = result.stdout + result.stderr
    for secret in (STORED, CANDIDATE, RACED_CANDIDATE, OTHER_ACCOUNT_TOKEN, ADMIN,
                   "synthetic-login-value", "synthetic-role-secret"):
        assert secret not in out
    return result.returncode, out


def writes(handler):
    return [
        r for r in handler.requests
        if r[0] in ("PATCH", "DELETE") or (r[0] == "POST" and r[1].startswith("/v1/secret/"))
    ]


def test_valid_active_token_is_reused(tmp_path):
    handler = make_handler(account={"role": "Editor", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc == 0, out
    assert "reusing it" in out
    assert writes(handler) == [] and handler.store["mcp_grafana_token"] == STORED


def test_active_token_for_another_service_account_is_not_reused(tmp_path):
    handler = make_handler(identities={STORED: "service-account:99"},
                           account={"role": "Editor", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "authenticated as a different service account" in out
    assert writes(handler) == [] and handler.store["mcp_grafana_token"] == STORED


def test_valid_active_token_with_staged_candidate_promotes_candidate(tmp_path):
    handler = make_handler(candidate=CANDIDATE, valid=(STORED, CANDIDATE),
                           account={"role": "Editor", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc == 0, out
    assert "will be promoted" in out
    assert handler.store["mcp_grafana_token"] == CANDIDATE
    assert "mcp_grafana_token_candidate" not in handler.store
    assert writes(handler) == [("POST", "/v1/secret/data/services/o11y")]


def test_valid_token_does_not_bypass_account_role_validation(tmp_path):
    handler = make_handler(account={"role": "Admin", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "the MCP Grafana needs an enabled Editor" in out
    assert writes(handler) == [] and handler.store["mcp_grafana_token"] == STORED


@pytest.mark.parametrize("rejection", [401, 403])
def test_rejected_active_token_promotes_verified_candidate(tmp_path, rejection):
    handler = make_handler(candidate=CANDIDATE, valid=(CANDIDATE,), rejection=rejection,
                           account={"role": "Editor", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc == 0, out
    assert handler.store["mcp_grafana_token"] == CANDIDATE
    assert "mcp_grafana_token_candidate" not in handler.store
    assert writes(handler) == [("POST", "/v1/secret/data/services/o11y")]


def test_concurrent_candidate_seed_is_not_promoted_or_removed(tmp_path):
    handler = make_handler(candidate=CANDIDATE, valid=(STORED, CANDIDATE),
                           account={"role": "Editor", "isDisabled": False}, race_on_merge=True)
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "version-guarded update" in out
    assert handler.store["mcp_grafana_token"] == STORED
    assert handler.store["mcp_grafana_token_candidate"] == RACED_CANDIDATE
    assert writes(handler) == [("POST", "/v1/secret/data/services/o11y")]


def test_rejected_active_token_without_candidate_fails_without_change(tmp_path):
    handler = make_handler(valid=(), account={"role": "Editor", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "seed secret/services/o11y:mcp_grafana_token_candidate" in out
    assert writes(handler) == [] and handler.store["mcp_grafana_token"] == STORED


def test_candidate_failure_preserves_active_and_candidate(tmp_path):
    handler = make_handler(candidate=CANDIDATE, valid=(), account={"role": "Editor", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "active OpenBao token was not changed" in out
    assert writes(handler) == []
    assert handler.store["mcp_grafana_token"] == STORED
    assert handler.store["mcp_grafana_token_candidate"] == CANDIDATE


def test_candidate_for_another_service_account_is_not_promoted(tmp_path):
    handler = make_handler(candidate=OTHER_ACCOUNT_TOKEN, valid=(STORED, OTHER_ACCOUNT_TOKEN),
                           identities={OTHER_ACCOUNT_TOKEN: "service-account:99"},
                           account={"role": "Editor", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "exact mcp-grafana service account" in out
    assert writes(handler) == []
    assert handler.store["mcp_grafana_token"] == STORED
    assert handler.store["mcp_grafana_token_candidate"] == OTHER_ACCOUNT_TOKEN


def test_outage_status_fails_without_promoting_candidate(tmp_path):
    handler = make_handler(candidate=CANDIDATE, valid=(STORED, CANDIDATE), health=503,
                           account={"role": "Editor", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "status 503" in out
    assert writes(handler) == [] and handler.store["mcp_grafana_token"] == STORED


@pytest.mark.parametrize("account", [
    {"role": "Admin", "isDisabled": False},
    {"role": "Editor", "isDisabled": True},
    {"role": "Editor", "isDisabled": None},
])
def test_unsafe_existing_account_is_refused(tmp_path, account):
    handler = make_handler(candidate=CANDIDATE, valid=(CANDIDATE,), account=account)
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "the MCP Grafana needs an enabled Editor" in out
    assert writes(handler) == [] and handler.store["mcp_grafana_token"] == STORED


def test_missing_account_is_created_as_editor_before_token_handoff(tmp_path):
    handler = make_handler(stored=None, valid=())
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "Create a token for the enabled mcp-grafana Editor account" in out
    assert handler.sa == {"id": 7, "name": "mcp-grafana", "role": "Editor", "isDisabled": False}
    assert writes(handler) == []


def test_check_mode_only_reports_candidate_promotion(tmp_path):
    handler = make_handler(candidate=CANDIDATE, valid=(CANDIDATE,), account={"role": "Editor", "isDisabled": False})
    rc, out = run(tmp_path, handler, check=True)
    assert rc == 0, out
    assert "would be promoted" in out
    assert writes(handler) == [] and handler.store["mcp_grafana_token"] == STORED
