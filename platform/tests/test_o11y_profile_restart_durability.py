import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from ansible_python import ansible_python, needs_ansible

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "platform/playbooks/files"
OP_PATH = FILES / "restart-o11y-pyroscope-durability.py"
VALIDATOR_PATH = FILES / "validate-o11y-profile-acceptance-task.py"
PLAYBOOK_PATH = ROOT / "platform/playbooks/restart-o11y-pyroscope-durability.yml"
TEMPLATE_PATH = ROOT / "platform/semaphore/templates.yml"
ALLOY_TEMPLATE_PATH = ROOT / "platform/services/o11y/deployment/templates/config.alloy.j2"


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


operation = _module("o11y_profile_restart_durability", OP_PATH)
validator = _module("validate_o11y_profile_acceptance_task", VALIDATOR_PATH)
pilot = _module("o11y_profile_pilot_operation", FILES / "o11y-profile-pilot-operation.py")

TASK_ID = 5854
PROJECT_ID = 2
ACCEPTANCE_SHA = "8cf5b90a2ca0b7362933fcba68a2b026ac1bf1ad"
RECEIVER_SHA = "23cc35595ca1aa0358f409920f40a48a80a6a100"
PRIVATE_SHA = "c" * 40
WINDOW_START = 1792055856
WINDOW_END = 1792056120


def _acceptance_fixture(receiver_sha=RECEIVER_SHA):
    task = {
        "id": TASK_ID, "project_id": PROJECT_ID, "template_id": 9, "status": "success",
        "commit_hash": ACCEPTANCE_SHA, "created": "2026-10-15T09:17:30Z",
        "start": "2026-10-15T09:17:36.844212Z", "end": "2026-10-15T09:21:59.271931Z",
        "playbook": "", "arguments": None, "git_branch": None, "limit": "", "secret": "",
        "schedule_id": None, "workflow_run_id": None,
        "environment": json.dumps({
            "expected_repository_sha": ACCEPTANCE_SHA, "expected_receiver_sha": receiver_sha,
        }),
    }
    template = {
        "id": 9, "project_id": PROJECT_ID, "name": "Accept o11y Profile Pilot (Dev)",
        "playbook": "platform/playbooks/accept-o11y-profile-pilot.yml",
        "repository_id": 10, "inventory_id": 11,
        "app": "ansible", "allow_override_args_in_task": False, "task_params": {},
    }
    repository = {
        "id": 10, "project_id": PROJECT_ID, "name": "agent-cloud dev",
        "git_url": "https://github.com/uhstray-io/agent-cloud.git", "git_branch": "dev",
    }
    receipt = {
        "schema_version": 1, "status": "verified",
        "reason": "sampled_delivered_restored_post_disable_query_observed",
        "service": "o11y/alloy", "sampling": "observed", "delivery": "observed",
        "restored": "disabled", "post_disable_query": "observed",
        "disabled_config_sha256": "d" * 64, "enabled_config_sha256": "e" * 64,
        "receiver_sha": receiver_sha, "public_sha": ACCEPTANCE_SHA, "private_sha": PRIVATE_SHA,
        "inventory_provenance": "exact_bytes_match", "candidate": "validated",
        "resource_gate": "passed", "profile_config_state_after_run": "disabled",
    }
    output = [
        {"id": 0, "task_id": TASK_ID, "stage_id": None,
         "time": "2026-10-15T09:17:35.500000Z", "output": "TASK [Report fixed receipt] ***"},
        {"id": 0, "task_id": TASK_ID, "stage_id": None,
         "time": "2026-10-15T09:17:35.800000Z", "output": "ok: [localhost] => {"},
        {"id": 0, "task_id": TASK_ID, "stage_id": None,
         "time": "2026-10-15T09:21:00Z",
         "output": '  "msg": ' + json.dumps(json.dumps(receipt))},
        {"id": 0, "task_id": TASK_ID, "stage_id": None,
         "time": "2026-10-15T09:21:01Z", "output": "}"},
    ]
    return {
        "task_id": TASK_ID, "project_id": PROJECT_ID, "expected_receiver_sha": receiver_sha,
        "expected_acceptance_sha": ACCEPTANCE_SHA,
        "task": task, "output": output, "template": template, "repository": repository,
    }


