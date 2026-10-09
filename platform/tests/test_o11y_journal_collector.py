"""Safety contract for the bounded receiver-host journald pilot."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
O11Y = ROOT / "platform/services/o11y/deployment"
PLAYBOOK = ROOT / "platform/playbooks/deploy-o11y-journal-collector.yml"
HELPER = ROOT / "platform/playbooks/files/probe-o11y-journal-directory.py"
SPEC = importlib.util.spec_from_file_location("o11y_journal_probe", HELPER)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)
SURVEY_HELPER = ROOT / "platform/playbooks/files/survey-o11y-journal-directory.py"
SURVEY_SPEC = importlib.util.spec_from_file_location("o11y_journal_survey", SURVEY_HELPER)
SURVEY = importlib.util.module_from_spec(SURVEY_SPEC)
SURVEY_SPEC.loader.exec_module(SURVEY)


def test_journal_probe_reports_only_a_sanitized_exact_target_count():
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "CONTAINER_NAME": ["o11y-alloy"],
                    "_SYSTEMD_UNIT": "private-unit.service",
                    "MESSAGE": "private log message",
                    "CONTAINER_ID_FULL": "private-id",
                }
            ),
        )
    )

    report = PROBE.probe("/var/log/journal", run=runner)

    assert report == {"status": "observed", "matching_entries": 1}
    assert "private" not in json.dumps(report)
    argv = runner.call_args.args[0]
    assert "--output-fields=CONTAINER_NAME" in argv
    assert argv[-1] == "CONTAINER_NAME=o11y-alloy"


def test_journal_probe_fails_closed_for_unmatched_or_malformed_entries():
    unmatched = Mock(
        return_value=SimpleNamespace(returncode=0, stdout='{"CONTAINER_NAME":"other"}\n')
    )
    malformed = Mock(return_value=SimpleNamespace(returncode=0, stdout='not-json\n'))
    failed = Mock(return_value=SimpleNamespace(returncode=1, stdout=""))

    assert PROBE.probe("/var/log/journal", run=unmatched) == {
        "status": "unavailable",
        "matching_entries": 0,
    }
    assert PROBE.probe("/var/log/journal", run=malformed)["status"] == "unavailable"
    assert PROBE.probe("/var/log/journal", run=failed)["status"] == "unavailable"


def _journal_survey_runner(viable_paths, rootless="true"):
    def run(argv, **_kwargs):
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout=rootless)
        if argv[:3] == ["podman", "image", "exists"]:
            assert argv[3] == "docker.io/grafana/alloy:v1.5.1"
            return SimpleNamespace(returncode=0, stdout="")
        if argv[0] == "journalctl":
            directory = argv[1].split("=", 1)[1]
            stdout = '{"CONTAINER_NAME":"o11y-alloy","MESSAGE":"private"}\n'
            return SimpleNamespace(returncode=0 if directory in viable_paths else 1, stdout=stdout)
        if argv[:2] == ["podman", "run"]:
            directory = next(arg.split("src=", 1)[1].split(",", 1)[0] for arg in argv if arg.startswith("type=bind,"))
            return SimpleNamespace(returncode=0 if directory in viable_paths else 1, stdout="")
        raise AssertionError("unexpected survey command")

    return run


def test_standard_journal_survey_reports_only_single_ambiguous_or_none():
    fixed = {"/var/log/journal", "/run/log/journal"}

    def isdir(path):
        return path in fixed

    def access(path, _mode):
        return path in fixed

    one = SURVEY.survey(
        run=_journal_survey_runner({"/var/log/journal"}), isdir=isdir, access=access
    )
    both = SURVEY.survey(
        run=_journal_survey_runner(fixed), isdir=isdir, access=access
    )
    none = SURVEY.survey(
        run=_journal_survey_runner(fixed, rootless="false"), isdir=isdir, access=access
    )

    assert one == {"result": "/var/log/journal"}
    assert both == {"result": "ambiguous"}
    assert none == {"result": "none"}
    assert "private" not in json.dumps(one)


def test_survey_file_probe_uses_rootless_uid_zero_and_never_pulls():
    calls = []
    runner = _journal_survey_runner({"/var/log/journal"})

    def capture_run(argv, **kwargs):
        calls.append(argv)
        return runner(argv, **kwargs)

    report = SURVEY.survey(
        run=capture_run,
        isdir=lambda path: path in SURVEY.DIRECTORIES,
        access=lambda path, _mode: path in SURVEY.DIRECTORIES,
    )

    probe = next(argv for argv in calls if argv[:2] == ["podman", "run"])
    assert report == {"result": "/var/log/journal"}
    assert probe[probe.index("--user") + 1] == "0:0"
    assert "--pull=never" in probe
    assert not any(argv[:2] == ["podman", "pull"] for argv in calls)


def test_standard_journal_survey_uses_only_fixed_paths_and_cached_probe_image():
    source = SURVEY_HELPER.read_text()

    assert 'DIRECTORIES = ("/var/log/journal", "/run/log/journal")' in source
    assert '"--pull=never"' in source
    assert 'IMAGE = "docker.io/grafana/alloy:v1.5.1"' in source
    assert '"--user",\n            "0:0"' in source
    assert '"podman", "pull"' not in source
    assert '"--output-fields=CONTAINER_NAME"' in source
    assert "MESSAGE" not in source


def test_standard_journal_survey_refuses_when_cached_probe_image_is_unavailable():
    calls = []

    def missing_image_run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:3] == ["podman", "image", "exists"]:
            return SimpleNamespace(returncode=1, stdout="private podman diagnostic")
        raise AssertionError("survey must not pull or run an unavailable probe image")

    report = SURVEY.survey(
        run=missing_image_run,
        isdir=lambda path: path in SURVEY.DIRECTORIES,
        access=lambda path, _mode: path in SURVEY.DIRECTORIES,
    )

    assert report == {"result": "probe_unavailable"}
    assert "private" not in json.dumps(report)
    assert not any(argv[:2] == ["podman", "pull"] for argv in calls)
    assert not any(argv[:2] == ["podman", "run"] for argv in calls)


def test_compose_mount_is_read_only_and_state_is_separate():
    compose = yaml.safe_load((O11Y / "compose.journal.yml").read_text())
    service = compose["services"]["journal-collector"]

    assert service["image"] == "docker.io/grafana/alloy:v1.9.2"
    assert service["restart"] == "always"
    assert service["read_only"] is True
    assert service["user"] == "0:0"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert "ports" not in service
    assert "privileged" not in service
    assert "network_mode" not in service
    assert "depends_on" not in service
    assert not any("docker.sock" in str(volume) for volume in service["volumes"])
    journal = next(volume for volume in service["volumes"] if isinstance(volume, dict))
    assert journal["target"] == "/var/log/journal"
    assert journal["read_only"] is True
    assert journal["bind"]["create_host_path"] is False
    assert "journal-collector-state:/var/lib/alloy/data" in service["volumes"]
    assert "journal-collector-state" in compose["volumes"]


def test_alloy_config_uses_only_the_fixed_bounded_selector_and_otlp():
    config = (O11Y / "templates/journal.alloy.j2").read_text()

    assert 'matches    = "CONTAINER_NAME=o11y-alloy"' in config
    assert 'max_age    = "15m"' in config
    assert 'labels     = { service = "o11y/alloy", signal = "container" }' in config
    assert "otelcol.exporter.otlp" in config
    assert "o11y_otlp_bind" in config
    assert "loki.write" not in config
    assert "__journal" not in config
    assert "CONTAINER_ID" not in config
    assert "MESSAGE" not in config


def test_playbook_guards_validation_delivery_and_non_destructive_rollback():
    source = PLAYBOOK.read_text()
    plays = yaml.safe_load(source)
    guard = source.index("import_playbook: refuse-internal-extra-vars.yml")
    reviewed = source.index("import_playbook: require-reviewed-checkout.yml")
    place = source.index("tasks/place-monorepo.yml")
    validate = source.index("Validate the candidate config")
    apply = source.index("Start only the journal collector service")
    verify = source.index("Verify the exact bounded journal stream reached Loki")
    stop = source.index("Force-remove only the failed collector while retaining positions")

    assert guard < reviewed < place < validate < apply < verify < stop
    assert "groups.get('o11y_svc', []) | length == 1" in source
    assert "journal_collector_action | default('apply') in ['survey', 'apply', 'stop']" in source
    assert "Require the actual runtime to be rootless" in source
    assert "read -r -N 1 _ < \"$file\"" in source
    assert "expected_repository_sha" in source
    assert "argv: [podman, rm, --force, --time, \"10\", o11y-journal-collector]" in source
    assert "podman rm -v" not in source
    assert "compose down" not in source
    assert "up, --no-deps, -d, journal-collector" in source
    assert "Require all existing receiver containers to remain unchanged" in source
    assert "_journal_verification_start" in source
    assert "values[0][0]" in source
    assert "State.Health.Status" in source
    assert "image: docker.io/grafana/alloy:v1.9.2" in (O11Y / "compose.journal.yml").read_text()
    assert "_journal_image: docker.io/grafana/alloy:v1.9.2" in source
    assert 'IMAGE = "docker.io/grafana/alloy:v1.5.1"' in SURVEY_HELPER.read_text()

    apply_play = next(play for play in plays if play.get("hosts") == "o11y_svc")
    apply_block = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Apply the collector and require exact-target Loki delivery"
    )
    assert apply_block["when"] == "not ansible_check_mode"
    image_exists = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Check whether the pinned Alloy validation image is already available"
    )
    image_pull = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Acquire the pinned Alloy validation image when it is not cached"
    )
    config_validation = next(
        task
        for task in apply_play["tasks"]
        if task.get("name", "").startswith("Validate the candidate config")
    )
    assert image_exists["when"] == "not ansible_check_mode"
    assert "not ansible_check_mode" in image_pull["when"]
    assert "_journal_image_exists.rc | default(1) != 0" in image_pull["when"]
    assert "--pull=never" in config_validation["ansible.builtin.command"]["argv"]
    loki_query = next(
        task
        for task in apply_block["block"]
        if task.get("name") == "Verify the exact bounded journal stream reached Loki"
    )
    assert loki_query["retries"] == 36
    assert loki_query["delay"] == 5
    assert "'&start='" in source  # keep freshness bound part of the Loki range request

    health_poll = next(task for task in apply_block["block"] if task.get("name") == "Read the collector state")
    assert health_poll["retries"] == 60
    assert health_poll["delay"] == 3

    placement = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Place the reviewed monorepo on the receiver through the shared mechanism"
    )
    revision = next(
        task for task in apply_play["tasks"] if task.get("name") == "Read the receiver checkout revision"
    )
    candidate_render = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Render a candidate journal config with the private receiver endpoint"
    )
    config_promote = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Promote the validated candidate config for the collector"
    )
    for task in (placement, revision, candidate_render, config_promote):
        assert task["when"] == "not ansible_check_mode"

    stop_play = next(
        play for play in plays if play.get("name", "").startswith("Stop only the journal collector")
    )
    stop_gate = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Leave the stop play when the reviewed action is survey or apply"
    )

    start_task = next(
        task
        for task in apply_block["block"]
        if task.get("name") == "Start only the journal collector service"
    )
    rollback_state = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Capture collector runtime state and automatic health results before rollback"
    )
    rollback_config = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Capture collector effective health schedule before rollback"
    )
    rollback_action = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Capture collector health failure action before rollback"
    )
    rollback_startup = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Capture collector startup healthcheck presence before rollback"
    )
    rollback_failure = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Fail closed after stopping the journal collector"
    )
    rollback_classifier = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Classify collector health probes without exposing stderr"
    )
    assert "check_mode" not in start_task
    assert start_task["when"] == "not ansible_check_mode"
    evidence_formats = [
        " ".join(task["ansible.builtin.command"]["argv"])
        for task in (rollback_state, rollback_config, rollback_action, rollback_startup)
    ]
    assert all(
        task["check_mode"] is False
        for task in (rollback_state, rollback_config, rollback_action, rollback_startup)
    )
    assert ".State.Health" in evidence_formats[0]
    assert ".FailingStreak" in evidence_formats[0]
    assert ".ExitCode" in evidence_formats[0]
    assert ".Output" not in " ".join(evidence_formats)
    assert ".Config.Healthcheck" in evidence_formats[1]
    assert ".Interval" in evidence_formats[1]
    assert ".Config.HealthcheckOnFailureAction" in evidence_formats[2]
    assert ".Config.StartupHealthCheck" in evidence_formats[3]
    failure_message = rollback_failure["ansible.builtin.fail"]["msg"]
    classifications = rollback_classifier["ansible.builtin.set_fact"]
    assert "container_missing" in classifications["_journal_rollback_state_class"]
    assert "permission_denied" in classifications["_journal_rollback_state_class"]
    assert "state_probe_failed" in classifications["_journal_rollback_state_class"]
    assert "config_probe_failed" in classifications["_journal_rollback_health_config_class"]
    assert "healthcheck_absent" in classifications["_journal_rollback_health_config_class"]
    assert "action_probe_failed" in classifications["_journal_rollback_health_action_class"]
    assert "action_unknown" in classifications["_journal_rollback_health_action_class"]
    assert "startup_probe_failed" in classifications["_journal_rollback_startup_health_class"]
    assert "state/config/action/startup classification=" in failure_message
    assert "Podman state/config/action/startup rc=" in failure_message
    assert "_journal_rollback_state_evidence.stdout" in failure_message
    assert "_journal_rollback_health_config.stdout" in failure_message
    assert "_journal_rollback_health_action.stdout" in failure_message
    assert "_journal_rollback_startup_health.stdout" in failure_message
    assert ".stderr" not in failure_message
    rollback_remove = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Force-remove only the failed collector while retaining positions"
    )
    rollback_volume_capture = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Capture the positions volume name before rollback"
    )
    rollback_volume_readback = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Read back the journal collector positions volume after rollback"
    )
    rollback_readback = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Read back the collector after delivery rollback"
    )
    assert rollback_remove["ansible.builtin.command"]["argv"] == [
        "podman",
        "rm",
        "--force",
        "--time",
        "10",
        "o11y-journal-collector",
    ]
    assert "when" not in rollback_remove
    assert all(
        apply_block["rescue"].index(task) < apply_block["rescue"].index(rollback_volume_capture)
        for task in (rollback_state, rollback_config, rollback_action, rollback_startup)
    )
    assert apply_block["rescue"].index(rollback_volume_capture) < apply_block["rescue"].index(rollback_remove)
    assert apply_block["rescue"].index(rollback_remove) < apply_block["rescue"].index(rollback_volume_readback)
    assert ".Name" in " ".join(rollback_volume_capture["ansible.builtin.command"]["argv"])
    assert "_journal_rollback_volume_name.stdout" in rollback_volume_readback["ansible.builtin.command"]["argv"][3]
    assert rollback_volume_readback["when"][0] == "_journal_rollback_volume_name.rc == 0"
    rollback_verify = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Require the failed pilot container to be absent after rollback"
    )
    assert rollback_readback["ansible.builtin.command"]["argv"][:3] == ["podman", "ps", "--all"]
    assert "_journal_rollback_readback.stdout | trim == ''" in rollback_verify["ansible.builtin.assert"]["that"]
    assert rollback_verify["when"] == "not ansible_check_mode"

    stop_remove = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Force-remove only the pilot container while preserving positions"
    )
    stop_readback = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Read back the stopped pilot container"
    )
    assert stop_remove["ansible.builtin.command"]["argv"] == [
        "podman",
        "rm",
        "--force",
        "--time",
        "10",
        "o11y-journal-collector",
    ]
    assert "not ansible_check_mode" in stop_remove["when"]
    stop_volume_capture = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Read the positions volume state before removing collector"
    )
    volume_readback = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Read back the positions volume after stop"
    )
    assert ".Name" in " ".join(stop_volume_capture["ansible.builtin.command"]["argv"])
    assert stop_play["tasks"].index(stop_gate) < stop_play["tasks"].index(stop_volume_capture)
    assert stop_play["tasks"].index(stop_volume_capture) < stop_play["tasks"].index(stop_remove)
    assert stop_play["tasks"].index(stop_remove) < stop_play["tasks"].index(volume_readback)
    assert stop_readback["ansible.builtin.command"]["argv"][:3] == ["podman", "ps", "--all"]
    assert stop_readback["when"] == "not ansible_check_mode"
    stop_verify = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Require the stopped pilot container to be absent across host reboots"
    )
    assert "_journal_stop_readback.stdout | trim == ''" in stop_verify["ansible.builtin.assert"]["that"]

    volume_verify = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Require the existing positions volume to survive stop"
    )
    assert volume_readback["ansible.builtin.command"]["argv"][:3] == [
        "podman",
        "volume",
        "exists",
    ]
    assert "_journal_stop_volume_name.stdout" in volume_readback["ansible.builtin.command"]["argv"][3]
    assert volume_verify["ansible.builtin.assert"]["that"] == [
        "_journal_stop_volume_name.stdout | default('') | trim | length > 0",
        "_journal_stop_volume_after.rc | default(1) == 0",
    ]

    rollback_volume = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Require the positions volume to survive collector rollback"
    )
    assert rollback_volume["ansible.builtin.assert"]["that"] == [
        "_journal_rollback_volume_name.stdout | default('') | trim | length > 0",
        "_journal_rollback_volume_readback.rc | default(1) == 0",
    ]


def test_survey_check_mode_skips_container_probe_and_reports_unverified():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    survey_play = next(
        play
        for play in plays
        if play.get("name") == "Survey fixed standard journal directory candidates"
    )
    survey_command = next(
        task
        for task in survey_play["tasks"]
        if task.get("name") == "Survey rootless journal path and exact-name file readability"
    )
    check_mode_result = next(
        task
        for task in survey_play["tasks"]
        if task.get("name") == "Record check-mode survey as unverified without probing the container runtime"
    )

    assert survey_command["when"] == "not ansible_check_mode"
    assert "check_mode" not in survey_command
    assert check_mode_result["when"] == "ansible_check_mode"
    assert check_mode_result["ansible.builtin.set_fact"]["_journal_directory_survey"] == {
        "rc": 0,
        "stdout": '{"result":"check_mode_unverified"}',
    }
    assert "check_mode_unverified" in PLAYBOOK.read_text()


def test_rollback_health_diagnostic_is_allowlisted_and_manual_check_is_gated():
    ansible_playbook = shutil.which("ansible-playbook")
    if not ansible_playbook:
        pytest.skip("ansible-playbook is unavailable")
    first_line = Path(ansible_playbook).read_text(errors="replace").splitlines()[0]
    ansible_python = first_line[2:].strip().split()[-1] if first_line.startswith("#!") else sys.executable
    state_cases = [
        {
            "rc": 125,
            "stderr": "Error: no container with name or ID found",
            "stdout": "",
            "expected": "container_missing",
        },
        {
            "rc": 125,
            "stderr": "permission denied while inspecting container",
            "stdout": "",
            "expected": "permission_denied",
        },
        {"rc": 1, "stderr": "unexpected inspect failure", "stdout": "", "expected": "state_probe_failed"},
        {"rc": 0, "stderr": "", "stdout": "", "expected": "empty_formatted_fields"},
        {
            "rc": 0,
            "stderr": "",
            "stdout": "state=running health=starting failing_streak=0 log_count=0 exit_codes=",
            "expected": "fields_available",
        },
    ]
    config_cases = [
        {"rc": 125, "stdout": "", "expected": "config_probe_failed"},
        {"rc": 0, "stdout": "", "expected": "empty_formatted_fields"},
        {
            "rc": 0,
            "stdout": (
                "healthcheck_present=absent test_kind=none interval=unavailable timeout=unavailable "
                "retries=unavailable start_period=unavailable"
            ),
            "expected": "healthcheck_absent",
        },
        {
            "rc": 0,
            "stdout": (
                "healthcheck_present=present test_kind=CMD-SHELL interval=15s timeout=5s "
                "retries=8 start_period=20s"
            ),
            "expected": "fields_available",
        },
    ]
    action_cases = [
        {"rc": 125, "stdout": "", "expected": "action_probe_failed"},
        {"rc": 0, "stdout": "", "expected": "empty_formatted_fields"},
        {"rc": 0, "stdout": "on_failure=unknown", "expected": "action_unknown"},
        {"rc": 0, "stdout": "on_failure=none", "expected": "fields_available"},
    ]
    startup_cases = [
        {"rc": 125, "stdout": "", "expected": "startup_probe_failed"},
        {"rc": 0, "stdout": "", "expected": "empty_formatted_fields"},
        {"rc": 0, "stdout": "startup_check=absent", "expected": "fields_available"},
        {"rc": 0, "stdout": "startup_check=present", "expected": "fields_available"},
    ]
    gate_cases = [
        {
            "state": {"rc": 0, "stdout": "state=running health=starting"},
            "config": {"rc": 0, "stdout": "healthcheck_present=present interval=15s"},
            "action": {"rc": 0, "stdout": "on_failure=none"},
            "expected": True,
        },
        {
            "state": {"rc": 0, "stdout": "state=running health=starting"},
            "config": {"rc": 0, "stdout": "healthcheck_present=present interval=15s"},
            "action": {"rc": 0, "stdout": "on_failure=restart"},
            "expected": False,
        },
        {
            "state": {"rc": 0, "stdout": "state=stopped health=starting"},
            "config": {"rc": 0, "stdout": "healthcheck_present=present interval=15s"},
            "action": {"rc": 0, "stdout": "on_failure=none"},
            "expected": False,
        },
        {
            "state": {"rc": 0, "stdout": "state=running health=starting"},
            "config": {"rc": 0, "stdout": "healthcheck_present=absent interval=unavailable"},
            "action": {"rc": 0, "stdout": "on_failure=none"},
            "expected": False,
        },
        {
            "state": {"rc": 0, "stdout": "state=running health=starting"},
            "config": {"rc": 125, "stdout": ""},
            "action": {"rc": 0, "stdout": "on_failure=none"},
            "expected": False,
        },
        {
            "state": {"rc": 0, "stdout": "state=running health=starting"},
            "config": {"rc": 0, "stdout": "healthcheck_present=present interval=15s"},
            "action": {"rc": 125, "stdout": ""},
            "expected": False,
        },
    ]
    manual_cases = [
        {"allowed": True, "rc": 0, "expected": "passed"},
        {"allowed": True, "rc": 1, "expected": "failed"},
        {"allowed": True, "rc": 125, "expected": "podman_error"},
        {"allowed": True, "rc": 42, "expected": "unexpected_error"},
        {"allowed": False, "expected": "not_run"},
    ]
    harness = r'''
import json, sys, yaml
from ansible.template import Templar, trust_as_template
from ansible.parsing.dataloader import DataLoader
plays = yaml.safe_load(open(sys.argv[1]))
tasks = next(play for play in plays if play.get("name") == "Deploy the bounded journal collector")["tasks"]
apply = next(task for task in tasks if task.get("name") == "Apply the collector and require exact-target Loki delivery")
state = next(
    task for task in apply["rescue"]
    if task.get("name") == "Capture collector runtime state and automatic health results before rollback"
)
config = next(
    task for task in apply["rescue"]
    if task.get("name") == "Capture collector effective health schedule before rollback"
)
action = next(
    task for task in apply["rescue"]
    if task.get("name") == "Capture collector health failure action before rollback"
)
startup = next(
    task for task in apply["rescue"]
    if task.get("name") == "Capture collector startup healthcheck presence before rollback"
)
classifier = next(
    task for task in apply["rescue"]
    if task.get("name") == "Classify collector health probes without exposing stderr"
)
manual = next(
    task for task in apply["rescue"]
    if task.get("name") == "Run one output-discarded manual healthcheck for bounded diagnosis"
)
manual_classifier = next(
    task for task in apply["rescue"]
    if task.get("name") == "Classify the one-shot manual healthcheck result"
)
postcheck = next(
    task for task in apply["rescue"]
    if task.get("name") == "Capture sanitized state and health log count after the manual check"
)
remove = next(
    task for task in apply["rescue"]
    if task.get("name") == "Force-remove only the failed collector while retaining positions"
)
render_format = lambda task: Templar(loader=DataLoader(), variables={}).template(
    trust_as_template(task["ansible.builtin.command"]["argv"][3])
)
rendered_formats = {
    "state": render_format(state),
    "config": render_format(config),
    "action": render_format(action),
    "startup": render_format(startup),
}
expressions = classifier["ansible.builtin.set_fact"]
gate_expression = next(
    task for task in apply["rescue"]
    if task.get("name") == "Decide whether one manual healthcheck is safe as diagnostic evidence"
)["ansible.builtin.set_fact"]["_journal_rollback_manual_healthcheck_allowed"]
manual_expression = manual_classifier["ansible.builtin.set_fact"]["_journal_manual_healthcheck_class"]
postcheck_format_expression = postcheck["ansible.builtin.command"]["argv"][3]
rendered_postcheck_format = Templar(loader=DataLoader(), variables={}).template(
    trust_as_template(postcheck_format_expression)
)
manual_argv = manual["ansible.builtin.command"]["argv"]
manual_settings = {
    "no_log": manual.get("no_log"),
    "failed_when": manual.get("failed_when"),
    "check_mode": manual.get("check_mode"),
    "when": manual.get("when"),
}
data = json.loads(sys.stdin.read())
classes = []
for case in data["classifier_cases"]:
    variables = {"_journal_rollback_state_evidence": {key: case[key] for key in ("rc", "stderr", "stdout")}}
    result = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_state_class"])
    ).strip()
    classes.append(result)
config_classes = []
for case in data["config_cases"]:
    variables = {"_journal_rollback_health_config": case}
    result = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_health_config_class"])
    ).strip()
    config_classes.append(result)
action_classes = []
for case in data["action_cases"]:
    variables = {"_journal_rollback_health_action": case}
    result = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_health_action_class"])
    ).strip()
    action_classes.append(result)
startup_classes = []
for case in data["startup_cases"]:
    variables = {"_journal_rollback_startup_health": case}
    result = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_startup_health_class"])
    ).strip()
    startup_classes.append(result)
gate_results = []
for case in data["gate_cases"]:
    variables = {
        "_journal_rollback_state_evidence": case["state"],
        "_journal_rollback_health_config": case["config"],
        "_journal_rollback_health_action": case["action"],
    }
    result = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(gate_expression))
    gate_results.append(str(result).lower() == "true")
manual_results = []
for case in data["manual_cases"]:
    variables = {
        "_journal_rollback_manual_healthcheck_allowed": case["allowed"],
        "_journal_manual_healthcheck": {"rc": case.get("rc", 125)},
    }
    result = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(manual_expression)).strip()
    manual_results.append(result)
print(json.dumps({
    "formats": rendered_formats,
    "classes": classes,
    "config_classes": config_classes,
    "action_classes": action_classes,
    "startup_classes": startup_classes,
    "gate_results": gate_results,
    "manual_results": manual_results,
    "manual_argv": manual_argv,
    "manual_settings": manual_settings,
    "postcheck_format": rendered_postcheck_format,
    "manual_precedes_removal": apply["rescue"].index(manual) < apply["rescue"].index(remove),
}))
'''
    temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="o11y-journal-templar-", dir=temp_root) as ansible_tmp:
        env = os.environ.copy() | {
            "ANSIBLE_LOCAL_TEMP": ansible_tmp,
            "ANSIBLE_REMOTE_TEMP": ansible_tmp,
        }
        result = subprocess.run(
            [ansible_python, "-c", harness, str(PLAYBOOK)],
            cwd=ROOT,
            env=env,
            input=json.dumps(
                {
                    "classifier_cases": state_cases,
                    "config_cases": config_cases,
                    "action_cases": action_cases,
                    "startup_cases": startup_cases,
                    "gate_cases": gate_cases,
                    "manual_cases": manual_cases,
                }
            ),
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert rendered["formats"] == {
        "state": (
        '{{printf "state=%s " .State.Status}}'
        "{{with .State.Health}}health={{.Status}} failing_streak={{.FailingStreak}} "
        "log_count={{len .Log}} exit_codes={{range .Log}}{{.ExitCode}},{{end}}"
        "{{else}}health=unavailable failing_streak=unavailable log_count=0 exit_codes=unavailable{{end}}"
        ),
        "config": (
        "{{with .Config.Healthcheck}}{{if .Test}}{{if eq (index .Test 0) \"NONE\"}}"
        "healthcheck_present=absent test_kind=none interval=unavailable timeout=unavailable "
        "retries=unavailable start_period=unavailable{{else}}healthcheck_present=present test_kind="
        "{{if eq (index .Test 0) \"CMD\"}}CMD{{else if eq (index .Test 0) \"CMD-SHELL\"}}CMD-SHELL"
        "{{else}}other{{end}} interval={{.Interval}} timeout={{.Timeout}} retries={{.Retries}} "
        "start_period={{.StartPeriod}}{{end}}{{else}}healthcheck_present=absent test_kind=none "
        "interval=unavailable timeout=unavailable retries=unavailable start_period=unavailable{{end}}"
        "{{else}}healthcheck_present=absent test_kind=none interval=unavailable timeout=unavailable "
        "retries=unavailable start_period=unavailable{{end}}"
        ),
        "action": (
        "{{if eq .Config.HealthcheckOnFailureAction \"none\"}}on_failure=none"
        "{{else if eq .Config.HealthcheckOnFailureAction \"restart\"}}on_failure=restart"
        "{{else if eq .Config.HealthcheckOnFailureAction \"stop\"}}on_failure=stop"
        "{{else if eq .Config.HealthcheckOnFailureAction \"kill\"}}on_failure=kill"
        "{{else}}on_failure=unknown{{end}}"
        ),
        "startup": "{{if .Config.StartupHealthCheck}}startup_check=present{{else}}startup_check=absent{{end}}",
    }
    assert rendered["classes"] == [case["expected"] for case in state_cases]
    assert rendered["config_classes"] == [case["expected"] for case in config_cases]
    assert rendered["action_classes"] == [case["expected"] for case in action_cases]
    assert rendered["startup_classes"] == [case["expected"] for case in startup_cases]
    assert rendered["gate_results"] == [case["expected"] for case in gate_cases]
    assert rendered["manual_results"] == [case["expected"] for case in manual_cases]
    assert rendered["manual_argv"] == [
        "/bin/bash",
        "-c",
        "podman healthcheck run o11y-journal-collector >/dev/null 2>&1",
    ]
    assert rendered["manual_settings"] == {
        "no_log": None,
        "failed_when": False,
        "check_mode": False,
        "when": "_journal_rollback_manual_healthcheck_allowed | bool",
    }
    assert rendered["postcheck_format"] == (
        "{{.State.Status}} {{with .State.Health}}{{.Status}} {{len .Log}}"
        "{{else}}unavailable 0{{end}}"
    )
    assert rendered["manual_precedes_removal"] is True
    assert result.stderr == ""


def test_semaphore_template_is_dev_bound_and_requires_exact_sha():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(
        item for item in templates if item["name"] == "Deploy o11y Journal Collector (Dev)"
    )

    assert template["playbook"] == "platform/playbooks/deploy-o11y-journal-collector.yml"
    assert template["repository"] == "agent-cloud dev"
    variables = {item["name"]: item for item in template["survey_vars"]}
    assert variables["expected_repository_sha"]["required"] is True
    assert variables["service_branch"]["default_value"] == "dev"
    assert variables["journal_collector_action"]["default_value"] == "survey"
    assert variables["journal_collector_action"]["values"] == [
        {"name": "Survey journal paths", "value": "survey"},
        {"name": "Apply pilot", "value": "apply"},
        {"name": "Stop pilot", "value": "stop"},
    ]
