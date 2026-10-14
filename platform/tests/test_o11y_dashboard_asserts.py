"""The o11y deploy's dashboard assertions name panels that exist in the committed dashboards.

`deploy-o11y.yml` verifies each provisioned dashboard by fetching it from Grafana
(`/api/dashboards/uid/<uid>`) and asserting a panel with an exact title exists once among
`.dashboard.panels`, and for some panels that a query expression contains a metric name.
Those strings are copies of the dashboard JSON. A panel renamed in the JSON without the
playbook following it passes every static check and fails only in a production verify
(Semaphore task 2662: 'Request duration p95' became 'Request duration p95 (HTTP and
model)'). This guard reads both sides and requires them to agree, the way Grafana reads
them: top-level panels only, so a titled panel inside a collapsed row is reported too.
"""

import difflib
import json
import re
from pathlib import Path

import playbook_yaml
import pytest

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/deploy-o11y.yml"
DASHBOARDS = REPO / "platform/services/o11y/deployment/config/grafana/dashboards"

_UID = re.compile(r"/api/dashboards/uid/([A-Za-z0-9_-]+)")
_TITLE = re.compile(
    r"\((?P<var>_\w+)\.stdout \| from_json\)\.dashboard\.panels"
    r" \| selectattr\('title', 'equalto', '(?P<title>[^']+)'\)"
)
_EXPR = re.compile(r"^'(?P<needle>[^']+)' in .*map\(attribute='expr'\)")


def _line_of(text: str, needle: str) -> int:
    for n, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return n
    return 0


def problems(playbook: Path, dashboards: Path) -> tuple[list[str], int]:
    """(findings, number of assertions checked) for one playbook against a dashboards dir."""
    text = playbook.read_text()
    doc = playbook_yaml.loads(text)
    by_uid = {}
    for f in sorted(dashboards.glob("*.json")):
        by_uid[json.loads(f.read_text())["uid"]] = f

    reg_uid: dict[str, str] = {}
    asserts: list[str] = []
    for task in playbook_yaml.tasks(doc):
        if not isinstance(task, dict):
            continue
        if "register" in task:
            m = _UID.search(json.dumps(task))
            if m:
                reg_uid[task["register"]] = m.group(1)
        that = (task.get("ansible.builtin.assert") or task.get("assert") or {}).get("that")
        if isinstance(that, list):
            asserts.extend(s for s in that if isinstance(s, str))

    found, checked = [], 0
    for cond in asserts:
        m = _TITLE.search(cond)
        if not m:
            continue
        checked += 1
        shown = playbook.relative_to(REPO) if playbook.is_relative_to(REPO) else playbook
        where = f"{shown}:{_line_of(text, cond)}"
        var, title = m.group("var"), m.group("title")
        uid = reg_uid.get(var)
        if uid is None or uid not in by_uid:
            found.append(f"{where}: {var} has no fetched dashboard uid with committed JSON ({uid})")
            continue
        panels = json.loads(by_uid[uid].read_text()).get("panels", [])
        top = [p for p in panels if p.get("title") == title]
        nested = [r.get("title") for r in panels for p in r.get("panels", []) if p.get("title") == title]
        if len(top) != 1:
            titles = [p.get("title", "") for p in panels]
            # A rename usually extends the old title; prefer that over edit distance.
            near = [t for t in titles if t.startswith(title)] or difflib.get_close_matches(title, titles, n=1, cutoff=0)
            msg = f"{where}: '{title}' appears {len(top)}x among top-level panels of {by_uid[uid].name}"
            if nested:
                msg += f" (inside collapsed row {nested[0]!r}: Grafana returns it under the row)"
            msg += f"; closest committed title: {near[0] if near else None!r}"
            found.append(msg)
            continue
        e = _EXPR.match(cond)
        if e:
            checked += 1
            exprs = " ".join(t.get("expr", "") for t in top[0].get("targets", []))
            if e.group("needle") not in exprs:
                needle = e.group("needle")
                found.append(f"{where}: '{needle}' not in the exprs of '{title}' in {by_uid[uid].name}")
    return found, checked


def test_every_dashboard_assertion_matches_the_committed_dashboard():
    found, checked = problems(PLAYBOOK, DASHBOARDS)
    assert checked >= 17, f"only {checked} dashboard assertions found; the parser lost them"
    assert not found, "\n".join(found)