def _operation_fixture(tmp_path):
    config_path = tmp_path / "deployment" / "config" / "config.alloy"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("disabled config")
    return {
        "config_path": str(config_path), "receiver_sha": RECEIVER_SHA,
        "disabled": "disabled config",
        "acceptance": {
            "schema_version": 1, "status": "verified",
            "reason": "acceptance_task_receipt_verified", "task_id": TASK_ID,
            "acceptance_sha": ACCEPTANCE_SHA, "receiver_sha": RECEIVER_SHA,
            "window_start": WINDOW_START, "window_end": WINDOW_END,
        },
    }


def test_acceptance_binds_success_exact_revision_and_task_window():
    payload = _acceptance_fixture()
    result = validator.evaluate(payload)

    assert result == {
        "schema_version": 1, "status": "verified", "reason": "acceptance_task_receipt_verified",
        "task_id": TASK_ID, "acceptance_sha": ACCEPTANCE_SHA, "receiver_sha": RECEIVER_SHA,
        "window_start": WINDOW_START, "window_end": WINDOW_END,
    }


def test_acceptance_parses_split_receipt_with_ansi_sgr_colors():
    payload = _acceptance_fixture()
    for row in payload["output"]:
        row["output"] = f"\x1b[0m{row['output']}\x1b[31;1m"

    assert validator.evaluate(payload)["status"] == "verified"


def test_acceptance_does_not_strip_malformed_ansi_color_sequence():
    payload = _acceptance_fixture()
    payload["output"][2]["output"] = payload["output"][2]["output"].replace(
        '"msg"', '\x1b[31X"msg"', 1
    )

    assert validator.evaluate(payload)["reason"] == "acceptance_receipt_ambiguous"


def test_acceptance_accepts_null_and_exact_explicit_real_run_params():
    payload = _acceptance_fixture()
    payload["task"]["params"] = None
    assert validator.evaluate(payload)["status"] == "verified"

    payload = _acceptance_fixture()
    payload["task"]["params"] = {"dry_run": False, "diff": False}

    assert validator.evaluate(payload)["status"] == "verified"


def test_acceptance_binds_receiver_sha_without_pinning_one_revision():
    payload = _acceptance_fixture("a" * 40)

    assert validator.evaluate(payload)["status"] == "verified"


def test_acceptance_refuses_receiver_sha_mismatch():
    payload = _acceptance_fixture()
    payload["expected_receiver_sha"] = "a" * 40

    assert validator.evaluate(payload)["reason"] == "acceptance_launch_mismatch"


def test_acceptance_accepts_strictly_increasing_positive_output_ids():
    payload = _acceptance_fixture()
    for row, row_id in zip(payload["output"], range(7, 11), strict=True):
        row["id"] = row_id
    payload["output"].append({
        "id": 11, "task_id": TASK_ID, "stage_id": None,
        "time": "2026-10-15T09:21:01Z", "output": "task complete",
    })

    assert validator.evaluate(payload)["status"] == "verified"


def _append_positive_id_row(payload):
    payload["output"].append({
        "id": 1, "task_id": TASK_ID, "stage_id": None,
        "time": "2026-10-15T09:21:01Z", "output": "task complete",
    })


def _append_zero_id_row_after_positive(payload):
    payload["output"][0]["id"] = 1
    payload["output"].append({
        "id": 0, "task_id": TASK_ID, "stage_id": None,
        "time": "2026-10-15T09:21:01Z", "output": "task complete",
    })


def _append_duplicate_receipt(payload):
    receipt_line = payload["output"][2]["output"]
    payload["output"].extend([
        {"id": 0, "task_id": TASK_ID, "stage_id": None,
         "time": "2026-10-15T09:21:02Z", "output": "ok: [localhost] => {"},
        {"id": 0, "task_id": TASK_ID, "stage_id": None,
         "time": "2026-10-15T09:21:03Z", "output": receipt_line},
        {"id": 0, "task_id": TASK_ID, "stage_id": None,
         "time": "2026-10-15T09:21:04Z", "output": "}"},
    ])


