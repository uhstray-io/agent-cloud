"""Bounded, metadata-only receiver-host log-source survey contract."""

import importlib.util
import json
import random
import time
from pathlib import Path
from types import SimpleNamespace

import playbook_yaml
import pytest
import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/survey-o11y-log-source.py"
PLAYBOOK = ROOT / "platform/playbooks/survey-o11y-log-source.yml"
SPEC = importlib.util.spec_from_file_location("o11y_log_source_survey", HELPER)
SURVEY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SURVEY)
RECEIVERS = ("node-exporter", "prometheus", "loki", "alloy", "tempo", "pyroscope", "grafana")
ZERO_CLASSES = {"email": 0, "ipv4": 0, "credential": 0, "secret_pair": 0, "url_query": 0}
UNVERIFIED = {"status": "unverified", "entries": None, "capped": None, "blocking_hits": None, "classes": None}


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
                [
                    {
                        "State": {"Running": True},
                        "HostConfig": {"LogConfig": {"Type": "journald"}},
                        "Mounts": [],
                    }
                ]
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
    content = report.pop("content_survey")
    assert report.pop("content_survey_status") == "unverified"

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
    metadata_argv = [argv for argv in journal_argv if "--output-fields=CONTAINER_NAME" in argv]
    content_argv = [argv for argv in journal_argv if "--output-fields=CONTAINER_NAME,MESSAGE" in argv]
    assert len(metadata_argv) == 1
    assert len(content_argv) == 1
    assert "--output=json" in metadata_argv[0]
    assert f"--since={SURVEY.JOURNAL_WINDOW}" in metadata_argv[0]
    assert "-n" in metadata_argv[0] and "100" in metadata_argv[0]
    assert "MESSAGE" not in " ".join(metadata_argv[0])
    assert content_argv[0][content_argv[0].index("-n") + 1] == str(SURVEY.CONTENT_CAP)
    assert content_argv[0][-1] == "CONTAINER_NAME=o11y-loki"
    assert content["loki"] == {
        "status": "clear",
        "entries": 1,
        "capped": False,
        "blocking_hits": 0,
        "classes": ZERO_CLASSES,
    }
    assert sorted(content) == sorted(RECEIVERS)
    assert all(content[short] == UNVERIFIED for short in RECEIVERS if short != "loki")
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
            return 0, '[{"State":{"Running":true},"HostConfig":{"LogConfig":{"Type":"journald"}},"Mounts":[]}]'
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


def test_container_stopped_after_running_list_fails_closed(monkeypatch):
    monkeypatch.setattr(SURVEY, "default_driver", lambda: "journald")
    monkeypatch.setattr(SURVEY, "running_containers", lambda: ["o11y-loki"])
    monkeypatch.setattr(
        SURVEY,
        "inspect",
        lambda name: {
            "State": {"Running": False},
            "HostConfig": {"LogConfig": {"Type": "journald"}},
        },
    )
    monkeypatch.setattr(
        SURVEY,
        "journal_status",
        lambda names, drivers: pytest.fail("stopped container must not be counted"),
    )
    report = SURVEY.survey()
    assert report == {"status": "unavailable", "reason": "container_metadata_unavailable"}


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
                            {"Source": "/var/log/journal", "Destination": "/run/log/journal", "RW": False}
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
                            {"Source": "/var/log/journal", "Destination": "/run/log/journal", "RW": True}
                        ],
                    }
                ]
            )
        raise AssertionError("writable mounts must not be probed")

    monkeypatch.setattr(SURVEY, "run", writable)
    assert SURVEY.alloy_source() is False


def test_alloy_unrelated_read_only_source_is_unverified_and_refuses_survey(monkeypatch):
    container = {
        "State": {"Running": True},
        "Mounts": [
            {"Source": "/srv/unrelated", "Destination": "/run/log/journal", "RW": False}
        ],
    }
    monkeypatch.setattr(SURVEY, "inspect", lambda name: container)
    monkeypatch.setattr(
        SURVEY,
        "run",
        lambda argv, timeout=8: pytest.fail("unverified source provenance must not be probed"),
    )
    assert SURVEY.alloy_source() is None

    monkeypatch.setattr(SURVEY, "default_driver", lambda: "journald")
    monkeypatch.setattr(SURVEY, "running_containers", lambda: ["o11y-loki"])
    monkeypatch.setattr(SURVEY, "journal_status", lambda names, drivers: ("no_entries", 0))
    report = SURVEY.survey()
    assert report == {"status": "unavailable", "reason": "alloy_source_unverified"}


