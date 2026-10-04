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