@pytest.fixture
def copies(tmp_path):
    dash = tmp_path / "dash"
    dash.mkdir()
    for f in DASHBOARDS.glob("*.json"):
        (dash / f.name).write_text(f.read_text())
    pb = tmp_path / "deploy-o11y.yml"
    pb.write_text(PLAYBOOK.read_text())
    return pb, dash


def test_renamed_panel_is_reported_with_closest_title(copies):
    pb, dash = copies
    f = dash / "agentgateway-client-view.json"
    f.write_text(f.read_text().replace('"First-token latency p95"', '"First-token latency p95 (s)"'))
    found, _ = problems(pb, dash)
    assert len(found) == 1
    assert "'First-token latency p95' appears 0x" in found[0]
    assert "closest committed title: 'First-token latency p95 (s)'" in found[0]


def test_dropped_expr_metric_is_reported(copies):
    pb, dash = copies
    f = dash / "o11y-self-monitoring.json"
    f.write_text(f.read_text().replace("tempo_distributor_spans_received_total", "tempo_spans_total"))
    found, _ = problems(pb, dash)
    assert any("tempo_distributor_spans_received_total" in m for m in found), found


def test_panel_inside_a_collapsed_row_is_reported(copies):
    pb, dash = copies
    f = dash / "agentgateway-traffic.json"
    d = json.loads(f.read_text())
    moved = [p for p in d["panels"] if p["title"] == "Agentgateway build"]
    d["panels"] = [p for p in d["panels"] if p not in moved]
    d["panels"].append({"type": "row", "title": "Build", "collapsed": True, "panels": moved})
    f.write_text(json.dumps(d))
    found, _ = problems(pb, dash)
    assert len(found) == 1 and "collapsed row 'Build'" in found[0], found


def _dashboard(name: str) -> dict:
    return json.loads((DASHBOARDS / name).read_text())


def _panel(dashboard: dict, title: str) -> dict:
    (panel,) = [p for p in dashboard["panels"] if p["title"] == title]
    return panel


def _selector(expr: str) -> str:
    return re.search(r"\{[^{}]+\}", expr).group(0)


def test_service_dashboard_identities_and_selected_service_names_are_explicit():
    conformance = _dashboard("service-conformance.json")
    overview = _dashboard("service-overview.json")
    assert conformance["uid"] == "service-conformance"
    assert overview["uid"] == "service-overview"
    assert all(target["datasource"]["uid"] == "loki"
               for panel in conformance["panels"] for target in panel["targets"])
    assert _panel(overview, "Scrape target health")["datasource"]["uid"] == "prometheus"
    assert _panel(overview, "Selected source records observed (5m)")["datasource"]["uid"] == "loki"
    variables = {item["name"]: item["label"] for item in overview["templating"]["list"]}
    assert variables == {"service": "Metrics service", "log_service": "Logs service"}
    content = _panel(overview, "Selected service identities")["options"]["content"]
    assert "$service" in content and "$log_service" in content


def test_unknown_newest_step_completion_replaces_older_known_age_without_fallback():
    dashboard = _dashboard("service-conformance.json")
    marker_age = _panel(dashboard, "Latest inventory marker age")["targets"][0]["expr"]
    workflow_age = _panel(dashboard, "Latest workflow completion age")["targets"][0]["expr"]
    step_ages = _panel(dashboard, "Latest workflow completion age by service and step")
    step_age_expr = step_ages["targets"][0]["expr"]
    provenance = _panel(dashboard, "Workflow completion and task provenance")
    provenance_expr = provenance["targets"][0]["expr"]
    assert "marker_time_seconds" in marker_age
    assert "completed_at_seconds" in workflow_age and "marker_time_seconds" not in workflow_age
    for expr in (workflow_age, step_age_expr):
        assert expr.index("| json completed_at_seconds") < expr.index("| label_format age_seconds=")
        assert expr.index("| json completed_at_seconds") < expr.index("| drop status")
        assert expr.index("| drop status") < expr.index("| label_format age_seconds=")
        assert 'default "unknown") "unknown"}}-1' in expr
        assert "| unwrap age_seconds" in expr
        assert "[45m]) by (service, step)" in expr
        assert "completed_at_seconds=~" not in expr
    assert ") by (service, step) > -1)" in workflow_age
    assert step_age_expr.endswith("by (service, step)")
    assert step_ages["fieldConfig"]["defaults"]["mappings"][0]["options"]["-1"] == {
        "text": "Unknown", "color": "gray"
    }
    transformations = step_ages["transformations"]
    assert [item["id"] for item in transformations] == [
        "labelsToFields", "merge", "organize", "renameByRegex"
    ]
    assert transformations[0]["options"] == {"mode": "columns"}
    assert transformations[2]["options"]["excludeByName"] == {"Time": True}
    assert transformations[-1]["options"]["renamePattern"] == "Age"
    assert "Latest step status by service" in step_ages["description"]
    assert "task_id, completed_at_seconds" in provenance_expr
    assert 'step!="none"' in provenance_expr
    assert "unknown" in provenance["description"]
    assert "collector" in provenance["description"].lower()
    assert "not workflow completion" in provenance["description"]


