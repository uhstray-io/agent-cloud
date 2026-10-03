"""The watcher-token mint, run for real against a synthetic OpenBao and Grafana.

The container engine is a stub that runs the in-container shell on the controller, so the
playbook's curl calls reach the loopback fake. All values are synthetic.
"""

import json
import os
import subprocess
from urllib.parse import urlsplit

import pytest
from seed_harness import ROLE, ROOT, FakeBao, serve

PLAYBOOK = "platform/playbooks/provision-o11y-watcher-token.yml"
STORED = "synthetic-stored-token"
NEW = "synthetic-new-token"
ADMIN = "synthetic-admin-password"
ENGINE = f"""#!/bin/sh
shift            # exec
[ "$1" = "-i" ] && shift
shift            # container name
GF_SECURITY_ADMIN_PASSWORD={ADMIN} exec "$@"
"""


def make_handler(stored=STORED, valid=(STORED,), health=200, account=None):
    """`account` is the existing service account's {role, isDisabled}, or None when absent."""

    class Handler(FakeBao):
        requests = []
        store = {"grafana_admin_password": "synthetic-gf", **({"watcher_token": stored} if stored else {})}
        tokens = [{"id": 3, "name": "watcher-old"}]
        accepted = set(valid)
        sa = dict(account, id=7, name="o11y-watcher") if account else None

        def _drain(self):
            return self.body() if int(self.headers.get("Content-Length", 0)) else {}

        def do_POST(self):
            self.record("POST")
            self._drain()
            cls = type(self)
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            if self.path == "/api/serviceaccounts":
                cls.sa = {"id": 7, "name": "o11y-watcher", "role": "Viewer", "isDisabled": False}
                return self.reply(cls.sa, 201)
            if self.path == "/api/serviceaccounts/7/tokens":
                cls.tokens.append({"id": 9, "name": "watcher-new"})
                cls.accepted.add(NEW)
                return self.reply({"id": 9, "name": "watcher-new", "key": NEW})
            return self.reply({}, 404)

        def do_PATCH(self):
            self.record("PATCH")
            type(self).store.update(self._drain().get("data", {}))
            return self.reply({"data": {"version": 2}})

        def do_DELETE(self):
            self.record("DELETE")
            token_id = int(self.path.rsplit("/", 1)[1])
            type(self).tokens = [t for t in type(self).tokens if t["id"] != token_id]
            return self.reply({"message": "API key deleted"})

        def do_GET(self):
            self.record("GET")
            cls = type(self)
            path = urlsplit(self.path).path
            if path == "/v1/secret/data/services/o11y":
                return self.reply({"data": {"data": cls.store, "metadata": {"version": 1}}})
            if path == "/api/datasources/uid/prometheus/health":
                if self.headers.get("Authorization", "").removeprefix("Bearer ") not in cls.accepted:
                    return self.reply({"message": "invalid API key"}, 401)
                return self.reply({"status": "OK" if health == 200 else "ERROR"}, health)
            if path == "/api/serviceaccounts/search":
                return self.reply({"serviceAccounts": [cls.sa] if cls.sa else []})
            if path == "/api/serviceaccounts/7":
                return self.reply(cls.sa) if cls.sa else self.reply({}, 404)
            if path == "/api/serviceaccounts/7/tokens":
                return self.reply(cls.tokens)
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
            ["ansible-playbook", "-v", *(["--check"] if check else []), "-i", str(inventory), PLAYBOOK,
             "-e", json.dumps(ROLE)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=180, stdin=subprocess.DEVNULL,
        )
    out = result.stdout + result.stderr
    for secret in (STORED, NEW, ADMIN, "synthetic-login-value", "synthetic-role-secret"):
        assert secret not in out
    return result.returncode, out


def mints(handler):
    return [r for r in handler.requests if r == ("POST", "/api/serviceaccounts/7/tokens")]


def test_valid_stored_token_is_reused(tmp_path):
    handler = make_handler(account={"role": "Viewer", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc == 0, out
    assert "reusing it" in out
    assert mints(handler) == [] and handler.store["watcher_token"] == STORED


@pytest.mark.parametrize("account", [None, {"role": "Viewer", "isDisabled": False}])
def test_rejected_token_rotates_and_prunes_superseded(tmp_path, account):
    handler = make_handler(valid=(), account=account)
    rc, out = run(tmp_path, handler)
    assert rc == 0, out
    assert len(mints(handler)) == 1
    assert handler.store["watcher_token"] == NEW
    assert handler.tokens == [{"id": 9, "name": "watcher-new"}]


def test_outage_status_fails_without_minting(tmp_path):
    handler = make_handler(health=503, account={"role": "Viewer", "isDisabled": False})
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "status 503" in out
    assert mints(handler) == [] and handler.store["watcher_token"] == STORED


@pytest.mark.parametrize("account", [{"role": "Admin", "isDisabled": False}, {"role": "Viewer", "isDisabled": True}])
def test_unsafe_existing_account_is_refused(tmp_path, account):
    handler = make_handler(valid=(), account=account)
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "the watcher needs an enabled Viewer" in out
    assert mints(handler) == [] and handler.store["watcher_token"] == STORED


def test_new_token_failure_is_visible(tmp_path):
    handler = make_handler(valid=(), account={"role": "Viewer", "isDisabled": False})
    handler.accepted = set()
    original = handler.do_POST

    def no_accept(self):
        original(self)
        type(self).accepted.discard(NEW)

    handler.do_POST = no_accept
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "The new watcher token got status 401" in out, out
    assert handler.tokens[0]["id"] == 3  # superseded token kept


def test_check_mode_mints_nothing(tmp_path):
    handler = make_handler(valid=(), account=None)
    rc, out = run(tmp_path, handler, check=True)
    assert rc == 0, out
    assert "would be minted" in out
    assert mints(handler) == [] and not [r for r in handler.requests if r[0] in ("PATCH", "DELETE")]