@pytest.mark.parametrize("mutate", [
    lambda payload: payload["task"].update(status="failed"),
    lambda payload: payload["task"].update(commit_hash="f" * 40),
    lambda payload: payload["task"].update(params={"dry_run": True, "diff": False}),
    lambda payload: payload["task"].update(params={"dry_run": False, "diff": False, "limit": "all"}),
    lambda payload: payload["task"].update(template_id=99),
    lambda payload: payload["repository"].update(git_branch="main"),
    lambda payload: payload["output"][0].update(task_id=77),
    lambda payload: payload["output"][0].update(id="0"),
    lambda payload: payload["output"][0].update(id=False),
    lambda payload: payload["output"][0].update(id=-1),
    _append_positive_id_row,
    _append_zero_id_row_after_positive,
    _append_duplicate_receipt,
    lambda payload: payload["output"][0].update(time="2026-10-15T09:15:00Z"),
    lambda payload: payload["output"][2].update(output="no receipt"),
])
def test_acceptance_refuses_unbound_or_unsuccessful_task(mutate):
    payload = _acceptance_fixture()
    mutate(payload)

    assert validator.evaluate(payload)["status"] == "unavailable"


def test_zero_output_ids_still_require_monotonic_timestamps():
    payload = _acceptance_fixture()
    payload["output"].append({
        "id": 0, "task_id": TASK_ID, "stage_id": None,
        "time": "2026-10-15T09:20:59Z", "output": "out of order",
    })

    assert validator.evaluate(payload)["reason"] == "acceptance_output_time_invalid"


def test_output_rows_may_precede_task_start_but_not_task_creation():
    payload = _acceptance_fixture()
    payload["output"][0]["time"] = "2026-10-15T09:17:29Z"

    assert validator.evaluate(payload)["reason"] == "acceptance_output_time_invalid"


def test_receipt_completed_before_task_start_is_refused():
    payload = _acceptance_fixture()
    for row, timestamp in zip(payload["output"], [
        "2026-10-15T09:17:31Z", "2026-10-15T09:17:32Z",
        "2026-10-15T09:17:33Z", "2026-10-15T09:17:34Z",
    ], strict=True):
        row["time"] = timestamp

    assert validator.evaluate(payload)["reason"] == "acceptance_output_time_invalid"


def test_receipt_marker_before_task_start_is_refused_even_if_wrapper_closes_later():
    payload = _acceptance_fixture()
    payload["output"][2]["time"] = "2026-10-15T09:17:36Z"
    payload["output"][3]["time"] = "2026-10-15T09:17:37Z"

    assert validator.evaluate(payload)["reason"] == "acceptance_output_time_invalid"


def _mock_snapshots(monkeypatch, config_path, *, changed=False):
    states = [
        {"pyroscope_id": "pyro-id", "pyroscope_volume": "pyroscope-data"},
        {"pyroscope_id": "other-id" if changed else "pyro-id", "pyroscope_volume": "pyroscope-data"},
    ]
    digests = iter(("containers-before", "containers-after" if changed else "containers-before"))
    volumes = iter((("volumes-before", {"pyroscope-data"}), ("volumes-before", {"pyroscope-data"})))
    monkeypatch.setattr(operation, "_pilot_module", lambda: pilot)
    monkeypatch.setattr(pilot, "_read_config", lambda _path: (config_path.read_bytes(), (1, 2)))
    monkeypatch.setattr(pilot, "_container_snapshot", lambda *_args: (next(digests), states.pop(0)))
    monkeypatch.setattr(pilot, "_volume_snapshot", lambda *_args: next(volumes))


def _runner(*, restart_failure=False, query_failure=False):
    calls = []
    query = json.dumps({"timeline": {"startTime": WINDOW_START, "durationDelta": 60, "samples": [2, 0]}}).encode()

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["podman", "restart"] and restart_failure:
            return subprocess.CompletedProcess(argv, 1, b"", b"private runtime detail")
        if argv[:2] == ["podman", "restart"]:
            return subprocess.CompletedProcess(argv, 0, b"o11y-pyroscope", b"")
        if "http://pyroscope:4040/ready" in argv:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if any("http://pyroscope:4040/pyroscope/render?" in arg for arg in argv):
            body = b"{}" if query_failure else query
            return subprocess.CompletedProcess(argv, 0, body, b"")
        raise AssertionError(argv)

    return run, calls


