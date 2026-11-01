"""Synthetic end-to-end checks for the operator inventory-sync frontend."""

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "semaphore-sync-inventory.py"
PLAYBOOK = ROOT / "platform" / "semaphore" / "sync-inventory.yml"
TOKEN = "SYNTHETIC_SYNC_TOKEN_CANARY_71c9"
PRIVATE = "PRIVATE_INVENTORY_CANARY_8d25"
ENV_TOKEN = "ENVIRONMENT_TOKEN_CANARY_5e10"
BASE = {
    "id": 17, "name": "production", "project_id": 1, "type": "static-yaml",
    "inventory": "old: {}\n", "ssh_key_id": 23, "become_key_id": 29,
}


class FakeSemaphore(BaseHTTPRequestHandler):
    state = None

    def log_message(self, _format, *_args):
        return

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_GET(self):
        if self.path == "/v1/secret/data/services/semaphore":
            if self.headers.get("X-Vault-Token") != "SYNTHETIC_BAO_TOKEN":
                self.reply(403, {"error": PRIVATE})
                return
            self.state["bao_reads"] += 1
            self.reply(200, {"data": {"data": {"api_token": TOKEN}}})
            return
        if not self.authorized():
            self.reply(401, {"error": f"{PRIVATE} {TOKEN}"})
        elif self.path.endswith("/inventory"):
            self.state["gets"] += 1
            if self.state["hold_collection_get"]:
                self.state["request_held"].set()
                if not self.state["release_request"].wait(timeout=30):
                    self.reply(504, {"error": "synthetic request hold timed out"})
                    return
            self.reply(200, [dict(self.state["record"])])
        elif self.path.endswith("/inventory/17"):
            self.state["readbacks"] += 1
            record = dict(self.state["record"])
            if self.state["metadata_mismatch"]:
                record["ssh_key_id"] += 1
            self.reply(200, record)
        else:
            self.reply(404, {"error": PRIVATE})

    def do_POST(self):
        if self.path != "/v1/auth/approle/login":
            self.reply(404, {"error": PRIVATE})
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body != {"role_id": "SYNTHETIC_ROLE_ID", "secret_id": "SYNTHETIC_SECRET_ID"}:
            self.reply(403, {"error": PRIVATE})
            return
        self.state["bao_logins"] += 1
        self.reply(200, {"auth": {"client_token": "SYNTHETIC_BAO_TOKEN"}})

    def do_PUT(self):
        if not self.authorized():
            self.reply(401, {"error": f"{PRIVATE} {TOKEN}"})
            return
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.state["puts"] += 1
        self.state["payload"] = data
        if self.state["accept_then_drop"]:
            self.state["record"].update(data)
            self.connection.shutdown(1)
            self.connection.close()
            return
        if not self.state["mismatch"]:
            self.state["record"].update(data)
        self.reply(200, {"ok": True})


@pytest.fixture
def fake_api():
    FakeSemaphore.state = {
        "record": dict(BASE), "gets": 0, "readbacks": 0, "puts": 0, "payload": None,
        "mismatch": False, "accept_then_drop": False, "metadata_mismatch": False,
        "bao_logins": 0, "bao_reads": 0, "hold_collection_get": False,
        "request_held": threading.Event(), "release_request": threading.Event(),
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSemaphore)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", FakeSemaphore.state
    finally:
        FakeSemaphore.state["release_request"].set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


VALID = f"""\
compute:
  hosts:
    host-a:
      private_note: {PRIVATE}
storage:
  children:
    storage_nodes:
      hosts:
        host-b: {{}}
"""


def ansible_bin(name):
    sibling = Path(sys.executable).resolve().parent / name
    if sibling.is_file():
        return str(sibling)
    found = shutil.which(name)
    if found:
        return found
    pytest.fail(f"{name} is required for the inventory-sync integration tests")


def run_sync(tmp_path, url, inventory, *options, token=TOKEN, path=None, env_overrides=None):
    source = tmp_path / "inventory with spaces.yml"
    source.write_text(inventory)
    local_tmp = tmp_path / "ansible-local"
    remote_tmp = tmp_path / "ansible-remote"
    local_tmp.mkdir(exist_ok=True)
    remote_tmp.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["SEMAPHORE_TOKEN"] = ENV_TOKEN
    env["ANSIBLE_LOCAL_TEMP"] = str(local_tmp)
    env["ANSIBLE_REMOTE_TEMP"] = str(remote_tmp)
    env["PATH"] = f"{path or Path(ansible_bin('ansible-playbook')).parent}:{env.get('PATH', '')}"
    env.update(env_overrides or {})
    return subprocess.run(
        [str(SCRIPT), "--url", url, "--inventory", str(source), "--name", "production",
         "--project", "1", *options],
        input=token + "\n", text=True, capture_output=True, env=env, cwd=ROOT, check=False,
    )


