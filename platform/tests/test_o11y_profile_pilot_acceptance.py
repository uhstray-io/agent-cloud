import importlib.util
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = ROOT / "platform/playbooks/files/o11y-profile-pilot-operation.py"
BOUNDING_HELPER_PATH = ROOT / "platform/playbooks/files/validate-o11y-profile-acceptance-binding.py"
PLAYBOOK_PATH = ROOT / "platform/playbooks/accept-o11y-profile-pilot.yml"
TEMPLATE_PATH = ROOT / "platform/semaphore/templates.yml"
COMPOSE_PATH = ROOT / "platform/services/o11y/deployment/compose.yml"

SPEC = importlib.util.spec_from_file_location("o11y_profile_pilot_operation", HELPER_PATH)
operation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operation)
BINDING_SPEC = importlib.util.spec_from_file_location("o11y_profile_acceptance_binding", BOUNDING_HELPER_PATH)
binding = importlib.util.module_from_spec(BINDING_SPEC)
BINDING_SPEC.loader.exec_module(binding)


def _container_rows(config_path, *, writable=False):
    rows = []
    for index, name in enumerate(sorted(operation.REQUIRED_CONTAINERS)):
        mounts = []
        if name == "o11y-alloy":
            mounts = [{
                "Type": "bind", "Source": str(config_path),
                "Destination": "/etc/alloy/config.alloy", "RW": writable,
            }]
        elif name == "o11y-pyroscope":
            mounts = [{
                "Type": "volume", "Source": "", "Destination": "/data",
                "RW": True, "Name": "o11y_pyroscope-data",
            }]
        rows.append({
            "Id": f"container-{index}", "Name": name, "Image": f"image-{index}",
            "State": {
                "Status": "running",
                "Health": {"Status": "healthy"} if name in operation.HEALTHY_CONTAINERS else None,
            },
            "Mounts": mounts,
        })
    return rows


def test_config_write_preserves_the_bind_mounted_inode(tmp_path):
    config = tmp_path / "config.alloy"
    config.write_bytes(b"disabled config")
    original = config.stat()

    operation._write_config_in_place(
        str(config), b"enabled config is longer", (original.st_dev, original.st_ino)
    )

    current = config.stat()
    assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
    assert config.read_bytes() == b"enabled config is longer"


def test_config_write_refuses_symlinks_and_hardlinks(tmp_path):
    config = tmp_path / "config.alloy"
    config.write_bytes(b"disabled")
    alias = tmp_path / "alias.alloy"
    alias.symlink_to(config)

    with pytest.raises(operation.Refusal, match="config_write_failed"):
        operation._write_config_in_place(str(alias), b"enabled", (config.stat().st_dev, config.stat().st_ino))

    alias.unlink()
    alias.hardlink_to(config)
    current = config.stat()
    with pytest.raises(operation.Refusal, match="config_write_failed"):
        operation._write_config_in_place(str(config), b"enabled", (current.st_dev, current.st_ino))


def test_live_mount_must_be_the_exact_read_only_config_file(tmp_path):
    config = tmp_path / "config" / "config.alloy"
    config.parent.mkdir()
    config.write_text("disabled")
    names = "\n".join(sorted(operation.REQUIRED_CONTAINERS)).encode()

    def runner(argv, **_kwargs):
        if argv[:3] == ["podman", "ps", "--all"]:
            return subprocess.CompletedProcess(argv, 0, names, b"")
        if argv[:3] == ["podman", "container", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(_container_rows(config)).encode(), b"")
        raise AssertionError(argv)

    operation._container_snapshot(str(config), runner)

    def writable_runner(argv, **_kwargs):
        if argv[:3] == ["podman", "ps", "--all"]:
            return subprocess.CompletedProcess(argv, 0, names, b"")
        if argv[:3] == ["podman", "container", "inspect"]:
            output = json.dumps(_container_rows(config, writable=True)).encode()
            return subprocess.CompletedProcess(argv, 0, output, b"")
        raise AssertionError(argv)

    with pytest.raises(operation.Refusal, match="mount_mismatch"):
        operation._container_snapshot(str(config), writable_runner)


