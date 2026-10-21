"""Exercise bao-merge-keys against a local KV-v2 HTTP double."""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "platform/playbooks/tasks/bao-merge-keys.yml"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


class _Vault:
    def __init__(self, initial=None, *, conflict_on_post=False, create_race=False):
        self.data = initial
        self.version = 1 if initial is not None else 0
        self.conflict_on_post = conflict_on_post
        self.create_race = create_race
        self.requests = []

    def handler(self):
        vault = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _body(self):
                return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))

            def _reply(self, status, body=None):
                raw = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                vault.requests.append(("GET", None))
                if vault.data is None:
                    self._reply(404)
                else:
                    self._reply(200, {"data": {"data": vault.data, "metadata": {"version": vault.version}}})

            def do_POST(self):
                body = self._body()
                vault.requests.append(("POST", body))
                if vault.conflict_on_post:
                    vault.conflict_on_post = False
                    vault.data["concurrent_sibling"] = "winner"
                    vault.version += 1
                    self._reply(400, {"errors": ["check-and-set parameter did not match the current version"]})
                    return
                cas = int(body.get("options", {}).get("cas", -1))
                if cas == 0 and vault.create_race:
                    vault.create_race = False
                    vault.data = {"concurrent_sibling": "winner"}
                    vault.version = 1
                    self._reply(400, {"errors": ["check-and-set conflict"]})
                    return
                if (cas == 0 and vault.data is not None) or (cas != 0 and cas != vault.version):
                    self._reply(400, {"errors": ["check-and-set conflict"]})
                    return
                vault.data = dict(body["data"])
                vault.version = 1 if cas == 0 else vault.version + 1
                self._reply(200, {"data": {"version": vault.version}})

            def do_PATCH(self):
                body = self._body()
                vault.requests.append(("PATCH", body))
                assert vault.data is not None
                for key, value in body["data"].items():
                    if value is None:
                        vault.data.pop(key, None)
                    else:
                        vault.data[key] = value
                vault.version += 1
                self._reply(200, {"data": {"version": vault.version}})

        return Handler


def _run(tmp_path, vault, *, data=None, guard_absent=None, guard_values=None, guard_key=None, guard_value=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), vault.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        vars_ = {
            "_bm_url": f"http://127.0.0.1:{server.server_port}",
            "_bm_token": "test-token",
            "_bm_path": "services/test",
            "_bm_data": data or {"new_value": "written"},
        }
        if guard_absent is not None:
            vars_["_bm_guard_absent_keys"] = guard_absent
        if guard_values is not None:
            vars_["_bm_guard_values"] = guard_values
        if guard_key is not None:
            vars_["_bm_guard_key"] = guard_key
            vars_["_bm_guard_value"] = guard_value
        playbook = tmp_path / "merge.yml"
        playbook.write_text(
            "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:\n"
            f"    - ansible.builtin.include_tasks: {TASK}\n      vars:\n"
            + "\n".join(f"        {key}: {json.dumps(value)}" for key, value in vars_.items())
            + "\n"
        )
        env = {key: value for key, value in os.environ.items() if key != "ANSIBLE_CONFIG"}
        env["ANSIBLE_NOCOLOR"] = "1"
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(playbook)],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
        return result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _writes(vault):
    return [request for request in vault.requests if request[0] in {"POST", "PATCH"}]


def test_concurrent_writer_fields_seen_by_fresh_get_refuse_before_write(tmp_path):
    fields = {
        "backup_user": "winner-user",
        "backup_token_id": "winner-token-id",
        "backup_token_secret": "winner-secret",
        "backup_datastore": "winner-datastore",
        "backup_fingerprint": "winner-fingerprint",
    }
    vault = _Vault({**fields, "sibling": "keep"})
    result = _run(tmp_path, vault, data={**fields, "backup_token_secret": "loser-secret"}, guard_absent=list(fields))
    assert result.returncode != 0
    assert "censored" in result.stdout.lower()
    assert "winner-secret" not in result.stdout and "loser-secret" not in result.stdout
    assert [method for method, _ in vault.requests] == ["GET"]
    assert _writes(vault) == []
    assert vault.data["backup_token_secret"] == "winner-secret"


def test_absent_key_guard_allows_cas_zero_create(tmp_path):
    vault = _Vault()
    result = _run(tmp_path, vault, guard_absent=["reserved_credential"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert [method for method, _ in _writes(vault)] == ["POST"]
    assert _writes(vault)[0][1]["options"]["cas"] == 0
    assert vault.data == {"new_value": "written"}


def test_absent_key_guard_allows_idempotent_noop_without_cas_write(tmp_path):
    vault = _Vault({"new_value": "written", "sibling": "keep"})
    result = _run(tmp_path, vault, guard_absent=["reserved_credential"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _writes(vault) == []
    assert vault.data == {"new_value": "written", "sibling": "keep"}


@pytest.mark.parametrize("present_value", [None, False])
def test_null_or_false_still_counts_as_present(tmp_path, present_value):
    vault = _Vault({"reserved_credential": present_value, "sibling": "keep"})
    result = _run(tmp_path, vault, guard_absent=["reserved_credential"])
    assert result.returncode != 0
    assert "censored" in result.stdout.lower()
    assert "reserved_credential" not in result.stdout
    assert _writes(vault) == []


def test_version_conflict_fails_closed_without_patch_fallback(tmp_path):
    vault = _Vault({"sibling": "before"}, conflict_on_post=True)
    result = _run(tmp_path, vault, guard_absent=["reserved_credential"])
    assert result.returncode != 0
    assert "OpenBao rejected the version-guarded update" in result.stdout
    assert [method for method, _ in _writes(vault)] == ["POST"]
    assert vault.data == {"sibling": "before", "concurrent_sibling": "winner"}


def test_create_race_keeps_cas_zero_guard_and_never_falls_back_to_patch(tmp_path):
    vault = _Vault(create_race=True)
    result = _run(tmp_path, vault, guard_absent=["reserved_credential"])
    assert result.returncode != 0
    assert "censored" in result.stdout.lower()
    assert [method for method, _ in _writes(vault)] == ["POST"]
    assert vault.data == {"concurrent_sibling": "winner"}


@pytest.mark.parametrize("guard_mode", ["values", "key"])
def test_existing_guard_forms_keep_siblings_on_cas_update(tmp_path, guard_mode):
    initial = {"stable": "expected", "target": "old", "sibling": "keep"}
    vault = _Vault(initial.copy())
    kwargs = {"guard_values": {"stable": "expected"}} if guard_mode == "values" else {
        "guard_key": "target", "guard_value": "old"
    }
    result = _run(tmp_path, vault, data={"target": "new"}, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [method for method, _ in _writes(vault)] == ["POST"]
    assert vault.data == {"stable": "expected", "target": "new", "sibling": "keep"}


@pytest.mark.parametrize("bad_guard", ["secret", {"key": "value"}, ["key", 3], ["key", "key"]])
def test_invalid_absent_key_guard_is_rejected_before_openbao_read(tmp_path, bad_guard):
    vault = _Vault({"sibling": "keep"})
    result = _run(tmp_path, vault, guard_absent=bad_guard)
    assert result.returncode != 0
    assert vault.requests == []
