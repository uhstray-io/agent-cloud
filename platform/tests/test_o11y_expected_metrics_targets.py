import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/summarize-o11y-scrape-baseline.py"
TEMPLATE = ROOT / "platform/services/o11y/deployment/templates/alerts.yml.j2"
SPEC = importlib.util.spec_from_file_location("scrape_baseline", HELPER)
assert SPEC and SPEC.loader
SURVEY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SURVEY)


def _expected(**overrides):
    value = {
        "uid": "o11y_missing_api",
        "job": "api",
        "instance": "api.example.test:9100",
        "service": "api/service",
        "cluster": "prod-west",
        "environment": "prod",
    }
    value.update(overrides)
    return value


def _active(target, health="up"):
    return {"labels": {field: target[field] for field in SURVEY.EXPECTED_IDENTITY_FIELDS}, "health": health}


def _target_document(rows):
    return json.dumps({"status": "success", "data": {"activeTargets": rows}})


def _render(**values):
    env = Environment(trim_blocks=True)
    env.filters["bool"] = bool
    env.filters["to_json"] = json.dumps
    template = env.from_string(TEMPLATE.read_text())
    defaults = {
        "local_mode": False,
        "o11y_alerts_enabled": True,
        "o11y_expected_metrics_targets_reviewed": True,
        "o11y_expected_metrics_targets": [_expected()],
        "o11y_cluster": "prod-west",
        "o11y_environment": "prod",
    }
    defaults.update(values)
    return yaml.safe_load(template.render(**defaults))


def _service_rules(document):
    group = next(group for group in document["groups"] if group["name"] == "service-telemetry")
    return group["rules"]


def test_expected_target_coverage_counts_exact_missing_unexpected_and_unhealthy_identities():
    expected = _expected()
    declaration = {"reviewed": True, "targets": [expected]}

    matched = SURVEY.expected_target_coverage(_target_document([_active(expected)]), declaration)
    assert matched == {
        "status": "observed",
        "reason": "exact_match",
        "expected_count": 1,
        "active_target_count": 1,
        "matched_count": 1,
        "missing_count": 0,
        "unexpected_count": 0,
        "unhealthy_count": 0,
    }

    missing = SURVEY.expected_target_coverage(_target_document([]), declaration)
    assert missing["reason"] == "identity_drift"
    assert (missing["matched_count"], missing["missing_count"], missing["unexpected_count"]) == (0, 1, 0)

    other = _expected(uid="o11y_missing_other", job="other", instance="other:9100", service="other")
    unexpected = SURVEY.expected_target_coverage(_target_document([_active(expected), _active(other)]), declaration)
    assert unexpected["reason"] == "identity_drift"
    assert (unexpected["matched_count"], unexpected["missing_count"], unexpected["unexpected_count"]) == (1, 0, 1)

    unhealthy = SURVEY.expected_target_coverage(_target_document([_active(expected, "down")]), declaration)
    assert unhealthy["reason"] == "unhealthy_targets"
    assert (unhealthy["matched_count"], unhealthy["unhealthy_count"]) == (1, 1)


def test_expected_target_coverage_refuses_ambiguous_and_untrusted_inputs_without_labels():
    expected = _expected()
    declaration = {"reviewed": True, "targets": [expected]}
    ambiguous = SURVEY.expected_target_coverage(
        _target_document([_active(expected), _active(expected)]), declaration
    )
    assert ambiguous["status"] == "unavailable"
    assert ambiguous["reason"] == "ambiguous_target_identity"
    assert "api.example.test" not in json.dumps(ambiguous)

    unreviewed = SURVEY.expected_target_coverage(b"", {"reviewed": False, "targets": [expected]})
    assert unreviewed["reason"] == "declaration_unreviewed"
    empty = SURVEY.expected_target_coverage(b"", {"reviewed": True, "targets": []})
    assert empty["reason"] == "no_expected_targets"
    assert SURVEY.expected_target_coverage(b"not-json", declaration)["reason"] == "malformed_targets"


@pytest.mark.parametrize(
    "targets,retired,other_uids",
    [
        ([_expected(uid="bad.uid")], [], []),
        ([_expected(), _expected(uid="o11y_missing_copy")], [], []),
        ([_expected(), {**_expected(), "uid": "o11y_missing_copy"}], [], []),
        ([_expected(service='api"} or vector(1) #')], [], []),
        ([_expected(instance="api.example.test:9100\\nup")], [], []),
        ([_expected()], ["o11y_missing_api"], []),
        ([_expected()], ["o11y_missing_retired"], ["o11y_missing_retired"]),
        ([], [], []),
        ([], ["unrelated_rule"], []),
    ],
)
def test_expected_target_declaration_refuses_invalid_duplicates_and_uid_collisions(targets, retired, other_uids):
    with pytest.raises(SURVEY.Refusal):
        SURVEY.validate_expected_target_declaration(
            targets, retired, reviewed=True, other_rule_uids=other_uids
        )