@pytest.mark.parametrize(
    "destination",
    ["/var/log", "/run", None, {"malformed": "destination"}],
    ids=["var-log-ancestor", "run-ancestor", "missing-destination", "malformed-destination"],
)
def test_alloy_ancestor_or_malformed_destination_is_unverified(monkeypatch, destination):
    monkeypatch.setattr(
        SURVEY,
        "inspect",
        lambda name: {
            "State": {"Running": True},
            "Mounts": [
                {"Source": "/var/log/journal", "Destination": destination, "RW": False}
            ],
        },
    )
    monkeypatch.setattr(
        SURVEY,
        "run",
        lambda argv, timeout=8: pytest.fail("uncertain destinations must not be probed"),
    )
    assert SURVEY.alloy_source() is None


@pytest.mark.parametrize(
    "other_mount",
    [
        {"Source": "/var/log/journal", "Destination": "/var/log", "RW": False},
        {"Source": "/var/log/journal", "Destination": None, "RW": False},
        {"Source": "/var/log/journal", "Destination": "/run/log/journal", "RW": True},
        {"Source": "/run/log/journal", "Destination": "/run/log/journal", "RW": False},
    ],
    ids=["ancestor", "malformed", "writable-duplicate", "different-source-duplicate"],
)
@pytest.mark.parametrize("uncertain_first", [False, True], ids=["readable-first", "uncertain-first"])
def test_alloy_valid_mount_does_not_hide_later_uncertain_mounts(
    monkeypatch, other_mount, uncertain_first
):
    readable_mount = {
        "Source": "/var/log/journal",
        "Destination": "/run/log/journal",
        "RW": False,
    }
    mounts = [other_mount, readable_mount] if uncertain_first else [readable_mount, other_mount]
    monkeypatch.setattr(
        SURVEY,
        "inspect",
        lambda name: {"State": {"Running": True}, "Mounts": mounts},
    )
    monkeypatch.setattr(SURVEY, "run", lambda argv, timeout=8: (0, "readable"))
    assert SURVEY.alloy_source() is None


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
            "Mounts": [{"Source": "/var/log/journal", "Destination": "/run/log/journal", "RW": False}],
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
        "Mounts": [{"Source": "/var/log/journal", "Destination": "/run/log/journal", "RW": False}],
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
        "_preflight_redact_hostnames": True,
    }
    shared_preflight = yaml.safe_load((ROOT / "platform/playbooks/preflight-target-group.yml").read_text())
    group_assert = next(
        task["ansible.builtin.assert"]
        for task in shared_preflight[0]["tasks"]
        if task.get("name") == "Require the group to exist and contain at least one host"
    )
    success_message = Environment().from_string(group_assert["success_msg"])
    success_message.environment.filters["bool"] = bool
    safe_message = success_message.render(
        preflight_group="o11y_svc",
        groups={"o11y_svc": ["private-host.example", "other-private-host.example"]},
        _preflight_redact_hostnames=True,
    )
    assert "2 host(s)" in safe_message
    assert "private-host.example" not in safe_message
    assert "other-private-host.example" not in safe_message
    ordinary_message = success_message.render(
        preflight_group="service_svc",
        groups={"service_svc": ["service-host.example"]},
        _preflight_redact_hostnames=False,
    )
    assert "service-host.example" in ordinary_message
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
    assert "expected_repository_sha | default('') is match('^[0-9a-f]{40}$')" in sha_guard["that"]
    verifier = next(
        task for task in reviewed_tasks
        if task.get("name") == "Compare checkout bytes with the reviewed commit"
    )
    assert verifier["ansible.builtin.command"]["argv"][:2] == ["python3", "-"]
    assert verifier["ansible.builtin.command"]["stdin_add_newline"] is False
    assert "--require-dev-ref" not in json.dumps(verifier)
    assert next(
        task for task in reviewed_tasks
        if task.get("name") == "Require pinned index and worktree bytes"
    )["no_log"] is True
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
    refusal_reasons = play["vars"]["_survey_refusal_reasons"]
    assert refusal_reasons == [
        "container_metadata_unavailable",
        "no_running_o11y_containers",
        "alloy_source_unverified",
        "survey_failed",
    ]
    assert helper_guard["fail_msg"] == (
        "Receiver log-source survey failed (survey_failed); no metadata is reported."
    )
    template_env = Environment()
    template_env.filters["from_json"] = json.loads
    rendered_refusal = template_env.from_string(result_guard["fail_msg"])
    for reason in refusal_reasons:
        rendered = rendered_refusal.render(
            _log_source_report={"stdout": json.dumps({"status": "unavailable", "reason": reason})},
            _survey_refusal_reasons=refusal_reasons,
        )
        assert f"({reason})" in rendered

    untrusted_reason = "/private/host/path secret-value"
    rendered = rendered_refusal.render(
        _log_source_report={
            "stdout": json.dumps({"status": "unavailable", "reason": untrusted_reason})
        },
        _survey_refusal_reasons=refusal_reasons,
    )
    assert "(unclassified)" in rendered
    assert untrusted_reason not in rendered
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


