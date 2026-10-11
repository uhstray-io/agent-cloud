"""Synthetic contract tests for the bounded receiver capability survey."""

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/survey-o11y-receiver-capabilities.py"
PLAYBOOK = ROOT / "platform/playbooks/survey-o11y-receiver-capabilities.yml"
SPEC = importlib.util.spec_from_file_location("o11y_receiver_capabilities", HELPER)
SURVEY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SURVEY)

OBSERVATION_NAMES = {
    "kernel_band", "systemd_band", "cgroup_mode", "receiver_identity",
    "receiver_privilege", "linger", "restart_context", "delegation",
    "container_restart_policy",
}


def _fixture_run(argv):
    command = tuple(argv)
    outputs = {
        ("/usr/bin/uname", "-r"): "6.8.0-privacy-host\n",
        ("/usr/bin/systemctl", "--version"): "systemd 252 (private-build)\n/path/secret 77123\n",
        ("/usr/bin/id", "-un"): "svcuser\n",
        ("/usr/bin/id", "-u"): "1001\n",
        ("/usr/bin/loginctl", "show-user", "svcuser", "--property=Linger"): "Linger=yes\n",
        (
            "/usr/bin/systemctl", "--user", "show", "podman-restart.service",
            "--property=LoadState,ActiveState,UnitFileState",
        ): "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n",
    }
    if command not in outputs:
        raise AssertionError("unexpected fixed probe")
    return outputs[command]


def _report(monkeypatch, run=_fixture_run, mountinfo=None):
    monkeypatch.setattr(SURVEY, "_run", run)
    monkeypatch.setattr(
        SURVEY, "_read_mountinfo",
        lambda: mountinfo or "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
    )
    return SURVEY.collect("svcuser")


def test_collects_only_fixed_categories_and_never_emits_probe_text(monkeypatch):
    calls = []

    def run(argv):
        calls.append(tuple(argv))
        return _fixture_run(argv)

    result = _report(monkeypatch, run=run)
    encoded = json.dumps(result)

    assert set(result) == {"schema_version", "status", "observations", "actions"}
    assert result["status"] == "observed"
    assert set(result["observations"]) == OBSERVATION_NAMES
    assert all(set(item) == {"status", "value", "reason"} for item in result["observations"].values())
    assert all(item["status"] in {"observed", "unknown"} for item in result["observations"].values())
    assert result["observations"]["kernel_band"]["value"] == "6.x"
    assert result["observations"]["systemd_band"]["value"] == "2xx"
    assert result["observations"]["cgroup_mode"]["value"] == "v2"
    assert result["observations"]["restart_context"]["value"] == "enabled_active"
    assert result["actions"] == {"podman_store": "not_called", "service_mutation": "not_called"}
    assert set(calls) == {
        ("/usr/bin/uname", "-r"),
        ("/usr/bin/systemctl", "--version"),
        ("/usr/bin/id", "-un"),
        ("/usr/bin/id", "-u"),
        ("/usr/bin/loginctl", "show-user", "svcuser", "--property=Linger"),
        (
            "/usr/bin/systemctl", "--user", "show", "podman-restart.service",
            "--property=LoadState,ActiveState,UnitFileState",
        ),
    }
    assert all(argv[0] not in {"/usr/bin/podman", "podman", "bao", "vault"} for argv in calls)
    for private_value in ("svcuser", "privacy-host", "private-build", "/path/secret", "77123", "1001"):
        assert private_value not in encoded


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("malformed", "probe_malformed"),
        ("missing", "probe_missing"),
        ("oversized", "probe_oversized"),
        ("timeout", "probe_timeout"),
    ],
)
def test_malformed_missing_oversized_and_timed_out_probes_become_unknown(
    monkeypatch, failure, expected_reason
):
    def run(argv):
        if argv[:2] == ["/usr/bin/uname", "-r"]:
            if failure == "malformed":
                return "not-a-kernel-version /private/host 4412"
            if failure == "missing":
                return ""
            raise SURVEY.ProbeError(expected_reason)
        return _fixture_run(argv)

    report = _report(monkeypatch, run=run)
    item = report["observations"]["kernel_band"]
    assert item == {"status": "unknown", "value": "unknown", "reason": expected_reason}
    assert "/private/host" not in json.dumps(report)
    assert "4412" not in json.dumps(report)


