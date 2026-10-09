"""Safety contract for the bounded receiver-host journald pilot."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_standard_journal_survey_uses_only_fixed_paths_and_no_image_pull():
    source = SURVEY_HELPER.read_text()

    assert 'DIRECTORIES = ("/var/log/journal", "/run/log/journal")' in source
    assert '"--pull=never"' in source
    assert '"--output-fields=CONTAINER_NAME"' in source
    assert "MESSAGE" not in source


def test_compose_mount_is_read_only_and_state_is_separate():
    compose = yaml.safe_load((O11Y / "compose.journal.yml").read_text())
    service = compose["services"]["journal-collector"]

    assert service["image"] == "docker.io/grafana/alloy:v1.5.1"
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
    stop = source.index("Stop only the journal collector after a failed delivery gate")

    assert guard < reviewed < place < validate < apply < verify < stop
    assert "groups.get('o11y_svc', []) | length == 1" in source
    assert "journal_collector_action | default('apply') in ['survey', 'apply', 'stop']" in source
    assert "Require the actual runtime to be rootless" in source
    assert "read -r -N 1 _ < \"$file\"" in source
    assert "expected_repository_sha" in source
    assert "argv: [podman, stop" in source
    assert "podman rm" not in source
    assert "compose down" not in source
    assert "up, --no-deps, -d, journal-collector" in source
    assert "Require all existing receiver containers to remain unchanged" in source
    assert "_journal_verification_start" in source
    assert "values[0][0]" in source
    assert "State.Health.Status" in source

    apply_play = next(play for play in plays if play.get("hosts") == "o11y_svc")
    apply_block = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Apply the collector and require exact-target Loki delivery"
    )
    assert apply_block["when"] == "not ansible_check_mode"
    loki_query = next(
        task
        for task in apply_block["block"]
        if task.get("name") == "Verify the exact bounded journal stream reached Loki"
    )
    assert loki_query["retries"] == 36
    assert loki_query["delay"] == 5
    assert "'&start='" in source  # keep freshness bound part of the Loki range request

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
    stop_action = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Stop only the pilot collector if it is running"
    )
    assert "not ansible_check_mode" in stop_action["when"]

    start_task = next(
        task
        for task in apply_block["block"]
        if task.get("name") == "Start only the journal collector service"
    )
    rollback_task = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Stop only the journal collector after a failed delivery gate"
    )
    assert "check_mode" not in start_task
    assert start_task["when"] == "not ansible_check_mode"
    assert "check_mode" not in rollback_task
    assert rollback_task["when"] == "not ansible_check_mode"


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


def test_semaphore_template_is_dev_bound_and_requires_exact_sha():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(
        item for item in templates if item["name"] == "Deploy o11y Journal Collector (Dev)"
    )

    assert template["playbook"] == "platform/playbooks/deploy-o11y-journal-collector.yml"
    assert template["repository"] == "agent-cloud dev"
    variables = {item["name"]: item for item in template["survey_vars"]}
    assert variables["expected_repository_sha"]["required"] is True
    assert variables["journal_collector_action"]["default_value"] == "survey"
    assert variables["journal_collector_action"]["values"] == [
        {"name": "Survey journal paths", "value": "survey"},
        {"name": "Apply pilot", "value": "apply"},
        {"name": "Stop pilot", "value": "stop"},
    ]