def test_invalid_disabled_config_refuses_before_pyroscope_restart(monkeypatch, tmp_path):
    payload = _operation_fixture(tmp_path)
    payload["disabled"] = "different config"
    _mock_snapshots(monkeypatch, Path(payload["config_path"]))
    runner, calls = _runner()

    result = operation.evaluate(payload, runner=runner, sleep_fn=lambda _seconds: None)

    assert result["reason"] == "config_not_disabled"
    assert not any(call[:2] == ["podman", "restart"] for call in calls)


def test_missing_historical_sample_refuses_without_restart(monkeypatch, tmp_path):
    payload = _operation_fixture(tmp_path)
    _mock_snapshots(monkeypatch, Path(payload["config_path"]))
    runner, calls = _runner(query_failure=True)

    result = operation.evaluate(payload, runner=runner, sleep_fn=lambda _seconds: None)

    assert result["reason"] == "profile_not_queryable_before_restart"
    assert not any(call[:2] == ["podman", "restart"] for call in calls)


def test_unverified_acceptance_task_refuses_before_any_receiver_command(monkeypatch, tmp_path):
    payload = _operation_fixture(tmp_path)
    payload["acceptance"]["status"] = "unavailable"
    _mock_snapshots(monkeypatch, Path(payload["config_path"]))
    runner, calls = _runner()

    result = operation.evaluate(payload, runner=runner, sleep_fn=lambda _seconds: None)

    assert result["reason"] == "invalid_input"
    assert calls == []


def test_restart_failure_is_sanitized_and_stops_before_post_restart_query(monkeypatch, tmp_path):
    payload = _operation_fixture(tmp_path)
    _mock_snapshots(monkeypatch, Path(payload["config_path"]))
    runner, calls = _runner(restart_failure=True)

    result = operation.evaluate(payload, runner=runner, sleep_fn=lambda _seconds: None)

    assert result["reason"] == "restart_failed"
    assert "private runtime detail" not in json.dumps(result)
    assert sum(any("/pyroscope/render?" in arg for arg in call) for call in calls) == 1


def test_receiver_play_vars_render_the_real_disabled_alloy_template():
    from jinja2 import Environment, StrictUndefined

    plays = yaml.safe_load(PLAYBOOK_PATH.read_text())
    receiver_play = next(play for play in plays if play.get("hosts") == "o11y_svc")
    assert receiver_play["vars_files"] == ["vars/o11y-metric-labels.yml"]
    play_vars = {}
    for relative_path in receiver_play["vars_files"]:
        play_vars.update(yaml.safe_load((PLAYBOOK_PATH.parent / relative_path).read_text()))
    render_task = next(
        child for task in receiver_play["tasks"] for child in task.get("block", [])
        if child.get("name") == "Render the disabled profile candidate"
    )
    template_path = (PLAYBOOK_PATH.parent / render_task["ansible.builtin.template"]["src"].replace(
        "{{ playbook_dir }}/", ""
    )).resolve()
    assert template_path == ALLOY_TEMPLATE_PATH.resolve()
    template = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    template.filters["bool"] = bool
    template.filters["to_json"] = json.dumps

    rendered = template.from_string(template_path.read_text()).render(
        **play_vars, **receiver_play["vars"], **render_task["vars"],
        local_mode=False, o11y_cluster="test-cluster", o11y_gateway_span_logs_enabled=False,
    )

    assert f"regex  = {json.dumps(play_vars['_o11y_forbidden_metric_label_names_regex'])}" in rendered
    assert 'pyroscope.scrape "alloy_self_profile"' not in rendered


