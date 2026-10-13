import fcntl
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import urllib.error
from pathlib import Path

import harness_sandbox
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("semaphore_rotate_token", ROOT / "scripts/semaphore-rotate-token.py")
rotate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rotate)

OLD = "oldtok01-secret"
NEW = "newtok01-secret"


class FakeSemaphore:
    def __init__(self, tokens=None, fail_create=False, fail_delete_after=False,
                 fail_delete_before=False):
        self.tokens = tokens or {OLD[:8]}
        self.fail_create = fail_create
        self.fail_delete_after = fail_delete_after
        self.fail_delete_before = fail_delete_before
        self.calls = []
        self.created = False
        self.deleted = False

    def client(self, url, project, token):
        server = self

        class Client:
            def __call__(self, path, body=None, project_scoped=True, method=None):
                server.calls.append((token, path, method))
                if path == "/info":
                    return {"version": "v2.19.11-02309ba-1774450250"}
                if path == "/user":
                    if token not in {OLD, NEW} or (token == OLD and server.deleted):
                        raise rotate.seed.Refusal("Semaphore HTTP 401 on /user; response body suppressed")
                    return {"id": 1}
                if path == "/user/tokens" and method == "POST":
                    if server.fail_create:
                        raise rotate.seed.Refusal("Semaphore request outcome unavailable on /user/tokens")
                    server.created = True
                    server.tokens.add(NEW[:8])
                    return {"id": NEW, "user_id": 1, "expired": False}
                if path == "/user/tokens":
                    if token not in {OLD, NEW} or (token == OLD and server.deleted):
                        raise rotate.seed.Refusal("Semaphore HTTP 401 on /user/tokens; response body suppressed")
                    return [{"id": item} for item in sorted(server.tokens)]
                if path == f"/user/tokens/{OLD[:8]}" and method == "DELETE":
                    if server.fail_delete_before:
                        server.fail_delete_before = False
                        raise rotate.seed.Refusal("Semaphore request outcome unavailable on delete")
                    server.tokens.remove(OLD[:8])
                    server.deleted = True
                    if server.fail_delete_after:
                        server.fail_delete_after = False
                        raise rotate.seed.Refusal("Semaphore request outcome unavailable on delete")
                    return None
                raise AssertionError((path, method))

        return Client()


def args(tmp_path):
    return rotate.argparse.Namespace(url="https://semaphore.test", project=1, inventory=2,
                                     expected_user_id=1,
                                     backup=tmp_path / "token", state=tmp_path / "rotation-state")


def test_parse_args_rejects_backup_path_reserved_for_rotation_lock(tmp_path):
    lock = tmp_path / ".semaphore-token-rotation.lock"
    with pytest.raises(SystemExit, match="2"):
        rotate.parse_args([
            "--url", "https://semaphore.test", "--project", "1",
            "--expected-user-id", "1", "--inventory", "2",
            "--backup", str(lock), "--state", str(tmp_path / "rotation-state"),
        ])


def install_fake(monkeypatch, server):
    monkeypatch.setattr(rotate.seed, "API", server.client)
    monkeypatch.setattr(rotate.seed, "declaration", lambda _name: {"name": rotate.ROTATION_TEMPLATE})
    monkeypatch.setattr(rotate.seed, "seed_target", lambda *a: object())
    monkeypatch.setattr(rotate.seed, "stage_and_seed", lambda api, project, target, values, **kwargs:
                        (assert_values(values)))


def assert_values(values):
    assert values == {rotate.OLD_INPUT: OLD, rotate.NEW_INPUT: NEW}


def test_token_id_wildcards_and_ambiguous_prefixes_fail_closed():
    with pytest.raises(rotate.RotationRefusal, match="wildcard"):
        rotate.token_ids(lambda *a, **k: [{"id": "abc%1234"}])
    with pytest.raises(rotate.RotationRefusal, match="ambiguous"):
        rotate.exact_id({"abc12345", "abc12345x"}, "abc12345-token")