def test_validator_cli_reports_only_fixed_refusal_and_hides_input_values():
    private = _expected(service="private-service-value")
    result = subprocess.run(
        ["python3", str(HELPER), "--validate-expected-targets"],
        input=json.dumps({"reviewed": True, "targets": [private], "retired_uids": []}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == '{"reason":"declaration_valid","status":"valid"}\n'
    assert "private-service-value" not in result.stdout + result.stderr


def test_production_gate_local_compatibility_and_five_exact_matchers():
    target = _expected()
    paused = _service_rules(
        _render(o11y_expected_metrics_targets_reviewed=False, o11y_expected_metrics_targets=[target])
    )
    assert all(rule["uid"] != target["uid"] for rule in paused)

    local = _service_rules(
        _render(
            local_mode=True,
            o11y_alerts_enabled=False,
            o11y_expected_metrics_targets_reviewed=False,
            o11y_expected_metrics_targets=[
                {"uid": "o11y_missing_caddy", "service": "caddy-reverse-proxy/caddy", "instance": "caddy:2021"}
            ],
            o11y_cluster="agent-cloud-local",
            o11y_environment="local",
        )
    )
    local_rule = next(rule for rule in local if rule["uid"] == "o11y_missing_caddy")
    assert local_rule["isPaused"] is True
    assert local_rule["data"][0]["model"]["expr"] == (
        'absent_over_time(up{job="caddy",instance="caddy:2021",service="caddy-reverse-proxy/caddy",'
        'cluster="agent-cloud-local",environment="local"}[5m])'
    )

    document = _render(o11y_expected_metrics_targets=[target])
    rule = next(rule for rule in _service_rules(document) if rule["uid"] == target["uid"])
    assert rule["for"] == "2m"
    assert rule["data"][0]["model"]["expr"] == (
        'absent_over_time(up{job="api",instance="api.example.test:9100",service="api/service",'
        'cluster="prod-west",environment="prod"}[5m])'
    )
    assert rule["labels"] == {
        "severity": "critical",
        "job": "api",
        "instance": "api.example.test:9100",
        "service": "api/service",
        "owner": "platform-operations",
        "environment": "prod",
        "cluster": "prod-west",
    }
    assert rule["notification_settings"]["receiver"] == "agent-cloud-ops"


def test_canary_pauses_expected_rules_and_retirement_deletes_only_named_uid():
    target = _expected()
    canary = _service_rules(
        _render(o11y_alert_canary_service="canary-probe", o11y_expected_metrics_targets=[target])
    )
    expected_rule = next(rule for rule in canary if rule["uid"] == target["uid"])
    assert expected_rule["isPaused"] is True
    assert "notification_settings" not in expected_rule

    retired = "o11y_missing_old_api"
    document = _render(
        o11y_alerts_enabled=False,
        o11y_expected_metrics_targets_reviewed=True,
        o11y_expected_metrics_targets=[],
        o11y_expected_metrics_targets_retired_uids=[retired],
    )
    deleted = {entry["uid"] for entry in document["deleteRules"]}
    assert retired in deleted
    assert "o11y_service_down" not in deleted


def test_deploy_preflight_precedes_placement_and_survey_output_is_protected():
    deploy = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text())
    phase = next(play for play in deploy if play.get("name") == "Phase 1: Place repo + manage o11y secrets")
    names = [task.get("name") for task in phase["tasks"]]
    placement = names.index("Place the monorepo + ensure podman/compose")
    validation = names.index("Validate the reviewed expected-target declaration before placement")
    collision = names.index("Refuse expected-target UIDs that collide with provisioned rules")
    assert validation < collision < placement
    assert phase["tasks"][validation]["no_log"] is True
    assert phase["tasks"][collision]["no_log"] is True

    survey_path = ROOT / "platform/playbooks/survey-o11y-expected-metrics-targets.yml"
    survey = yaml.safe_load(survey_path.read_text())
    receiver = next(
        play for play in survey if play.get("name") == "Compare the declaration with active Prometheus targets"
    )
    task = next(
        task
        for task in receiver["tasks"]
        if task.get("name") == "Survey expected-target coverage with private input protected"
    )
    assert task["no_log"] is True
    assert task["when"] == "not ansible_check_mode"
    report = next(
        task
        for task in receiver["tasks"]
        if task.get("name") == "Report expected-target coverage without target labels"
    )
    assert "_expected_target_coverage | to_json" in report["ansible.builtin.debug"]["msg"]
