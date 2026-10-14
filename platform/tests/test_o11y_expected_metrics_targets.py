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
    if "_o11y_expected_target_tombstones" not in values:
        tombstones = []
        known = {target["uid"]: target for target in defaults.get("o11y_expected_metrics_targets", [])}
        retired_uids = defaults.get("_o11y_retired_expected_target_uids", []) or []
        for uid in (defaults.get("_o11y_previous_expected_target_uids", []) or []) + retired_uids:
            tombstones.append(SURVEY._as_tombstone(known.get(uid, {"uid": uid}), retired=uid in retired_uids))
        defaults["_o11y_expected_target_tombstones"] = tombstones
    return yaml.safe_load(template.render(**defaults))


def _service_rules(document):
    group = next(group for group in document["groups"] if group["name"] == "service-telemetry")
    return group["rules"]


def _grouped_render(targets, **values):
    groups = SURVEY.expected_target_groups(targets)
    for group in groups:
        group["expr"] = SURVEY.expected_target_group_expression(group)
    values.setdefault("_o11y_expected_target_tombstones",
                      [SURVEY._as_tombstone({"_kind": "target", **target}) for target in targets])
    return _render(
        o11y_expected_metrics_targets=targets,
        _o11y_expected_target_mode="grouped",
        _o11y_expected_target_groups=groups,
        **values,
    )


def _live_rules(document, folder_uid="agent-cloud-folder"):
    result = []
    for rule in _service_rules(document):
        if not rule["uid"].startswith("o11y_missing_"):
            continue
        serialized = json.loads(json.dumps(rule))
        serialized.update(folderUID=folder_uid, ruleGroup="service-telemetry")
        serialized["labels"] = {
            key: value.replace("$$", "$") if isinstance(value, str) else value
            for key, value in serialized["labels"].items()
        }
        for query in serialized["data"]:
            query.setdefault("queryType", "")
            if query["refId"] == "B":
                query.setdefault("relativeTimeRange", {"from": 0, "to": 0})
            model = query["model"]
            model.setdefault("datasource", {"type": query["datasourceUid"], "uid": query["datasourceUid"]})
            model.setdefault("hide", False)
        result.append(serialized)
    return result


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
        ([_expected(uid="o11y_missing_g00000000000000000000000000")], [], []),
        ([_expected(), _expected(uid="o11y_missing_copy")], [], []),
        ([_expected(), {**_expected(), "uid": "o11y_missing_copy"}], [], []),
        ([_expected(service='api"} or vector(1) #')], [], []),
        ([_expected(instance="api.example.test:9100\\nup")], [], []),
        ([_expected()], ["o11y_missing_api"], []),
        ([], ["o11y_missing_g00000000000000000000000000"], []),
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
    parsed = json.loads(result.stdout)
    assert parsed["reason"] == "declaration_valid" and parsed["status"] == "valid"
    assert parsed == {"reason": "declaration_valid", "status": "valid"}
    assert "private-service-value" not in result.stdout + result.stderr


