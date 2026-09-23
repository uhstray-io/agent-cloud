"""The service-deployment workflow registry agrees with the catalog, the drawing and OPA.

Spec: change service-deployment-workflow, platform/service-onboarding-workflow
("One registry defines the workflow", scenario "Registry and catalog agree") and
platform/agent-orchestration ("Four least-privilege agent identities").
"""

import json
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / "platform/workflows/service-onboarding"
REGISTRY = WORKFLOW / "registry.yml"
SCHEMAS = WORKFLOW / "schemas"
CATALOG = REPO / "platform/semaphore/templates.yml"
DRAWING = REPO / "docs/agent-cloud-service-deploy.excalidraw"
OPA_DATA = REPO / "platform/services/opa/deployment/policies/agentcloud/data.json"

ROLES = {"infra-agent", "security-agent", "o11y-agent", "service-agent"}
PER_SERVICE = "Deploy {service}"
# The secret store, the orchestrator, the IPAM authority, the runner security boundary and the
# all-services wrapper: never an agent's per-service deploy.
FOUNDATION = {"Deploy OpenBao", "Deploy Semaphore", "Deploy NetBox", "Deploy GitHub Runner",
              "Deploy All Services"}
STEPS = yaml.safe_load(REGISTRY.read_text())["steps"]


def _template_names() -> set[str]:
    return {t["name"] for t in yaml.safe_load(CATALOG.read_text())["templates"]}


def _drawn_labels() -> set[str]:
    elements = json.loads(DRAWING.read_text())["elements"]
    return {
        e["text"].replace("\n", " ").strip()
        for e in elements
        if e.get("type") == "text" and not e.get("isDeleted")
    }


def test_twenty_two_steps_in_order():
    assert len(STEPS) == 22
    assert [s["order"] for s in STEPS] == list(range(1, 23))
    ids = [s["id"] for s in STEPS]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("step", STEPS, ids=lambda s: s["id"])
def test_step_is_complete(step):
    for key in ("owner", "undo", "policy", "criteria", "evidence_keys"):
        assert key in step, f"{step['id']} lacks {key}"
    assert "reviewed" in step
    assert step["owner"] in ROLES
    assert step["policy"] in {"required", "advisory"}
    assert step["criteria"] and all(isinstance(c, str) and c for c in step["criteria"])


@pytest.mark.parametrize("step", STEPS, ids=lambda s: s["id"])
def test_named_templates_exist_and_planned_ones_do_not(step):
    names = _template_names()
    for key in ("executor", "snapshot", "undo"):
        value = step.get(key)
        if value in (None, "none"):
            continue
        if value == PER_SERVICE:
            assert any(n.startswith("Deploy ") for n in names)
        else:
            assert value in names, f"{step['id']}.{key} names a template not in the catalog: {value}"
    for key in ("planned_executor", "planned_snapshot"):
        if step.get(key):
            assert step[key] not in names, f"{step['id']}.{key} now exists: promote it to {key[8:]}"


@pytest.mark.parametrize("step", [s for s in STEPS if s.get("schema")], ids=lambda s: s["id"])
def test_reasoning_steps_have_snapshot_schema_and_feeds(step):
    assert step.get("snapshot") or step.get("planned_snapshot"), f"{step['id']} has no snapshot template"
    assert (SCHEMAS / step["schema"]).is_file()
    assert step["executor"] is None, "a reasoning step proposes; the steps it feeds execute"
    known = {s["id"] for s in STEPS}
    assert step["feeds"] and set(step["feeds"]) <= known


def test_non_reasoning_steps_have_an_executor_or_a_planned_one():
    for step in STEPS:
        if not step.get("schema"):
            assert step.get("executor") or step.get("planned_executor"), step["id"]