def receiver_host(monkeypatch, *, running, messages=None, failing=(), drivers=None, calls=None):
    """Fake podman and journalctl for a receiver host; messages maps a container name to MESSAGE values."""
    messages = messages or {}
    drivers = drivers or {}

    def run(argv, timeout=8):
        if argv[:3] == ["podman", "info", "--format"]:
            return 0, '{"host":{"logDriver":"journald"}}'
        if argv[:2] == ["podman", "ps"]:
            return 0, json.dumps([{"Names": [name]} for name in running])
        if argv[:5] == ["podman", "inspect", "--type", "container", "--format"]:
            driver = drivers.get(argv[-1], "journald")
            return 0, json.dumps(
                [{"State": {"Running": True}, "HostConfig": {"LogConfig": {"Type": driver}}, "Mounts": []}]
            )
        if argv[0] == "journalctl":
            name = argv[-1].removeprefix("CONTAINER_NAME=")
            if calls is not None:
                calls.append(argv)
            if name in failing:
                return 1, "private diagnostic"
            return 0, "\n".join(
                json.dumps({"CONTAINER_NAME": name, "MESSAGE": message}) for message in messages.get(name, [])
            )
        raise AssertionError("unexpected command")

    monkeypatch.setattr(SURVEY, "run", run)


def test_content_allowlist_is_the_seven_receiver_containers_without_the_collector():
    compose = yaml.safe_load((ROOT / "platform/services/o11y/deployment/compose.yml").read_text())
    container_names = {
        service.get("container_name") for service in compose["services"].values() if isinstance(service, dict)
    }
    journal = yaml.safe_load((ROOT / "platform/services/o11y/deployment/compose.journal.yml").read_text())
    collector = journal["services"]["journal-collector"]["container_name"]
    assert sorted(SURVEY.CONTENT_CONTAINERS) == sorted(RECEIVERS)
    assert {f"o11y-{short}" for short in RECEIVERS} <= container_names
    assert collector == "o11y-journal-collector"
    assert collector not in container_names
    assert collector not in {f"o11y-{short}" for short in SURVEY.CONTENT_CONTAINERS}