@needs_ansible
def test_receiver_play_assertion_resolves_acceptance_id_from_localhost_hostvars():
    plays = yaml.safe_load(PLAYBOOK_PATH.read_text())
    receiver_play = next(play for play in plays if play.get("hosts") == "o11y_svc")
    assertion = next(
        child for task in receiver_play["tasks"] for child in task.get("block", [])
        if child.get("name") == "Require the closed durability receipt schema"
    )
    expression = next(
        condition for condition in assertion["ansible.builtin.assert"]["that"]
        if "hostvars['localhost']" in condition
    )
    harness = r'''
import json, sys
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
payload = json.load(sys.stdin)
results = []
for acceptance_task_id in payload["ids"]:
    variables = {
        "_durability_receipt": {"acceptance_task_id": acceptance_task_id},
        "hostvars": {"localhost": {"_durability_acceptance": {"task_id": 5854}}},
    }
    results.append(Templar(loader=DataLoader(), variables=variables).evaluate_conditional(
        trust_as_template(payload["expression"])
    ))
print(json.dumps(results))
'''
    result = subprocess.run(
        [ansible_python(), "-c", harness],
        input=json.dumps({"expression": expression, "ids": [5854, 5855]}),
        text=True, capture_output=True, check=False,
        env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(Path("/private/tmp"))},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, False]


def test_success_restarts_only_pyroscope_and_preserves_exact_sample(monkeypatch, tmp_path):
    payload = _operation_fixture(tmp_path)
    _mock_snapshots(monkeypatch, Path(payload["config_path"]))
    runner, calls = _runner()

    result = operation.evaluate(payload, runner=runner, sleep_fn=lambda _seconds: None)

    assert result["status"] == "verified"
    assert result["reason"] == "prior_sample_queryable_after_pyroscope_restart"
    assert result["query_before"] == result["query_after"] == "observed"
    assert result["container_identity"] == result["volume_metadata"] == "preserved"
    assert result["sample_fingerprint_sha256"]
    assert [call for call in calls if call[:2] == ["podman", "restart"]] == [["podman", "restart", "o11y-pyroscope"]]


def test_restart_playbook_is_dev_bound_and_reports_only_a_fixed_receipt():
    plays = yaml.safe_load(PLAYBOOK_PATH.read_text())
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[-1]["hosts"] == "o11y_svc"
    assert "restart-o11y-pyroscope-durability.py" in PLAYBOOK_PATH.read_text()
    commands = [task.get("ansible.builtin.command", {}).get("argv", []) for task in plays[-1]["tasks"]]
    assert all("podman" not in argv for argv in commands)
    operation_task = next(
        child for task in plays[-1]["tasks"] for child in task.get("block", [])
        if child.get("ansible.builtin.command", {}).get("argv", [])[-1:]
        == ["{{ _durability_workspace.path }}/restart.py"]
    )
    assert "restarted == 'completed'" in operation_task["changed_when"]
    operation_block = next(task["block"] for task in plays[-1]["tasks"]
                           if operation_task in task.get("block", []))
    pilot_helper_copy = "{{ playbook_dir }}/files/o11y-profile-pilot-operation.py"
    helper_copy = next(
        index for index, task in enumerate(operation_block)
        if task.get("ansible.builtin.copy", {}).get("src") == pilot_helper_copy
        and task["ansible.builtin.copy"].get("dest")
        == "{{ _durability_workspace.path }}/o11y-profile-pilot-operation.py"
    )
    restart_runner = next(index for index, task in enumerate(operation_block)
                          if task is operation_task)
    assert helper_copy < restart_runner
    templates = yaml.safe_load(TEMPLATE_PATH.read_text())["templates"]
    template = next(row for row in templates if row["name"] == "Prove o11y Pyroscope Restart Durability (Dev)")
    assert template["repository"] == "agent-cloud dev"
    assert template["playbook"] == "platform/playbooks/restart-o11y-pyroscope-durability.yml"
    controller_tasks = plays[2]["tasks"]
    for task in controller_tasks:
        if "ansible.builtin.uri" in task:
            assert task.get("no_log") is True
            assert "loop" not in task
    assert "o11y_profile_acceptance_task_id" not in str(
        plays[2]["vars"]["_durability_internal_names"]
    )