def test_active_expected_target_validator_accepts_the_ansible_stdin_array_shape():
    target = _expected()
    active = _render(o11y_expected_metrics_targets=[target])
    # The Ansible task parses both API responses before to_json serializes stdin.
    ansible_stdin = json.loads(json.dumps({
        "reviewed": True,
        "targets": [target],
        "retired_uids": [],
        "live_rules": _live_rules(active),
        "live_folders": [{"uid": "agent-cloud-folder", "title": "agent-cloud"}],
    }))
    result = subprocess.run(
        ["python3", str(HELPER), "--validate-active-expected-target-rules"],
        input=json.dumps(ansible_stdin),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == '{"active_count":1,"retired_count":0,"status":"validated"}\n'


def test_interrupted_restore_accepts_validated_empty_owner_snapshot():
    empty_rules = _render(o11y_expected_metrics_targets=[])
    result = SURVEY.prepare_expected_target_transition({
        "enabled": False,
        "targets": [],
        "retired_uids": [],
        "previous_rules": yaml.safe_dump(empty_rules),
        "pending_rules": yaml.safe_dump(empty_rules),
        "live_rules": json.dumps(_live_rules(empty_rules)),
        "live_folders": json.dumps([{"uid": "agent-cloud-folder", "title": "agent-cloud"}]),
    })
    assert result["reason"] == "disabled"

    with pytest.raises(SURVEY.Refusal, match="invalid_pending_rules"):
        SURVEY.prepare_expected_target_transition({
            "enabled": False,
            "targets": [],
            "retired_uids": [],
            "previous_rules": yaml.safe_dump(empty_rules),
            "pending_rules": yaml.safe_dump(_render(o11y_expected_metrics_targets=[_expected()])),
            "live_rules": json.dumps([]),
            "live_folders": json.dumps([{"uid": "agent-cloud-folder", "title": "agent-cloud"}]),
        })


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


def test_local_compatibility_transition_returns_the_complete_task_result_schema():
    result = SURVEY.prepare_expected_target_transition({"managed": False})
    assert result == {
        "status": "prepared",
        "reason": "local_compatibility",
        "paused_uids": [],
        "retired_uids": [],
        "tombstones": [],
    }


def test_local_compatibility_cli_result_supplies_every_unconditional_ansible_consumer():
    result = subprocess.run(
        ["python3", str(HELPER), "--prepare-expected-target-transition"],
        input=json.dumps({"managed": False}),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)

    task_file = ROOT / "platform/playbooks/tasks/o11y-prepare-expected-target-transition.yml"
    tasks = yaml.safe_load(task_file.read_text())
    save_task = next(
        task for task in tasks
        if task.get("name") == "Save only the validated previous expected-target UIDs"
    )
    assignments = save_task["ansible.builtin.set_fact"]
    for result_key in ("paused_uids", "retired_uids", "tombstones"):
        assert result_key in payload
        assert any(f").{result_key}" in expression for expression in assignments.values())


def test_canary_pauses_expected_rules_and_retirement_keeps_owned_tombstone():
    target = _expected()
    canary = _service_rules(
        _render(o11y_alert_canary_service="canary-probe", o11y_expected_metrics_targets=[target])
    )
    expected_rule = next(rule for rule in canary if rule["uid"] == target["uid"])
    assert expected_rule["isPaused"] is True
    assert "notification_settings" not in expected_rule

    retired = _transition(_render(o11y_expected_metrics_targets=[_expected()]), enabled=True,
                          targets=[], retired=[_expected()["uid"]])
    document = _render(o11y_expected_metrics_targets=[], _o11y_retired_expected_target_uids=retired["retired_uids"],
                       _o11y_expected_target_tombstones=retired["tombstones"])
    tombstone = next(rule for rule in _service_rules(document) if rule["uid"] == _expected()["uid"])
    assert tombstone["title"] == f"Expected target retired ({target['uid']})"
    assert _expected()["uid"] not in {entry["uid"] for entry in document.get("deleteRules", [])}


def test_grouped_expected_targets_share_one_rule_and_keep_uid_qualified_tombstones():
    first = _expected()
    second = _expected(uid="o11y_missing_api_backup", instance="api-backup.example.test:9100")
    document = _grouped_render([first, second])
    rules = [rule for rule in _service_rules(document) if rule["uid"].startswith("o11y_missing_")]
    active = [rule for rule in rules if not rule["isPaused"]]
    tombstones = [rule for rule in rules if rule["isPaused"]]
    assert len(active) == 1
    group = active[0]
    assert group["uid"].startswith("o11y_missing_g")
    assert group["title"] == f"Missing telemetry group ({group['uid']})"
    grouped = SURVEY.expected_target_groups([first, second])
    assert len(grouped) == 1
    assert group["data"][0]["model"]["expr"] == SURVEY.expected_target_group_expression(grouped[0])
    assert group["labels"]["instance"] == "{{ $$labels.instance }}"
    assert group["labels"]["job"] == "{{ $$labels.job }}"
    live_group = next(rule for rule in _live_rules(document) if rule["uid"] == group["uid"])
    assert live_group["labels"]["instance"] == "{{ $labels.instance }}"
    assert live_group["labels"]["job"] == "{{ $labels.job }}"
    assert group["notification_settings"]["group_by"] == [
        "service", "environment", "cluster", "alertname",
    ]
    assert len(tombstones) == 2
    assert {rule["uid"] for rule in tombstones} == {first["uid"], second["uid"]}
    assert len({rule["title"] for rule in rules}) == len(rules)
    assert all(rule["title"].endswith(f"({rule['uid']})") for rule in tombstones)


def test_expected_group_uids_fail_closed_on_hash_or_target_uid_collision(monkeypatch):
    first = _expected()
    second = _expected(uid="o11y_missing_other", service="api/other")

    class SameDigest:
        def hexdigest(self):
            return "a" * 64

    monkeypatch.setattr(SURVEY.hashlib, "sha256", lambda _value: SameDigest())
    with pytest.raises(SURVEY.Refusal):
        SURVEY.validate_expected_target_declaration([first, second])

    monkeypatch.undo()
    group_uid = SURVEY.expected_target_groups([first])[0]["uid"]
    colliding = {**_expected(), "uid": group_uid}
    with pytest.raises(SURVEY.Refusal):
        SURVEY.validate_expected_target_declaration([colliding])

    class CollisionDigest:
        def hexdigest(self):
            return "b" * 64

    monkeypatch.setattr(SURVEY.hashlib, "sha256", lambda _value: CollisionDigest())
    first = _expected()
    previous_grouped = _grouped_render([first])
    changed_identity = _expected(uid="o11y_missing_new_identity", service="api/other")
    with pytest.raises(SURVEY.Refusal, match="group_uid_collision"):
        _transition(previous_grouped, enabled=True, targets=[changed_identity], mode="grouped")


def test_group_migration_and_reviewed_rollback_keep_both_uid_families_owned():
    targets = [
        _expected(),
        _expected(uid="o11y_missing_api_backup", instance="api-backup.example.test:9100"),
    ]
    legacy = _render(o11y_expected_metrics_targets=targets)
    migration = _transition(legacy, enabled=True, targets=targets, mode="grouped")
    grouped = _grouped_render(
        targets,
        _o11y_expected_target_tombstones=migration["tombstones"],
    )
    group_uid = SURVEY.expected_target_groups(targets)[0]["uid"]
    assert migration["paused_uids"] == sorted(target["uid"] for target in targets)
    assert group_uid in {rule["uid"] for rule in _service_rules(grouped)}
    assert SURVEY.validate_active_expected_target_rules({
        "reviewed": True, "mode": "grouped", "targets": targets, "retired_uids": [],
        "paused_uids": migration["paused_uids"], "tombstones": migration["tombstones"],
        "live_rules": _live_rules(grouped),
        "live_folders": [{"uid": "agent-cloud-folder", "title": "agent-cloud"}],
    })["status"] == "validated"

    rollback = _transition(
        grouped, enabled=True, targets=targets, mode="legacy", group_retired=[group_uid],
    )
    legacy_rollback = _render(
        o11y_expected_metrics_targets=targets,
        _o11y_expected_target_mode="legacy",
        _o11y_previous_expected_target_uids=rollback["paused_uids"],
        _o11y_expected_target_tombstones=rollback["tombstones"],
    )
    rollback_rules = [rule for rule in _service_rules(legacy_rollback)
                      if rule["uid"].startswith("o11y_missing_")]
    assert {rule["uid"] for rule in rollback_rules if rule["isPaused"]} == {group_uid}
    assert {rule["uid"] for rule in rollback_rules if not rule["isPaused"]} == {
        target["uid"] for target in targets
    }
    assert SURVEY.validate_active_expected_target_rules({
        "reviewed": True, "mode": "legacy", "targets": targets, "retired_uids": [],
        "paused_uids": rollback["paused_uids"], "tombstones": rollback["tombstones"],
        "live_rules": _live_rules(legacy_rollback),
        "live_folders": [{"uid": "agent-cloud-folder", "title": "agent-cloud"}],
    })["status"] == "validated"

    # Retry after Grafana has provisioned the explicit group tombstone.
    rollback_retry = _transition(
        legacy_rollback, enabled=True, targets=targets, mode="legacy", group_retired=[group_uid],
    )
    assert group_uid in rollback_retry["paused_uids"]
    assert group_uid in {row["uid"] for row in rollback_retry["tombstones"]}

    with pytest.raises(SURVEY.Refusal, match="group_rollback_not_explicit"):
        _transition(grouped, enabled=True, targets=targets, mode="legacy")
    with pytest.raises(SURVEY.Refusal, match="group_rollback_not_explicit"):
        _transition(
            grouped, enabled=True, targets=targets, mode="legacy",
            group_retired=["o11y_missing_g00000000000000000000000000"],
        )


def test_stopped_grafana_allows_unchanged_grouped_reactivation():
    target = _expected()
    grouped = _grouped_render([target])
    result = _transition(
        grouped, enabled=True, targets=[target], mode="grouped", grafana_running=False, live="",
    )
    assert result["reason"] == "enabled"
    assert result["paused_uids"] == [target["uid"]]

    changed = _expected(instance="changed.example.test:9100")
    with pytest.raises(SURVEY.Refusal, match="stopped_grafana_target_drift"):
        _transition(
            grouped, enabled=True, targets=[changed], mode="grouped", grafana_running=False, live="",
        )


def test_group_rollback_ignores_retired_target_tombstones_for_identity_match():
    active = _expected()
    retired = _expected(uid="o11y_missing_retired", instance="retired.example.test:9100")
    targets = [active, retired]
    grouped = _grouped_render(targets)
    group_uid = SURVEY.expected_target_groups(targets)[0]["uid"]
    retire = _transition(
        grouped, enabled=True, targets=[active], retired=[retired["uid"]], mode="grouped",
    )
    retired_grouped = _grouped_render(
        [active], _o11y_expected_target_tombstones=retire["tombstones"],
    )
    rollback = _transition(
        retired_grouped, enabled=True, targets=[active], retired=[retired["uid"]],
        mode="legacy", group_retired=[group_uid],
    )
    assert retired["uid"] in rollback["retired_uids"]
    assert group_uid in rollback["paused_uids"]


def test_maximum_group_expression_round_trips_and_rejects_over_limit_target_count():
    def max_value(prefix, index):
        suffix = f"{index:03d}"
        return prefix + suffix + "x" * (SURVEY.MAX_IDENTITY_VALUE_LENGTH - len(prefix) - len(suffix))

    targets = [
        _expected(
            uid=f"o11y_missing_t{index:03d}",
            job=max_value("job", index),
            instance=max_value("host", index),
            service="s" * SURVEY.MAX_IDENTITY_VALUE_LENGTH,
            cluster="c" * SURVEY.MAX_IDENTITY_VALUE_LENGTH,
            environment="e" * SURVEY.MAX_IDENTITY_VALUE_LENGTH,
        )
        for index in range(SURVEY.MAX_EXPECTED_TARGETS)
    ]
    grouped = _grouped_render(targets)
    expression = SURVEY.expected_target_groups(targets)[0]
    expression_length = len(SURVEY.expected_target_group_expression(expression))
    assert expression_length == SURVEY.MAX_EXPECTED_GROUP_EXPRESSION_LENGTH
    assert expression_length > 32768
    assert _transition(grouped, enabled=True, targets=targets, mode="grouped")["reason"] == "enabled"

    overlong_rule = next(
        rule for rule in _service_rules(grouped)
        if rule["uid"] == SURVEY.expected_target_groups(targets)[0]["uid"]
    )
    overlong_rule["data"][0]["model"]["expr"] += " "
    with pytest.raises(SURVEY.Refusal, match="invalid_previous_rules"):
        SURVEY._group_from_rule(overlong_rule)

    with pytest.raises(SURVEY.Refusal, match="invalid_declaration"):
        SURVEY.validate_expected_target_declaration([*targets, _expected(uid="o11y_missing_over")])


def test_grouped_migration_accepts_the_dev_per_target_summary_contract():
    target = _expected()
    previous = _render(o11y_expected_metrics_targets=[target])
    legacy_rule = next(rule for rule in _service_rules(previous) if rule["uid"] == target["uid"])
    legacy_rule["annotations"]["summary"] = f"Expected metrics have disappeared for {target['service']}."

    migration = _transition(previous, enabled=True, targets=[target], mode="grouped")
    assert migration["paused_uids"] == [target["uid"]]
    assert migration["tombstones"][0]["uid"] == target["uid"]


def test_target_uid_with_group_prefix_remains_a_per_target_owner():
    target = _expected(uid="o11y_missing_gateway")
    previous = _render(o11y_expected_metrics_targets=[target])
    migration = _transition(previous, enabled=True, targets=[target], mode="grouped")
    tombstone = next(row for row in migration["tombstones"] if row["uid"] == target["uid"])
    assert tombstone["labels"]["job"] == target["job"]
    assert tombstone["labels"]["instance"] == target["instance"]


def test_interrupted_grouped_transition_accepts_live_pending_grouped_ownership():
    targets = [_expected(), _expected(uid="o11y_missing_api_backup", instance="api-backup.example.test:9100")]
    previous = _render(o11y_expected_metrics_targets=targets)
    pending = _grouped_render(targets)
    prepared = _transition(
        previous, pending=pending, enabled=True, targets=targets, mode="grouped",
        live=_live_rules(pending),
    )
    assert set(prepared["paused_uids"]) == {target["uid"] for target in targets}


def test_grouped_live_readback_refuses_rule_contract_drift():
    targets = [_expected(), _expected(uid="o11y_missing_api_backup", instance="api-backup.example.test:9100")]
    document = _grouped_render(targets)
    input_document = {
        "reviewed": True,
        "mode": "grouped",
        "targets": targets,
        "retired_uids": [],
        "paused_uids": [target["uid"] for target in targets],
        "tombstones": [SURVEY._as_tombstone({"_kind": "target", **target}) for target in targets],
        "live_rules": _live_rules(document),
        "live_folders": [{"uid": "agent-cloud-folder", "title": "agent-cloud"}],
    }
    for mutate in (
        lambda rule: rule["labels"].update(instance="wrong-instance:9100"),
        lambda rule: rule["annotations"].update(summary="wrong summary"),
        lambda rule: rule.update(**{"for": "3m"}),
        lambda rule: rule.update(isPaused=True),
        lambda rule: rule["notification_settings"].update(group_by=["service", "alertname"]),
    ):
        drifted = json.loads(json.dumps(input_document))
        group_rule = next(rule for rule in drifted["live_rules"] if rule["uid"].startswith("o11y_missing_g"))
        mutate(group_rule)
        with pytest.raises(SURVEY.Refusal, match="live_target_drift"):
            SURVEY.validate_active_expected_target_rules(drifted)


def _transition(previous, *, enabled, targets=None, retired=None, group_retired=None, mode="legacy",
                live=None, pending=None, grafana_running=None):
    previous_text = "" if previous is None else yaml.safe_dump(previous, sort_keys=False)
    pending_text = "" if pending is None else yaml.safe_dump(pending, sort_keys=False)
    live_rules = _live_rules(previous) if live is None and previous is not None else live
    return SURVEY.prepare_expected_target_transition({
        "enabled": enabled,
        "targets": _expected() if targets is None else targets,
        "retired_uids": [] if retired is None else retired,
        "group_retired_uids": [] if group_retired is None else group_retired,
        "mode": mode,
        "previous_rules": previous_text,
        "pending_rules": pending_text,
        "grafana_running": grafana_running,
        "live_rules": "" if live_rules is None or live_rules == "" else json.dumps(live_rules),
        "live_folders": "" if live_rules is None else json.dumps(
            [{"uid": "agent-cloud-folder", "title": "agent-cloud"}]
        ),
    })


def test_expected_target_disable_retry_and_reenable_preserve_only_owned_uid():
    target = _expected()
    enabled = _render(o11y_expected_metrics_targets=[target])
    live = _service_rules(enabled)
    original_target_rule = next(rule for rule in live if rule["uid"] == target["uid"])
    assert _transition(None, enabled=True, targets=[target])["reason"] == "enabled"

    disabled_plan = _transition(enabled, enabled=False, targets=[])
    assert disabled_plan["paused_uids"] == [target["uid"]]
    disabled = _render(
        o11y_alerts_enabled=False,
        o11y_expected_metrics_targets_reviewed=False,
        o11y_expected_metrics_targets=[],
        _o11y_previous_expected_target_uids=disabled_plan["paused_uids"],
        _o11y_expected_target_tombstones=disabled_plan["tombstones"],
    )
    paused = next(rule for rule in _service_rules(disabled) if rule["uid"] == target["uid"])
    assert paused["isPaused"] is True
    assert paused["data"][0]["model"]["expr"] == "vector(0)"
    assert paused["labels"] == {
        "severity": "warning", "job": target["job"], "instance": target["instance"],
        "service": target["service"], "cluster": target["cluster"],
        "owner": "platform-operations", "environment": target["environment"],
    }
    assert "notification_settings" not in paused
    paused_live = _live_rules(disabled)
    assert SURVEY.validate_active_expected_target_rules({
        "reviewed": True,
        "targets": [],
        "retired_uids": [],
        "paused_uids": disabled_plan["paused_uids"],
        "tombstones": disabled_plan["tombstones"],
        "live_rules": paused_live,
        "live_folders": [{"uid": "agent-cloud-folder", "title": "agent-cloud"}],
    })["status"] == "validated"

    repeated = _transition(disabled, enabled=False, targets="malformed")
    assert repeated["paused_uids"] == [target["uid"]]
    enabled_again = _transition(disabled, enabled=True, targets=[target])
    assert enabled_again["paused_uids"] == []
    active_again = _render(_o11y_previous_expected_target_uids=[])
    restored = next(rule for rule in _service_rules(active_again) if rule["uid"] == target["uid"])
    assert restored["isPaused"] is False
    assert restored["data"][0]["model"]["expr"] == original_target_rule["data"][0]["model"]["expr"]
    assert SURVEY.validate_active_expected_target_rules({
        "reviewed": True,
        "targets": [target],
        "retired_uids": [],
        "paused_uids": [],
        "live_rules": _live_rules(active_again),
        "live_folders": [{"uid": "agent-cloud-folder", "title": "agent-cloud"}],
    })["status"] == "validated"


@pytest.mark.parametrize("targets", [None, "bad", {}, [], [{"uid": "bad"}]])
def test_disabled_gate_carries_previous_owner_despite_missing_or_malformed_current_list(targets):
    target = _expected()
    previous = _render(o11y_expected_metrics_targets=[target])
    assert _transition(previous, enabled=False, targets=targets)["paused_uids"] == [target["uid"]]


def test_transition_refuses_corrupt_or_unretired_ownership_and_preserves_unrelated_rules():
    target = _expected()
    previous = _render(o11y_expected_metrics_targets=[target])
    corrupted = json.loads(json.dumps(previous))
    owned_rule = next(rule for rule in _service_rules(corrupted) if rule["uid"] == target["uid"])
    owned_rule["labels"]["service"] = "not-the-rendered-identity"
    with pytest.raises(SURVEY.Refusal):
        _transition(corrupted, enabled=False, targets=[])
    with pytest.raises(SURVEY.Refusal):
        _transition(previous, enabled=True, targets=[])
    with pytest.raises(SURVEY.Refusal):
        _transition(previous, enabled=True, targets=[_expected(instance="changed.example.test:9100")])

    explicit = _transition(previous, enabled=True, targets=[], retired=[target["uid"]])
    assert explicit["retired_uids"] == [target["uid"]]
    paused = _transition(previous, enabled=False, targets=[], retired=[target["uid"]])
    assert paused["paused_uids"] == []

    with pytest.raises(SURVEY.Refusal):
        _transition(None, enabled=False, targets=[], live=[{"uid": target["uid"], "isPaused": False}])
    with pytest.raises(SURVEY.Refusal):
        _transition(previous, enabled=False, targets=[], live=[{"uid": target["uid"], "title": "drift"}])


def test_canary_restore_and_interrupted_retry_retain_expected_target_ownership():
    target = _expected()
    canary = _render(o11y_alert_canary_service="canary-probe", o11y_expected_metrics_targets=[target])
    canary_rule = next(rule for rule in _service_rules(canary) if rule["uid"] == target["uid"])
    assert canary_rule["isPaused"] is True
    restore_plan = _transition(canary, enabled=False, targets=[])
    assert restore_plan["paused_uids"] == [target["uid"]]
    retry_plan = _transition(canary, enabled=False, targets=[])
    assert retry_plan == restore_plan


def test_retirement_stays_inert_through_gate_off_restore_and_retry():
    target = _expected()
    active = _render(o11y_expected_metrics_targets=[target])
    retired = _transition(active, enabled=True, targets=[], retired=[target["uid"]])
    assert retired["paused_uids"] == []
    assert retired["retired_uids"] == [target["uid"]]

    retired_file = _render(
        o11y_expected_metrics_targets=[],
        _o11y_retired_expected_target_uids=retired["retired_uids"],
        _o11y_expected_target_tombstones=retired["tombstones"],
    )
    tombstone = next(rule for rule in _service_rules(retired_file) if rule["uid"] == target["uid"])
    assert tombstone["title"] == f"Expected target retired ({target['uid']})"
    assert tombstone["isPaused"] is True
    assert tombstone["data"][0]["model"]["expr"] == "vector(0)"
    assert tombstone["labels"] == {
        "severity": "warning", "job": target["job"], "instance": target["instance"],
        "service": target["service"], "cluster": target["cluster"],
        "owner": "platform-operations", "environment": target["environment"],
    }
    assert "notification_settings" not in tombstone
    assert target["uid"] not in {entry["uid"] for entry in retired_file.get("deleteRules", [])}

    gate_off = _transition(retired_file, enabled=False, targets=[])
    assert gate_off["retired_uids"] == [target["uid"]]
    restored = _render(
        o11y_alerts_enabled=False,
        o11y_expected_metrics_targets_reviewed=False,
        o11y_expected_metrics_targets=[],
        _o11y_previous_expected_target_uids=gate_off["paused_uids"],
        _o11y_retired_expected_target_uids=gate_off["retired_uids"],
        _o11y_expected_target_tombstones=gate_off["tombstones"],
    )
    assert next(rule for rule in _service_rules(restored) if rule["uid"] == target["uid"]) == tombstone
    assert _transition(restored, enabled=False, targets=[]) == gate_off

    with pytest.raises(SURVEY.Refusal):
        _transition(retired_file, enabled=True, targets=[target])


@pytest.mark.parametrize(
    "drift",
    ["folder", "expression", "labels", "pause", "missing", "unowned"],
)
def test_live_expected_target_contract_must_match_validated_previous_ownership(drift):
    target = _expected()
    previous = _render(o11y_expected_metrics_targets=[target])
    live = _live_rules(previous)
    folders = [{"uid": "agent-cloud-folder", "title": "agent-cloud"}]
    if drift == "folder":
        live[0]["folderUID"] = "other-folder"
    elif drift == "expression":
        live[0]["data"][0]["model"]["expr"] = "vector(1)"
    elif drift == "labels":
        live[0]["labels"]["service"] = "other/service"
    elif drift == "pause":
        live[0]["isPaused"] = True
    elif drift == "missing":
        live = []
    else:
        live.append({**live[0], "uid": "o11y_missing_unowned"})
    with pytest.raises(SURVEY.Refusal):
        SURVEY.prepare_expected_target_transition({
            "enabled": False,
            "targets": [],
            "retired_uids": [],
            "previous_rules": yaml.safe_dump(previous),
            "live_rules": json.dumps(live),
            "live_folders": json.dumps(folders),
        })


@pytest.mark.parametrize("drift", ["condition", "datasource", "threshold"])
def test_live_expected_target_preflight_rejects_complete_query_contract_drift(drift):
    previous = _render(o11y_expected_metrics_targets=[_expected()])
    live = _live_rules(previous)
    if drift == "condition":
        live[0]["condition"] = "A"
    elif drift == "datasource":
        live[0]["data"][0]["datasourceUid"] = "loki"
    else:
        live[0]["data"][1]["model"]["conditions"][0]["evaluator"]["params"] = [1]
    with pytest.raises(SURVEY.Refusal):
        _transition(previous, enabled=False, targets=[], live=live)


def test_grafana_api_serialization_preserves_the_provisioned_query_contract():
    previous = _render(o11y_expected_metrics_targets=[_expected()])
    result = _transition(previous, enabled=False, targets=[], live=_live_rules(previous))
    assert result["reason"] == "disabled"


def test_check_mode_validates_source_without_claiming_live_transition_proof():
    previous = _render(o11y_expected_metrics_targets=[_expected()])
    result = SURVEY.prepare_expected_target_transition({
        "enabled": False,
        "reviewed": True,
        "targets": [_expected()],
        "retired_uids": [],
        "previous_rules": yaml.safe_dump(previous),
        "live_rules": "",
        "live_folders": "",
        "check_mode": True,
    })
    assert result["status"] == "prepared"
    assert result["reason"] == "check_mode_unverified"
    assert result["paused_uids"] == [_expected()["uid"]]
    with pytest.raises(SURVEY.Refusal):
        SURVEY.prepare_expected_target_transition({
            "enabled": False,
            "reviewed": True,
            "targets": [{**_expected(), "uid": "invalid"}],
            "retired_uids": [],
            "previous_rules": yaml.safe_dump(previous),
            "live_rules": "",
            "live_folders": "",
            "check_mode": True,
        })


def test_interrupted_restore_accepts_only_the_exact_pending_paused_transition():
    target = _expected()
    active = _render(o11y_expected_metrics_targets=[target])
    paused = _render(
        o11y_alerts_enabled=False,
        o11y_expected_metrics_targets_reviewed=False,
        o11y_expected_metrics_targets=[],
        _o11y_previous_expected_target_uids=[target["uid"]],
        _o11y_expected_target_tombstones=[SURVEY._as_tombstone(target)],
    )
    live_active = _live_rules(active)
    live_paused = _live_rules(paused)

    def retry(live, pending=paused):
        return SURVEY.prepare_expected_target_transition({
            "enabled": False,
            "targets": [],
            "retired_uids": [],
            "previous_rules": yaml.safe_dump(active),
            "pending_rules": yaml.safe_dump(pending),
            "live_rules": json.dumps(live),
            "live_folders": json.dumps([{"uid": "agent-cloud-folder", "title": "agent-cloud"}]),
        })

    assert retry(live_active)["reason"] == "disabled"
    assert retry(live_active, pending=active)["reason"] == "disabled"
    assert retry(live_paused)["reason"] == "disabled"
    drifted = json.loads(json.dumps(live_paused))
    drifted[0]["data"][1]["model"]["conditions"][0]["evaluator"]["params"] = [1]
    with pytest.raises(SURVEY.Refusal):
        retry(drifted)


def test_active_rule_validation_separates_declared_rules_from_retired_tombstones():
    active_target = _expected()
    retired_target = _expected(
        uid="o11y_missing_old_api", instance="old-api.example.test:9100", service="old-api/service",
    )
    active = _render(o11y_expected_metrics_targets=[active_target])
    tombstone_doc = _render(
        o11y_alerts_enabled=False,
        o11y_expected_metrics_targets_reviewed=False,
        o11y_expected_metrics_targets=[],
        _o11y_retired_expected_target_uids=[retired_target["uid"]],
    )
    rules = _live_rules(active) + _live_rules(tombstone_doc)
    input_document = {
        "reviewed": True,
        "targets": [active_target],
        "retired_uids": [retired_target["uid"]],
        "live_rules": rules,
        "live_folders": [{"uid": "agent-cloud-folder", "title": "agent-cloud"}],
    }
    assert SURVEY.validate_active_expected_target_rules(input_document) == {
        "status": "validated", "active_count": 1, "retired_count": 1,
    }

    for mutate in (
        lambda rule: rule.update(isPaused=False),
        lambda rule: rule["data"][0]["model"].update(expr="vector(1)"),
        lambda rule: rule["labels"].update(environment="staging"),
        lambda rule: rule.update(notification_settings={"receiver": "unexpected"}),
    ):
        drifted = json.loads(json.dumps(input_document))
        rows = drifted["live_rules"]
        mutate(next(row for row in rows if row["uid"] == retired_target["uid"]))
        drifted["live_rules"] = rows
        with pytest.raises(SURVEY.Refusal):
            SURVEY.validate_active_expected_target_rules(drifted)


def test_active_rule_validation_accepts_gate_off_owners_and_refuses_bad_paused_uids():
    target = _expected()
    inert = _render(
        o11y_alerts_enabled=False,
        o11y_expected_metrics_targets_reviewed=False,
        o11y_expected_metrics_targets=[],
        _o11y_previous_expected_target_uids=[target["uid"]],
        _o11y_expected_target_tombstones=[SURVEY._as_tombstone(target)],
    )
    input_document = {
        "reviewed": True,
        "targets": [],
        "retired_uids": [],
        "paused_uids": [target["uid"]],
        "tombstones": [SURVEY._as_tombstone(target)],
        "live_rules": _live_rules(inert),
        "live_folders": [{"uid": "agent-cloud-folder", "title": "agent-cloud"}],
    }
    assert SURVEY.validate_active_expected_target_rules(input_document) == {
        "status": "validated", "active_count": 0, "retired_count": 0,
    }
    for mutate in (
        lambda rule: rule.update(isPaused=False),
        lambda rule: rule["data"][0]["model"].update(expr="vector(1)"),
        lambda rule: rule["labels"].update(environment="staging"),
    ):
        drifted = json.loads(json.dumps(input_document))
        mutate(drifted["live_rules"][0])
        with pytest.raises(SURVEY.Refusal):
            SURVEY.validate_active_expected_target_rules(drifted)

    for paused in (None, "not-a-list", ["invalid"], [target["uid"], target["uid"]]):
        with pytest.raises(SURVEY.Refusal):
            SURVEY.validate_active_expected_target_rules({**input_document, "paused_uids": paused})

    active = _render(o11y_expected_metrics_targets=[target])
    with pytest.raises(SURVEY.Refusal):
        SURVEY.validate_active_expected_target_rules({
            **input_document,
            "targets": [target],
            "paused_uids": [target["uid"]],
            "live_rules": _live_rules(active),
        })

    retired = _render(
        o11y_alerts_enabled=False,
        o11y_expected_metrics_targets_reviewed=False,
        o11y_expected_metrics_targets=[],
        _o11y_retired_expected_target_uids=[target["uid"]],
    )
    with pytest.raises(SURVEY.Refusal):
        SURVEY.validate_active_expected_target_rules({
            **input_document,
            "retired_uids": [target["uid"]],
            "live_rules": _live_rules(retired),
        })


def test_live_expected_target_preflight_requires_exact_folder_and_readback():
    target = _expected()
    previous = _render(o11y_expected_metrics_targets=[target])
    live = _live_rules(previous)
    with pytest.raises(SURVEY.Refusal):
        SURVEY.prepare_expected_target_transition({
            "enabled": False,
            "targets": [],
            "previous_rules": yaml.safe_dump(previous),
            "live_rules": json.dumps(live),
            "live_folders": json.dumps([{"uid": "other", "title": "other"}]),
        })
    with pytest.raises(SURVEY.Refusal):
        SURVEY.prepare_expected_target_transition({
            "enabled": False,
            "targets": [],
            "previous_rules": yaml.safe_dump(previous),
            "live_rules": "",
            "live_folders": "",
        })


@pytest.mark.parametrize(
    ("live_rules", "live_folders"),
    [
        ("", '[{"uid":"agent-cloud-folder","title":"agent-cloud"}]'),
        ("not-json", '[{"uid":"agent-cloud-folder","title":"agent-cloud"}]'),
        ("[]", ""),
        ("[]", "not-json"),
    ],
)
def test_running_grafana_requires_parseable_live_reads_without_previous_owners(live_rules, live_folders):
    with pytest.raises(SURVEY.Refusal, match="invalid_live_rules"):
        SURVEY.prepare_expected_target_transition({
            "enabled": False,
            "reviewed": False,
            "targets": [],
            "retired_uids": [],
            "previous_rules": "",
            "live_rules": live_rules,
            "live_folders": live_folders,
            "grafana_running": True,
        })


def test_running_grafana_accepts_successful_empty_live_rules_without_previous_owners():
    result = SURVEY.prepare_expected_target_transition({
        "enabled": False,
        "reviewed": False,
        "targets": [],
        "retired_uids": [],
        "previous_rules": "",
        "live_rules": "[]",
        "live_folders": '[{"uid":"agent-cloud-folder","title":"agent-cloud"}]',
        "grafana_running": True,
    })
    assert result["status"] == "prepared"


def test_stopped_grafana_allows_only_an_owned_no_change_redeploy():
    target = _expected()
    active = _render(o11y_expected_metrics_targets=[target])
    request = {
        "enabled": True,
        "reviewed": True,
        "targets": [target],
        "retired_uids": [],
        "previous_rules": yaml.safe_dump(active),
        "live_rules": "",
        "live_folders": "",
        "grafana_running": False,
    }
    assert SURVEY.prepare_expected_target_transition(request)["reason"] == "enabled"

    disabled = SURVEY.prepare_expected_target_transition({
        **request,
        "enabled": False,
        "reviewed": False,
        "targets": [],
    })
    assert disabled["paused_uids"] == [target["uid"]]
    assert set(disabled["paused_uids"] + disabled["retired_uids"]) == {target["uid"]}

    identity_changed = json.loads(json.dumps(request))
    changed_target = _expected(instance="changed.example.test:9100")
    identity_changed["targets"] = [changed_target]
    with pytest.raises(SURVEY.Refusal, match="target_identity_changed"):
        SURVEY.prepare_expected_target_transition(identity_changed)

    new_uid = {**request, "targets": [_expected(uid="o11y_missing_new")]}
    with pytest.raises(SURVEY.Refusal, match="stopped_grafana_target_drift"):
        SURVEY.prepare_expected_target_transition(new_uid)

    retirement = {
        **request,
        "enabled": False,
        "reviewed": False,
        "targets": [],
        "retired_uids": [target["uid"]],
    }
    with pytest.raises(SURVEY.Refusal, match="stopped_grafana_retirement_drift"):
        SURVEY.prepare_expected_target_transition(retirement)


def test_stopped_grafana_refuses_api_failure_and_interrupted_reactivation():
    target = _expected()
    inert = _render(
        o11y_alerts_enabled=False,
        o11y_expected_metrics_targets_reviewed=False,
        o11y_expected_metrics_targets=[],
        _o11y_previous_expected_target_uids=[target["uid"]],
    )
    active = _render(o11y_expected_metrics_targets=[target])
    request = {
        "enabled": True,
        "reviewed": True,
        "targets": [target],
        "retired_uids": [],
        "previous_rules": yaml.safe_dump(inert),
        "live_rules": "",
        "live_folders": "",
        "grafana_running": False,
    }
    with pytest.raises(SURVEY.Refusal, match="stopped_grafana_reactivation"):
        SURVEY.prepare_expected_target_transition(request)

    interrupted = {
        **request,
        "previous_rules": yaml.safe_dump(active),
        "pending_rules": yaml.safe_dump(inert),
    }
    with pytest.raises(SURVEY.Refusal, match="stopped_grafana_reactivation"):
        SURVEY.prepare_expected_target_transition(interrupted)

    invalid_snapshot = {
        **interrupted,
        "pending_rules": yaml.safe_dump({
            "apiVersion": 1,
            "groups": [{
                "orgId": 1, "name": "service-telemetry", "folder": "agent-cloud",
                "rules": [{"uid": target["uid"], "title": "unexpected"}],
            }],
        }),
    }
    with pytest.raises(SURVEY.Refusal, match="invalid_previous_rules|invalid_pending_rules"):
        SURVEY.prepare_expected_target_transition(invalid_snapshot)

    with pytest.raises(SURVEY.Refusal, match="live_rules_unavailable"):
        SURVEY.prepare_expected_target_transition({**request, "grafana_running": True})


def test_private_alert_rule_reads_and_writes_are_hidden_and_check_mode_skips_render():
    provision = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-alert-provision.yml").read_text())
    rule_write = next(task for task in provision if task.get("name") == "Render declarative Grafana alert rules")
    assert rule_write["no_log"] is True and rule_write["diff"] is False
    assert rule_write["when"] == "not ansible_check_mode"
    contact_write = next(
        task for task in provision if task.get("name") == "Render Discord contact point or its removal"
    )
    assert contact_write["no_log"] is True and contact_write["diff"] is False

    prepare_path = ROOT / "platform/playbooks/tasks/o11y-prepare-expected-target-transition.yml"
    prepare = yaml.safe_load(prepare_path.read_text())
    for name in (
        "Read previous alert provisioning file presence privately",
        "Read pending expected-target ownership snapshot presence privately",
        "Require the pending expected-target snapshot to be a regular file",
        "Read pending expected-target ownership snapshot privately",
        "Require the previous alert provisioning path to be a regular file",
        "Read previous alert provisioning rules privately",
        "Read live Grafana alert ownership before alert transition",
        "Read live Grafana folders before alert transition",
        "Prepare the expected-target alert transition privately",
        "Require a safe expected-target alert transition",
        "Save only the validated previous expected-target UIDs",
    ):
        assert next(task for task in prepare if task.get("name") == name)["no_log"] is True
    transition = next(
        task for task in prepare if task.get("name") == "Prepare the expected-target alert transition privately"
    )
    transition_input = transition["ansible.builtin.command"]["stdin"]
    assert "grafana_running" in transition_input
    for name in (
        "Read live Grafana alert ownership before alert transition",
        "Read live Grafana folders before alert transition",
    ):
        read = next(task for task in prepare if task.get("name") == name)
        assert "failed_when" not in read
        assert read["no_log"] is True

    deploy = yaml.safe_load((ROOT / "platform/playbooks/deploy-o11y.yml").read_text())
    phase = next(play for play in deploy if play.get("name") == "Phase 1: Place repo + manage o11y secrets")
    names = [task.get("name") for task in phase["tasks"]]
    assert names.index("Validate previous expected-target ownership before placement") < names.index(
        "Place the monorepo + ensure podman/compose"
    )
    verify = next(play for play in deploy if play.get("name") == "Phase 3: Verify o11y")
    verify_block = next(
        task
        for task in verify["tasks"]
        if task.get("name") == "Verify Grafana alert provisioning after every real deploy"
    )
    assert verify_block["when"] == "not ansible_check_mode" and not verify_block.get("no_log", False)

    protected_tasks = {
        "platform/playbooks/tasks/o11y-restore-alert-baseline.yml": (
            "Read the restored service-down rule",
            "Require the service-down rule to be paused again",
            "Read all restored o11y alert rules",
            "Require every o11y rule to be paused again",
        ),
        "platform/playbooks/drill-o11y-active-alert-delivery.yml": (
            "Read the active production service-down rule",
            "Require exactly one active service-down rule",
            "Read every provisioned o11y alert rule before the drill",
            "Require all provisioned o11y rules to remain active",
        ),
        "platform/playbooks/tasks/o11y-recover-active-alert-drill.yml": (
            "Read active Prometheus targets after recovery",
            "Require the drill scrape job to be absent",
            "Read the production service-down rule after drill recovery",
            "Require the production service-down rule to remain active",
            "Read all production o11y alert rules after drill recovery",
        "Validate expected-target ownership before active-drill recovery",
            "Require every o11y alert rule to remain active",
        ),
    }
    for relative_path, names in protected_tasks.items():
        tasks = yaml.safe_load((ROOT / relative_path).read_text())
        pending = list(tasks)
        flat_tasks = []
        while pending:
            task = pending.pop()
            if not isinstance(task, dict):
                continue
            flat_tasks.append(task)
            pending.extend(task.get(key, []) for key in ("block", "rescue", "always", "tasks"))
            pending = [child for group in pending for child in (group if isinstance(group, list) else [group])]
        for name in names:
            assert next(task for task in flat_tasks if task.get("name") == name)["no_log"] is True

    active_target_validation = yaml.safe_load(
        (ROOT / "platform/playbooks/tasks/o11y-validate-active-expected-target-rules.yml").read_text()
    )
    for name in (
        "Read the agent-cloud alert folder for target rule validation",
        "Validate active expected-target rules and retired tombstones privately",
        "Require active target declarations and inert retired tombstones to match",
    ):
        assert next(task for task in active_target_validation if task.get("name") == name)["no_log"] is True
    active_rule_reader = next(
        task for task in active_target_validation
        if task.get("name") == "Validate active expected-target rules and retired tombstones privately"
    )
    assert "paused_uids" in active_rule_reader["ansible.builtin.command"]["stdin"]
    for relative_path in (
        "platform/playbooks/drill-o11y-active-alert-delivery.yml",
        "platform/playbooks/tasks/o11y-recover-active-alert-drill.yml",
    ):
        text = (ROOT / relative_path).read_text()
        assert "o11y-validate-active-expected-target-rules.yml" in text
        assert "o11y-prepare-expected-target-transition.yml" in text
    drill = yaml.safe_load((ROOT / "platform/playbooks/drill-o11y-active-alert-delivery.yml").read_text())
    drill_tasks = next(
        play for play in drill
        if play.get("name") == "Prove production alert delivery while preserving active state"
    )["tasks"]
    owner_validation = next(
        task for task in drill_tasks
        if task.get("name") == "Validate previous expected-target ownership before the production drill"
    )
    assert owner_validation["no_log"] is True

    probe = yaml.safe_load((ROOT / "platform/playbooks/tasks/o11y-alert-probe.yml").read_text())
    pending = list(probe)
    flat_probe = []
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            continue
        flat_probe.append(item)
        pending.extend(item.get(key, []) for key in ("block", "rescue", "always", "tasks"))
        pending = [child for group in pending for child in (group if isinstance(group, list) else [group])]
    for name in (
        "Read the provisioned service-down rule before the alert drill",
        "Require an active provisioned rule before injecting the alert fault",
        "Wait for the declared failed scrape",
        "Require the onboarding verifier to refuse the named endpoint",
        "Wait for Grafana's service-down rule to fire for the probe",
    ):
        task = next(item for item in flat_probe if item.get("name") == name)
        parent = next((item for item in flat_probe if item.get("block") and task in item["block"]), {})
        assert task.get("no_log") is True or parent.get("no_log") is True

    probe_summary = next(
        item for item in flat_probe if item.get("name") == "Report the fired rule and confirmed Discord receipt"
    )
    assert "{{ expected_service }}" not in json.dumps(probe_summary)
    receipt_summary = next(
        item for item in flat_probe
        if item.get("name") == "Count the matching Discord receipt without retaining its content"
    )
    receipt_assertion = next(
        item for item in flat_probe if item.get("name") == "Require a readable firing receipt from the owned webhook"
    )
    assert receipt_summary["no_log"] is True
    assert "no_log" not in receipt_assertion and receipt_assertion["ansible.builtin.assert"]["quiet"] is True
    assert "_discord_messages" not in json.dumps(receipt_assertion)
    assert ".content" not in json.dumps(receipt_assertion)

    restored = yaml.safe_load(
        (ROOT / "platform/playbooks/tasks/o11y-restore-alert-baseline.yml").read_text()
    )
    restore_names = [task.get("name") for task in restored]
    assert restore_names.index("Validate alert ownership before restoring the paused baseline") < restore_names.index(
        "Preserve previous alert ownership before the paused transition"
    ) < restore_names.index("Render paused Grafana alert rules without OpenBao")
    assert restore_names.index("Require the canary contact point to be absent again") < restore_names.index(
        "Remove the expected-target transition snapshot after verified restoration"
    )
    snapshot = next(
        task for task in restored
        if task.get("name") == "Preserve previous alert ownership before the paused transition"
    )
    assert snapshot["no_log"] is True and snapshot["ansible.builtin.copy"]["mode"] == "0600"
    for name in (
        "Require the service-down rule to be paused again",
        "Require every o11y rule to be paused again",
        "Require the canary contact point to be absent again",
    ):
        task = next(task for task in restored if task.get("name") == name)
        assert task["when"] == "not ansible_check_mode"

    canary = yaml.safe_load((ROOT / "platform/playbooks/drill-o11y-alert-canary.yml").read_text())
    canary_phase = next(
        play for play in canary if play.get("name") == "Prove alert delivery and restore the paused baseline"
    )
    canary_names = [task.get("name") for task in canary_phase["tasks"]]
    preflight = canary_names.index("Validate previous expected-target ownership before activating canary writes")
    activation = canary_names.index("Activate only the canary rule, prove delivery, and restore the baseline")
    assert preflight < activation


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