def test_overview_source_status_queries_match_disjoint_aggregate_selectors_and_show_empty():
    dashboard = _dashboard("service-overview.json")
    aggregate = _panel(dashboard, "Log volume by source")
    status = _panel(dashboard, "Selected source records observed (5m)")
    aggregate_by_source = {
        target["legendFormat"].split(" · ", 1)[0]: _selector(target["expr"])
        for target in aggregate["targets"]
    }
    status_by_source = {target["legendFormat"]: _selector(target["expr"])
                        for target in status["targets"]}
    assert len(aggregate_by_source) == len(status_by_source) == 5
    assert aggregate_by_source == status_by_source
    assert 'container=~".+"' in aggregate_by_source["Legacy container"]
    assert 'signal!~"^(container|access-log|span)$"' in aggregate_by_source["Legacy container"]
    assert 'signal="container"' in aggregate_by_source["Journal container"]
    assert 'signal="access-log"' in aggregate_by_source["Access"]
    assert 'signal="span"' in aggregate_by_source["Span"]
    assert 'job="agent-cloud-conformance"' in aggregate_by_source["Conformance"]
    assert (
        _selector(_panel(dashboard, "All Loki streams (mixed-source drill-down)")["targets"][0]["expr"])
        not in aggregate_by_source.values()
    )
    assert all("or on() vector(0)" in target["expr"] for target in status["targets"])
    mappings = status["fieldConfig"]["defaults"]["mappings"]
    assert mappings[0]["options"]["0"]["text"] == "No records"
    assert mappings[0]["options"]["0"]["color"] == "gray"
    assert mappings[1]["options"]["result"]["text"] == "Records observed"
    assert "ingestion diagnosis" in status["description"]

    sources = {
        "Legacy container": lambda labels: bool(labels.get("container"))
            and labels.get("signal", "") not in {"container", "access-log", "span"}
            and labels.get("job") != "agent-cloud-conformance",
        "Journal container": lambda labels: labels.get("signal") == "container"
            and labels.get("job") != "agent-cloud-conformance",
        "Access": lambda labels: labels.get("signal") == "access-log"
            and labels.get("job") != "agent-cloud-conformance",
        "Span": lambda labels: labels.get("signal") == "span"
            and labels.get("job") != "agent-cloud-conformance",
        "Conformance": lambda labels: labels.get("job") == "agent-cloud-conformance",
    }
    records = [
        {"container": "app", "signal": "legacy", "job": "alloy"},
        {"container": "app", "signal": "container", "job": "alloy"},
        {"signal": "container", "job": "alloy"},
        {"signal": "access-log", "job": "alloy"},
        {"signal": "span", "job": "alloy"},
        {"job": "agent-cloud-conformance"},
        {"container": "app", "signal": "other", "job": "agent-cloud-conformance"},
        {"job": "alloy"},
    ]
    assert [sum(match(record) for match in sources.values()) for record in records] == [1, 1, 1, 1, 1, 1, 1, 0]


@pytest.mark.parametrize("title, status", [("4xx request ratio", "4.."), ("5xx request ratio", "5..")])
def test_client_view_error_ratio_numerator_is_zero_when_no_error_series(title, status):
    # With no 4xx/5xx series the numerator is an empty vector and the division returns
    # nothing, so the panel stays empty however much traffic arrives (Semaphore task 3302
    # failed "Panels without data" on the 5xx ratio). The numerator must fall back to 0.
    d = json.loads((DASHBOARDS / "agentgateway-client-view.json").read_text())
    (panel,) = [p for p in d["panels"] if p["title"] == title]
    (target,) = panel["targets"]
    # Concatenated, not formatted: the PromQL braces would collide with format placeholders.
    numerator = (
        'sum(rate(agentgateway_requests_total{job="agentgateway", status=~"'
        + status
        + '", identity=~"$identity"}[5m]))'
    )
    assert target["expr"].startswith(f"({numerator} or vector(0)) / "), target["expr"]