def _mocked_operation(
    monkeypatch, tmp_path, *, component_failure=False, changed_container=False, changed_volume=False
):
    config = tmp_path / "deployment" / "config" / "config.alloy"
    config.parent.mkdir(parents=True)
    disabled, enabled = "disabled profile config", "enabled profile config"
    config.write_text(disabled)
    inode = config.stat()
    monkeypatch.setattr(operation, "_candidate_module", lambda _path: object())
    snapshots = iter(("before", "after" if changed_container else "before"))
    monkeypatch.setattr(
        operation, "_container_snapshot",
        lambda *_args: (next(snapshots), {"pyroscope_volume": "o11y_pyroscope-data"}),
    )
    volumes = iter((
        ("before", {"o11y_pyroscope-data"}),
        ("after", {"o11y_pyroscope-data"}) if changed_volume else ("before", {"o11y_pyroscope-data"}),
    ))
    monkeypatch.setattr(operation, "_volume_snapshot", lambda *_args: next(volumes))
    reloads = []
    monkeypatch.setattr(operation, "_reload", lambda _runner: reloads.append(config.read_text()))

    def component_state(_candidate, expected, _runner):
        if component_failure and expected == "healthy":
            raise operation.Refusal("profile_component_unhealthy")

    monkeypatch.setattr(operation, "_component_state", component_state)
    monkeypatch.setattr(operation, "_wait_for_profile", lambda *_args, **_kwargs: True)
    payload = {
        "config_path": str(config), "receiver_sha": "a" * 40,
        "disabled": disabled, "enabled": enabled, "candidate_helper": str(HELPER_PATH),
    }
    return payload, config, inode, reloads


def test_sigterm_during_rollback_uses_the_registered_handler_and_finishes_restore(monkeypatch, tmp_path):
    payload, config, _inode, _reloads = _mocked_operation(monkeypatch, tmp_path)
    write_config = operation._write_config_in_place
    calls = 0

    def signal_during_restore(path, content, inode):
        nonlocal calls
        calls += 1
        if calls == 2:
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
        write_config(path, content, inode)

    monkeypatch.setattr(operation, "_write_config_in_place", signal_during_restore)

    result = operation._evaluate_with_signal_handlers(payload)

    assert result["status"] == "unavailable"
    assert result["reason"] == "operation_interrupted"
    assert result["restored"] == "disabled"
    assert config.read_text() == "disabled profile config"


def test_bounded_command_refuses_oversized_output_and_reaps_child(monkeypatch):
    real_popen = operation.subprocess.Popen
    processes = []

    def capture_process(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(operation.subprocess, "Popen", capture_process)

    with pytest.raises(operation.Refusal, match="receiver_unavailable"):
        operation._run([
            sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 2097152)",
        ])

    assert len(processes) == 1
    assert processes[0].returncode is not None


def test_failed_profile_proof_restores_disabled_config(monkeypatch, tmp_path):
    payload, config, inode, reloads = _mocked_operation(monkeypatch, tmp_path, component_failure=True)

    result = operation.evaluate(payload, runner=lambda *_args, **_kwargs: None, sleep_fn=lambda _seconds: None)

    assert result["status"] == "unavailable"
    assert result["reason"] == "profile_component_unhealthy"
    assert result["restored"] == "disabled"
    assert config.read_text() == "disabled profile config"
    assert (config.stat().st_dev, config.stat().st_ino) == (inode.st_dev, inode.st_ino)
    assert reloads == ["enabled profile config", "disabled profile config"]


def test_known_enabled_baseline_is_restored_but_refused(monkeypatch, tmp_path):
    payload, config, _inode, reloads = _mocked_operation(monkeypatch, tmp_path)
    config.write_text("enabled profile config")

    result = operation.evaluate(payload, runner=lambda *_args, **_kwargs: None, sleep_fn=lambda _seconds: None)

    assert result["status"] == "unavailable"
    assert result["reason"] == "baseline_ambiguous"
    assert result["restored"] == "disabled"
    assert config.read_text() == "disabled profile config"
    assert reloads == ["disabled profile config"]