def test_backend_version_must_match_pinned_delete_semantics():
    assert rotate.info(lambda *a, **k: {"version": "v2.19.11-02309ba-1774450250"}) == "2.19.11"
    with pytest.raises(rotate.RotationRefusal, match="backend version is unverified"):
        rotate.info(lambda *a, **k: {"version": "2.20.0"})


def test_authentication_uses_upstream_user_route_and_fake_contract(monkeypatch, tmp_path):
    server = FakeSemaphore()
    install_fake(monkeypatch, server)
    assert rotate.authenticate(rotate.api_for(args(tmp_path), OLD), 1) == {OLD[:8]}
    assert any(path == "/user" for _token, path, _method in server.calls)
    assert all(path != "/user/" for _token, path, _method in server.calls)


def test_shared_api_supports_delete_without_leaking_token_prefix():
    api = rotate.seed.API("https://semaphore.test", 1, "operator-secret")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b""

    def success(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    api.open = success
    api("/user/tokens/prefix99", project_scoped=False, method="DELETE")
    assert captured["request"].get_method() == "DELETE"
    assert captured["request"].full_url == "https://semaphore.test/api/user/tokens/prefix99"
    assert captured["request"].get_header("Authorization") == "Bearer operator-secret"

    def refused(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    api.open = refused
    with pytest.raises(rotate.seed.Refusal) as error:
        api("/user/tokens/prefix99", project_scoped=False, method="DELETE")
    assert "prefix99" not in str(error.value)
    assert "/user/tokens/{id}" in str(error.value)


def test_dev_template_and_playbook_guard_controller_write():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    declaration = next(item for item in templates if item.get("name") == rotate.ROTATION_TEMPLATE)
    assert declaration["dev_variant"] is True
    assert declaration["isolated_environment"] == "Semaphore token rotation inputs"
    assert declaration["seed_inputs"] == [rotate.OLD_INPUT, rotate.NEW_INPUT]

    play = yaml.safe_load((ROOT / "platform/playbooks/rotate-semaphore-api-token.yml").read_text())[-1]
    tasks = play["tasks"]
    read = next(task for task in tasks if task["name"] == "Read the controller-owned credential")
    compare = next(task for task in tasks if task["name"] == "Compare the protected values in memory")
    update_name = "Update OpenBao only when the protected value matches exactly"
    update = next(task for task in tasks if task["name"] == update_name)
    report = next(task for task in tasks if task["name"] == "Report the protected comparison result")
    assert read["no_log"] is True
    assert compare["no_log"] is True
    assert update["when"] == "_rot_matches | bool"
    assert update["vars"]["_bm_guard_key"] == "api_token"
    assert update["vars"]["_bm_guard_value"] == "{{ _rot_old }}"
    assert "differed; it was left unchanged" in report["ansible.builtin.debug"]["msg"]
    merge = yaml.safe_load((ROOT / "platform/playbooks/tasks/bao-merge-keys.yml").read_text())
    guarded_assert = next(task for task in merge if task["name"] == "Require the guarded key to be unchanged")
    assert guarded_assert["no_log"] is True
    address_guard = next(task for task in tasks
                         if task["name"] == "Refuse an OpenBao address that is not the inventory's")
    assert address_guard["vars"]["_bao_url"] == "{{ _rot_url }}"


@pytest.mark.parametrize("missing", [None, "endpoint", "old_encrypted_input", "new_encrypted_input"])
@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
def test_actual_playbook_input_preflight_reports_categories_without_values(tmp_path, missing):
    endpoint = None if missing == "endpoint" else "https://SENTINEL-ENDPOINT.test"
    inventory_data = {"hosts": {"localhost": {"ansible_connection": "local"}}}
    if endpoint:
        inventory_data["vars"] = {"openbao_addr": endpoint}
    inventory = tmp_path / "inventory.yml"
    inventory.write_text(yaml.safe_dump({"all": inventory_data}))

    env = harness_sandbox.env_for(tmp_path)
    env.update(
        SEMAPHORE_OLD_TOKEN="SENTINEL-OLD-ENCRYPTED-TOKEN",
        SEMAPHORE_NEW_TOKEN="SENTINEL-NEW-ENCRYPTED-TOKEN",
    )
    if missing == "old_encrypted_input":
        env["SEMAPHORE_OLD_TOKEN"] = ""
    elif missing == "new_encrypted_input":
        env["SEMAPHORE_NEW_TOKEN"] = ""

    source = yaml.safe_load((ROOT / "platform/playbooks/rotate-semaphore-api-token.yml").read_text())[-1]
    names = {
        "Check required rotation input presence without values",
        "Report bounded rotation input presence",
        "Refuse missing rotation inputs before OpenBao access",
        "List seed input names without values",
        "Refuse undeclared or leftover secret inputs",
    }
    tasks = [dict(task) for task in source["tasks"] if task["name"] in names]
    assert len(tasks) == len(names)
    auth_index = next(i for i, task in enumerate(source["tasks"])
                      if task["name"] == "Authenticate to OpenBao (AppRole)")
    refusal_index = next(i for i, task in enumerate(source["tasks"])
                         if task["name"] == "Refuse missing rotation inputs before OpenBao access")
    assert refusal_index < auth_index
    assert tasks[0]["no_log"] is True
    assert all(task["no_log"] is True for task in tasks
               if task["name"] in {"List seed input names without values",
                                   "Refuse undeclared or leftover secret inputs"})

    offline = tmp_path / "offline-rotation-input-preflight.yml"
    offline.write_text(yaml.safe_dump([{
        "name": "Run extracted rotation input preflight tasks offline",
        "hosts": "localhost",
        "gather_facts": False,
        "vars": source["vars"],
        "tasks": tasks,
    }]))
    result = subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(offline)],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False, timeout=120)
    if "Local RPC server did not start" in result.stderr:
        pytest.skip("installed Ansible local executor cannot start its RPC server in this environment")

    output = result.stdout + result.stderr
    for sentinel in ("SENTINEL-ENDPOINT", "SENTINEL-OLD-ENCRYPTED-TOKEN",
                     "SENTINEL-NEW-ENCRYPTED-TOKEN"):
        assert sentinel not in output
    assert "Rotation input presence:" in output
    if missing is None:
        assert result.returncode == 0, output
        assert "endpoint=present" in output
        assert "old encrypted input=present" in output
        assert "new encrypted input=present" in output
    else:
        assert result.returncode != 0, output
        assert f"{missing.replace('_', ' ')}=missing" in output
        assert "before OpenBao access" in output


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
def test_actual_playbook_executes_python_command_and_address_guard_offline(tmp_path):
    inventory = tmp_path / "inventory.yml"
    inventory.write_text(yaml.safe_dump({"all": {"vars": {"openbao_addr": "https://openbao.test"},
                                          "hosts": {"localhost": {"ansible_connection": "local"}}}}))
    env = harness_sandbox.env_for(tmp_path)
    env.update(SEMAPHORE_OLD_TOKEN="synthetic-old-token", SEMAPHORE_NEW_TOKEN="synthetic-new-token")
    source = yaml.safe_load((ROOT / "platform/playbooks/rotate-semaphore-api-token.yml").read_text())[-1]
    names = {"List seed input names without values", "Refuse undeclared or leftover secret inputs",
             "Refuse an OpenBao address that is not the inventory's"}
    tasks = [dict(task) for task in source["tasks"] if task["name"] in names]
    assert len(tasks) == len(names)
    address = next(task for task in tasks if task["name"] == "Refuse an OpenBao address that is not the inventory's")
    address["ansible.builtin.include_tasks"] = str(
        ROOT / "platform/playbooks/tasks/assert-bao-addr-declared.yml"
    )
    offline = tmp_path / "offline-rotation-check.yml"
    offline.write_text(yaml.safe_dump([{"name": "Run rotation preflight tasks offline", "hosts": "localhost",
                                       "gather_facts": False, "vars": source["vars"], "tasks": tasks}]))
    result = subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(offline)],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False, timeout=120)
    if "Local RPC server did not start" in result.stderr:
        pytest.skip("installed Ansible local executor cannot start its RPC server in this environment")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "List seed input names without values" in result.stdout
    assert "Refuse an OpenBao address that is not the inventory's" in result.stdout
    assert "synthetic-old-token" not in result.stdout + result.stderr
    assert "synthetic-new-token" not in result.stdout + result.stderr


