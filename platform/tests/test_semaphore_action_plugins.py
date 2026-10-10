"""Focused action-plugin tests using disposable in-process HTTP responses."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "platform/playbooks/action_plugins"
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")


def _ansible_site_packages():
    if not ANSIBLE_PLAYBOOK:
        return None
    interpreter = Path(Path(ANSIBLE_PLAYBOOK).read_text().splitlines()[0][2:])
    package_dir = (interpreter.parent.parent / "lib" / "python"
                   f"{sys.version_info.major}.{sys.version_info.minor}" / "site-packages")
    return package_dir if package_dir.is_dir() else None


ANSIBLE_SITE = _ansible_site_packages()
if ANSIBLE_SITE:
    sys.path.insert(0, str(ANSIBLE_SITE))
os.environ.setdefault("ANSIBLE_LOCAL_TEMP", str(Path(tempfile.gettempdir()) / "agent-cloud-ansible-local"))
os.environ.setdefault("ANSIBLE_HOME", str(Path(tempfile.gettempdir()) / "agent-cloud-ansible-home"))
Path(os.environ["ANSIBLE_LOCAL_TEMP"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["ANSIBLE_HOME"]).mkdir(parents=True, exist_ok=True)


def _plugin(name):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNTIME = _plugin("semaphore_runtime_access")
ATTRIBUTION = _plugin("semaphore_task_attribution")


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


def _action(module, args, task_vars=None, *, check_mode=False, monkeypatch):
    monkeypatch.setattr(module.ActionBase, "run", lambda self, tmp=None, task_vars=None: {})
    monkeypatch.setattr(
        module.ActionBase, "_execute_module",
        lambda *_args, **_kwargs: pytest.fail("controller action invoked _execute_module"),
        raising=False,
    )
    action = module.ActionModule.__new__(module.ActionModule)
    action._task = SimpleNamespace(args=args)
    action._play_context = SimpleNamespace(check_mode=check_mode)
    action._connection = SimpleNamespace(
        exec_command=lambda *_args, **_kwargs: pytest.fail("controller action used a connection")
    )
    return action.run(task_vars=task_vars or {})


def test_runtime_login_and_kv_read_use_task_vars_and_return_only_expected_values(monkeypatch):
    seen = []

    class Client:
        def open(self, request, timeout):
            seen.append((request.full_url, request.headers, request.data))
            if request.full_url.endswith("/v1/auth/approle/login"):
                return _Response(b'{"auth":{"client_token":"synthetic-client-token"}}')
            return _Response(b'{"data":{"data":{"api_token":"synthetic-semaphore-token"}}}')

    monkeypatch.setattr(RUNTIME, "build_opener", lambda *_args: Client())
    url = "http://127.0.0.1:8200"
    login = _action(RUNTIME, {"operation": "login"}, {
        "_runtime_bao_url": url, "bao_role_id": "synthetic-role-id",
        "bao_secret_id": "synthetic-secret-id",
    }, monkeypatch=monkeypatch)
    assert login == {"changed": False, "json": {"auth": {"client_token": "synthetic-client-token"}}}
    read = _action(RUNTIME, {"operation": "read"}, {
        "_runtime_bao_url": url, "_runtime_login": login,
    }, monkeypatch=monkeypatch)
    assert read == {"changed": False, "json": {"data": {"data": {"api_token": "synthetic-semaphore-token"}}}}
    assert json.loads(seen[0][2]) == {
        "role_id": "synthetic-role-id", "secret_id": "synthetic-secret-id"
    }
    assert seen[1][1]["X-vault-token"] == "synthetic-client-token"
    assert "secret_id" not in seen[0][1]


@pytest.mark.parametrize(("mode", "reason"), [
    ("redirect", "redirect_refused"),
    ("malformed", "malformed_response"),
    ("timeout", "timeout"),
])
def test_runtime_errors_are_fixed_and_do_not_return_credentials(monkeypatch, mode, reason):
    class Client:
        def open(self, *_args, **_kwargs):
            if mode == "redirect":
                raise HTTPError("http://127.0.0.1", 302, "secret location", {}, None)
            if mode == "malformed":
                return _Response(b"not-json")
            time.sleep(0.2)

    monkeypatch.setattr(RUNTIME, "build_opener", lambda *_args: Client())
    monkeypatch.setattr(RUNTIME, "REQUEST_TIMEOUT", 0.03)
    result = _action(RUNTIME, {"operation": "login"}, {
        "_runtime_bao_url": "http://127.0.0.1:8200",
        "bao_role_id": "synthetic-role-id", "bao_secret_id": "synthetic-secret-id",
    }, monkeypatch=monkeypatch)
    assert result == {"failed": True, "msg": reason}
    assert "synthetic" not in json.dumps(result)
    assert "secret location" not in json.dumps(result)


def test_runtime_action_reads_controller_environment_without_secret_task_args(monkeypatch):
    monkeypatch.setattr(RUNTIME, "build_opener", lambda *_args: SimpleNamespace(
        open=lambda *_args, **_kwargs: _Response(b'{"auth":{"client_token":"synthetic-client-token"}}')
    ))
    monkeypatch.setenv("OPENBAO_ADDR", "http://127.0.0.1:8200")
    monkeypatch.setenv("BAO_ROLE_ID", "environment-role-id")
    monkeypatch.setenv("BAO_SECRET_ID", "environment-secret-id")
    result = _action(RUNTIME, {"operation": "login"}, monkeypatch=monkeypatch)
    assert result["json"]["auth"]["client_token"] == "synthetic-client-token"


def test_defined_empty_role_value_does_not_switch_to_environment(monkeypatch):
    monkeypatch.setenv("OPENBAO_ADDR", "http://127.0.0.1:8200")
    monkeypatch.setenv("BAO_ROLE_ID", "environment-role-id")
    monkeypatch.setenv("BAO_SECRET_ID", "environment-secret-id")
    result = _action(RUNTIME, {"operation": "login"}, {"bao_role_id": ""}, monkeypatch=monkeypatch)
    assert result == {"failed": True, "msg": "credentials_unavailable"}


def test_runtime_access_include_preserves_transport_guard_fallback_and_rescue():
    tasks = yaml.safe_load((ROOT / "platform/semaphore/tasks/runtime-access.yml").read_text())
    names = [task["name"] for task in tasks]
    assert names[:3] == [
        "Require a non-cleartext transport to Semaphore",
        "Take an already injected Semaphore runtime token",
        "Check the controller OpenBao endpoint",
    ]
    injected = tasks[1]
    assert injected["ansible.builtin.set_fact"]["_token"] == "{{ lookup('env', 'SEMAPHORE_TOKEN') }}"
    assert injected["no_log"] is True
    assert tasks[2]["when"] == "_token | length == 0"
    block = tasks[3]["block"]
    assert [task["semaphore_runtime_access"]["operation"] for task in block[:2]] == ["login", "read"]
    assert all(task["no_log"] is True and task["check_mode"] is False for task in block[:2])
    assert not any("ansible.builtin.uri" in task for task in block)
    assert block[2]["ansible.builtin.set_fact"]["_token"] == (
        "{{ _runtime_secret.json.data.data.api_token | default('') }}"
    )
    assert tasks[3]["rescue"][0]["ansible.builtin.fail"]["msg"].startswith(
        "Runtime Semaphore access is unavailable"
    )
    conformance = (ROOT / "platform/playbooks/collect-service-conformance.yml").read_text()
    assert "when: _runtime_login.json is not defined" in conformance
    assert "(_runtime_login.json | default(_bao_auth.json)).auth.client_token" in conformance


def test_attribution_prepare_returns_nonce_only_in_result(monkeypatch):
    monkeypatch.setattr(ATTRIBUTION._HELPER.secrets, "token_bytes", lambda size: b"n" * size)
    args = {"mode": "prepare", "task_id": "41", "project_id": "7", "revision": "a" * 40}
    result = _action(ATTRIBUTION, args, monkeypatch=monkeypatch)
    assert result["status"] == "prepared"
    assert result["nonce_hex"] == (b"n" * 32).hex()
    assert "nonce_hex" not in args


def test_attribution_verify_reads_nonce_and_token_from_task_vars(monkeypatch):
    seen = {}

    def verify(data):
        seen.update(data)
        return {"status": "partial", "reason": "marker_observed", "marker_count": 1}

    monkeypatch.setattr(ATTRIBUTION._HELPER, "verify", verify)
    args = {"mode": "verify", "api_origin": "http://127.0.0.1:3000", "task_id": "41",
            "project_id": "7", "revision": "a" * 40, "marker": "safe-digest"}
    task_vars = {"_token": "synthetic-runtime-token",
                 "_attribution_prepared_raw": {"nonce_hex": (b"n" * 32).hex()}}
    result = _action(ATTRIBUTION, args, task_vars, monkeypatch=monkeypatch)
    assert result["status"] == "partial"
    assert seen["token"] == "synthetic-runtime-token"
    assert seen["nonce_hex"] == (b"n" * 32).hex()
    assert "nonce_hex" not in args and "token" not in args


def test_attribution_check_mode_stops_before_nonce_or_api(monkeypatch):
    monkeypatch.setattr(ATTRIBUTION._HELPER, "prepare", lambda _data: pytest.fail("nonce generated"))
    monkeypatch.setattr(ATTRIBUTION._HELPER, "verify", lambda _data: pytest.fail("API called"))
    result = _action(ATTRIBUTION, {"mode": "verify"}, check_mode=True, monkeypatch=monkeypatch)
    assert result["status"] == "unverified"
    assert result["reason"] == "check_mode_unverified"


@pytest.mark.skipif(not ANSIBLE_PLAYBOOK, reason="ansible-playbook not installed")
def test_actual_ansible_discovery_check_mode_skips_attribution_api(tmp_path):
    playbook = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                 "tasks": [
                     {"name": "Prepare attribution", "semaphore_task_attribution": {
                         "mode": "prepare", "task_id": "41", "project_id": "7", "revision": "a" * 40
                      }, "register": "_prepared", "no_log": True},
                     {"name": "Report check-mode result", "ansible.builtin.debug": {
                         "msg": "{{ _prepared.reason }}"}, "when": "ansible_check_mode"},
                 ]}]
    path = tmp_path / "attribution-plugin-discovery.yml"
    path.write_text(yaml.safe_dump(playbook), encoding="utf-8")
    env = os.environ.copy()
    env["ANSIBLE_LOCAL_TEMP"] = str(tmp_path / "local-tmp")
    env["ANSIBLE_HOME"] = str(tmp_path / "ansible-home")
    Path(env["ANSIBLE_LOCAL_TEMP"]).mkdir()
    Path(env["ANSIBLE_HOME"]).mkdir()
    proc = subprocess.run([ANSIBLE_PLAYBOOK, "-i", "localhost,", str(path), "--check"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "check_mode_unverified" in output


@pytest.mark.skipif(not ANSIBLE_PLAYBOOK, reason="ansible-playbook not installed")
def test_actual_ansible_runtime_plugin_discovery_and_callback_redaction(tmp_path):
    playbook = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
                 "vars": {"_runtime_bao_url": "http://example.com",
                          "bao_role_id": "callback-role-secret", "bao_secret_id": "callback-secret"},
                 "tasks": [
                     {"name": "Public cleartext endpoint remains refused", "semaphore_runtime_access": {
                         "operation": "login"}, "register": "_result", "no_log": True,
                      "failed_when": False},
                     {"name": "Assert safe refusal", "ansible.builtin.assert": {"that":
                         "_result.msg == 'insecure_endpoint'"}},
                     {"name": "Visible safe result", "ansible.builtin.debug": {"msg": "safe-marker-only"}}
                 ]}]
    path = tmp_path / "runtime-plugin-discovery.yml"
    path.write_text(yaml.safe_dump(playbook), encoding="utf-8")
    env = os.environ.copy()
    env["ANSIBLE_LOCAL_TEMP"] = str(tmp_path / "local-tmp")
    env["ANSIBLE_HOME"] = str(tmp_path / "ansible-home")
    Path(env["ANSIBLE_LOCAL_TEMP"]).mkdir()
    Path(env["ANSIBLE_HOME"]).mkdir()
    proc = subprocess.run([ANSIBLE_PLAYBOOK, "-i", "localhost,", str(path)], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=30)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "safe-marker-only" in output
    assert "callback-role-secret" not in output
    assert "callback-secret" not in output
