"""The service-deployment workflow registry agrees with the catalog, the drawing and OPA.

Spec: change service-deployment-workflow, platform/service-onboarding-workflow
("One registry defines the workflow", scenario "Registry and catalog agree") and
platform/agent-orchestration ("Four least-privilege agent identities").
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / "platform/workflows/service-onboarding"
REGISTRY = WORKFLOW / "registry.yml"
SCHEMAS = WORKFLOW / "schemas"
CATALOG = REPO / "platform/semaphore/templates.yml"
CONFORMANCE_DASHBOARD = REPO / "platform/services/o11y/deployment/config/grafana/dashboards/service-conformance.json"
SERVICE_OVERVIEW_DASHBOARD = REPO / "platform/services/o11y/deployment/config/grafana/dashboards/service-overview.json"
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


def test_twenty_three_steps_in_order():
    assert len(STEPS) == 23
    assert [s["order"] for s in STEPS] == list(range(1, 24))
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


def test_conformance_collection_and_dashboard_absence_are_explicit():
    catalog = yaml.safe_load(CATALOG.read_text())["templates"]
    collectors = [template for template in catalog if template["name"] == "Collect Service Conformance (Dev)"]
    assert len(collectors) == 1
    assert collectors[0]["repository"] == "agent-cloud dev"
    assert "dev_variant" not in collectors[0]
    assert collectors[0]["schedule"] == {"cron": "*/15 * * * *"}
    assert not any(template["name"] == "Collect Service Conformance" for template in catalog)

    conformance = json.loads(CONFORMANCE_DASHBOARD.read_text())
    stats = {panel["title"]: panel for panel in conformance["panels"] if panel["type"] == "stat"}
    assert stats["Recent failed step reports"]["fieldConfig"]["defaults"]["noValue"] == "No recent data"
    assert stats["Services tracked"]["fieldConfig"]["defaults"]["noValue"] == "No recent data"
    failure_query = stats["Recent failed step reports"]["targets"][0]["expr"]
    assert 'status="fail"' in failure_query
    assert 'or (sum(count_over_time({job="agent-cloud-conformance"} [16m])) * 0)' in failure_query
    assert "No conformance records in the window" in stats["Recent failed step reports"]["description"]
    assert "latest recorded result" not in stats["Recent failed step reports"]["description"]

    overview = json.loads(SERVICE_OVERVIEW_DASHBOARD.read_text())
    health = next(panel for panel in overview["panels"] if panel["title"] == "Scrape target health")
    assert health["targets"][0]["expr"] == 'min by (service) (up{service=~"$service"})'
    failed = next(panel for panel in overview["panels"] if panel["title"] == "Unhealthy scrape targets")
    assert failed["targets"][0]["expr"] == 'up{service=~"$service"} == 0'
    loki_panels = [panel for panel in overview["panels"] if panel["datasource"]["type"] == "loki"]
    assert len(loki_panels) == 2
    assert all("does not indicate service health" in panel["description"] for panel in loki_panels)


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


def test_only_the_collector_writes_workflow_status():
    # Spec scenario "Only the collector writes status": the NetBox status fields and the Loki
    # conformance stream have exactly one writer.
    # The raw Loki push lives in ONE shared task (production-internal-ca task 6.0), so the
    # conformance stream's writer is whoever hands that task the aggregate's streams.
    playbooks = REPO / "platform/playbooks"
    files = {str(p.relative_to(REPO)): p.read_text() for p in playbooks.rglob("*.yml")}
    collector = "platform/playbooks/collect-service-conformance.yml"
    assert sorted(f for f, text in files.items() if "ac_workflow_status" in text) == [collector]
    assert sorted(f for f, text in files.items() if "/loki/api/v1/push" in text) == [
        "platform/playbooks/tasks/push-loki-lines.yml"
    ]
    # Code, not comments: the shared task's header shows the collector's call as its example.
    code = {f: "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
            for f, text in files.items()}
    assert sorted(f for f, text in code.items() if "_agg.loki_streams" in text) == [collector]
    assert not [f for f, text in files.items() if "agent-cloud-conformance" in text]


def test_production_conformance_uses_the_exact_private_otlp_receiver():
    collector = yaml.safe_load((REPO / "platform/playbooks/collect-service-conformance.yml").read_text())[0]
    tasks = collector["tasks"]
    pattern = collector["vars"]["_o11y_private_ipv4_pattern"]
    assert re.match(pattern, "10.23.45.67")
    assert re.match(pattern, "192.168.1.2")
    assert re.match(pattern, "172.16.1.2")
    assert re.match(pattern, "172.31.255.255")
    assert not re.match(pattern, "10.23.45.67@external.example")
    assert not re.match(pattern, "10.23.45.67suffix")
    assert not re.match(pattern, "10.23.45.999")
    assert not re.match(pattern, "172.32.1.2")
    guard = next(t for t in tasks if t.get("name") ==
                 "Require the exact private Alloy OTLP/HTTP destination in production")
    assert guard["when"] == "not (local_mode | default(false) | bool)"
    assert "hostvars.get(_semaphore_host, {}).get('collector_otlp_url', '')" in collector["vars"]["_collector_otlp_url"]
    assert any("_collector_otlp_url == 'http://' ~ _o11y_bind ~ ':4318/v1/logs'" in item
               for item in guard["ansible.builtin.assert"]["that"])
    assert any("_o11y_bind is match(_o11y_private_ipv4_pattern)" in item
               for item in guard["ansible.builtin.assert"]["that"])
    push = next(t for t in tasks if t.get("name") ==
                "Production Alloy: deliver conformance records over OTLP/HTTP")
    assert push["ansible.builtin.uri"]["body_format"] == "json"
    assert push["ansible.builtin.uri"]["url"] == "{{ _collector_otlp_url }}"
    assert push["ansible.builtin.uri"]["status_code"] == [200]
    assert push["failed_when"] is False
    assert "retries" not in push
    assert "not (local_mode | default(false) | bool)" in push["when"]
    local = next(t for t in tasks if t.get("name") == "Local Loki: one line per step result")
    assert "local_mode | default(false) | bool" in local["when"]
    assert "collector_loki_url is defined" in local["when"]
    classify = next(t for t in tasks if t.get("name") == "Classify the OTLP response without exposing its body")
    assert "rejectedLogRecords" in classify["ansible.builtin.set_fact"]["_otlp_delivery_ok"]
    assert tasks.index(classify) < next(i for i, t in enumerate(tasks) if t.get("name") == "Report")
    report = next(t for t in tasks if t.get("name") == "Report")
    fail = next(t for t in tasks if t.get("name") == "Fail after reporting unsuccessful production OTLP delivery")
    assert tasks.index(report) < tasks.index(fail)
    assert fail["ansible.builtin.fail"]["msg"] == (
        "Conformance OTLP delivery failed; see the preceding report for status."
    )


def test_production_collector_refuses_missing_or_mismatched_host_destination(tmp_path):
    bind = ".".join(("10", "23", "45", "67"))
    playbook = REPO / "platform/playbooks/collect-service-conformance.yml"
    bind_octets = bind.split(".")
    invalid_binds = [
        f"{bind}@external.example",
        f"{bind}suffix",
        ".".join((*bind_octets[:3], "999")),
    ]
    cases = [
        (bind, {}),
        (bind, {"collector_otlp_url": f"http://{bind}:4317/v1/logs"}),
        *((bad_bind, {"collector_otlp_url": f"http://{bad_bind}:4318/v1/logs"})
          for bad_bind in invalid_binds),
    ]
    for inventory_bind, host_vars in cases:
        inventory = {
            "all": {"children": {
                "semaphore_svc": {"hosts": {"semaphore": host_vars}},
                "o11y_svc": {"hosts": {"o11y": {"o11y_otlp_bind": inventory_bind}}},
            }},
        }
        inventory_path = tmp_path / "inventory.yml"
        inventory_path.write_text(yaml.safe_dump(inventory))
        env = os.environ.copy()
        env.update(ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path),
                   ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
        result = subprocess.run(
            ["ansible-playbook", "-i", str(inventory_path), str(playbook),
             "-e", json.dumps({"local_mode": False})],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=30,
        )
        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "Production conformance delivery requires collector_otlp_url" in output


@pytest.mark.parametrize(
    ("response", "expected_status", "expected_rc", "private_marker"),
    [
        ({"status": 200, "json": {}}, "delivered", 0, None),
        ({"status": 503, "content": "private response body"}, "failed", 1, "private response body"),
        ({"msg": "connection refused"}, "failed", 1, "connection refused"),
        ({"status": 200, "json": {"partialSuccess": {"rejectedLogRecords": 2,
                                                       "errorMessage": "private partial detail"}}},
         "failed", 1, "private partial detail"),
    ],
)
def test_otlp_delivery_is_reported_before_a_generic_failure(tmp_path, response, expected_status,
                                                              expected_rc, private_marker):
    collector = yaml.safe_load((REPO / "platform/playbooks/collect-service-conformance.yml").read_text())[0]
    tasks = collector["tasks"]
    selected = [next(t for t in tasks if t.get("name") == name) for name in (
        "Classify the OTLP response without exposing its body",
        "Report",
        "Fail after reporting unsuccessful production OTLP delivery",
    )]
    fixture = [{
        "name": "Exercise conformance OTLP response reporting",
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {
            "local_mode": False,
            "_otlp": response,
            "_agg": {"report": {}, "otlp_payload": {"resourceLogs": [{}]}},
            "_nb_writes": {"results": []},
            "_vm_missing": [],
            "_vm_ambiguous": [],
            "_vm_unreachable": [],
            "_pick": {"stdout": json.dumps({"window_full": []})},
            "push_loki_result": {"status": 204},
        },
        "tasks": selected,
    }]
    fixture_path = tmp_path / "otlp-response.yml"
    fixture_path.write_text(yaml.safe_dump(fixture))
    env = os.environ.copy()
    env.update(ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path),
               ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
    result = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(fixture_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=30,
    )
    output = result.stdout + result.stderr
    assert (result.returncode != 0) == (expected_rc != 0)
    assert f'"otlp": "{expected_status}"' in output
    if expected_rc:
        assert "Conformance OTLP delivery failed; see the preceding report for status." in output
        assert output.index(f'"otlp": "{expected_status}"') < output.index(
            "Conformance OTLP delivery failed; see the preceding report for status."
        )
    if private_marker:
        assert private_marker not in output


def _emitted_evidence() -> dict[str, set[str]]:
    """step id -> evidence keys, for every playbook that records a step result."""
    found: dict[str, set[str]] = {}

    def walk(tasks):
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            for key in ("block", "rescue", "always"):
                walk(task.get(key))
            include = task.get("ansible.builtin.include_tasks") or ""
            if isinstance(include, str) and include.endswith("emit-step-result.yml"):
                v = task.get("vars", {})
                assert isinstance(v.get("step_result_evidence"), dict), f"non-literal evidence for {v}"
                found.setdefault(v["step_result_step"], set()).update(v["step_result_evidence"])

    for path in (REPO / "platform/playbooks").glob("*.yml"):
        doc = yaml.safe_load(path.read_text())
        for play in doc if isinstance(doc, list) else []:
            if isinstance(play, dict):
                for key in ("pre_tasks", "tasks", "post_tasks"):
                    walk(play.get(key))
    return found


def test_registry_evidence_keys_match_what_the_steps_emit():
    # The registry is what an agent reads to know what a step proves; a key list that
    # disagrees with the emitting playbook misleads it (four of five disagreed, 2026-09-22).
    emitted = _emitted_evidence()
    assert emitted, "no step-result emitters found: the scan is broken"
    by_id = {s["id"]: s for s in STEPS}
    for step, keys in emitted.items():
        assert set(by_id[step]["evidence_keys"]) == keys, step


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


# Templates that predate the service deployment workflow and already run from main. Every
# other template a step names is new with the workflow, so main has no copy of its playbook
# until promotion and only a dev-bound variant can run in production (2026-09-28: Lookup
# Service Inventory, Validate Address Free and the three snapshots had none).
PREDATES_WORKFLOW = {"Create VM Template", "Check Secrets"}


def test_workflow_templates_are_dev_bound_until_promoted():
    catalog = {t["name"]: t for t in yaml.safe_load(CATALOG.read_text())["templates"]}
    missing = sorted(
        name
        for step in STEPS
        for key in ("executor", "snapshot")
        if (name := step.get(key)) not in (None, "none", PER_SERVICE)
        and name not in PREDATES_WORKFLOW
        and not catalog[name].get("dev_variant")
    )
    assert not missing, f"workflow templates with no (Dev) variant: {missing}"


def test_templates_targeting_a_group_ask_for_it():
    """A playbook whose play hosts are `{{ target_service }}` falls back to "ungrouped"
    and reaches no host unless the template offers the field; the launcher refuses a
    non-survey extra var. Six templates had no field (2026-09-28), and production grew
    hand-made per-group copies instead."""
    missing = []
    for tpl in yaml.safe_load(CATALOG.read_text())["templates"]:
        playbook = REPO / tpl["playbook"]
        assert playbook.is_file(), f"{tpl['name']} names a missing playbook: {tpl['playbook']}"
        plays = yaml.safe_load(playbook.read_text()) or []
        hosts = [str(p.get("hosts", "")) for p in plays if isinstance(p, dict)]
        fields = {v["name"] for v in tpl.get("survey_vars") or []}
        if any("target_service" in h for h in hosts) and "target_service" not in fields:
            missing.append(tpl["name"])
    assert not missing, f"templates whose playbook targets target_service without the field: {missing}"


# D10 (tasks 7.1): `reviewed` is stamped only for a step whose executor records its result and
# whose undo is named; an unreviewed executor step says why in `review_gap`.
@pytest.mark.parametrize("step", [s for s in STEPS if not s.get("schema")], ids=lambda s: s["id"])
def test_review_stamp_requires_a_recorded_result_and_a_named_undo(step):
    passed = str(step.get("review_gap", "")).startswith("none in the executor")
    if passed:
        assert step["id"] in _emitted_evidence(), \
            f"{step['id']} claims a passed review but no playbook emits its result"
    if not step.get("reviewed"):
        if step.get("executor"):
            assert step.get("review_gap"), f"{step['id']} is unreviewed without a recorded review_gap"
        return
    assert "review_gap" not in step, f"{step['id']} is reviewed but still carries a review_gap"
    assert step["id"] in _emitted_evidence(), f"{step['id']} is reviewed but no playbook emits its result"
    assert step["undo"] == "none" or step["undo"] in _template_names(), step["id"]