def test_state_and_backup_are_atomic_private_files(tmp_path):
    path = tmp_path / "state"
    rotate.save_state(path, {"schema": 1, "phase": "test"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    backup = tmp_path / "token"
    rotate.replace_backup(backup, NEW)
    assert backup.read_text() == NEW + "\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_create_then_verify_openbao_and_backup_before_old_revocation(monkeypatch, tmp_path, capsys):
    server = FakeSemaphore()
    install_fake(monkeypatch, server)
    options = args(tmp_path)

    rotate.rotate(options, OLD)

    assert server.deleted
    assert server.tokens == {NEW[:8]}
    assert options.backup.read_text() == NEW + "\n"
    assert not options.state.exists()
    delete_at = next(i for i, call in enumerate(server.calls) if call[1].endswith(OLD[:8]) and call[2] == "DELETE")
    new_auth_at = max(i for i, call in enumerate(server.calls[:delete_at]) if call[0] == NEW and call[1] == "/info")
    assert new_auth_at < delete_at
    out = capsys.readouterr()
    assert OLD not in out.out + out.err
    assert NEW not in out.out + out.err
    assert OLD[:8] not in out.out + out.err
    assert NEW[:8] not in out.out + out.err


def test_unknown_create_is_durable_and_never_retried(monkeypatch, tmp_path):
    server = FakeSemaphore(fail_create=True)
    install_fake(monkeypatch, server)
    options = args(tmp_path)

    with pytest.raises(rotate.RotationRefusal, match="Create outcome is unknown"):
        rotate.rotate(options, OLD)
    assert rotate.load_state(options.state)["phase"] == "create_started"
    calls_before = len(server.calls)
    with pytest.raises(rotate.RotationRefusal, match="Create outcome is unknown"):
        rotate.rotate(options, OLD)
    assert len([call for call in server.calls[calls_before:] if call[2] == "POST"]) == 0


@pytest.mark.parametrize("seed_error", [
    "SENTINEL-SEED-REFUSAL",
    "Seed task 5013 failed; SENTINEL-SEED-FAILURE",
])
def test_seed_refusal_or_task_failure_on_verified_resume_is_safe_and_never_creates_another_token(
        monkeypatch, tmp_path, capsys, seed_error):
    server = FakeSemaphore(tokens={OLD[:8], NEW[:8]})
    install_fake(monkeypatch, server)
    options = args(tmp_path)
    options.backup.write_text(OLD + "\n")
    options.backup.chmod(0o600)
    rotate.save_state(options.state, {
        "schema": 1,
        "phase": "verified",
        "old_id": OLD[:8],
        "old_token": OLD,
        "before_ids": [OLD[:8]],
        "new_id": NEW[:8],
        "new_token": NEW,
    })

    def refuse_seed(*_args, **_kwargs):
        raise rotate.seed.Refusal(seed_error)

    monkeypatch.setattr(rotate.seed, "stage_and_seed", refuse_seed)
    monkeypatch.setattr("sys.stdin", io.StringIO(OLD + "\n"))
    status = rotate.main([
        "--url", "https://semaphore.test", "--project", "1",
        "--expected-user-id", "1", "--inventory", "2",
        "--backup", str(options.backup), "--state", str(options.state),
    ])

    output = capsys.readouterr()
    assert status == 2
    assert (
        "Semaphore seed reconciliation refused or failed; "
        "preserve private state and reconcile before retrying"
    ) in output.err
    assert seed_error not in output.out + output.err
    assert "stopped unexpectedly" not in output.err
    assert not any(method == "POST" for _token, _path, method in server.calls)
    assert server.created is False
    assert options.backup.read_text() == OLD + "\n"
    assert stat.S_IMODE(options.backup.stat().st_mode) == 0o600
    assert rotate.load_state(options.state)["phase"] == "verified"


def test_local_lock_refuses_a_concurrent_rotation_before_api_calls(monkeypatch, tmp_path):
    server = FakeSemaphore()
    install_fake(monkeypatch, server)
    options = args(tmp_path)
    lock = tmp_path / ".semaphore-token-rotation.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(rotate.RotationRefusal, match="Another local token rotation"):
            rotate.rotate(options, OLD)
    finally:
        os.close(fd)
    assert not server.calls


def test_replacement_prefix_collision_preserves_recovery_state(monkeypatch, tmp_path):
    server = FakeSemaphore(tokens={OLD[:8], NEW[:8]})
    install_fake(monkeypatch, server)
    options = args(tmp_path)

    with pytest.raises(rotate.RotationRefusal, match="collides"):
        rotate.rotate(options, OLD)
    state = rotate.load_state(options.state)
    assert state["new_token"] == NEW
    assert not server.deleted


def test_unknown_delete_resumes_from_new_token_without_recreating_or_reseeding(monkeypatch, tmp_path):
    server = FakeSemaphore(fail_delete_after=True)
    install_fake(monkeypatch, server)
    options = args(tmp_path)

    with pytest.raises(rotate.RotationRefusal, match="Revocation outcome is unknown"):
        rotate.rotate(options, OLD)
    assert rotate.load_state(options.state)["phase"] == "revoke_unknown"
    delete_calls = len([call for call in server.calls if call[2] == "DELETE"])
    rotate.rotate(options, OLD)

    assert len([call for call in server.calls if call[2] == "DELETE"]) == delete_calls
    assert server.tokens == {NEW[:8]}
    assert not options.state.exists()


def test_resume_after_backup_replace_recovers_old_token_from_private_state(monkeypatch, tmp_path):
    server = FakeSemaphore(fail_delete_before=True)
    install_fake(monkeypatch, server)
    options = args(tmp_path)

    with pytest.raises(rotate.RotationRefusal, match="Revocation outcome is unknown"):
        rotate.rotate(options, OLD)
    assert options.backup.read_text() == NEW + "\n"
    state = rotate.load_state(options.state)
    assert state["old_token"] == OLD

    recovered = rotate.resume_input_token(options, NEW)
    assert recovered == OLD
    rotate.rotate(options, recovered)
    assert server.tokens == {NEW[:8]}
    assert not options.state.exists()


@pytest.mark.parametrize("backup_state", ["missing", "wrong-content", "wrong-mode"])
def test_resume_restores_private_replacement_backup_before_revocation(monkeypatch, tmp_path, backup_state):
    server = FakeSemaphore(fail_delete_before=True)
    install_fake(monkeypatch, server)
    options = args(tmp_path)

    with pytest.raises(rotate.RotationRefusal, match="Revocation outcome is unknown"):
        rotate.rotate(options, OLD)
    if backup_state == "missing":
        options.backup.unlink()
    elif backup_state == "wrong-content":
        options.backup.write_text("different\n")
        options.backup.chmod(0o600)
    else:
        options.backup.chmod(0o644)

    delete_old = rotate.delete_old

    def assert_backup_before_revocation(*args, **kwargs):
        assert options.backup.read_text() == NEW + "\n"
        assert stat.S_IMODE(options.backup.stat().st_mode) == 0o600
        delete_old(*args, **kwargs)

    monkeypatch.setattr(rotate, "delete_old", assert_backup_before_revocation)

    rotate.rotate(options, OLD)

    assert options.backup.read_text() == NEW + "\n"
    assert stat.S_IMODE(options.backup.stat().st_mode) == 0o600
    assert not options.state.exists()


def test_backup_is_rechecked_before_state_deletion(monkeypatch, tmp_path):
    server = FakeSemaphore()
    install_fake(monkeypatch, server)
    options = args(tmp_path)
    delete_old = rotate.delete_old

    def corrupt_after_revocation(*args, **kwargs):
        delete_old(*args, **kwargs)
        options.backup.write_text("corrupted\n")
        options.backup.chmod(0o600)

    monkeypatch.setattr(rotate, "delete_old", corrupt_after_revocation)
    rotate.rotate(options, OLD)
    assert options.backup.read_text() == NEW + "\n"
    assert stat.S_IMODE(options.backup.stat().st_mode) == 0o600
    assert not options.state.exists()