def test_complete_acceptance_restores_and_emits_only_a_bounded_receipt(monkeypatch, tmp_path):
    payload, config, _inode, reloads = _mocked_operation(monkeypatch, tmp_path)

    result = operation.evaluate(payload, runner=lambda *_args, **_kwargs: None, sleep_fn=lambda _seconds: None)
    encoded = json.dumps(result)

    assert result["status"] == "verified"
    assert result["reason"] == "sampled_delivered_restored_post_disable_query_observed"
    assert result["service"] == "o11y/alloy"
    assert result["post_disable_query"] == "observed"
    assert config.read_text() == "disabled profile config"
    assert reloads == ["enabled profile config", "disabled profile config"]
    assert "enabled profile config" not in encoded


def test_profile_query_rejects_stale_timeline_and_accepts_positive_post_activation_sample(monkeypatch):
    activation_start = 1_760_000_000
    scripts = []
    responses = iter((
        {"timeline": {"startTime": activation_start - 20, "samples": [4, 7], "durationDelta": 10}},
        {"timeline": {"startTime": activation_start - 10, "samples": [0, 7], "durationDelta": 10}},
    ))

    def api_body(script, _runner):
        scripts.append(script)
        body = json.dumps(next(responses)).encode()
        return b"HTTP/1.1 200 OK", body

    monkeypatch.setattr(operation, "_api_body", api_body)

    assert not operation._query_profile(activation_start, runner=lambda *_args: None)
    assert operation._query_profile(activation_start, runner=lambda *_args: None)
    assert len(scripts) == 2
    assert all(f"&from={activation_start}&until=now" in script for script in scripts)
    assert all("service_name%3D%22o11y%2Falloy%22" in script for script in scripts)


def test_changed_receiver_identity_or_mounts_fail_the_preservation_gate(monkeypatch, tmp_path):
    payload, config, _inode, _reloads = _mocked_operation(monkeypatch, tmp_path, changed_container=True)

    result = operation.evaluate(payload, runner=lambda *_args, **_kwargs: None, sleep_fn=lambda _seconds: None)

    assert result["status"] == "unavailable"
    assert result["reason"] == "container_state_changed"
    assert result["restored"] == "disabled"
    assert config.read_text() == "disabled profile config"


def test_changed_named_volume_state_fails_after_restoration(monkeypatch, tmp_path):
    payload, config, _inode, _reloads = _mocked_operation(monkeypatch, tmp_path, changed_volume=True)

    result = operation.evaluate(payload, runner=lambda *_args, **_kwargs: None, sleep_fn=lambda _seconds: None)

    assert result["status"] == "unavailable"
    assert result["reason"] == "volume_state_changed"
    assert result["restored"] == "disabled"
    assert config.read_text() == "disabled profile config"


def test_container_drift_reason_precedes_simultaneous_volume_drift(monkeypatch, tmp_path):
    payload, config, _inode, _reloads = _mocked_operation(
        monkeypatch, tmp_path, changed_container=True, changed_volume=True
    )

    result = operation.evaluate(payload, runner=lambda *_args, **_kwargs: None, sleep_fn=lambda _seconds: None)

    assert result["reason"] == "container_state_changed"
    assert config.read_text() == "disabled profile config"


def test_rollback_failure_reason_survives_later_snapshot_exceptions(monkeypatch, tmp_path):
    payload, config, _inode, _reloads = _mocked_operation(monkeypatch, tmp_path)
    original_write = operation._write_config_in_place
    writes = 0

    def fail_restore(path, content, inode):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise operation.Refusal("config_write_failed")
        original_write(path, content, inode)

    snapshots = 0

    def fail_snapshot(*_args):
        nonlocal snapshots
        snapshots += 1
        if snapshots == 1:
            return "before", {"pyroscope_volume": "o11y_pyroscope-data"}
        raise operation.Refusal("receiver_unavailable")

    volumes = 0

    def fail_volume_snapshot(*_args):
        nonlocal volumes
        volumes += 1
        if volumes == 1:
            return "before", {"o11y_pyroscope-data"}
        raise operation.Refusal("receiver_unavailable")

    monkeypatch.setattr(operation, "_write_config_in_place", fail_restore)
    monkeypatch.setattr(operation, "_container_snapshot", fail_snapshot)
    monkeypatch.setattr(operation, "_volume_snapshot", fail_volume_snapshot)

    result = operation.evaluate(payload, runner=lambda *_args, **_kwargs: None, sleep_fn=lambda _seconds: None)

    assert result["reason"] == "rollback_failed"
    assert config.read_text() == "enabled profile config"


