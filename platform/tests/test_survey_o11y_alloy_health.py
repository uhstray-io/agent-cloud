"""Sanitization and exact-scope contract for the Alloy health survey."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/survey-o11y-alloy-health.py"
PLAYBOOK = ROOT / "platform/playbooks/survey-o11y-alloy-health.yml"
CHECKOUT = ROOT / "platform/playbooks/require-reviewed-checkout.yml"
SPEC = importlib.util.spec_from_file_location("survey_o11y_alloy_health", HELPER)
SURVEY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SURVEY)
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")


def inspect_data(**changes):
    state = {
        "Status": "running",
        "Health": {"Status": "starting", "Log": [{"ExitCode": 7, "Output": "sensitive probe output"}]},
        "RestartCount": 4,
        "OOMKilled": False,
        "StartedAt": None,
        "Error": "private runtime error",
        "Pid": 98765,
    }
    container = {
        "Name": "o11y-alloy",
        "Id": "private-container-id",
        "Config": {"Env": ["TOKEN=secret-token"], "Cmd": ["private command"]},
        "Mounts": [{"Source": "/private/path"}],
        "State": state,
        "RestartCount": 4,
    }
    container.update(changes)
    return [container]


def test_projects_only_fixed_allowlist_from_inspect_data():
    result = SURVEY._project(inspect_data())
    assert set(result) == {
        "status",
        "container_present",
        "run_status",
        "health_status",
        "restart_count",
        "oom_killed",
        "last_health_exit_code",
        "started_age_bucket",
    }
    assert result["status"] == "observed"
    assert result["container_present"] is True
    assert result["health_status"] == "starting"
    assert result["last_health_exit_code"] == 7
    rendered = json.dumps(result)
    planted_fields = (
        "private-container-id",
        "secret-token",
        "private command",
        "/private/path",
        "sensitive probe output",
        "private runtime error",
    )
    for planted in planted_fields:
        assert planted not in rendered


def test_started_age_is_coarse_and_refuses_future_timestamp():
    now = datetime(2026, 10, 13, 12, tzinfo=UTC)
    assert SURVEY._age_bucket("2026-10-13T11:30:00Z", now) == "under_1h"
    assert SURVEY._age_bucket("2026-10-12T13:00:00Z", now) == "1_to_24h"
    assert SURVEY._age_bucket("2026-10-01T12:00:00Z", now) == "7d_or_more"
    try:
        SURVEY._age_bucket("2026-10-13T13:00:00Z", now)
    except SURVEY.Refusal as exc:
        assert str(exc) == "inspect_malformed"
    else:
        raise AssertionError("future timestamp must be refused")


def test_exact_zero_started_at_variants_are_not_started():
    for value in (
        "0001-01-01T00:00:00Z",
        "0001-01-01T00:00:00.0Z",
        "0001-01-01T00:00:00.000000000Z",
        "0001-01-01T00:00:00.000000000+00:00",
    ):
        assert SURVEY._age_bucket(value) is None


def test_empty_health_status_and_null_log_are_sanitized():
    data = inspect_data()
    data[0]["State"]["Health"] = {
        "Status": "",
        "Log": None,
        "Output": "sensitive health output",
    }

    report = SURVEY._project(data)

    assert report["health_status"] == "none"
    assert report["last_health_exit_code"] is None
    assert "sensitive health output" not in json.dumps(report)


def test_health_log_output_is_not_disclosed():
    data = inspect_data()
    data[0]["State"]["Health"] = {
        "Status": "starting",
        "Log": [{"ExitCode": 0, "Output": "sensitive probe output"}],
    }

    report = SURVEY._project(data)

    assert report["health_status"] == "starting"
    assert report["last_health_exit_code"] == 0
    assert "sensitive probe output" not in json.dumps(report)


def test_missing_container_is_fixed_category_and_hides_stderr(monkeypatch):
    def run(argv, **_kwargs):
        assert argv == ["podman", "container", "exists", "o11y-alloy"]
        return SimpleNamespace(returncode=1, stdout="", stderr="secret /private/path")

    monkeypatch.setattr(SURVEY.subprocess, "run", run)
    try:
        SURVEY.inspect()
    except SURVEY.Refusal as exc:
        assert str(exc) == "container_missing"
    else:
        raise AssertionError("missing container must be refused")


def test_ambiguous_and_malformed_inspect_shapes_fail_closed():
    for malformed in (
        [],
        inspect_data() * 2,
        [{**inspect_data()[0], "Name": "o11y-loki"}],
        [{**inspect_data()[0], "State": {**inspect_data()[0]["State"], "OOMKilled": "false"}}],
        [{**inspect_data()[0], "RestartCount": True}],
        [
            {
                **inspect_data()[0],
                "State": {
                    **inspect_data()[0]["State"],
                    "Health": {"Status": "starting", "Log": [{"ExitCode": True}]},
                },
            }
        ],
        [
            {
                **inspect_data()[0],
                "State": {
                    **inspect_data()[0]["State"],
                    "Health": {"Status": "unrecognized-health-state", "Log": None},
                },
            }
        ],
        [
            {
                **inspect_data()[0],
                "State": {
                    **inspect_data()[0]["State"],
                    "Health": {"Status": "starting", "Log": [None]},
                },
            }
        ],
    ):
        try:
            SURVEY._project(malformed)
        except SURVEY.Refusal as exc:
            assert str(exc) in {"inspect_ambiguous", "inspect_malformed"}
        else:
            raise AssertionError("malformed or ambiguous inspect data must be refused")


def test_inspect_runner_never_returns_raw_error_or_runtime_document(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[1:3] == ["container", "exists"]:
            assert _kwargs["stdout"] == subprocess.DEVNULL
            assert _kwargs["stderr"] == subprocess.DEVNULL
            return SimpleNamespace(returncode=0, stdout="", stderr="private stderr")
        raise AssertionError("inspect must use the bounded Popen reader")

    monkeypatch.setattr(SURVEY.subprocess, "run", run)
    monkeypatch.setattr(
        SURVEY,
        "_read_bounded_stdout",
        lambda argv, **_kwargs: json.dumps(inspect_data()),
    )
    report = SURVEY.inspect()
    assert calls == [
        ["podman", "container", "exists", "o11y-alloy"],
    ]
    assert "private" not in json.dumps(report)
    assert "secret-token" not in json.dumps(report)


def test_inspect_reader_caps_stream_and_kills_and_reaps_overflow_child(monkeypatch):
    children = []
    original_popen = SURVEY.subprocess.Popen

    def track_child(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append((child, kwargs))
        return child

    monkeypatch.setattr(SURVEY.subprocess, "Popen", track_child)
    with pytest.raises(SURVEY.Refusal, match="^inspect_malformed$"):
        SURVEY._read_bounded_stdout(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('PRIVATE_SENTINEL'); sys.stdout.write('x' * 1000000)",
            ],
            timeout=5,
            max_bytes=1024,
        )
    assert len(children) == 1
    child, options = children[0]
    assert child.poll() is not None
    assert options["stdout"] is SURVEY.subprocess.PIPE
    assert options["stderr"] is SURVEY.subprocess.DEVNULL
    assert child.stderr is None


def test_inspect_reader_kills_and_reaps_child_on_timeout(monkeypatch):
    children = []
    original_popen = SURVEY.subprocess.Popen

    def track_child(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(SURVEY.subprocess, "Popen", track_child)
    with pytest.raises(SURVEY.Refusal, match="^inspect_failed$"):
        SURVEY._read_bounded_stdout(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.05
        )
    assert len(children) == 1
    assert children[0].poll() is not None


def test_playbook_is_dev_pinned_single_host_and_read_only():
    playbook = yaml.safe_load(PLAYBOOK.read_text())
    assert playbook[0]["hosts"] == "localhost"
    assert playbook[0]["connection"] == "local"
    assert playbook[0]["tasks"][0]["ansible.builtin.assert"]
    assert playbook[1]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert playbook[2]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert any(
        play.get("ansible.builtin.import_playbook") == "require-reviewed-checkout.yml"
        and play.get("vars", {}).get("_reviewed_checkout_payload_paths")
        == ["platform/playbooks/files/survey-o11y-alloy-health.py"]
        for play in playbook
    )
    early_guard = playbook[0]["tasks"][0]["ansible.builtin.assert"]["that"]
    assert "_survey_forged_names | length == 0" in early_guard
    assert "_survey_receivers | length == 1" in early_guard
    assert "_survey_receiver_valid | bool" in early_guard
    assert "expected_repository_sha | default('') is match('^[0-9a-f]{40}$')" in CHECKOUT.read_text()
    source = PLAYBOOK.read_text()
    assert "_survey_override_names" in source
    assert "extra_var_names" in source
    assert "podman_rootful" in source
    assert "podman" in source
    assert "ansible.builtin.command" in source
    forbidden = ("podman start", "podman stop", "podman rm", "podman restart", "podman recreate")
    assert all(word not in source for word in forbidden)
    assert "no_log: true" in source
    assert "require-tmpfs-remote-tmp.yml" in source
    receiver_play = next(play for play in playbook if play.get("hosts") == "o11y_svc")
    assert receiver_play["vars"]["ansible_become"] is False
    assert "rescue" in next(
        task for task in receiver_play["tasks"] if task.get("block") is not None
    )


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="needs ansible-playbook")
@pytest.mark.parametrize(
    "extra_vars",
    [
        {"ansible_python_interpreter": "/nonexistent"},
        {"ansible_become": True},
        {"become": True},
    ],
    ids=["poisoned-python", "ansible-become", "become"],
)
def test_full_playbook_refuses_context_overrides_before_imported_playbooks(tmp_path, extra_vars):
    proc = subprocess.run(
        [
            ANSIBLE_PLAYBOOK,
            "-i",
            "localhost,",
            str(PLAYBOOK),
            "--extra-vars",
            json.dumps(extra_vars),
        ],
        cwd=ROOT,
        env=_ansible_env(tmp_path / "local-tmp"),
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "Refusing the Alloy survey because its context or receiver scope is unsafe." in output
    assert "Require the reviewed source and helper bytes" not in output
    assert "/nonexistent" not in output


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="needs ansible-playbook")
def test_full_playbook_refuses_receiver_local_mode_before_checkout(tmp_path):
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "[o11y_svc]\n"
        "localhost ansible_connection=local local_mode=true container_engine=podman podman_rootful=false\n"
    )
    proc = subprocess.run(
        [ANSIBLE_PLAYBOOK, "-i", str(inventory), str(PLAYBOOK)],
        cwd=ROOT,
        env=_ansible_env(tmp_path / "local-tmp"),
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "Refusing the Alloy survey because its context or receiver scope is unsafe." in output
    assert "Require the reviewed source and helper bytes" not in output


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="needs ansible-playbook")
def test_remote_play_disables_inventory_become_override(tmp_path):
    receiver_play = next(
        play for play in yaml.safe_load(PLAYBOOK.read_text()) if play.get("hosts") == "o11y_svc"
    )
    harness = [
        {
            "name": "Test receiver privilege policy offline",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "become": receiver_play["become"],
            "vars": {
                "ansible_become": receiver_play["vars"]["ansible_become"],
                "expected_uid": os.getuid(),
            },
            "tasks": [
                {
                    "name": "Read the effective process uid",
                    "ansible.builtin.command": {"argv": ["id", "-u"]},
                    "changed_when": False,
                    "register": "_effective_uid",
                },
                {
                    "name": "Require the inventory become override to be disabled",
                    "ansible.builtin.assert": {
                        "that": "_effective_uid.stdout | int == expected_uid",
                        "quiet": True,
                    },
                }
            ],
        }
    ]
    playbook_path = tmp_path / "privilege-check.yml"
    inventory_path = tmp_path / "inventory.ini"
    playbook_path.write_text(yaml.safe_dump(harness))
    inventory_path.write_text("localhost ansible_connection=local ansible_become=true\n")
    proc = subprocess.run(
        [ANSIBLE_PLAYBOOK, "-i", str(inventory_path), str(playbook_path)],
        cwd=ROOT,
        env=_ansible_env(tmp_path / "local-tmp"),
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _receipt_playbook(harness_path: Path, stdout: str, rc: int) -> None:
    receiver_play = next(
        play for play in yaml.safe_load(PLAYBOOK.read_text()) if play.get("hosts") == "o11y_svc"
    )
    tasks = receiver_play["tasks"]
    protected_receipt = next(task for task in tasks if task.get("block") is not None)
    public_reports = [
        task
        for task in tasks
        if task.get("name")
        in {
            "Report the bounded Alloy health receipt",
            "Report a fixed Alloy health refusal category",
        }
    ]
    harness = [
        {
            "name": "Exercise the production receipt parser offline",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                "_alloy_health_result": {"stdout": stdout, "rc": rc},
                "_alloy_health_refusals": receiver_play["vars"]["_alloy_health_refusals"],
                "_alloy_health_observed_fields": receiver_play["vars"]["_alloy_health_observed_fields"],
                "_alloy_health_run_states": receiver_play["vars"]["_alloy_health_run_states"],
                "_alloy_health_states": receiver_play["vars"]["_alloy_health_states"],
                "_alloy_health_age_buckets": receiver_play["vars"]["_alloy_health_age_buckets"],
            },
            "tasks": [protected_receipt, *public_reports],
        }
    ]
    harness_path.write_text(yaml.safe_dump(harness))


def _ansible_env(local_temp: Path) -> dict[str, str]:
    local_temp.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "ANSIBLE_LOCAL_TEMP": str(local_temp)}


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="needs ansible-playbook")
@pytest.mark.parametrize(
    ("stdout", "rc", "expected"),
    [
        ("", 1, "invalid_receipt"),
        ('{"private":"RAW_SENTINEL"', 1, "invalid_receipt"),
        ('{"status":"observed","private":"RAW_SENTINEL"}', 0, "invalid_receipt"),
        ('{"status":"unavailable","reason":"container_missing"}', 1, "container_missing"),
    ],
    ids=["empty", "malformed-json", "malformed-schema", "valid-container-missing"],
)
def test_ansible_receipt_block_fails_closed_without_echoing_raw_output(tmp_path, stdout, rc, expected):
    harness_path = tmp_path / "receipt-test.yml"
    inventory_path = tmp_path / "inventory.ini"
    inventory_path.write_text("localhost ansible_connection=local\n")
    _receipt_playbook(harness_path, stdout, rc)
    proc = subprocess.run(
        [ANSIBLE_PLAYBOOK, "-i", str(inventory_path), str(harness_path)],
        cwd=ROOT,
        env=_ansible_env(tmp_path / "local-tmp"),
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )
    output = proc.stdout + proc.stderr
    assert expected in output
    assert "RAW_SENTINEL" not in output
    if expected == "invalid_receipt":
        assert proc.returncode != 0
    else:
        assert proc.returncode == 0, output
