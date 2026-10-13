"""Sanitization and exact-scope contract for the Alloy health survey."""

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/survey-o11y-alloy-health.py"
PLAYBOOK = ROOT / "platform/playbooks/survey-o11y-alloy-health.yml"
SPEC = importlib.util.spec_from_file_location("survey_o11y_alloy_health", HELPER)
SURVEY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SURVEY)


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
            return SimpleNamespace(returncode=0, stdout="", stderr="private stderr")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(inspect_data()),
            stderr="private inspect stderr",
        )

    monkeypatch.setattr(SURVEY.subprocess, "run", run)
    report = SURVEY.inspect()
    assert calls == [
        ["podman", "container", "exists", "o11y-alloy"],
        ["podman", "inspect", "--type", "container", "o11y-alloy"],
    ]
    assert "private" not in json.dumps(report)
    assert "secret-token" not in json.dumps(report)


def test_playbook_is_dev_pinned_single_host_and_read_only():
    playbook = yaml.safe_load(PLAYBOOK.read_text())
    assert any(
        play.get("ansible.builtin.import_playbook") == "require-reviewed-checkout.yml"
        and play.get("vars", {}).get("_reviewed_checkout_payload_paths")
        == ["platform/playbooks/files/survey-o11y-alloy-health.py"]
        for play in playbook
    )
    assert any(
        "expected_repository_sha | default('') is match('^[0-9a-f]{40}$')"
        in task.get("ansible.builtin.assert", {}).get("that", [])
        and "_survey_receivers | length == 1" in task.get("ansible.builtin.assert", {}).get("that", [])
        for play in playbook
        for task in play.get("tasks", [])
    )
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