@pytest.mark.parametrize(
    ("program", "expected_reason"),
    [
        ("import time; time.sleep(1)", "probe_timeout"),
        ("import sys; sys.stdout.buffer.write(b'x' * (65536 + 1))", "probe_oversized"),
        ("raise SystemExit(7)", "probe_failed"),
    ],
)
def test_run_enforces_timeout_output_bound_and_exit_status(monkeypatch, program, expected_reason):
    monkeypatch.setattr(SURVEY, "PROBE_TIMEOUT", 0.05)
    with pytest.raises(SURVEY.ProbeError, match=expected_reason):
        SURVEY._run([sys.executable, "-c", program])


def test_identity_mismatch_skips_the_receiver_user_systemd_probe(monkeypatch):
    called = []

    def run(argv):
        called.append(tuple(argv))
        if argv == ["/usr/bin/id", "-un"]:
            return "other-user\n"
        return _fixture_run(argv)

    report = _report(monkeypatch, run=run)
    assert report["observations"]["receiver_identity"]["value"] == "mismatch"
    assert report["observations"]["restart_context"]["reason"] == "not_read"
    assert not any("--user" in argv for argv in called)


def test_invalid_user_produces_a_closed_unknown_report_without_probes(monkeypatch, capsys):
    def unexpected(_argv):
        raise AssertionError("invalid service identity must be rejected before probing")

    monkeypatch.setattr(SURVEY, "_run", unexpected)
    assert SURVEY.main(["invalid/name"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {"schema_version", "status", "observations", "actions"}
    assert set(result["observations"]) == OBSERVATION_NAMES
    assert all(item["status"] == "unknown" for item in result["observations"].values())


def test_playbook_is_dev_bound_receiver_only_check_mode_safe_and_schema_guarded():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(item for item in templates if item["name"] == "Survey o11y Receiver Capabilities (Dev)")
    normal_tasks = plays[-1]["tasks"]
    probe = next(
        task for task in normal_tasks
        if task["name"] == "Read bounded Linux, systemd, cgroup, and receiver-user metadata"
    )
    checkout = next(
        task for task in normal_tasks
        if task["name"] == "Verify the receiver's existing clean Dev checkout"
    )
    fallback = next(
        task for task in normal_tasks
        if task["name"] == "Validate the strict sanitized result and retain categories only"
    )
    unknown_guard = next(
        task for task in normal_tasks
        if task["name"] == "Require a fixed unknown result when collection is unavailable"
    )
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert plays[1]["vars"]["_reviewed_checkout_payload_paths"] == [
        "platform/playbooks/files/reviewed-checkout.py",
        "platform/playbooks/files/survey-o11y-receiver-capabilities.py",
    ]
    assert plays[-1]["hosts"] == "o11y_svc"
    assert probe["ansible.builtin.command"]["argv"] == ["python3", "-", "{{ _receiver_account }}"]
    assert probe["no_log"] is True
    assert probe["when"] == "not ansible_check_mode"
    assert checkout["no_log"] is True
    assert checkout["when"] == "not ansible_check_mode"
    assert "--require-dev-ref" in checkout["ansible.builtin.command"]["argv"]
    assert "hostvars['localhost']._reviewed_checkout_payloads" in plays[-1]["vars"]["_checkout_helper"]
    assert "hostvars['localhost']._reviewed_checkout_payloads" in plays[-1]["vars"]["_receiver_helper"]
    assert "probe_unavailable" in json.dumps(fallback)
    assert "probe_unavailable" in json.dumps(unknown_guard)
    assert SURVEY.MAX_OUTPUT == 64 * 1024
    assert SURVEY.PROBE_TIMEOUT == 4
    assert "check_mode_unverified" in json.dumps(normal_tasks)
    assert "expected_repository_sha" in {item["name"] for item in template["survey_vars"]}
    assert template["repository"] == "agent-cloud dev"
    assert "ansible_user | default('') is match" in json.dumps(plays[3]["tasks"])
    checkout_expression = plays[-1]["vars"]["_receiver_checkout"]
    environment = Environment(undefined=StrictUndefined)
    render_checkout = environment.from_string(checkout_expression).render
    assert render_checkout(local_monorepo_dir="/srv/agent-cloud") == "/srv/agent-cloud"
    assert Path(render_checkout(ansible_user="receiver")).parts[-2:] == ("receiver", "agent-cloud")
    assert Path(render_checkout()).parts[-2:] == ("deploy", "agent-cloud")
    source = HELPER.read_text()
    assert "openbao" not in source.lower()
    assert "shell=True" not in source
    assert "subprocess.Popen(" in source
    assert "verify-dev-source" not in json.dumps(plays)



def _playbook_task(name):
    tasks = yaml.safe_load(PLAYBOOK.read_text())[-1]["tasks"]
    return next(task for task in tasks if task["name"] == name)


def _evaluate_assertions(task, context):
    expressions = task["ansible.builtin.assert"]["that"]
    if isinstance(expressions, str):
        expressions = [expressions]
    environment = Environment(undefined=StrictUndefined)
    return all(environment.compile_expression(expression)(**context) for expression in expressions)


def test_report_validation_fails_closed_and_nonzero_uses_fixed_fallback():
    mapping_guard = _playbook_task("Require the receiver report to be a mapping")
    status_guard = _playbook_task("Require a supported top-level receiver report status")
    observed_guard = _playbook_task("Require the helper's closed report schema")
    tasks = yaml.safe_load(PLAYBOOK.read_text())[-1]["tasks"]
    names = [task["name"] for task in tasks]
    visible_report_index = names.index("Report only the sanitized receiver capability categories")
    for guard in (mapping_guard, status_guard, observed_guard):
        assert names.index(guard["name"]) < visible_report_index

    context = {"_receiver_report": {"schema_version": 1, "status": "unexpected"}}
    assert _evaluate_assertions(mapping_guard, context)
    assert not _evaluate_assertions(status_guard, context)
    assert not _evaluate_assertions(mapping_guard, {"_receiver_report": []})
    report_with_extra_key = {
        "schema_version": 1,
        "status": "observed",
        "observations": {},
        "actions": {"podman_store": "not_called", "service_mutation": "not_called"},
        "extra_sensitive_key": "must-not-print",
    }
    context = {"_receiver_report": report_with_extra_key}
    assert _evaluate_assertions(status_guard, context)
    assert not _evaluate_assertions(observed_guard, context)

    unavailable = _playbook_task("Validate the strict sanitized result and retain categories only")
    expression = unavailable["ansible.builtin.set_fact"]["_receiver_report"]
    environment = Environment(undefined=StrictUndefined)
    environment.filters["from_json"] = json.loads
    fallback = ast.literal_eval(
        environment.from_string(expression).render(
            _receiver_capability_raw={"rc": 1, "stdout": "untrusted command output"},
        )
    )
    context = {"_receiver_report": fallback, "_observation_names": sorted(OBSERVATION_NAMES)}
    unknown_guard = _playbook_task("Require a fixed unknown result when collection is unavailable")
    assert _evaluate_assertions(mapping_guard, context)
    assert _evaluate_assertions(status_guard, context)
    assert _evaluate_assertions(unknown_guard, context)
    assert fallback["reason"] == "probe_unavailable"
