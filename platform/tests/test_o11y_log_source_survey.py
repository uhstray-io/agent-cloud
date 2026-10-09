"""Bounded, metadata-only receiver-host log-source survey contract."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import playbook_yaml
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/survey-o11y-log-source.py"
PLAYBOOK = ROOT / "platform/playbooks/survey-o11y-log-source.yml"
SPEC = importlib.util.spec_from_file_location("o11y_log_source_survey", HELPER)
SURVEY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SURVEY)


def test_reports_only_driver_counts_and_bounded_journal_metadata(monkeypatch):
    journal_argv = []

    def run(argv, timeout=8):
        if argv[:3] == ["podman", "info", "--format"]:
            return 0, json.dumps({"host": {"logDriver": "journald"}})
        if argv[:2] == ["podman", "ps"]:
            return 0, json.dumps([{"Names": ["o11y-loki", "private-container-id"]}])
        if argv[:5] == ["podman", "inspect", "--type", "container", "--format"]:
            if argv[-1] == "o11y-alloy":
                return 0, json.dumps([{"State": {"Running": True}, "Mounts": []}])
            return 0, json.dumps(
                [{"HostConfig": {"LogConfig": {"Type": "journald"}}, "Mounts": []}]
            )
        if argv[0] == "journalctl":
            journal_argv.append(argv)
            return 0, "\n".join(
                [
                    json.dumps(
                        {
                            "CONTAINER_NAME": "o11y-loki",
                            "MESSAGE": "private log body /private/path",
                            "__CURSOR": "private-journal-id",
                        }
                    ),
                    json.dumps(
                        {
                            "CONTAINER_NAME": "o11y-loki-other",
                            "MESSAGE": "another private body",
                        }
                    ),
                ]
            )
        raise AssertionError("unexpected command")

    monkeypatch.setattr(SURVEY, "run", run)
    report = SURVEY.survey()
    serialized = json.dumps(report)

    assert report == {
        "status": "observed",
        "rootless_default_log_driver": "journald",
        "running_o11y_container_count": 1,
        "running_log_driver_counts": {
            "journald": 1,
            "json-file": 0,
            "k8s-file": 0,
            "none": 0,
            "other": 0,
            "unknown": 0,
        },
        "journald_metadata_read": "readable",
        "journald_metadata_entry_count": 1,
        "alloy_read_only_journal_source_mounted": False,
    }
    assert len(journal_argv) == 1
    assert "--output=json" in journal_argv[0]
    assert "--output-fields=CONTAINER_NAME" in journal_argv[0]
    assert f"--since={SURVEY.JOURNAL_WINDOW}" in journal_argv[0]
    assert "-n" in journal_argv[0] and "100" in journal_argv[0]
    assert "MESSAGE" not in " ".join(journal_argv[0])
    assert "private-container-id" not in serialized
    assert "private-journal-id" not in serialized
    assert "private log body" not in serialized
    assert "another private body" not in serialized
    assert "/var/log" not in serialized


def test_journald_denial_is_a_fixed_category_and_non_journald_is_unsupported(monkeypatch):
    def denied(argv, timeout=8):
        if argv[:3] == ["podman", "info", "--format"]:
            return 0, '{"host":{"logDriver":"journald"}}'
        if argv[:2] == ["podman", "ps"]:
            return 0, '[{"Names":["o11y-loki"]}]'
        if argv[:5] == ["podman", "inspect", "--type", "container", "--format"]:
            if argv[-1] == "o11y-alloy":
                return 0, '[{"State":{"Running":true},"Mounts":[]}]'
            return 0, '[{"HostConfig":{"LogConfig":{"Type":"journald"}},"Mounts":[]}]'
        if argv[0] == "journalctl":
            return 1, "secret token /private/path"
        raise AssertionError("unexpected command")

    monkeypatch.setattr(SURVEY, "run", denied)
    report = SURVEY.survey()
    assert report["journald_metadata_read"] == "unreadable"
    assert report["journald_metadata_entry_count"] == 0
    assert "secret" not in json.dumps(report)
    assert "/private/path" not in json.dumps(report)

    assert SURVEY.journal_status(["o11y-loki"], {"o11y-loki": "json-file"}) == ("unsupported", 0)


def test_missing_or_malformed_container_readback_fails_closed(monkeypatch):
    def malformed(argv, timeout=8):
        if argv[:2] == ["podman", "ps"]:
            return 0, "raw host path and container id"
        return None, "secret diagnostic"

    monkeypatch.setattr(SURVEY, "run", malformed)
    report = SURVEY.survey()
    assert report == {"status": "unavailable", "reason": "container_metadata_unavailable"}
    assert "raw" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_zero_running_o11y_containers_fails_closed(monkeypatch):
    def empty(argv, timeout=8):
        if argv[:2] == ["podman", "ps"]:
            return 0, "[]"
        if argv[:3] == ["podman", "info", "--format"]:
            return 0, '{"host":{"logDriver":"journald"}}'
        raise AssertionError("unexpected command")

    monkeypatch.setattr(SURVEY, "run", empty)
    report = SURVEY.survey()
    assert report == {"status": "unavailable", "reason": "no_running_o11y_containers"}


@pytest.mark.parametrize(
    "malformed",
    [
        "malformed-container-row",
        {"Names": None},
        {"Names": ["private-container-id", None]},
    ],
    ids=["malformed-row", "missing-name-field", "malformed-name-list"],
)
def test_malformed_running_container_rows_fail_closed_without_partial_counts(monkeypatch, malformed):
    def mixed_rows(argv, timeout=8):
        if argv[:3] == ["podman", "info", "--format"]:
            return 0, '{"host":{"logDriver":"journald"}}'
        if argv[:2] == ["podman", "ps"]:
            return 0, json.dumps([{"Names": ["o11y-loki"]}, malformed])
        raise AssertionError("unexpected command")

    monkeypatch.setattr(SURVEY, "run", mixed_rows)
    report = SURVEY.survey()
    assert report == {"status": "unavailable", "reason": "container_metadata_unavailable"}
    assert "o11y-loki" not in json.dumps(report)
    assert "private-container-id" not in json.dumps(report)


@pytest.mark.parametrize(
    ("inspect_rc", "inspect_output"),
    [(1, "private inspect failure"), (0, "malformed private inspect JSON")],
    ids=["inspect-failed", "inspect-malformed"],
)
def test_running_container_inspect_failure_fails_closed(monkeypatch, inspect_rc, inspect_output):
    def failed_inspect(argv, timeout=8):
        if argv[:3] == ["podman", "info", "--format"]:
            return 0, '{"host":{"logDriver":"journald"}}'
        if argv[:2] == ["podman", "ps"]:
            return 0, '[{"Names":["o11y-loki"]}]'
        if argv[:5] == ["podman", "inspect", "--type", "container", "--format"]:
            return inspect_rc, inspect_output
        raise AssertionError("unexpected command")

    monkeypatch.setattr(SURVEY, "run", failed_inspect)
    report = SURVEY.survey()
    assert report == {"status": "unavailable", "reason": "container_metadata_unavailable"}
    assert "private" not in json.dumps(report)


def test_malformed_journal_json_is_unverified_and_never_reported(monkeypatch):
    def malformed(argv, timeout=8):
        if argv[0] == "journalctl":
            return 0, '{"MESSAGE":"secret /private/path"'
        raise AssertionError("unexpected command")

    monkeypatch.setattr(SURVEY, "run", malformed)
    report = SURVEY.journal_status(["o11y-loki"], {"o11y-loki": "journald"})
    assert report == ("unverified", 0)
    assert "secret" not in json.dumps(report)
    assert "/private/path" not in json.dumps(report)


@pytest.mark.parametrize(
    "failure",
    [
        SURVEY.subprocess.TimeoutExpired(["journalctl"], 12),
        FileNotFoundError("/private/path/journalctl"),
    ],
    ids=["timeout", "missing-command"],
)
def test_journal_timeout_or_missing_command_is_unverified(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(SURVEY.subprocess, "run", fail)
    assert SURVEY.journal_status(["o11y-loki"], {"o11y-loki": "journald"}) == ("unverified", 0)
    assert "/private/path" not in json.dumps(SURVEY.journal_status(["o11y-loki"], {"o11y-loki": "journald"}))


def test_zero_journal_entries_are_distinct_from_unsupported(monkeypatch):
    monkeypatch.setattr(SURVEY, "run", lambda argv, timeout=8: (0, ""))
    assert SURVEY.journal_status(["o11y-loki"], {"o11y-loki": "journald"}) == ("no_entries", 0)
    assert SURVEY.journal_status(["o11y-loki"], {"o11y-loki": "json-file"}) == ("unsupported", 0)


def test_unknown_container_driver_makes_journal_status_unverified_without_partial_counts(monkeypatch):
    def must_not_query_journal(argv, timeout=8):
        raise AssertionError("unknown drivers must prevent a partial journald count")

    monkeypatch.setattr(SURVEY, "run", must_not_query_journal)
    assert SURVEY.journal_status(
        ["o11y-loki", "o11y-agentgateway"],
        {"o11y-loki": "journald", "o11y-agentgateway": "unknown"},
    ) == ("unverified", 0)
    assert SURVEY.journal_status(
        ["o11y-loki", "o11y-agentgateway"],
        {"o11y-loki": "unknown", "o11y-agentgateway": "unknown"},
    ) == ("unverified", 0)


def test_command_helper_discards_stderr_from_its_return_value(monkeypatch):
    monkeypatch.setattr(
        SURVEY.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="bounded stdout", stderr="secret stderr /private/path"
        ),
    )
    assert SURVEY.run(["journalctl"]) == (1, "bounded stdout")


def test_alloy_source_requires_read_only_mount_and_container_read_access(monkeypatch):
    calls = []

    def mounted(argv, timeout=8):
        if argv[:5] == ["podman", "inspect", "--type", "container", "--format"]:
            return 0, json.dumps(
                [
                    {
                        "State": {"Running": True},
                        "Mounts": [
                            {"Source": "/private/journal", "Destination": "/run/log/journal", "RW": False}
                        ],
                    }
                ]
            )
        if argv[:4] == ["podman", "exec", "o11y-alloy", "sh"]:
            calls.append(argv)
            return 0, "readable"
        raise AssertionError("unexpected command")

    monkeypatch.setattr(SURVEY, "run", mounted)
    assert SURVEY.alloy_source() is True
    assert calls == [
        [
            "podman",
            "exec",
            "o11y-alloy",
            "sh",
            "-c",
            "if [ ! -d /run/log/journal ]; then printf missing; elif [ ! -r /run/log/journal ]; "
            "then printf unreadable; else printf readable; fi",
        ]
    ]

    def writable(argv, timeout=8):
        if argv[:5] == ["podman", "inspect", "--type", "container", "--format"]:
            return 0, json.dumps(
                [
                    {
                        "State": {"Running": True},
                        "Mounts": [
                            {"Source": "/private/journal", "Destination": "/run/log/journal", "RW": True}
                        ],
                    }
                ]
            )
        raise AssertionError("writable mounts must not be probed")

    monkeypatch.setattr(SURVEY, "run", writable)
    assert SURVEY.alloy_source() is False


@pytest.mark.parametrize("probe", ["missing", "unreadable"], ids=["directory-absent", "directory-unreadable"])
def test_alloy_absent_mount_and_unreadable_directory_are_known_false(monkeypatch, probe):
    monkeypatch.setattr(
        SURVEY,
        "inspect",
        lambda name: {"State": {"Running": True}, "Mounts": []},
    )
    assert SURVEY.alloy_source() is False

    monkeypatch.setattr(
        SURVEY,
        "inspect",
        lambda name: {
            "State": {"Running": True},
            "Mounts": [{"Source": "/private/journal", "Destination": "/run/log/journal", "RW": False}],
        },
    )
    monkeypatch.setattr(SURVEY, "run", lambda argv, timeout=8: (0, probe))
    assert SURVEY.alloy_source() is False


@pytest.mark.parametrize(
    "container",
    [None, {"State": {"Running": False}, "Mounts": []}, {"State": {"Running": True}, "Mounts": None}],
    ids=["missing-or-uninspectable", "stopped", "malformed-mounts"],
)
def test_alloy_missing_stopped_or_uninspectable_is_unverified(monkeypatch, container):
    monkeypatch.setattr(SURVEY, "inspect", lambda name: container)
    assert SURVEY.alloy_source() is None


def test_alloy_exec_invocation_failure_is_unverified_and_fails_survey_closed(monkeypatch):
    container = {
        "State": {"Running": True},
        "Mounts": [{"Source": "/private/journal", "Destination": "/run/log/journal", "RW": False}],
    }
    monkeypatch.setattr(SURVEY, "inspect", lambda name: container)
    monkeypatch.setattr(SURVEY, "run", lambda argv, timeout=8: (None, "private diagnostic"))
    assert SURVEY.alloy_source() is None

    monkeypatch.setattr(SURVEY, "default_driver", lambda: "journald")
    monkeypatch.setattr(SURVEY, "running_containers", lambda: ["o11y-loki"])
    monkeypatch.setattr(SURVEY, "journal_status", lambda names, drivers: ("no_entries", 0))
    monkeypatch.setattr(SURVEY, "alloy_source", lambda: None)
    report = SURVEY.survey()
    assert report == {"status": "unavailable", "reason": "alloy_source_unverified"}
    assert "private" not in json.dumps(report)


def test_playbook_is_dev_bound_guarded_exact_head_and_read_only_in_check_mode():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert plays[2]["ansible.builtin.import_playbook"] == "preflight-target-group.yml"
    assert plays[2]["vars"] == {
        "preflight_group": "o11y_svc",
        "preflight_group_expected": "o11y_svc",
    }
    play = plays[3]
    assert play["hosts"] == "o11y_svc"
    assert play["become"] is False
    tasks = play["tasks"]
    scope_guard = next(
        task["ansible.builtin.assert"]
        for task in tasks
        if task.get("name") == "Require one receiver and production mode"
    )
    assert scope_guard["that"] == ["groups['o11y_svc'] | length == 1", "not (local_mode | default(false) | bool)"]
    reviewed_checkout = yaml.safe_load((ROOT / "platform/playbooks/require-reviewed-checkout.yml").read_text())
    reviewed_tasks = reviewed_checkout[0]["tasks"]
    sha_guard = reviewed_tasks[0]["ansible.builtin.assert"]
    assert sha_guard["that"] == "expected_repository_sha | default('') is match('^[0-9a-f]{40}$')"
    assert [
        task["ansible.builtin.command"]["argv"]
        for task in reviewed_tasks
        if "ansible.builtin.command" in task
    ] == [
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--porcelain", "--untracked-files=all"],
    ]
    assert all(task.get("check_mode") is False for task in tasks if "ansible.builtin.command" in task)
    command = next(
        task["ansible.builtin.command"]
        for task in tasks
        if task.get("name") == "Classify receiver log-source metadata"
    )
    helper_guard = next(
        task["ansible.builtin.assert"]
        for task in tasks
        if task.get("name") == "Require the survey helper to complete"
    )
    result_guard = next(
        task["ansible.builtin.assert"]
        for task in tasks
        if task.get("name") == "Require a sanitized survey result"
    )
    assert command["argv"] == ["python3", "-"]
    assert "stdin" in command
    assert helper_guard["that"] == "(_log_source_report.rc | default(1)) == 0"
    assert result_guard["that"] == ["(_log_source_report.stdout | from_json).status == 'observed'"]
    assert "stdout" not in helper_guard["fail_msg"]
    assert "stderr" not in helper_guard["fail_msg"]
    assert all(
        not any(key in task for key in ("ansible.builtin.copy", "ansible.builtin.file"))
        for task in tasks
    )

    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(item for item in templates if item.get("name") == "Survey o11y Log Source (Dev)")
    assert template["playbook"] == "platform/playbooks/survey-o11y-log-source.yml"
    assert template["repository"] == "agent-cloud dev"
    assert [item["name"] for item in template["survey_vars"]] == ["expected_repository_sha"]

    assert playbook_yaml.REPO == ROOT