def test_content_survey_counts_classes_per_receiver_and_never_returns_text(monkeypatch):
    calls = []
    running = [f"o11y-{short}" for short in RECEIVERS] + ["o11y-journal-collector"]
    receiver_host(
        monkeypatch,
        running=running,
        messages={
            "o11y-grafana": ["notify ops@example.com with Bearer abcdefgh12345678"],
            "o11y-tempo": ["upstream answered from 203.0.113.7"],
            "o11y-loki": ["private log body /private/path"],
            "o11y-journal-collector": ["collector echoes ops@example.com"],
        },
        failing={"o11y-node-exporter"},
        calls=calls,
    )
    report = SURVEY.survey()
    serialized = json.dumps(report)
    content = report["content_survey"]

    assert report["content_survey_status"] == "blocked"
    assert sorted(content) == sorted(RECEIVERS)
    assert content["grafana"] == {
        "status": "blocked",
        "entries": 1,
        "capped": False,
        "blocking_hits": 2,
        "classes": {**ZERO_CLASSES, "email": 1, "credential": 1},
    }
    assert content["tempo"] == {
        "status": "clear",
        "entries": 1,
        "capped": False,
        "blocking_hits": 0,
        "classes": {**ZERO_CLASSES, "ipv4": 1},
    }
    assert content["loki"]["status"] == "clear"
    assert content["loki"]["classes"] == ZERO_CLASSES
    assert content["node-exporter"] == UNVERIFIED
    for short in ("prometheus", "alloy", "pyroscope"):
        assert content[short]["status"] == "clear"
    content_argv = [argv for argv in calls if "--output-fields=CONTAINER_NAME,MESSAGE" in argv]
    assert sorted(argv[-1] for argv in content_argv) == sorted(f"CONTAINER_NAME=o11y-{short}" for short in RECEIVERS)
    for leaked in (
        "ops@example.com",
        "abcdefgh12345678",
        "203.0.113.7",
        "private log body",
        "/private/path",
        "journal-collector",
    ):
        assert leaked not in serialized


@pytest.mark.parametrize("offset", [-1, 0], ids=["below-cap", "at-cap"])
def test_content_survey_flags_a_read_that_reaches_the_cap(monkeypatch, offset):
    receiver_host(
        monkeypatch,
        running=[f"o11y-{short}" for short in RECEIVERS],
        messages={"o11y-prometheus": ["ordinary line"] * (SURVEY.CONTENT_CAP + offset)},
    )
    report = SURVEY.survey()
    prometheus = report["content_survey"]["prometheus"]
    assert prometheus["entries"] == SURVEY.CONTENT_CAP + offset
    assert prometheus["capped"] is (offset == 0)
    assert report["content_survey_status"] == "clear"


def test_receiver_not_running_or_not_journald_is_unverified_and_never_read(monkeypatch):
    calls = []
    receiver_host(
        monkeypatch,
        running=[f"o11y-{short}" for short in RECEIVERS if short != "pyroscope"],
        drivers={"o11y-tempo": "json-file"},
        calls=calls,
    )
    report = SURVEY.survey()
    content = report["content_survey"]
    assert content["pyroscope"] == UNVERIFIED
    assert content["tempo"] == UNVERIFIED
    assert report["content_survey_status"] == "unverified"
    read = [argv[-1] for argv in calls if "--output-fields=CONTAINER_NAME,MESSAGE" in argv]
    assert "CONTAINER_NAME=o11y-pyroscope" not in read
    assert "CONTAINER_NAME=o11y-tempo" not in read


@pytest.mark.parametrize("content_rc", [1, None], ids=["nonzero-exit", "timeout-or-missing-command"])
def test_failed_content_read_is_unverified_and_leaks_nothing(monkeypatch, content_rc):
    receiver_host(monkeypatch, running=["o11y-loki"])
    inner = SURVEY.run

    def failing_content(argv, timeout=8):
        if "--output-fields=CONTAINER_NAME,MESSAGE" in argv:
            return content_rc, "ops@example.com private diagnostic"
        return inner(argv, timeout=timeout)

    monkeypatch.setattr(SURVEY, "run", failing_content)
    report = SURVEY.survey()
    assert report["content_survey"]["loki"] == UNVERIFIED
    assert report["content_survey_status"] == "unverified"
    serialized = json.dumps(report)
    assert "private diagnostic" not in serialized
    assert "ops@example.com" not in serialized


@pytest.mark.parametrize(
    ("message", "label"),
    [
        ("contact ops@example.com now", "email"),
        ("call 203.0.113.7 now", "ipv4"),
        ("sent Bearer abcdefgh12345678", "credential"),
        ("config token: abcd1234", "secret_pair"),
        ("open https://example.com/cb?code=1", "url_query"),
        ("ordinary private log body", None),
    ],
    ids=["email", "ipv4", "credential", "secret_pair", "url_query", "clean"],
)
def test_each_content_class_counts_its_own_hit(message, label):
    output = json.dumps({"CONTAINER_NAME": "o11y-loki", "MESSAGE": message})
    expected = {**ZERO_CLASSES, **({label: 1} if label else {})}
    assert SURVEY.content_scan(output, "o11y-loki") == {"entries": 1, "classes": expected}


_BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_RANDOM_BASE64 = "".join(random.Random(0).choice(_BASE64) for _ in range(48000))


@pytest.mark.parametrize(
    "text",
    ["a" * 48000, "QUJD" * 12000, _RANDOM_BASE64],
    ids=["letters", "repeated-base64", "random-base64"],
)
def test_classifier_stays_linear_on_a_48k_letter_or_base64_line(text):
    line = json.dumps({"CONTAINER_NAME": "o11y-loki", "MESSAGE": text})
    started = time.perf_counter()
    scanned = SURVEY.content_scan(line, "o11y-loki")
    elapsed = time.perf_counter() - started
    assert scanned["entries"] == 1
    assert elapsed < 0.2, f"classifier took {elapsed:.3f}s on a 48 KB line"


@pytest.mark.parametrize(
    ("message", "addresses"),
    [
        ("contact ops@example.com now", 1),
        ("ops@example.com,ops2@example.org", 2),
        ("(ops@example.com)", 1),
        ("a.b+c@sub.example.org", 1),
        ("x_ops@example.com", 1),
        ("user:ops@example.com;", 1),
    ],
)
def test_email_class_keeps_matching_addresses_after_the_anchor(message, addresses):
    output = json.dumps({"CONTAINER_NAME": "o11y-loki", "MESSAGE": message})
    assert SURVEY.content_scan(output, "o11y-loki")["classes"]["email"] == addresses


@pytest.mark.parametrize(
    ("message", "hits"),
    [("addr 203.0.113.1 x", 1), ("addr 999.1.1.1 x", 0), ("addr 1.1.1.1.1 x", 0)],
    ids=["plain-address", "three-digit-first-octet", "five-part-number"],
)
def test_ipv4_class_rejects_parts_of_longer_dotted_numbers(message, hits):
    output = json.dumps({"CONTAINER_NAME": "o11y-loki", "MESSAGE": message})
    assert SURVEY.content_scan(output, "o11y-loki")["classes"]["ipv4"] == hits


def test_binary_messages_are_decoded_and_unusable_messages_are_counted_not_scanned():
    email = list(b"ops@example.com")
    lines = [
        json.dumps({"CONTAINER_NAME": "o11y-loki", "MESSAGE": email}),
        json.dumps({"CONTAINER_NAME": "o11y-loki", "MESSAGE": [0xFF] + email}),
        json.dumps({"CONTAINER_NAME": "o11y-loki", "MESSAGE": {"not": "text"}}),
        json.dumps({"CONTAINER_NAME": "o11y-loki", "MESSAGE": [300, 1]}),
        json.dumps({"CONTAINER_NAME": "o11y-loki"}),
        json.dumps({"CONTAINER_NAME": "o11y-loki-other", "MESSAGE": "ops@example.com"}),
    ]
    assert SURVEY.content_scan("\n".join(lines), "o11y-loki") == {
        "entries": 5,
        "classes": {**ZERO_CLASSES, "email": 2},
    }


@pytest.mark.parametrize(
    "line",
    [
        "not json ops@example.com",
        json.dumps({"CONTAINER_NAME": {"not": "a name"}, "MESSAGE": "ops@example.com"}),
        json.dumps({"CONTAINER_NAME": ["o11y-loki", None], "MESSAGE": "ops@example.com"}),
    ],
    ids=["non-json", "dict-name", "non-string-name-list"],
)
def test_malformed_content_row_makes_the_whole_container_unscannable(line):
    assert SURVEY.content_scan(line, "o11y-loki") is None


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["clear", "clear"], "clear"),
        (["clear", "unverified"], "unverified"),
        (["unverified", "blocked"], "blocked"),
        (["blocked", "clear"], "blocked"),
    ],
)
def test_content_status_reports_blocked_over_unverified_over_clear(statuses, expected):
    assert SURVEY.content_status({f"c{index}": {"status": status} for index, status in enumerate(statuses)}) == (
        expected
    )