def assert_clean(result):
    assert TOKEN not in result.stdout + result.stderr
    assert PRIVATE not in result.stdout + result.stderr
    assert ENV_TOKEN not in result.stdout + result.stderr


def test_actual_child_stdin_is_only_token_channel_and_path_is_json(tmp_path, fake_api):
    url, _state = fake_api
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    child = bindir / "ansible-playbook"
    token_digest = hashlib.sha256(TOKEN.encode()).hexdigest()
    child.write_text(
        "#!/usr/bin/env python3\nimport hashlib,json,os,sys\n"
        "data=json.load(sys.stdin)\n"
        f"assert hashlib.sha256(data['sync_inventory_token'].encode()).hexdigest()=={token_digest!r}\n"
        "assert 'SEMAPHORE_TOKEN' not in os.environ\n"
        f"assert all(hashlib.sha256(value.encode()).hexdigest()!={token_digest!r} for value in os.environ.values())\n"
        "assert data['inventory_source'].endswith('inventory with spaces.yml')\n"
        "assert '--extra-vars' in sys.argv and '@/dev/stdin' in sys.argv\n"
        "assert '--check' in sys.argv and '--apply' not in sys.argv\n"
        f"assert all(hashlib.sha256(arg.encode()).hexdigest()!={token_digest!r} for arg in sys.argv)\n"
        "print('Validated inventory already matches; no write is needed.')\n"
    )
    child.chmod(0o755)
    result = run_sync(tmp_path, url, VALID, path=bindir)
    assert result.returncode == 0
    assert result.stdout == "Inventory check completed; the existing record already matches.\n"
    assert_clean(result)
    assert all(TOKEN.encode() not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


@pytest.mark.parametrize("inventory", ["", "{ broken", "groups: {}\n", "{}\n"])
def test_empty_malformed_and_hostless_inventory_refuse_before_put(tmp_path, fake_api, inventory):
    url, state = fake_api
    result = run_sync(tmp_path, url, inventory, "--apply")
    assert result.returncode != 0
    assert state["gets"] == state["puts"] == 0
    assert "uncertain" in result.stderr.lower()
    assert_clean(result)


def test_preview_is_default_and_explicit_apply_updates_once_with_metadata(tmp_path, fake_api):
    url, state = fake_api
    preview = run_sync(tmp_path, url, VALID)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert "Inventory check completed" in preview.stdout
    assert state["puts"] == 0

    result = run_sync(tmp_path, url, VALID, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "updated content was verified" in result.stdout
    assert state["puts"] == 1 and state["readbacks"] == 2
    assert state["record"]["inventory"] == VALID
    for field in ("id", "name", "project_id", "type", "ssh_key_id", "become_key_id"):
        assert state["payload"][field] == BASE[field]
        assert state["record"][field] == BASE[field]
    assert_clean(preview)
    assert_clean(result)


def test_matching_apply_is_idempotent_and_check_mode_never_puts(tmp_path, fake_api):
    url, state = fake_api
    state["record"]["inventory"] = VALID
    same = run_sync(tmp_path, url, VALID, "--apply")
    check = run_sync(tmp_path, url, VALID.replace("host-a", "host-c"), "--check")
    assert same.returncode == check.returncode == 0
    assert state["puts"] == 0
    assert "existing record already matched" in same.stdout
    assert "Inventory check completed; the existing record differs." in check.stdout
    assert_clean(same)
    assert_clean(check)


def test_missing_record_and_readback_mismatch_fail_closed(tmp_path, fake_api):
    url, state = fake_api
    state["record"]["name"] = "other"
    missing = run_sync(tmp_path, url, VALID, "--apply")
    assert missing.returncode != 0 and state["puts"] == 0
    state["record"] = dict(BASE)
    state["mismatch"] = True
    mismatch = run_sync(tmp_path, url, VALID, "--apply")
    assert mismatch.returncode != 0 and state["puts"] == 1 and state["readbacks"] == 1
    assert "uncertain" in mismatch.stderr.lower()
    assert_clean(missing)
    assert_clean(mismatch)


def test_readback_key_metadata_mismatch_refuses_success(tmp_path, fake_api):
    url, state = fake_api
    state["record"]["inventory"] = VALID
    state["metadata_mismatch"] = True
    result = run_sync(tmp_path, url, VALID, "--apply")
    assert result.returncode != 0
    assert state["puts"] == 0 and state["readbacks"] == 1
    assert "uncertain" in result.stderr.lower()
    assert_clean(result)


def test_lost_write_acknowledgement_is_uncertain_and_not_retried(tmp_path, fake_api):
    url, state = fake_api
    state["accept_then_drop"] = True
    result = run_sync(tmp_path, url, VALID, "--apply")
    assert result.returncode != 0
    assert state["puts"] == 1
    assert state["record"]["inventory"] == VALID
    assert "uncertain" in result.stderr.lower()
    assert "read-only" in result.stderr.lower()
    assert_clean(result)


def test_empty_token_fails_before_child_or_api(tmp_path, fake_api):
    url, state = fake_api
    result = run_sync(tmp_path, url, VALID, token="")
    assert result.returncode == 2 and state["gets"] == state["puts"] == 0
    assert_clean(result)


def test_multiline_token_fails_before_child_or_api(tmp_path, fake_api):
    url, state = fake_api
    result = run_sync(tmp_path, url, VALID, token=f"{TOKEN}\nSECOND_LINE")
    assert result.returncode == 2 and state["gets"] == state["puts"] == 0
    assert_clean(result)


@pytest.mark.parametrize("token", ["", None, [TOKEN]])
def test_defined_empty_or_nonstring_sync_token_never_falls_back_to_environment(tmp_path, fake_api, token):
    url, state = fake_api
    source = tmp_path / "invalid-sync-token.yml"
    source.write_text(VALID)
    env = os.environ.copy()
    env["PATH"] = f"{Path(ansible_bin('ansible-playbook')).parent}:{env.get('PATH', '')}"
    env["SEMAPHORE_TOKEN"] = TOKEN
    extra = json.dumps({
        "semaphore_url": url, "semaphore_project_id": 1, "inventory_name": "production",
        "inventory_source": str(source), "sync_inventory_token": token,
    })
    result = subprocess.run(
        [ansible_bin("ansible-playbook"), "-i", "localhost,", str(PLAYBOOK), "--extra-vars", extra],
        text=True, capture_output=True, env=env, cwd=ROOT, check=False,
    )
    assert result.returncode != 0
    assert state["gets"] == state["puts"] == 0
    assert_clean(result)


def test_api_error_body_diagnostics_stay_hidden_and_apply_is_uncertain(tmp_path, fake_api):
    url, state = fake_api
    result = run_sync(tmp_path, url, VALID, "--apply", token="WRONG_SYNTHETIC_TOKEN")
    assert result.returncode != 0
    assert state["gets"] == state["puts"] == 0
    assert "uncertain" in result.stderr.lower()
    assert_clean(result)


def test_existing_environment_token_route_still_runs_canonical_play(tmp_path, fake_api):
    url, state = fake_api
    source = tmp_path / "existing-route.yml"
    source.write_text(VALID)
    env = os.environ.copy()
    env["PATH"] = f"{Path(ansible_bin('ansible-playbook')).parent}:{env.get('PATH', '')}"
    env["SEMAPHORE_TOKEN"] = TOKEN
    extra = json.dumps({
        "semaphore_url": url, "semaphore_project_id": 1, "inventory_name": "production",
        "inventory_source": str(source),
    })
    result = subprocess.run(
        [ansible_bin("ansible-playbook"), "-i", "localhost,", str(PLAYBOOK), "--extra-vars", extra],
        text=True, capture_output=True, env=env, cwd=ROOT, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert state["puts"] == 1 and state["readbacks"] == 1
    assert_clean(result)


def test_existing_controller_approle_route_still_resolves_and_syncs(tmp_path, fake_api):
    url, state = fake_api
    source = tmp_path / "approle-route.yml"
    source.write_text(VALID)
    env = os.environ.copy()
    env.pop("SEMAPHORE_TOKEN", None)
    env["PATH"] = f"{Path(ansible_bin('ansible-playbook')).parent}:{env.get('PATH', '')}"
    extra = json.dumps({
        "semaphore_url": url, "semaphore_project_id": 1, "inventory_name": "production",
        "inventory_source": str(source), "openbao_addr": url,
        "bao_role_id": "SYNTHETIC_ROLE_ID", "bao_secret_id": "SYNTHETIC_SECRET_ID",
    })
    result = subprocess.run(
        [ansible_bin("ansible-playbook"), "-i", "localhost,", str(PLAYBOOK), "--extra-vars", extra],
        text=True, capture_output=True, env=env, cwd=ROOT, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert state["bao_logins"] == state["bao_reads"] == 1
    assert state["puts"] == 1 and state["readbacks"] == 1
    assert_clean(result)


@pytest.mark.parametrize(
    ("source_caller", "caller_kind", "include"),
    [
        (ROOT / "platform/semaphore/setup-templates.yml", "semaphore", "tasks/runtime-access.yml"),
        (
            ROOT / "platform/playbooks/collect-service-conformance.yml",
            "playbooks",
            "../semaphore/tasks/runtime-access.yml",
        ),
    ],
)
def test_shared_runtime_access_relative_include_and_sync_token_isolation(
    source_caller, caller_kind, include, fake_api, tmp_path,
):
    url, _state = fake_api
    caller_text = source_caller.read_text()
    assert f"ansible.builtin.include_tasks: {include}" in caller_text
    platform = tmp_path / "platform"
    semaphore = platform / "semaphore"
    tasks = semaphore / "tasks"
    tasks.mkdir(parents=True)
    for name in ("runtime-access.yml", "runtime-preflight.yml"):
        (tasks / name).symlink_to(ROOT / "platform" / "semaphore" / "tasks" / name)
    caller_dir = platform / caller_kind
    caller_dir.mkdir(parents=True, exist_ok=True)
    platform_playbooks_tasks = platform / "playbooks" / "tasks"
    platform_playbooks_tasks.mkdir(parents=True, exist_ok=True)
    (platform_playbooks_tasks / "require-parsed-inventory.yml").symlink_to(
        ROOT / "platform" / "playbooks" / "tasks" / "require-parsed-inventory.yml"
    )
    playbook_path = caller_dir / "synthetic-caller.yml"
    play = f"""\
- name: Synthetic shared runtime caller
  hosts: localhost
  gather_facts: false
  vars:
    semaphore_url: {url}
    _semaphore_url: {url}
  tasks:
    - name: Include the existing shared runtime task from this caller directory
      ansible.builtin.include_tasks: {include}
    - name: Prove the shared route used its existing environment credential
      ansible.builtin.assert:
        that: _token == lookup('ansible.builtin.env', 'SEMAPHORE_TOKEN')
"""
    env = os.environ.copy()
    env["SEMAPHORE_TOKEN"] = ENV_TOKEN
    env["ANSIBLE_LOCAL_TEMP"] = str(tmp_path / "ansible-local")
    env["ANSIBLE_REMOTE_TEMP"] = str(tmp_path / "ansible-remote")
    extra = json.dumps({"sync_inventory_token": TOKEN})
    playbook_path.write_text(play)
    result = subprocess.run(
        [ansible_bin("ansible-playbook"), "-i", "localhost,", str(playbook_path), "--extra-vars", extra],
        text=True, capture_output=True, env=env, cwd=caller_dir, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert TOKEN not in result.stdout + result.stderr
    assert ENV_TOKEN not in result.stdout + result.stderr


def _token_files(roots):
    return [
        str(path.relative_to(roots[0].parent))
        for root in roots if root.exists()
        for path in root.rglob("*")
        if path.is_file() and TOKEN.encode() in path.read_bytes()
    ]


def test_actual_uri_module_tempfile_red_without_pipelining_green_with_pipelining(
    tmp_path, fake_api, monkeypatch, capsys,
):
    url, state = fake_api
    source = tmp_path / "held request inventory.yml"
    source.write_text(VALID)
    script_digest = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    captured = {}

    def prepare_dirs(name):
        local_tmp = tmp_path / name / "local"
        remote_tmp = tmp_path / name / "remote"
        local_tmp.mkdir(parents=True)
        remote_tmp.mkdir(parents=True)
        return local_tmp, remote_tmp

    local_tmp, remote_tmp = prepare_dirs("red")
    monkeypatch.setenv("ANSIBLE_LOCAL_TEMP", str(local_tmp))
    monkeypatch.setenv("ANSIBLE_REMOTE_TEMP", str(remote_tmp))
    monkeypatch.setenv("PATH", f"{Path(ansible_bin('ansible-playbook')).parent}:{os.environ.get('PATH', '')}")
    module_spec = importlib.util.spec_from_file_location("sync_inventory_under_test", SCRIPT)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    original_environment = module.ansible_child_environment

    def unprotected_environment():
        child_env = original_environment()
        child_env["ANSIBLE_PIPELINING"] = "False"
        return child_env

    monkeypatch.setattr(module, "ansible_child_environment", unprotected_environment)
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT), "--url", url, "--inventory", str(source)])
    monkeypatch.setattr(module.sys, "stdin", io.StringIO(TOKEN + "\n"))

    def invoke_red():
        captured["red_rc"] = module.main()

    state["hold_collection_get"] = True
    worker = threading.Thread(target=invoke_red, daemon=True)
    worker.start()
    assert state["request_held"].wait(timeout=20), "actual canonical URI GET was not held"
    red_files = _token_files((local_tmp, remote_tmp))
    state["release_request"].set()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert captured["red_rc"] == 0
    assert red_files, "pipelining-off control did not demonstrate the known transient-file leak"
    red_output = capsys.readouterr()
    assert TOKEN not in red_output.out + red_output.err

    # Reset the synthetic API and run the unmodified wrapper as a child process.
    fake_api_state = state
    fake_api_state["record"] = dict(BASE)
    fake_api_state["gets"] = fake_api_state["readbacks"] = fake_api_state["puts"] = 0
    fake_api_state["hold_collection_get"] = True
    fake_api_state["request_held"].clear()
    fake_api_state["release_request"].clear()
    local_green, remote_green = prepare_dirs("green")
    env = os.environ.copy()
    env["ANSIBLE_LOCAL_TEMP"] = str(local_green)
    env["ANSIBLE_REMOTE_TEMP"] = str(remote_green)
    env["SEMAPHORE_TOKEN"] = ENV_TOKEN
    env["PATH"] = f"{Path(ansible_bin('ansible-playbook')).parent}:{env.get('PATH', '')}"
    green = subprocess.Popen(
        [str(SCRIPT), "--url", url, "--inventory", str(source)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, cwd=ROOT,
    )
    green.stdin.write(TOKEN + "\n")
    green.stdin.flush()
    green.stdin.close()
    green.stdin = None
    assert fake_api_state["request_held"].wait(timeout=20), "protected canonical URI GET was not held"
    green_files = _token_files((local_green, remote_green))
    fake_api_state["release_request"].set()
    green_out, green_err = green.communicate(timeout=30)
    assert green.returncode == 0, green_out + green_err
    assert not green_files
    assert TOKEN not in green_out + green_err
    assert ENV_TOKEN not in green_out + green_err
    assert hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == script_digest
    print(json.dumps({
        "proof": "actual canonical URI request held while scanning Ansible temp files",
        "synthetic_only": True,
        "pipelining_off_token_files": len(red_files),
        "pipelining_on_token_files": len(green_files),
        "wrapper_stdout_leaked_token": False,
        "source_unchanged": True,
    }, sort_keys=True))


def test_runtime_access_keeps_shared_preflight_and_existing_auth_routes():
    runtime = (ROOT / "platform/semaphore/tasks/runtime-access.yml").read_text()
    preflight = (ROOT / "platform/semaphore/tasks/runtime-preflight.yml").read_text()
    assert "runtime-preflight.yml" in runtime
    assert "sync_inventory_token" not in runtime
    assert "SEMAPHORE_TOKEN" in runtime and "BAO_ROLE_ID" in runtime and "BAO_SECRET_ID" in runtime
    assert "require-parsed-inventory.yml" in preflight
    assert "non-cleartext transport to Semaphore" in preflight


def test_canonical_play_keeps_both_credential_routes_and_inventory_stdin_parser():
    play = PLAYBOOK.read_text()
    assert "ansible.builtin.include_tasks: tasks/runtime-preflight.yml" in play
    assert "ansible.builtin.include_tasks: tasks/runtime-access.yml" in play
    assert '"@/dev/stdin"' in play
    assert 'stdin: "{}"' in play
