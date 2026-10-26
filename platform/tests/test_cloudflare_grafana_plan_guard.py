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
FIXTURE = REPO / "platform/tests/fixtures/cloudflare-ruleset-v5.21.1-update.json"
LOCK = REPO / "platform/infra/cloudflare/.terraform.lock.hcl"
HELPER = REPO / "platform/playbooks/files/cloudflare_grafana_plan_guard.py"
spec = importlib.util.spec_from_file_location("cloudflare_grafana_plan_guard", HELPER)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def _plan(insertion=True):
    fixture = json.loads(FIXTURE.read_text())
    plan = {"errored": False, "applyable": True, "resource_changes": fixture["resource_changes"]}
    if not insertion:
        change = plan["resource_changes"][0]["change"]
        change["actions"] = ["no-op"]
        change["before"] = copy.deepcopy(change["after"])
        change["before"]["id"] = "11111111111111111111111111111111"
        change["before"]["version"] = "2"
        change["before"]["last_updated"] = "2026-10-13T00:00:00Z"
        change["after"]["id"] = "11111111111111111111111111111111"
        change["after"]["version"] = "2"
        change["after"]["last_updated"] = "2026-10-13T00:00:00Z"
        change["after"]["rules"][5]["id"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        change["before"]["rules"][5]["id"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        change["after_unknown"] = {}
    plan["resource_changes"].append({"address": "cloudflare_record.unchanged", "change": {
        "actions": ["no-op"], "before": {"content": "same"}, "after": {"content": "same"}}})
    return plan


def _move_rule_before(plan):
    change = plan["resource_changes"][0]["change"]
    change["after"]["rules"].insert(0, change["after"]["rules"].pop(5))
    for field in ("id", "version", "last_updated"):
        change["after"][field] = change["before"][field]
    change["after_unknown"] = {}


def test_provider_fixture_matches_the_locked_cloudflare_provider():
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["provider_version"] == "5.21.1"
    assert 'version     = "5.21.1"' in LOCK.read_text()
    assert guard.inspect_plan(_plan(), "plan")["safe"]


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
    assert receipt["root_field_changes"] == []
    assert "sensitive-zone-id" not in json.dumps(receipt)


def test_root_field_refusal_reports_only_allowlisted_names_and_key_presence():
    plan = _plan()
    after = plan["resource_changes"][0]["change"]["after"]
    after["description"] = "unrelated root drift"
    del after["zone_id"]
    after["private_metadata"] = "must not leave the plan boundary"

    receipt = guard.inspect_plan(plan, "plan")

    assert not receipt["safe"] and receipt["reason"] == "root_field_change"
    assert receipt["root_field_changes"] == ["description", "unknown_field", "zone_id"]
    assert "private_metadata" not in json.dumps(receipt)
    assert "unrelated root drift" not in json.dumps(receipt)


def test_root_field_diagnostic_uses_fixed_marker_for_unknown_field_names():
    plan = _plan()
    plan["resource_changes"][0]["change"]["after"]["private_metadata"] = "sensitive value"

    receipt = guard.inspect_plan(plan, "plan")

    assert receipt["reason"] == "root_field_change"
    assert receipt["root_field_changes"] == ["unknown_field"]
    assert "private_metadata" not in json.dumps(receipt)
    assert "sensitive value" not in json.dumps(receipt)


@pytest.mark.parametrize(("field", "missing_from"), [
    ("version", "before"),
    ("version", "after"),
    ("last_updated", "before"),
    ("last_updated", "after"),
])
def test_explicitly_unknown_computed_root_field_may_be_absent_or_null(field, missing_from):
    plan = _plan()
    change = plan["resource_changes"][0]["change"]
    change["after_unknown"][field] = True
    change[missing_from].pop(field)
    if missing_from == "before":
        change["after"][field] = None

    receipt = guard.inspect_plan(plan, "plan")

    assert receipt["safe"] and receipt["status"] == "insert"
    assert receipt["root_field_changes"] == []


@pytest.mark.parametrize(("field", "missing_from"), [
    ("version", "before"),
    ("version", "after"),
    ("last_updated", "before"),
    ("last_updated", "after"),
])
@pytest.mark.parametrize("marker", [1, 2, "not-true"])
def test_computed_metadata_presence_exception_rejects_nonboolean_unknown_markers(field, missing_from, marker):
    plan = _plan()
    change = plan["resource_changes"][0]["change"]
    change["after_unknown"][field] = marker
    change[missing_from].pop(field)
    if missing_from == "before":
        change["after"][field] = None

    receipt = guard.inspect_plan(plan, "plan")

    assert not receipt["safe"] and receipt["reason"] == "unknown_values"


def test_computed_resource_identity_presence_change_still_refuses():
    plan = _plan()
    change = plan["resource_changes"][0]["change"]
    change["after_unknown"]["id"] = True
    change["after"].pop("id")

    receipt = guard.inspect_plan(plan, "plan")

    assert not receipt["safe"] and receipt["reason"] == "root_field_change"
    assert receipt["root_field_changes"] == ["id"]


def test_provider_computed_metadata_absent_after_is_accepted_when_marked_unknown():
    plan = _plan()
    change = plan["resource_changes"][0]["change"]
    for field in ("last_updated", "version"):
        change["after_unknown"][field] = True
        change["after"].pop(field)

    receipt = guard.inspect_plan(plan, "plan")

    assert receipt["safe"] and receipt["status"] == "insert"
    assert receipt["root_field_changes"] == []


def _change_computed_root_value_without_unknown(plan):
    change = plan["resource_changes"][0]["change"]
    change["after_unknown"].pop("version")
    change["after"]["version"] = "2"


@pytest.mark.parametrize("mutate,reason", [
    (lambda p: p.update(complete=False), "plan_not_applyable"),
    (lambda p: p["resource_changes"].append({"address": "cloudflare_record.drift", "change": {"actions": ["update"]}}),
     "other_resource_changes"),
    (lambda p: p.update(resource_drift=[{"address": "cloudflare_record.drift"}]), "resource_drift"),
    (lambda p: p["resource_changes"][0]["change"]["after"].update(description="unrelated root drift"),
     "root_field_change"),
    (_change_computed_root_value_without_unknown, "root_field_change"),
    (lambda p: p["resource_changes"][0]["change"]["after"]["rules"][5].update(expression="changed"),
     "not_exact_rule_insertion"),
    (_move_rule_before, "not_exact_rule_insertion"),
    (lambda p: p["resource_changes"][0]["change"].update(
        actions=["delete", "create"],
        before=copy.deepcopy(p["resource_changes"][0]["change"]["before"]),
        after=copy.deepcopy(p["resource_changes"][0]["change"]["before"]),
        after_unknown={}), "target_action_not_update"),
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


def test_matching_zero_diff_saved_plan_is_an_idempotent_apply():
    plan = _plan(insertion=False)
    digest = guard.inspect_plan(plan, "plan")["digest"]
    receipt = guard.inspect_plan(plan, "apply", digest)
    assert receipt["safe"] and receipt["status"] == "present" and receipt["digest_matches"]


@pytest.mark.parametrize("mutate", [
    lambda rule: rule.update(ratelimit=None),
    lambda rule: rule["action_parameters"].update(response=None),
    lambda rule: rule["action_parameters"].update(ruleset=None),
])
def test_unset_null_rule_fields_are_ignored_recursively(mutate):
    plan = _plan()
    mutate(plan["resource_changes"][0]["change"]["after"]["rules"][5])
    receipt = guard.inspect_plan(plan, "plan")
    assert receipt["safe"] and receipt["reason"] == "exact_rule_insertion"


@pytest.mark.parametrize("mutate", [
    lambda p: p["resource_changes"][0]["change"]["after_unknown"].update(name=True),
    lambda p: p["resource_changes"][0]["change"]["after_unknown"].update(version={"nested": True}),
    lambda p: p["resource_changes"][0]["change"]["after"]["rules"][5].update(custom_field="populated"),
    lambda p: p["resource_changes"][0]["change"]["after"]["rules"][5].update(ratelimit={"requests_per_period": 10}),
    lambda p: p["resource_changes"][0]["change"]["after"]["rules"][5]["action_parameters"].update(
        response={"content_type": "application/json"}),
    lambda p: p["resource_changes"][0]["change"]["after"]["rules"][5]["action_parameters"].update(ruleset="unreviewed"),
    lambda p: p["resource_changes"][0]["change"]["after"]["rules"][5]["action_parameters"]["phases"].append(None),
    lambda p: p["resource_changes"][0]["change"]["after"].update(id="11111111111111111111111111111111"),
    lambda p: p["resource_changes"][0]["change"]["after"]["rules"][5].update(
        id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
])
def test_unexpected_unknown_and_provider_fields_are_rejected(mutate):
    plan = _plan()
    mutate(plan)
    assert not guard.inspect_plan(plan, "plan")["safe"]


def test_existing_ruleset_identity_is_required():
    plan = _plan()
    plan["resource_changes"][0]["change"]["before"]["id"] = None
    receipt = guard.inspect_plan(plan, "plan")
    assert not receipt["safe"] and receipt["reason"] == "missing_resource_identity"


def test_verify_requires_the_exact_present_rule_and_zero_diff():
    clean = _plan(insertion=False)
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
    receipt_keys = ["actions", "digest", "digest_matches", "position", "reason", "resource",
                    "root_field_changes", "rule_ref", "safe", "status"]
    for name, variable in (("Unpack the safe plan receipt", "_tf_plan_guard"),
                           ("Unpack the post-apply zero-diff receipt", "_tf_verify_guard")):
        unpack = next(t for t in tasks if t.get("name") == name)
        conditions = unpack["block"][1]["ansible.builtin.assert"]["that"]
        assert [" ".join(condition.split()) for condition in conditions] == [
            f"{variable} is mapping",
            f"({variable}.keys() | list | sort) == {receipt_keys!r} | sort",
        ]
    gate_i = names.index("Require the reviewed Grafana rule insertion before apply")
    apply_i = names.index("OpenTofu apply")
    apply = tasks[apply_i]["ansible.builtin.command"]["argv"]
    assert gate_i < apply_i
    assert apply[-1] == "{{ _tf_planfile if _cloudflare_grafana_guard | default(false) else '-auto-approve' }}"
    assert tasks[names.index("Validate the exact Grafana ruleset plan")]["no_log"] is True
    validate = tasks[names.index("Validate the exact Grafana ruleset plan")]["ansible.builtin.command"]
    assert validate["stdin"] and "reviewed_plan_digest" in " ".join(validate["argv"])
    gate = tasks[gate_i]["ansible.builtin.assert"]
    assert gate["that"] == [
        "_tf_plan_guard.safe | default(false) | bool",
        "_tf_plan_guard.status | default('') in ['insert', 'present']",
        "_tf_plan_guard.digest_matches | default(false) | bool",
    ]
    template = yaml.safe_load(TEMPLATES.read_text())["templates"]
    entry = next(t for t in template if t.get("name") == "Apply Grafana API Cloudflare Tofu (Dev)")
    assert entry["repository"] == "agent-cloud dev"
    assert "dev_variant" not in entry
    assert not any(t.get("name") == "Apply Grafana API Cloudflare Tofu" for t in template)
    digest = next(v for v in entry["survey_vars"] if v["name"] == "reviewed_plan_digest")
    assert digest["required"] is False
    assert "apply" in digest["description"].lower() and "SHA-256" in digest["description"]


def test_generic_cloudflare_executor_stays_unrestricted_and_grafana_uses_wrapper():
    generic = next(t for t in yaml.safe_load(TEMPLATES.read_text())["templates"]
                   if t.get("name") == "Apply Cloudflare Tofu")
    dedicated = next(t for t in yaml.safe_load(TEMPLATES.read_text())["templates"]
                     if t.get("name") == "Apply Grafana API Cloudflare Tofu (Dev)")
    wrapper = yaml.safe_load((REPO / "platform/playbooks/apply-cloudflare-grafana-api-tofu.yml").read_text())
    assert "reviewed_plan_digest" not in {v["name"] for v in generic["survey_vars"]}
    assert dedicated["repository"] == "agent-cloud dev"
    assert "dev_variant" not in dedicated
    assert not any(t.get("name") == "Apply Grafana API Cloudflare Tofu"
                   for t in yaml.safe_load(TEMPLATES.read_text())["templates"])
    assert dedicated["playbook"] == "platform/playbooks/apply-cloudflare-grafana-api-tofu.yml"
    assert wrapper[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert wrapper[1]["vars"] == {"_extra_var_guard_nested": True, "_cloudflare_grafana_guard": True}


def test_editor_exception_is_scoped_and_capacity_gates_stay_open():
    tasks = TASKS.read_text()
    task_status = {}
    task_text = {}
    for line in tasks.splitlines():
        if line.startswith(("- [ ] ", "- [x] ")):
            task_id = line.split("] ", 1)[1].split(maxsplit=1)[0]
            task_status[task_id] = line[3] == "x"
            task_text[task_id] = line.lower()

    assert {task_id: task_status[task_id] for task_id in (
        "2.2a", "2.2b", "2.2c", "2.3", "3.3", "4.3", "5.3", "5.4", "5.5",
    )} == {
        "2.2a": True,
        "2.2b": True,
        "2.2c": True,
        "2.3": True,
        "3.3": True,
        "4.3": False,
        "5.3": False,
        "5.4": False,
        "5.5": False,
    }
    editor_task = task_text["2.2b"]
    assert "editor" in editor_task and "dedicated account" in editor_task and "scoped" in editor_task
    assert "watcher" in editor_task and "viewer" in editor_task
    assert "openbao" in editor_task and "source of truth" in editor_task
    assert "mode 0600" in editor_task and "~/.config/mcp-grafana/prod.token" in editor_task
    assert "local profile" in task_text["2.2c"] and "--disable-write" in task_text["2.2c"]
    assert "production profile" in task_text["2.2c"]
    assert "without `--disable-write`" in task_text["2.2c"]