def _binding_payload():
    return {
        "project_id": 7,
        "repository_id": 12,
        "inventories": [{
            "id": 31, "project_id": 7, "name": "production", "type": "static-yaml",
        }],
        "templates": [{
            "id": 44, "name": binding.EXPECTED_TEMPLATE_NAME,
            "project_id": 7, "repository_id": 12, "inventory_id": 31,
            "playbook": binding.EXPECTED_PLAYBOOK,
        }],
        "repositories": [{
            "id": 12, "project_id": 7, "name": binding.EXPECTED_REPOSITORY_NAME,
            "git_url": binding.EXPECTED_REPOSITORY_URL, "git_branch": binding.EXPECTED_BRANCH,
        }],
    }


def test_acceptance_template_binding_matches_the_exact_provenance_inventory():
    report = binding.evaluate(_binding_payload())

    assert report == {
        "schema_version": 1, "status": "verified",
        "reason": "unique_acceptance_template_bound_to_production_inventory",
    }


def test_acceptance_template_inventory_mismatch_and_duplicate_are_refused():
    mismatch = _binding_payload()
    mismatch["templates"][0]["inventory_id"] = 99
    malformed_id = _binding_payload()
    malformed_id["templates"][0]["repository_id"] = True
    duplicate = _binding_payload()
    duplicate["templates"].append(dict(duplicate["templates"][0]))
    wrong_production = _binding_payload()
    wrong_production["inventories"][0]["id"] = 99

    assert binding.evaluate(mismatch)["reason"] == "acceptance_template_mismatch"
    assert binding.evaluate(malformed_id)["reason"] == "acceptance_template_mismatch"
    assert binding.evaluate(duplicate)["reason"] == "metadata_ambiguous"
    assert binding.evaluate(wrong_production)["reason"] == "acceptance_template_mismatch"


def test_acceptance_is_dev_bound_and_pins_existing_disabled_deploy_guards():
    playbook = yaml.safe_load(PLAYBOOK_PATH.read_text())
    templates = yaml.safe_load(TEMPLATE_PATH.read_text())["templates"]
    acceptance = next(
        row for row in templates
        if row.get("playbook") == "platform/playbooks/accept-o11y-profile-pilot.yml"
    )
    alloy = next(
        row for row in yaml.safe_load(COMPOSE_PATH.read_text())["services"].values()
        if row.get("container_name") == "o11y-alloy"
    )

    assert acceptance["repository"] == "agent-cloud dev"
    assert acceptance["name"].endswith("(Dev)")
    for included in (
        "platform/playbooks/survey-o11y-inventory-provenance.yml",
        "platform/playbooks/verify-o11y-profile-pilot.yml",
        "platform/playbooks/verify-o11y-production-budgets.yml",
    ):
        imported = next(
            row for row in playbook
            if row.get("ansible.builtin.import_playbook") == Path(included).name
        )
        assert imported["vars"]["_extra_var_guard_nested"] is True
    assert "verify-o11y-profile-pilot.yml" in str(playbook)
    assert "survey-o11y-inventory-provenance.yml" in str(playbook)
    assert "verify-o11y-production-budgets.yml" in str(playbook)
    assert "validate-o11y-profile-acceptance-binding.py" in str(playbook)
    assert any("o11y-profile-pilot-operation.py" in str(task) for task in playbook)
    assert "POST /-/reload" in operation.RELOAD_SCRIPT
    assert "service_name%3D%22o11y%2Falloy%22" in operation.PROFILE_QUERY_PREFIX
    assert (
        "./config/config.alloy:/etc/alloy/config.alloy:ro"
        in alloy["volumes"]
    )
