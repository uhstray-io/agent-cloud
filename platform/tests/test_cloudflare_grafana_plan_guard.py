"""The Cloudflare apply gate accepts only the reviewed Grafana rule insertion."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/apply-cloudflare-tofu.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
TASKS = REPO / "plan/development/openspec/changes/o11y-signal-labeling-profiling-alerting/tasks.md"
DESIGN = REPO / "plan/development/openspec/changes/o11y-signal-labeling-profiling-alerting/design.md"
HELPER = REPO / "platform/playbooks/files/cloudflare_grafana_plan_guard.py"
spec = importlib.util.spec_from_file_location("cloudflare_grafana_plan_guard", HELPER)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def _rule(ref):
    return {"ref": ref, "action": "skip", "enabled": True, "expression": f"match {ref}"}


def _plan(insertion=True):
    before = [_rule(f"existing-{n}") for n in range(7)]
    after = before[:5] + [copy.deepcopy(guard.EXPECTED_RULE)] + before[5:] if insertion else copy.deepcopy(before)
    change = {
        "actions": ["update"] if insertion else ["no-op"],
        "before": {"id": "sensitive-ruleset-id", "zone_id": "sensitive-zone-id", "name": "default", "rules": before},
        "after": {"id": "sensitive-ruleset-id", "zone_id": "sensitive-zone-id", "name": "default", "rules": after},
        "after_unknown": {"rules": [{}, {}, {}, {}, {}, {"id": True}]} if insertion else {},
    }
    return {"errored": False, "applyable": True, "resource_changes": [
        {"address": guard.RESOURCE, "change": change},
        {"address": "cloudflare_record.unchanged", "change": {
            "actions": ["no-op"], "before": {"content": "same"}, "after": {"content": "same"}}},
    ]}


def test_exact_insertion_has_stable_redacted_receipt():
    plan = _plan()
    receipt = guard.inspect_plan(plan, "plan")
    reordered = json.loads(json.dumps(plan, sort_keys=True))
    metadata_changed = copy.deepcopy(plan)
    metadata_changed["resource_changes"][0]["provider_name"] = "registry.example/provider"
    assert receipt["safe"] and receipt["status"] == "insert"
    assert receipt["digest"] == guard.inspect_plan(reordered, "plan")["digest"]
    assert receipt["digest"] != guard.inspect_plan(metadata_changed, "plan")["digest"]
    assert set(receipt) == set(guard.RECEIPT_FIELDS)
    assert "sensitive-zone-id" not in json.dumps(receipt)


@pytest.mark.parametrize("mutate,reason", [
    (lambda p: p.update(complete=False), "plan_not_applyable"),
    (lambda p: p["resource_changes"].append({"address": "cloudflare_record.drift", "change": {"actions": ["update"]}}),
     "other_resource_changes"),
    (lambda p: p.update(resource_drift=[{"address": "cloudflare_record.drift"}]), "resource_drift"),
    (lambda p: p["resource_changes"][0]["change"]["after"].update(description="unrelated root drift"),
     "root_field_change"),
    (lambda p: p["resource_changes"][0]["change"]["after"]["rules"][5].update(expression="changed"),
     "not_exact_rule_insertion"),
    (lambda p: p["resource_changes"][0]["change"]["after"]["rules"].insert(
        0, p["resource_changes"][0]["change"]["after"]["rules"].pop(5)), "not_exact_rule_insertion"),
    (lambda p: p["resource_changes"][0]["change"].update(actions=["delete", "create"], after_unknown={}),
     "target_action_not_update"),
    (lambda p: p["resource_changes"][0]["change"].update(after_unknown={"rules": [{"expression": True}]}),
     "unknown_values"),
])
def test_unsafe_resource_rule_order_and_unknown_drift_is_rejected(mutate, reason):
    plan = _plan()
    mutate(plan)
    receipt = guard.inspect_plan(plan, "plan")
    assert not receipt["safe"] and receipt["reason"] == reason


def test_missing_and_mismatched_digest_refuse_apply():
    plan = _plan()
    digest = guard.inspect_plan(plan, "plan")["digest"]
    assert guard.inspect_plan(plan, "apply")["reason"] == "reviewed_digest_missing_or_mismatch"
    assert guard.inspect_plan(plan, "apply", "sha256:" + "0" * 64)["reason"] == "reviewed_digest_missing_or_mismatch"
    receipt = guard.inspect_plan(plan, "apply", digest)
    assert receipt["safe"] and receipt["digest_matches"]


def test_verify_requires_the_exact_present_rule_and_zero_diff():
    clean = _plan(insertion=False)
    clean["resource_changes"][0]["change"]["after"]["rules"].insert(
        5, copy.deepcopy(guard.EXPECTED_RULE)
    )
    clean["resource_changes"][0]["change"]["before"]["rules"].insert(
        5, copy.deepcopy(guard.EXPECTED_RULE)
    )
    receipt = guard.inspect_plan(clean, "verify")
    assert receipt["safe"] and receipt["status"] == "present"
    assert not guard.inspect_plan(_plan(), "verify")["safe"]


def _tasks():
    play = next(p for p in yaml.safe_load(PLAYBOOK.read_text()) if p.get("hosts") == "localhost")
    block = next(t for t in play["tasks"] if t.get("name") == "OpenTofu run")
    return block["block"]


def test_apply_is_bound_to_the_saved_plan_and_digest_is_a_survey_input():
    tasks = _tasks()
    names = [t.get("name") for t in tasks]
    gate_i = names.index("Require the reviewed Grafana rule insertion before apply")
    apply_i = names.index("OpenTofu apply")
    apply = tasks[apply_i]["ansible.builtin.command"]["argv"]
    assert gate_i < apply_i
    assert apply == ["tofu", "apply", "-input=false", "-no-color", "{{ _tf_planfile }}"]
    assert "-auto-approve" not in apply
    assert tasks[names.index("Validate the exact Grafana ruleset plan")]["no_log"] is True
    validate = tasks[names.index("Validate the exact Grafana ruleset plan")]["ansible.builtin.command"]
    assert validate["stdin"] and "reviewed_plan_digest" in " ".join(validate["argv"])
    gate = tasks[gate_i]["ansible.builtin.assert"]
    assert gate["that"] == [
        "_tf_plan_guard.safe | default(false) | bool",
        "_tf_plan_guard.status | default('') == 'insert'",
        "_tf_plan_guard.digest_matches | default(false) | bool",
    ]
    template = yaml.safe_load(TEMPLATES.read_text())["templates"]
    entry = next(t for t in template if t.get("name") == "Apply Cloudflare Tofu")
    digest = next(v for v in entry["survey_vars"] if v["name"] == "reviewed_plan_digest")
    assert digest["required"] is False
    assert "apply" in digest["description"].lower() and "SHA-256" in digest["description"]


def test_editor_exception_is_scoped_and_rollout_tasks_stay_open():
    tasks = TASKS.read_text()
    design = DESIGN.read_text()
    assert "- [ ] 2.2a" in tasks and "- [ ] 2.2b" in tasks
    assert "requested Editor role" in tasks
    assert "OpenBao remains the token source of truth" in tasks
    assert "0600 client copy" in tasks and "~/.config/mcp-grafana/prod.token" in tasks
    assert "remains Viewer" in tasks
    assert "scoped grafana mcp write access" in design.lower()
    assert "existing o11y liveness watcher" in design