def test_drawn_steps_match_the_drawing():
    drawn = _drawn_labels()
    labelled = [s for s in STEPS if s.get("diagram_label")]
    assert len(labelled) == 18, "the drawing shows eighteen workflow steps"
    for step in STEPS:
        if step.get("diagram_label"):
            assert step["diagram_label"] in drawn, f"{step['id']}: {step['diagram_label']!r} not in the drawing"
        else:
            assert step.get("added") is True, f"{step['id']} is undrawn but not marked added"


def test_every_schema_parses_and_is_referenced():
    referenced = {s["schema"] for s in STEPS if s.get("schema")}
    shared = {"step-result.json", "proposal-envelope.json", "verdict.json"}
    for path in SCHEMAS.glob("*.json"):
        doc = json.loads(path.read_text())
        assert doc["$id"].endswith("/" + path.name)
        assert path.name in referenced | shared, f"unused schema {path.name}"


def test_opa_allowlists_match_registry_ownership():
    catalog = json.loads(OPA_DATA.read_text())["catalog"]
    for role in ROLES:
        owned = {
            step[key]
            for step in STEPS
            if step["owner"] == role
            for key in ("executor", "snapshot")
            if step.get(key) and step[key] != PER_SERVICE
        }
        entry = catalog[role]
        assert set(entry["allowed_templates"]) == owned, role
        wants_deploys = any(s["owner"] == role and s.get("executor") == PER_SERVICE for s in STEPS)
        deploys = set(entry.get("service_deploy_templates", []))
        assert bool(deploys) == wants_deploys, role
        assert "allowed_template_prefixes" not in entry, f"{role}: prefix grants are open-ended"
        names = _template_names()
        claimed = {n for other, e in catalog.items() if other != role and isinstance(e, dict)
                   for n in e.get("allowed_templates", [])}
        for name in deploys:
            assert name.startswith("Deploy ") and name in names, f"{role}: {name} is not a catalog deploy"
            assert name not in claimed, f"{role}: {name} belongs to another role"
        assert not deploys & FOUNDATION, f"{role} may not deploy the foundation: {deploys & FOUNDATION}"


def test_opa_step_map_matches_the_registry():
    # OPA cannot read the registry, so catalog.workflow_steps carries what the policy binds a
    # task to: owner, executing templates, the proposal it acts on, review state. It must be
    # exactly the registry's, or the policy judges tasks against a stale workflow.
    feeders = {f: s["id"] for s in STEPS for f in (s.get("feeds") or [])}
    want = {}
    for s in STEPS:
        entry = {
            "owner": s["owner"],
            "templates": [t for t in (s.get("executor"), s.get("snapshot")) if t and t != PER_SERVICE],
            "per_service": s.get("executor") == PER_SERVICE,
            "reviewed": bool(s.get("reviewed")),
        }
        if s["id"] in feeders:  # absent, not null: a null is TRUE in a Rego condition
            entry["proposal_from"] = feeders[s["id"]]
        want[s["id"]] = entry
    assert json.loads(OPA_DATA.read_text())["catalog"]["workflow_steps"] == want


# The read-only step checks a local dry run exercises. Each needs a (Local) twin, because only
# templates-local.yml entries are bound to the working tree; the shared template runs GitHub's
# copy (PR 203 Codex review; MISTAKES 10.9).
LOCAL_DRY_RUN_EXECUTORS = {"Check Secrets", "Verify Service Health"}


def test_local_dry_run_executors_have_a_working_tree_twin():
    local = yaml.safe_load((REPO / "platform/semaphore/templates-local.yml").read_text())["templates"]
    shared = {t["name"]: t["playbook"] for t in yaml.safe_load(CATALOG.read_text())["templates"]}
    twins = {t["name"].removesuffix(" (Local)"): t["playbook"] for t in local}
    executors = {s.get("executor") for s in STEPS}
    assert executors >= LOCAL_DRY_RUN_EXECUTORS
    for name in LOCAL_DRY_RUN_EXECUTORS:
        assert twins.get(name) == shared[name], name
