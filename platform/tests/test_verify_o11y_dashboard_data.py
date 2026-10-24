"""Verify o11y Dashboard Data evaluates a provisioned dashboard's Prometheus and Loki panels.

The evaluator (platform/playbooks/files/verify-o11y-dashboard-data.py) is exercised directly
against the committed dashboards and fake backends, then the playbook is run end to end through
ansible-playbook with a local-connection receiver whose deploy dir holds dashboard copies.
"""

import errno
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import forgeries
import pytest
import yaml
from ansible_python import needs_ansible
from fake_http import DrainingHandler

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/verify-o11y-dashboard-data.yml"
WORKFLOW_REGISTRY = REPO / "platform/workflows/service-onboarding/registry.yml"
GRAFANA = REPO / "platform/services/o11y/deployment/config/grafana"
DASHBOARDS = GRAFANA / "dashboards"
_spec = importlib.util.spec_from_file_location(
    "verify_dashboard_data", REPO / "platform/playbooks/files/verify-o11y-dashboard-data.py"
)
vdd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vdd)

# Label values the fake answers with; none may appear in any report.
SECRET_LABELS = ("identity-alpha", "identity-bravo", "model-zulu")


class FakePrometheus:
    """Answers query and query_range; `rules` maps an expression substring to a behaviour."""

    def __init__(self):
        self.rules: dict[str, str] = {}
        self.requests: list[tuple[str, dict]] = []
        outer = self

        class Handler(DrainingHandler):
            def do_POST(self):  # noqa: N802 (http.server API)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read().decode()).items()}
                outer.requests.append((self.path, form))
                status, body = outer.answer(self.path, form)
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/redirect-target")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):  # noqa: N802 (http.server API)
                outer.requests.append((self.path, {}))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def answer(self, path, form):
        behaviour = next((b for key, b in self.rules.items() if key in form["query"]), "data")
        if behaviour == "error":
            return 400, {"status": "error", "errorType": "bad_data", "error": "parse error"}
        if behaviour == "redirect":
            return 302, {"error": "redirect body must not be followed"}
        if behaviour in ("execution", "odd_error"):
            # Prometheus's one-to-one match failure names both colliding label sets.
            text = f"found duplicate series for the match group on the right hand-side: [{', '.join(SECRET_LABELS)}]"
            kind = "execution" if behaviour == "execution" else "something_new"
            return 422, {"status": "error", "errorType": kind, "error": text}
        if behaviour == "structured_error":
            return 422, {"status": "error", "errorType": {"kind": "execution"}, "error": "x"}
        sample = {"data": "1.5", "nan": "NaN", "empty": None}[behaviour]
        series = (
            []
            if sample is None
            else [{"metric": {"identity": name, "gen_ai_request_model": "model-zulu"}} for name in SECRET_LABELS[:2]]
        )
        if path.endswith("/query_range"):
            for s in series:
                s["values"] = [[1700000000, sample], [1700000015, sample]]
            kind = "matrix"
        else:
            for s in series:
                s["value"] = [1700000000, sample]
            kind = "vector"
        return 200, {"status": "success", "data": {"resultType": kind, "result": series}}


class FakeLoki:
    """Answers LogQL queries without retaining or exposing returned log lines."""

    def __init__(self, host="127.0.0.1"):
        self.rules: dict[str, str] = {}
        self.requests: list[tuple[str, dict]] = []
        outer = self

        class Handler(DrainingHandler):
            def do_POST(self):  # noqa: N802 (http.server API)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read().decode()).items()}
                outer.requests.append((self.path, form))
                status, body = outer.answer(self.path, form)
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/redirect-target")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):  # noqa: N802 (http.server API)
                outer.requests.append((self.path, {}))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server_type = ThreadingHTTPServer
        if ":" in host:
            server_type = type("IPv6ThreadingHTTPServer", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
        self.server = server_type((host, 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        url_host = f"[{host}]" if ":" in host else host
        self.url = f"http://{url_host}:{self.server.server_address[1]}"

    def answer(self, path, form):
        behaviour = next((b for key, b in self.rules.items() if key in form["query"]), "streams")
        if behaviour == "error":
            return 400, {"status": "error", "errorType": "bad_data", "error": f"bad query {SECRET_LABELS[0]}"}
        if behaviour == "query_error":
            return 200, {"status": "error", "errorType": "unknown", "error": f"parse {SECRET_LABELS[0]} private log"}
        if behaviour == "redirect":
            return 302, {"error": "redirect body must not be followed"}
        if behaviour == "empty":
            result_type, result = "streams", []
        elif behaviour == "workflow_failure":
            result_type = "vector"
            result = [{
                "metric": {"service": "netbox", "step": "provision-vm"},
                "value": [1700000000, "0"],
            }]
        elif behaviour == "workflow_age":
            result_type = "vector"
            result = [{
                "metric": {"service": "netbox", "step": "provision-vm"},
                "value": [1700000000, "42.5"],
            }]
        elif behaviour == "metric":
            result_type = "vector" if path.endswith("/query") else "matrix"
            point = [1700000000, "2.5"]
            result = [{"metric": {"model": SECRET_LABELS[2]}, "value": point}]
            if result_type == "matrix":
                result[0]["values"] = [[1700000000, "2.5"]]
                result[0].pop("value")
        else:
            result_type = "streams"
            result = [{"stream": {"identity": SECRET_LABELS[0]}, "values": [["1700000000", "private log line"]]}]
        return 200, {"status": "success", "data": {"resultType": result_type, "result": result}}


@pytest.fixture
def loki():
    fake = FakeLoki()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def prometheus():
    fake = FakePrometheus()
    yield fake
    fake.server.shutdown()


def _payload(uid, **extra):
    payload = {
        "dashboards_dir": str(DASHBOARDS),
        "dashboard_uid": uid,
        "lookback": "1h",
        "panel_titles": [],
        "variables": {},
        "prometheus_url": "http://127.0.0.1:9",
        "loki_url": "http://127.0.0.1:9",
        "scrape_interval_seconds": 15,
        "_vdd_workflow_steps": yaml.safe_load(WORKFLOW_REGISTRY.read_text(encoding="utf-8"))["steps"],
    }
    payload.update(extra)
    return payload


def _dashboard_dir(tmp_path, dashboard):
    target = tmp_path / "dashboards"
    target.mkdir(exist_ok=True)
    (target / f"{dashboard['uid']}.json").write_text(json.dumps(dashboard))
    return str(target)


def _synthetic(expr, templating=None, datasource=None):
    return {
        "uid": "synthetic",
        "title": "Synthetic",
        "templating": {"list": templating or []},
        "panels": [
            {"title": "Only", "type": "timeseries", "datasource": datasource, "targets": [{"refId": "A", "expr": expr}]}
        ],
    }


# ── Planning against the committed dashboards ─────────────────────────────────────────────


def test_client_view_resolves_identity_all_value_and_plans_every_panel():
    planned = vdd.plan(_payload("agentgateway-client-view"))
    assert planned["variables"] == {"identity": "all_value"}
    titles = [p["title"] for p in planned["panels"]]
    assert "First-token latency p50" in titles and "First-token latency p95" in titles
    assert "Request rate by identity" in titles
    exprs = [t["expr"] for p in planned["panels"] for t in p["targets"]]
    assert exprs and all('identity=~".*"' in e and "$" not in e for e in exprs)
    assert all(t["modes"] == ["range"] for p in planned["panels"] for t in p["targets"])


@pytest.mark.parametrize(
    "uid", ["inference-fleet-health", "inference-latency-capacity", "inference-placement-comparison"]
)
def test_inference_dashboards_plan_without_overrides(uid):
    planned = vdd.plan(_payload(uid))
    exprs = [t["expr"] for p in planned["panels"] for t in p["targets"] if "expr" in t]
    assert exprs and not any("$" in e for e in exprs)
    skipped = [t for p in planned["panels"] for t in p["targets"] if t.get("status") == "skipped"]
    if uid == "inference-fleet-health":
        assert {t["datasource"] for t in skipped} == {"loki"}


def test_placement_comparison_uses_the_saved_custom_offset_and_honours_an_override():
    planned = vdd.plan(_payload("inference-placement-comparison"))
    assert planned["variables"] == {"model_alias": "all_value", "compare_offset": "saved_value"}
    exprs = [t["expr"] for p in planned["panels"] for t in p["targets"]]
    assert any("offset 1d" in e for e in exprs)
    overridden = vdd.plan(_payload("inference-placement-comparison", variables={"compare_offset": "7d"}))
    assert overridden["variables"]["compare_offset"] == "override sha256:" + hashlib.sha256(b"7d").hexdigest()[:12]
    assert any("offset 7d" in t["expr"] for p in overridden["panels"] for t in p["targets"])


def test_instant_targets_query_instant_only():
    planned = vdd.plan(_payload("agentgateway-traffic", panel_titles=["Gateway scrape up"]))
    assert [t["modes"] for p in planned["panels"] for t in p["targets"]] == [["instant"]]


def test_service_overview_plans_saved_query_defaults_without_overrides():
    planned = vdd.plan(
        _payload(
            "service-overview",
            panel_titles=["Scrape target health", "Log volume by source"],
        )
    )
    assert planned["variables"] == {"service": "saved_value", "log_service": "saved_value"}
    expressions = {
        panel["title"]: [target["expr"] for target in panel["targets"] if "expr" in target]
        for panel in planned["panels"]
    }
    assert any('service=~"agentgateway"' in expr for expr in expressions["Scrape target health"])
    assert all('service=~"agentgateway"' in expr for expr in expressions["Log volume by source"])
    assert all(
        "$service" not in expr and "$log_service" not in expr
        for exprs in expressions.values()
        for expr in exprs
    )


@pytest.mark.parametrize(
    "current",
    [None, "$__all", "All", "foo.bar", 'agentgateway" or vector(1)', "a" * 129],
)
def test_an_unresolved_or_unsafe_query_default_is_refused(tmp_path, current):
    variable = {"name": "service", "type": "query", "current": {"value": current}}
    dashboard = _synthetic('up{service=~"$service"}', [variable], datasource={"type": "prometheus"})
    with pytest.raises(vdd.Refused, match="'service' has no default"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


@pytest.mark.parametrize("selection_options", [{"multi": True}, {"includeAll": True}])
def test_query_default_refuses_multi_select_or_all_selection(tmp_path, selection_options):
    variable = {
        "name": "service",
        "type": "query",
        "current": {"value": "agentgateway"},
        **selection_options,
    }
    dashboard = _synthetic('up{service=~"$service"}', [variable], datasource={"type": "prometheus"})
    with pytest.raises(vdd.Refused, match="'service' has no default"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


def test_a_query_variable_with_no_saved_default_remains_refused(tmp_path):
    dashboard = _synthetic(
        'up{service=~"$service"}',
        [{"name": "service", "type": "query"}],
        datasource={"type": "prometheus"},
    )
    with pytest.raises(vdd.Refused, match="'service' has no default"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


def test_unknown_panel_title_override_or_uid_is_refused():
    with pytest.raises(vdd.Refused, match="not on this dashboard"):
        vdd.plan(_payload("agentgateway-client-view", panel_titles=["No such panel"]))
    with pytest.raises(vdd.Refused, match="not declared"):
        vdd.plan(_payload("agentgateway-client-view", variables={"nope": "x"}))
    with pytest.raises(vdd.Refused, match="0 provisioned dashboards"):
        vdd.plan(_payload("no-such-dashboard"))
    with pytest.raises(vdd.Refused, match="one Prometheus duration unit"):
        vdd.plan(_payload("agentgateway-client-view", lookback="1h30m"))


def test_a_loki_only_selection_is_planned():
    planned = vdd.plan(_payload("inference-fleet-health", panel_titles=["Recent vLLM journal"]))
    assert planned["panels"][0]["targets"][0]["datasource"] == "loki"
    assert planned["panels"][0]["targets"][0]["modes"] == ["range"]


def test_dashboard_panel_time_from_is_planned_for_both_relative_windows():
    planned = vdd.plan(_payload(
        "service-conformance",
        panel_titles=["Recent failed task snapshots", "Workflow completion and task provenance"],
    ))
    by_title = {panel["title"]: panel for panel in planned["panels"]}
    assert by_title["Recent failed task snapshots"]["lookback"] == 7 * 86400
    assert by_title["Workflow completion and task provenance"]["lookback"] == 45 * 60


def test_real_time_from_panels_query_their_own_range(loki):
    titles = ["Recent failed task snapshots", "Workflow completion and task provenance"]
    report = vdd.evaluate(
        _payload("service-conformance", panel_titles=titles, loki_url=loki.url), now=1700000000
    )
    assert report["status"] == "pass", report
    assert [form["start"] for _, form in loki.requests] == ["1699395200.000", "1699997300.000"]
    assert [form["end"] for _, form in loki.requests] == ["1700000000.000", "1700000000.000"]


def _numeric_vector(values):
    return {
        "resultType": "vector",
        "result": [
            {"metric": {"service": "sanitized-test-service"}, "value": [1700000000, str(value)]}
            for value in values
        ],
    }


def _workflow_step_vector(rows):
    return {
        "resultType": "vector",
        "result": [
            {"metric": labels, "value": [1700000000, str(state)]}
            for labels, state in rows
        ],
    }


def _observe_dashboard(monkeypatch, uid, title, data):
    seen = []

    def query(base, backend, mode, expr, end, lookback, step):
        seen.append((backend, mode, expr))
        if data == "query_error":
            return {"error": "execution"}
        return {"data": data}

    monkeypatch.setattr(vdd, "_query", query)
    report = vdd.evaluate(_payload(uid, panel_titles=[title]), now=1700000000)
    assert report["status"] in ("pass", "fail")
    assert len(seen) == 1
    assert "sanitized-test-service" not in json.dumps(report)
    return report["panels"][0]


@pytest.mark.parametrize(("value", "observation"), [(1, "healthy"), (0, "unhealthy")])
def test_known_scrape_health_observation_does_not_change_query_validity(monkeypatch, value, observation):
    panel = _observe_dashboard(
        monkeypatch, "service-overview", "Scrape target health", _numeric_vector([value])
    )
    assert panel["status"] == "pass"
    assert panel["observation"] == {"name": "scrape_health", "status": observation}


def test_known_scrape_health_distinguishes_absent_from_zero(monkeypatch):
    panel = _observe_dashboard(monkeypatch, "service-overview", "Scrape target health", _numeric_vector([]))
    assert panel["status"] == "empty"
    assert panel["observation"] == {"name": "scrape_health", "status": "no_metrics"}


@pytest.mark.parametrize(("age", "observation"), [(1799, "fresh"), (1800, "fresh"), (1801, "stale")])
def test_inventory_marker_freshness_uses_the_provisioned_boundary(monkeypatch, age, observation):
    panel = _observe_dashboard(
        monkeypatch, "service-conformance", "Latest inventory marker age", _numeric_vector([age])
    )
    assert panel["status"] == "pass"
    assert panel["observation"] == {"name": "inventory_marker_freshness", "status": observation}


def test_inventory_marker_absence_is_not_zero_age(monkeypatch):
    panel = _observe_dashboard(
        monkeypatch, "service-conformance", "Latest inventory marker age", _numeric_vector([])
    )
    assert panel["status"] == "empty"
    assert panel["observation"] == {"name": "inventory_marker_freshness", "status": "no_data"}


@pytest.mark.parametrize(("values", "observation"), [([0], "none"), ([1], "present")])
def test_current_workflow_failure_observation_preserves_zero_and_failure(monkeypatch, values, observation):
    panel = _observe_dashboard(
        monkeypatch, "service-conformance", "Current failed step states", _numeric_vector(values)
    )
    assert panel["status"] == "pass"
    assert panel["observation"] == {"name": "current_workflow_failures", "status": observation}


def test_current_workflow_failure_absence_differs_from_zero(monkeypatch):
    panel = _observe_dashboard(
        monkeypatch, "service-conformance", "Current failed step states", _numeric_vector([])
    )
    assert panel["status"] == "empty"
    assert panel["observation"] == {"name": "current_workflow_failures", "status": "no_data"}


def test_latest_workflow_status_reports_only_registry_backed_current_failure_pairs(monkeypatch):
    data = _workflow_step_vector([
        ({"service": "netbox", "step": "provision-vm"}, 0),
        ({"service": "netbox", "step": "service-validate"}, 1),
    ])
    panel = _observe_dashboard(monkeypatch, "service-conformance", "Latest step status by service", data)
    assert panel["status"] == "pass"
    assert panel["observation"] == {
        "name": "current_workflow_failed_steps",
        "status": "present",
        "failed_step_count": 1,
        "failed_steps": [{
            "service": "netbox", "step": "provision-vm", "completion_age_seconds": None,
        }],
        "truncated_count": 0,
        "unknown_series_count": 0,
        "unknown_label_shape_count": 0,
        "unknown_identity_count": 0,
        "unknown_sample_count": 0,
        "duplicate_pair_count": 0,
        "window_seconds": 2700,
        "scope": "latest_recorded_state",
        "completion_age_status": "not_selected",
        "failed_pairs_with_known_completion_age": 0,
        "failed_pairs_with_unknown_completion_age": 1,
        "completion_age_diagnostics": {
            "registry_unavailable_count": 0,
            "invalid_result_shape_count": 0,
            "invalid_label_shape_count": 0,
            "unknown_identity_count": 0,
            "duplicate_pair_count": 0,
            "age_rows_with_unavailable_value_count": 0,
            "failed_pairs_missing_age_row_count": 0,
            "failed_pairs_with_unavailable_age_value_count": 0,
        },
    }
    assert panel["observation"]["failed_steps"][0]["completion_age_seconds"] is None


def test_unknown_workflow_series_are_split_into_fixed_categories_without_echo(monkeypatch):
    data = {"resultType": "vector", "result": [
        {"metric": {"service": "netbox", "step": "provision-vm", "private": "private-label"},
         "value": [1700000000, "0"]},
        {"metric": {"service": "private-service", "step": "provision-vm"},
         "value": [1700000000, "0"]},
        {"metric": {"service": "netbox", "step": "service-validate"}, "value": "bad-sample"},
        {"metric": {"service": "netbox", "step": "provision-vm"}, "value": [1700000000, "0"]},
        {"metric": {"service": "netbox", "step": "provision-vm"}, "value": [1700000001, "1"]},
    ]}
    panel = _observe_dashboard(monkeypatch, "service-conformance", "Latest step status by service", data)
    observation = panel["observation"]
    assert observation["status"] == "unverified"
    assert observation["unknown_series_count"] == 4
    assert observation["unknown_label_shape_count"] == 1
    assert observation["unknown_identity_count"] == 1
    assert observation["unknown_sample_count"] == 1
    assert observation["duplicate_pair_count"] == 1
    assert "private-label" not in json.dumps(observation)
    assert "private-service" not in json.dumps(observation)


def test_selected_latest_status_joins_only_exact_completion_age_panel(monkeypatch):
    status_data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 0)])
    age_data = {
        "resultType": "vector",
        "result": [{
            "metric": {"service": "netbox", "step": "provision-vm"},
            "value": [1700000000, "42.5"],
        }],
    }
    seen = []

    def query(base, backend, mode, expr, end, lookback, step):
        seen.append(expr)
        return {"data": age_data if "completed_at_seconds" in expr else status_data}

    monkeypatch.setattr(vdd, "_query", query)
    report = vdd.evaluate(_payload(
        "service-conformance",
        panel_titles=["Latest step status by service", "Latest workflow completion age by service and step"],
    ), now=1700000000)
    failure = next(p for p in report["panels"] if p["title"] == "Latest step status by service")["observation"]
    assert len(seen) == 2
    assert failure["status"] == "present"
    assert failure["completion_age_status"] == "observed"
    assert failure["failed_steps"] == [{
        "service": "netbox", "step": "provision-vm", "completion_age_seconds": 42.5,
    }]
    assert failure["failed_pairs_with_known_completion_age"] == 1
    assert failure["failed_pairs_with_unknown_completion_age"] == 0
    assert failure["completion_age_diagnostics"] == {
        "registry_unavailable_count": 0,
        "invalid_result_shape_count": 0,
        "invalid_label_shape_count": 0,
        "unknown_identity_count": 0,
        "duplicate_pair_count": 0,
        "age_rows_with_unavailable_value_count": 0,
        "failed_pairs_missing_age_row_count": 0,
        "failed_pairs_with_unavailable_age_value_count": 0,
    }


@pytest.mark.parametrize(
    ("age_rows", "expected_status", "expected_diagnostics"),
    [
        (
            [
                {"metric": {"service": "netbox", "step": "provision-vm", "private": "x"},
                 "value": [1700000000, "42"]},
                {"metric": {"service": "private-service", "step": "provision-vm"},
                 "value": [1700000000, "42"]},
            ],
            "unverified",
            {"invalid_label_shape_count": 1, "unknown_identity_count": 1},
        ),
        (
            [{"metric": {"service": "netbox", "step": "service-validate"},
              "value": [1700000000, "42"]}],
            "observed",
            {"failed_pairs_missing_age_row_count": 1},
        ),
        (
            [{"metric": {"service": "netbox", "step": "provision-vm"},
              "value": [1700000000, "-1"]}],
            "observed",
            {
                "age_rows_with_unavailable_value_count": 1,
                "failed_pairs_with_unavailable_age_value_count": 1,
            },
        ),
        (
            [
                {"metric": {"service": "netbox", "step": "provision-vm"},
                 "value": [1700000000, "42"]},
                {"metric": {"service": "netbox", "step": "provision-vm"},
                 "value": [1700000001, "43"]},
            ],
            "observed",
            {
                "duplicate_pair_count": 1,
                "failed_pairs_with_unavailable_age_value_count": 1,
            },
        ),
        (
            [
                {"metric": {"service": "netbox", "step": "provision-vm"},
                 "value": [1700000000, "42"]},
                {"metric": {"service": "netbox", "step": "provision-vm"},
                 "value": [1700000001, "-1"]},
            ],
            "observed",
            {
                "duplicate_pair_count": 1,
                "age_rows_with_unavailable_value_count": 1,
                "failed_pairs_with_unavailable_age_value_count": 1,
            },
        ),
        (
            [
                {"metric": {"service": "netbox", "step": "provision-vm"},
                 "value": [1700000000, "-1"]},
                {"metric": {"service": "netbox", "step": "provision-vm"},
                 "value": [1700000001, "42"]},
            ],
            "observed",
            {
                "duplicate_pair_count": 1,
                "age_rows_with_unavailable_value_count": 1,
                "failed_pairs_with_unavailable_age_value_count": 1,
            },
        ),
        (
            [
                {"metric": {"service": "netbox", "step": "provision-vm"},
                 "value": [1700000000, "-1"]},
                {"metric": {"service": "netbox", "step": "provision-vm"},
                 "value": [1700000001, "-1"]},
            ],
            "observed",
            {
                "duplicate_pair_count": 1,
                "age_rows_with_unavailable_value_count": 2,
                "failed_pairs_with_unavailable_age_value_count": 1,
            },
        ),
    ],
)
def test_completion_age_reports_fixed_rejection_and_pair_join_counts(
    monkeypatch, age_rows, expected_status, expected_diagnostics
):
    status_data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 0)])
    age_data = {"resultType": "vector", "result": age_rows}

    def query(_base, _backend, _mode, expr, _end, _lookback, _step):
        return {"data": age_data if "completed_at_seconds" in expr else status_data}

    monkeypatch.setattr(vdd, "_query", query)
    report = vdd.evaluate(_payload(
        "service-conformance",
        panel_titles=["Latest step status by service", "Latest workflow completion age by service and step"],
    ), now=1700000000)
    observation = next(
        panel for panel in report["panels"] if panel["title"] == "Latest step status by service"
    )["observation"]
    diagnostics = observation["completion_age_diagnostics"]

    assert observation["completion_age_status"] == expected_status
    assert observation["failed_steps"][0]["completion_age_seconds"] is None
    assert set(diagnostics) == set(vdd.COMPLETION_AGE_DIAGNOSTIC_COUNTERS)
    assert {key: value for key, value in diagnostics.items() if value} == expected_diagnostics
    serialized = json.dumps(observation)
    assert "private-service" not in serialized
    assert '"private"' not in serialized


@pytest.mark.parametrize("rejection", ["registry", "result_shape"])
def test_completion_age_reports_registry_and_result_shape_rejections(monkeypatch, rejection):
    status_data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 0)])
    age_data = {"resultType": "matrix", "result": []}
    if rejection == "registry":
        monkeypatch.setattr(vdd, "_workflow_identity_allowlists", lambda *_args: None)

    def query(_base, _backend, _mode, expr, _end, _lookback, _step):
        return {"data": age_data if "completed_at_seconds" in expr else status_data}

    monkeypatch.setattr(vdd, "_query", query)
    report = vdd.evaluate(_payload(
        "service-conformance",
        panel_titles=["Latest step status by service", "Latest workflow completion age by service and step"],
    ), now=1700000000)
    observation = next(
        panel for panel in report["panels"] if panel["title"] == "Latest step status by service"
    )["observation"]
    diagnostics = observation["completion_age_diagnostics"]
    expected_key = "registry_unavailable_count" if rejection == "registry" else "invalid_result_shape_count"

    assert observation["completion_age_status"] == "unverified"
    if rejection == "result_shape":
        assert observation["failed_steps"][0]["completion_age_seconds"] is None
    else:
        assert observation["failed_steps"] == []
    assert diagnostics[expected_key] == 1
    assert sum(diagnostics.values()) == 1


@pytest.mark.parametrize("ambiguity", ["wrong_uid", "wrong_type", "duplicate"])
def test_completion_age_ignores_mixed_or_ambiguous_targets_after_exact_target_error(
    monkeypatch, tmp_path, ambiguity
):
    dashboard = json.loads((DASHBOARDS / "service-conformance.json").read_text())
    age_panel = next(
        panel for panel in dashboard["panels"]
        if panel.get("title") == "Latest workflow completion age by service and step"
    )
    exact_target = age_panel["targets"][0]
    other_target = json.loads(json.dumps(exact_target))
    if ambiguity == "wrong_uid":
        other_target["datasource"]["uid"] = "other-loki"
    elif ambiguity == "wrong_type":
        other_target["datasource"] = {"type": "prometheus", "uid": "prometheus"}
    age_panel["targets"] = [exact_target, other_target]

    status_data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 0)])
    age_data = {
        "resultType": "vector",
        "result": [{
            "metric": {"service": "netbox", "step": "provision-vm"},
            "value": [1700000000, "42"],
        }],
    }
    age_queries = 0

    def query(_base, _backend, _mode, expr, _end, _lookback, _step):
        nonlocal age_queries
        if "completed_at_seconds" in expr:
            age_queries += 1
            return {"error": "target-specific query failure"} if age_queries == 1 else {"data": age_data}
        return {"data": status_data}

    original_allowlists = vdd._workflow_identity_allowlists
    monkeypatch.setattr(
        vdd, "_workflow_identity_allowlists",
        lambda _dashboards_dir, steps: original_allowlists(str(DASHBOARDS), steps),
    )
    monkeypatch.setattr(vdd, "_query", query)
    report = vdd.evaluate(_payload(
        "service-conformance",
        panel_titles=["Latest step status by service", "Latest workflow completion age by service and step"],
        dashboards_dir=_dashboard_dir(tmp_path, dashboard),
    ), now=1700000000)

    observation = next(
        panel for panel in report["panels"] if panel["title"] == "Latest step status by service"
    )["observation"]
    assert report["status"] == "fail"
    assert age_queries == 2
    assert observation["completion_age_status"] == "unverified"
    assert observation["failed_steps"] == [{
        "service": "netbox", "step": "provision-vm", "completion_age_seconds": None,
    }]
    assert observation["failed_pairs_with_known_completion_age"] == 0
    assert observation["failed_pairs_with_unknown_completion_age"] == 1


@pytest.mark.parametrize(
    "age_rows",
    [
        [],
        [{"metric": {"service": "netbox", "step": "provision-vm"}, "value": [1700000000, "-1"]}],
        [
            {"metric": {"service": "netbox", "step": "provision-vm"}, "value": [1700000000, "42"]},
            {"metric": {"service": "netbox", "step": "provision-vm"}, "value": [1700000001, "43"]},
        ],
        [{"metric": {"service": "netbox", "step": "service-validate"}, "value": [1700000000, "42"]}],
        [{"metric": {"service": "netbox", "step": "provision-vm"}, "value": [1700000000, "nan"]}],
        [{"metric": {"service": "netbox", "step": "provision-vm"}, "value": [1700000000, False]}],
        [{"metric": {"service": "netbox", "step": "provision-vm"}, "value": [1700000000, "-2"]}],
        [{"metric": {"service": "netbox", "step": "provision-vm"}, "value": ["invalid-time", "42"]}],
        [{"metric": {"service": "netbox", "step": "provision-vm", "private": "private-label"},
          "value": [1700000000, "42"]}],
    ],
)
def test_invalid_or_unavailable_age_rows_remain_null(monkeypatch, age_rows):
    status_data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 0)])
    age_data = {"resultType": "vector", "result": age_rows}

    def query(base, backend, mode, expr, end, lookback, step):
        return {"data": age_data if "completed_at_seconds" in expr else status_data}

    monkeypatch.setattr(vdd, "_query", query)
    report = vdd.evaluate(_payload(
        "service-conformance",
        panel_titles=["Latest step status by service", "Latest workflow completion age by service and step"],
    ), now=1700000000)
    observation = next(p for p in report["panels"] if p["title"] == "Latest step status by service")["observation"]
    assert observation["failed_steps"][0]["completion_age_seconds"] is None
    assert observation["failed_pairs_with_known_completion_age"] == 0
    assert observation["failed_pairs_with_unknown_completion_age"] == 1


@pytest.mark.parametrize("drift", ["expr", "datasource"])
def test_age_contract_drift_does_not_authorize_age_join(monkeypatch, tmp_path, drift):
    dashboard = json.loads((DASHBOARDS / "service-conformance.json").read_text())
    for panel in dashboard["panels"]:
        if panel.get("title") == "Latest workflow completion age by service and step":
            if drift == "expr":
                panel["targets"][0]["expr"] += " + 0"
            else:
                panel["targets"][0]["datasource"] = {"type": "prometheus", "uid": "prometheus"}
    original_allowlists = vdd._workflow_identity_allowlists
    monkeypatch.setattr(
        vdd, "_workflow_identity_allowlists",
        lambda _dashboards_dir, steps: original_allowlists(str(DASHBOARDS), steps),
    )
    status_data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 0)])
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": status_data})
    report = vdd.evaluate(_payload(
        "service-conformance",
        panel_titles=["Latest step status by service", "Latest workflow completion age by service and step"],
        dashboards_dir=_dashboard_dir(tmp_path, dashboard),
    ), now=1700000000)
    observation = next(p for p in report["panels"] if p["title"] == "Latest step status by service")["observation"]
    assert observation["status"] == "present"
    assert observation["completion_age_status"] == "unverified"
    assert observation["failed_steps"][0]["completion_age_seconds"] is None


def test_completion_age_join_is_bounded_to_returned_failure_pairs(monkeypatch):
    service_ids, step_ids = vdd._workflow_identity_allowlists(
        str(DASHBOARDS), _payload("service-conformance")["_vdd_workflow_steps"]
    )
    pairs = [(service, step) for service in sorted(service_ids) for step in sorted(step_ids)][:51]
    assert len(pairs) == 51
    status_data = _workflow_step_vector([
        ({"service": service, "step": step}, 0) for service, step in pairs
    ])
    age_data = {
        "resultType": "vector",
        "result": [
            {"metric": {"service": service, "step": step}, "value": [1700000000, "42"]}
            for service, step in pairs
        ],
    }
    monkeypatch.setattr(
        vdd, "_query",
        lambda *args: {"data": age_data if "completed_at_seconds" in args[3] else status_data},
    )
    report = vdd.evaluate(_payload(
        "service-conformance",
        panel_titles=["Latest step status by service", "Latest workflow completion age by service and step"],
    ), now=1700000000)
    observation = next(p for p in report["panels"] if p["title"] == "Latest step status by service")["observation"]
    assert observation["failed_step_count"] == 51
    assert observation["truncated_count"] == 1
    assert len(observation["failed_steps"]) == 50
    assert observation["failed_pairs_with_known_completion_age"] == 50
    assert observation["failed_pairs_with_unknown_completion_age"] == 0


def test_latest_status_query_reduction_does_not_relabel_a_later_pass_as_failure(monkeypatch):
    data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 1)])
    panel = _observe_dashboard(monkeypatch, "service-conformance", "Latest step status by service", data)
    assert panel["observation"]["status"] == "none"
    assert panel["observation"]["failed_steps"] == []
    assert panel["observation"]["window_seconds"] == 2700
    assert panel["observation"]["scope"] == "latest_recorded_state"


def test_latest_status_skip_is_not_reported_as_a_current_failure(monkeypatch):
    data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 2)])
    panel = _observe_dashboard(monkeypatch, "service-conformance", "Latest step status by service", data)
    assert panel["observation"]["status"] == "none"
    assert panel["observation"]["failed_steps"] == []


def test_latest_status_empty_result_is_no_data_not_a_cleared_failure(monkeypatch):
    data = _workflow_step_vector([])
    panel = _observe_dashboard(monkeypatch, "service-conformance", "Latest step status by service", data)
    assert panel["status"] == "empty"
    assert panel["observation"]["status"] == "no_data"
    assert panel["observation"]["failed_steps"] == []
    assert panel["observation"]["window_seconds"] == 2700
    assert panel["observation"]["scope"] == "latest_recorded_state"


@pytest.mark.parametrize(
    "sample",
    [
        "10",
        [1700000000, False],
        ["invalid-time", "1"],
        [1700000000, "1", "unexpected-extra"],
        [True, "0"],
        [1700000000, 0],
        [10**1000, "0"],
    ],
)
def test_malformed_latest_status_sample_is_unknown_not_failure_or_none(monkeypatch, sample):
    data = {"resultType": "vector", "result": [{
        "metric": {"service": "netbox", "step": "provision-vm"},
        "value": sample,
    }]}
    panel = _observe_dashboard(monkeypatch, "service-conformance", "Latest step status by service", data)
    observation = panel["observation"]
    assert observation["status"] == "unverified"
    assert observation["unknown_series_count"] == 1
    assert observation["failed_steps"] == []
    assert observation["window_seconds"] == 2700
    assert observation["scope"] == "latest_recorded_state"


def test_duplicate_current_status_for_a_pair_is_unverified(monkeypatch):
    data = _workflow_step_vector([
        ({"service": "netbox", "step": "provision-vm"}, 0),
        ({"service": "netbox", "step": "provision-vm"}, 1),
    ])
    panel = _observe_dashboard(monkeypatch, "service-conformance", "Latest step status by service", data)
    assert panel["observation"]["status"] == "unverified"
    assert panel["observation"]["failed_steps"] == []
    assert panel["observation"]["unknown_series_count"] == 1


def test_duplicate_pair_with_malformed_sample_is_not_reported_as_current_failure(monkeypatch):
    rows = [
        {"metric": {"service": "netbox", "step": "provision-vm"}, "value": [1700000000, "0"]},
        {"metric": {"service": "netbox", "step": "provision-vm"}, "value": "malformed"},
    ]
    observations = []
    for ordered_rows in (rows, list(reversed(rows))):
        data = {"resultType": "vector", "result": ordered_rows}
        panel = _observe_dashboard(
            monkeypatch, "service-conformance", "Latest step status by service", data
        )
        observations.append(panel["observation"])
    for observation in observations:
        assert observation["status"] == "unverified"
        assert observation["failed_steps"] == []
        assert observation["unknown_series_count"] == 1
        assert observation["unknown_sample_count"] == 0
        assert observation["duplicate_pair_count"] == 1


def test_unknown_identity_labels_are_counted_but_never_echoed(monkeypatch):
    data = _workflow_step_vector([
        ({"service": "netbox", "step": "provision-vm", "identity": "private-user-123"}, 0),
        ({"service": "private-user-123", "step": "provision-vm"}, 0),
    ])
    panel = _observe_dashboard(monkeypatch, "service-conformance", "Latest step status by service", data)
    serialized = json.dumps(panel)
    assert panel["observation"]["status"] == "unverified"
    assert panel["observation"]["unknown_series_count"] == 2
    assert "private-user-123" not in serialized


def test_failed_step_output_is_capped_and_deterministic(monkeypatch):
    service_ids, step_ids = vdd._workflow_identity_allowlists(
        str(DASHBOARDS), _payload("service-conformance")["_vdd_workflow_steps"]
    )
    pairs = [(service, step) for service in sorted(service_ids) for step in sorted(step_ids)][:51]
    assert len(pairs) == 51
    data = _workflow_step_vector([({"service": service, "step": step}, 0) for service, step in pairs])
    panel = _observe_dashboard(monkeypatch, "service-conformance", "Latest step status by service", data)
    observation = panel["observation"]
    assert observation["status"] == "present"
    assert observation["failed_step_count"] == 51
    assert len(observation["failed_steps"]) == 50
    assert observation["truncated_count"] == 1
    assert observation["failed_steps"] == sorted(
        observation["failed_steps"], key=lambda row: (row["service"], row["step"])
    )


@pytest.mark.parametrize("drift", ["expr", "datasource"])
def test_changed_latest_status_contract_is_unverified_without_returning_labels(monkeypatch, tmp_path, drift):
    dashboard = json.loads((DASHBOARDS / "service-conformance.json").read_text())
    dashboard["panels"] = [
        p for p in dashboard["panels"] if p.get("title") == "Latest step status by service"
    ]
    if drift == "expr":
        dashboard["panels"][0]["targets"][0]["expr"] += " + 0"
    else:
        dashboard["panels"][0]["targets"][0]["datasource"] = {"type": "prometheus", "uid": "prometheus"}
    data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 0)])
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": data})
    report = vdd.evaluate(
        _payload(
            "service-conformance",
            panel_titles=["Latest step status by service"],
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
        ),
        now=1700000000,
    )
    assert report["status"] == "pass"
    assert report["panels"][0]["observation"] == {
        "name": "current_workflow_failed_steps",
        "status": "unverified",
        "reason": "contract_mismatch",
        "window_seconds": 2700,
        "scope": "latest_recorded_state",
        "unknown_series_count": 0,
        "unknown_label_shape_count": 0,
        "unknown_identity_count": 0,
        "unknown_sample_count": 0,
        "duplicate_pair_count": 0,
        "failed_step_count": 0,
        "failed_steps": [],
        "truncated_count": 0,
        "completion_age_status": "not_selected",
        "failed_pairs_with_known_completion_age": 0,
        "failed_pairs_with_unknown_completion_age": 0,
        "completion_age_diagnostics": {
            "registry_unavailable_count": 0,
            "invalid_result_shape_count": 0,
            "invalid_label_shape_count": 0,
            "unknown_identity_count": 0,
            "duplicate_pair_count": 0,
            "age_rows_with_unavailable_value_count": 0,
            "failed_pairs_missing_age_row_count": 0,
            "failed_pairs_with_unavailable_age_value_count": 0,
        },
    }
    assert "netbox" not in json.dumps(report)


def test_unavailable_public_identity_sources_are_unverified_without_labels(monkeypatch, tmp_path):
    dashboard = json.loads((DASHBOARDS / "service-conformance.json").read_text())
    dashboard["panels"] = [
        p for p in dashboard["panels"] if p.get("title") == "Latest step status by service"
    ]
    dashboard_dir = _dashboard_dir(tmp_path, dashboard)
    data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 0)])
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": data})
    report = vdd.evaluate(
        _payload("service-conformance", panel_titles=["Latest step status by service"], dashboards_dir=dashboard_dir),
        now=1700000000,
    )
    assert report["status"] == "pass"
    assert report["panels"][0]["observation"]["status"] == "unverified"
    assert report["panels"][0]["observation"]["unknown_series_count"] == 1
    assert "netbox" not in json.dumps(report)


@pytest.mark.parametrize(
    "registry",
    [
        None,
        [],
        {},
        {"steps": "not-a-list"},
        {"steps": {"forged": {"id": "forged-step"}}},
        {"steps": []},
        {"steps": [], "unrelated": [{"id": "forged-step"}]},
        {"steps": ["not-a-mapping"]},
        {"steps": [{"name": "missing-id"}]},
        {"steps": [{"id": "invalid id"}]},
        {"steps": [{"id": "provision-vm"}, {"id": "provision-vm"}]},
    ],
)
def test_malformed_workflow_registry_is_unverified_without_returning_unknown_step(monkeypatch, registry):
    payload = _payload(
        "service-conformance",
        panel_titles=["Latest step status by service"],
        _vdd_workflow_steps=registry.get("steps") if isinstance(registry, dict) else registry,
    )
    data = _workflow_step_vector([({"service": "netbox", "step": "forged-step"}, 0)])
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": data})
    report = vdd.evaluate(payload, now=1700000000)
    observation = report["panels"][0]["observation"]
    assert observation["status"] == "unverified"
    assert observation["unknown_series_count"] == 1
    assert "forged-step" not in json.dumps(report)


def test_valid_registry_does_not_accept_ids_from_an_unrelated_collection(monkeypatch):
    registry_doc = yaml.safe_load(WORKFLOW_REGISTRY.read_text(encoding="utf-8"))
    registry_doc = {**registry_doc, "unrelated": [{"id": "forged-step"}]}
    data = _workflow_step_vector([({"service": "netbox", "step": "forged-step"}, 0)])
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": data})
    report = vdd.evaluate(
        _payload("service-conformance", panel_titles=["Latest step status by service"],
                 _vdd_workflow_steps=registry_doc.get("steps")),
        now=1700000000,
    )
    observation = report["panels"][0]["observation"]
    assert observation["status"] == "unverified"
    assert observation["unknown_series_count"] == 1
    assert "forged-step" not in json.dumps(report)


@pytest.mark.parametrize(
    ("sample", "expected_status"),
    [
        ([1700000000, "0"], "present"),
        ([1700000000, "1"], "none"),
        (None, "no_data"),
    ],
)
def test_latest_status_reports_its_own_45m_window_for_any_dashboard_lookback(
    monkeypatch, sample, expected_status
):
    data = (
        {"resultType": "vector", "result": []}
        if sample is None
        else {"resultType": "vector", "result": [{
            "metric": {"service": "netbox", "step": "provision-vm"}, "value": sample,
        }]}
    )
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": data})
    report = vdd.evaluate(
        _payload("service-conformance", lookback="1d", panel_titles=["Latest step status by service"]),
        now=1700000000,
    )
    panel = report["panels"][0]
    assert panel["lookback_seconds"] == 86400
    assert panel["observation"]["status"] == expected_status
    assert panel["observation"]["window_seconds"] == 2700
    assert panel["observation"]["scope"] == "latest_recorded_state"


def test_standalone_cli_uses_canonical_workflow_registry_and_reports_window(monkeypatch, capsys):
    data = _workflow_step_vector([({"service": "netbox", "step": "provision-vm"}, 0)])
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": data})
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(json.dumps(_payload(
            "service-conformance", panel_titles=["Latest step status by service"]
        ))),
    )
    assert vdd.main() == 0
    report = json.loads(capsys.readouterr().out)
    observation = report["panels"][0]["observation"]
    assert observation["failed_steps"] == [{
        "service": "netbox", "step": "provision-vm", "completion_age_seconds": None,
    }]
    assert observation["window_seconds"] == 2700


@pytest.mark.parametrize(("age", "observation"), [(42, "known"), (-1, "unknown")])
def test_completion_age_observation_uses_the_provisioned_unknown_mapping(monkeypatch, age, observation):
    panel = _observe_dashboard(
        monkeypatch,
        "service-conformance",
        "Latest workflow completion age by service and step",
        _numeric_vector([age]),
    )
    assert panel["status"] == "pass"
    assert panel["observation"] == {"name": "workflow_completion_age", "status": observation}


def test_known_observation_query_error_stays_a_query_failure_without_backend_text(monkeypatch):
    panel = _observe_dashboard(
        monkeypatch, "service-overview", "Scrape target health", "query_error"
    )
    assert panel["status"] == "error"
    assert panel["observation"] == {
        "name": "scrape_health",
        "status": "unverified",
        "reason": "query_error",
    }
    assert "error" not in panel["observation"]


def test_known_title_with_changed_query_is_unverified(monkeypatch, tmp_path):
    dashboard = json.loads((DASHBOARDS / "service-overview.json").read_text())
    dashboard["panels"] = [p for p in dashboard["panels"] if p.get("title") == "Scrape target health"]
    dashboard["panels"][0]["targets"][0]["expr"] += " + 0"
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": _numeric_vector([1])})
    report = vdd.evaluate(
        _payload(
            "service-overview",
            panel_titles=["Scrape target health"],
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
        ),
        now=1700000000,
    )
    assert report["status"] == "pass"
    assert report["panels"][0]["observation"] == {
        "name": "scrape_health",
        "status": "unverified",
        "reason": "contract_mismatch",
    }


@pytest.mark.parametrize(
    "source_drift",
    [
        {"location": "target", "field": "uid", "value": "different-backend"},
        {"location": "panel", "field": "uid", "value": "different-backend"},
        {"location": "panel", "field": "type", "value": "loki"},
    ],
)
def test_known_query_with_datasource_identity_drift_is_unverified(
    monkeypatch, tmp_path, source_drift
):
    dashboard = json.loads((DASHBOARDS / "service-overview.json").read_text())
    panel = next(p for p in dashboard["panels"] if p.get("title") == "Scrape target health")
    target = panel["targets"][0]
    if source_drift["location"] == "target":
        target["datasource"] = {"type": "prometheus", "uid": "prometheus"}
        target["datasource"][source_drift["field"]] = source_drift["value"]
    else:
        # The provisioned target inherits the panel source; mutate that source.
        panel["datasource"][source_drift["field"]] = source_drift["value"]
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": _numeric_vector([1])})
    report = vdd.evaluate(
        _payload(
            "service-overview",
            panel_titles=["Scrape target health"],
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
        ),
        now=1700000000,
    )
    assert report["status"] == "pass"
    assert report["panels"][0]["status"] == "pass"
    assert report["panels"][0]["observation"] == {
        "name": "scrape_health",
        "status": "unverified",
        "reason": "contract_mismatch",
    }


def test_provisioned_datasource_identity_verifies_known_observation(monkeypatch):
    panel = _observe_dashboard(
        monkeypatch,
        "service-overview",
        "Scrape target health",
        _numeric_vector([1]),
    )
    assert panel["observation"] == {"name": "scrape_health", "status": "healthy"}


def test_known_query_without_an_explicit_datasource_identity_is_unverified(
    monkeypatch, tmp_path
):
    dashboard = json.loads((DASHBOARDS / "service-overview.json").read_text())
    panel = next(p for p in dashboard["panels"] if p.get("title") == "Scrape target health")
    panel.pop("datasource")
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": _numeric_vector([1])})
    report = vdd.evaluate(
        _payload(
            "service-overview",
            panel_titles=["Scrape target health"],
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
        ),
        now=1700000000,
    )
    assert report["status"] == "pass"
    assert report["panels"][0]["observation"] == {
        "name": "scrape_health",
        "status": "unverified",
        "reason": "contract_mismatch",
    }


@pytest.mark.parametrize("target_datasource", [{}, None, {"type": "prometheus"}])
def test_ambiguous_target_datasource_does_not_fall_back_to_panel_identity(
    monkeypatch, tmp_path, target_datasource
):
    dashboard = json.loads((DASHBOARDS / "service-overview.json").read_text())
    panel = next(p for p in dashboard["panels"] if p.get("title") == "Scrape target health")
    panel["targets"][0]["datasource"] = target_datasource
    monkeypatch.setattr(vdd, "_query", lambda *args: {"data": _numeric_vector([1])})
    report = vdd.evaluate(
        _payload(
            "service-overview",
            panel_titles=["Scrape target health"],
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
        ),
        now=1700000000,
    )
    assert report["status"] == "pass"
    assert report["panels"][0]["observation"] == {
        "name": "scrape_health",
        "status": "unverified",
        "reason": "contract_mismatch",
    }


@pytest.mark.parametrize("time_from", ["1h30m", "0m", "7d;up", "-45m", "45M"])
def test_malformed_panel_time_from_is_refused(tmp_path, time_from):
    dashboard = _synthetic("up")
    dashboard["panels"][0]["timeFrom"] = time_from
    with pytest.raises(vdd.Refused, match="one Prometheus duration unit"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


def test_malformed_time_from_on_skipped_loki_panel_does_not_refuse_dashboard_plan(tmp_path):
    dashboard = _synthetic("up")
    dashboard["panels"].append(
        {
            "title": "Skipped Loki panel",
            "type": "logs",
            "datasource": {"type": "loki"},
            "timeFrom": "1h30m",
            "targets": [{"refId": "B", "expr": "{job=\"app\"}"}],
        }
    )

    planned = vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))

    assert planned["panels"][1]["lookback"] == 3600
    assert planned["panels"][1]["targets"] == [
        {"ref": "B", "datasource": "loki", "status": "skipped"}
    ]


def test_supported_panel_time_from_does_not_allow_other_unsupported_options(tmp_path):
    dashboard = _synthetic("up")
    dashboard["panels"][0].update(timeFrom="45m", maxDataPoints=100)
    with pytest.raises(vdd.Refused, match="maxDataPoints"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


def test_scrape_interval_matches_the_prometheus_datasource():
    sources = yaml.safe_load((GRAFANA / "provisioning/datasources/datasources.yml").read_text())["datasources"]
    prom = next(s for s in sources if s["type"] == "prometheus")
    declared = int(re.fullmatch(r"(\d+)s", prom["jsonData"]["timeInterval"]).group(1))
    passed = re.search(r"'scrape_interval_seconds': (\d+)", PLAYBOOK.read_text())
    assert passed and int(passed.group(1)) == declared


# ── Variable interpolation ────────────────────────────────────────────────────────────────


def test_builtins_follow_grafanas_definitions(tmp_path):
    expr = "rate(x[$__rate_interval]) + rate(x[${__interval}]) + increase(x[$__range]) + $__range_s + [[__range_ms]]"
    planned = vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, _synthetic(expr)), lookback="1h"))
    # step = max(15, ceil(3600 / 1000)) = 15; rate interval = max(15 + 15, 4 * 15) = 60
    assert planned["step"] == 15
    assert planned["panels"][0]["targets"][0]["expr"] == (
        "rate(x[60s]) + rate(x[15s]) + increase(x[3600s]) + 3600 + 3600000"
    )
    long = vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, _synthetic(expr)), lookback="2d"))
    # step = ceil(172800 / 1000) = 173; rate interval = max(173 + 15, 60) = 188
    assert long["step"] == 173
    assert long["panels"][0]["targets"][0]["expr"].startswith("rate(x[188s]) + rate(x[173s])")


def test_panel_time_from_sets_range_start_step_and_interpolated_builtins(tmp_path, prometheus):
    dashboard = _synthetic("increase(requests[$__range])")
    dashboard["panels"][0]["timeFrom"] = "45m"
    dashboards_dir = _dashboard_dir(tmp_path, dashboard)
    planned = vdd.plan(_payload("synthetic", dashboards_dir=dashboards_dir))
    assert planned["panels"][0]["targets"][0]["expr"] == "increase(requests[2700s])"
    report = vdd.evaluate(
        _payload("synthetic", dashboards_dir=dashboards_dir, prometheus_url=prometheus.url), now=1700003600
    )
    assert report["status"] == "pass"
    assert report["panels"][0]["lookback_seconds"] == 2700
    _, form = prometheus.requests[0]
    assert (form["start"], form["end"], form["step"]) == ("1700000900.000", "1700003600.000", "15s")


def test_panel_time_from_keeps_instant_queries_at_now(tmp_path, prometheus):
    dashboard = _synthetic("up", datasource={"type": "prometheus", "uid": "prometheus"})
    dashboard["panels"][0]["timeFrom"] = "45m"
    dashboard["panels"][0]["targets"][0]["instant"] = True
    report = vdd.evaluate(
        _payload(
            "synthetic",
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
            prometheus_url=prometheus.url,
        ),
        now=1700003600,
    )
    assert report["status"] == "pass"
    assert prometheus.requests[0] == ("/api/v1/query", {"query": "up", "time": "1700003600.000"})


@pytest.mark.parametrize(
    ("expr", "message"),
    [
        ('x{a=~"${v:regex}"}', "format option"),
        ("x offset $__from", "not supported"),
        ("x offset $undeclared", "no default"),
    ],
)
def test_unreproducible_references_are_refused(tmp_path, expr, message):
    dashboard = _synthetic(expr, [{"name": "v", "type": "custom", "current": {"value": "a"}}])
    with pytest.raises(vdd.Refused, match=message):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


def test_all_without_a_custom_all_value_is_refused(tmp_path):
    variable = {"name": "v", "type": "query", "includeAll": True, "current": {"value": "$__all"}}
    dashboard = _synthetic('x{a=~"$v"}', [variable])
    with pytest.raises(vdd.Refused, match="'v' has no default"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


def test_a_variable_datasource_is_refused(tmp_path):
    dashboard = _synthetic("up", datasource={"type": "prometheus", "uid": "${ds}"})
    dashboard["panels"][0]["datasource"] = "${ds}"
    with pytest.raises(vdd.Refused, match="datasource from a variable"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


# ── Evaluation against a fake Prometheus ─────────────────────────────────────────────────


def test_every_panel_with_data_passes_and_reports_counts_only(prometheus):
    report = vdd.evaluate(_payload("agentgateway-client-view", prometheus_url=prometheus.url), now=1700003600)
    assert report["status"] == "pass", report
    assert report["panels_failing"] == [] and report["panels_verified"] == 6
    rows = [t for p in report["panels"] for t in p["targets"]]
    assert all((t["series"], t["series_with_values"]) == (2, 2) for t in rows)
    dumped = json.dumps(report)
    assert not any(label in dumped for label in SECRET_LABELS)
    path, form = prometheus.requests[0]
    assert path == "/api/v1/query_range"
    assert (form["start"], form["end"], form["step"]) == ("1700000000.000", "1700003600.000", "15s")


def test_nan_only_series_render_nothing_and_fail_the_panel(prometheus):
    prometheus.rules["histogram_quantile(0.95, sum by (le) (rate(agentgateway_gen_ai_server_time_to_first_token"] = (
        "nan"
    )
    report = vdd.evaluate(_payload("agentgateway-client-view", prometheus_url=prometheus.url))
    assert report["status"] == "fail"
    assert report["panels_failing"] == ["First-token latency p95"]
    p95 = next(p for p in report["panels"] if p["title"] == "First-token latency p95")
    assert p95["status"] == "empty"
    assert (p95["targets"][0]["series"], p95["targets"][0]["series_with_values"]) == (2, 0)


def test_one_empty_target_fails_a_two_target_panel(prometheus):
    prometheus.rules["agentgateway_gen_ai_server_request_duration_bucket"] = "empty"
    report = vdd.evaluate(_payload("agentgateway-client-view", prometheus_url=prometheus.url))
    assert report["panels_failing"] == ["Request duration p95 (HTTP and model)"]


def test_an_expected_empty_panel_is_reported_without_sample_values(tmp_path, prometheus):
    dashboard = _synthetic("up")
    prometheus.rules["up"] = "empty"
    report = vdd.evaluate(
        _payload(
            "synthetic",
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
            panel_titles=["Only"],
            expected_empty_panels=["Only"],
            prometheus_url=prometheus.url,
        )
    )
    assert report["status"] == "pass"
    assert report["panels_failing"] == []
    assert report["panels"][0]["status"] == "expected_empty"
    assert report["panels"][0]["targets"][0]["status"] == "empty"


# The fleet-health dashboard's "Inference probe serving" panel reads the synthetic probe, which
# exists only where o11y_inference_probe_enabled is true and the probe has sampled. A
# whole-dashboard run elsewhere must either have the probe on or name the panel as expected-empty.
PROBE_PANEL = "Inference probe serving"


def test_a_whole_fleet_health_run_fails_on_an_empty_probe_panel(prometheus):
    prometheus.rules["inference_probe_success"] = "empty"
    report = vdd.evaluate(_payload("inference-fleet-health", prometheus_url=prometheus.url))
    assert report["status"] == "fail"
    assert report["panels_failing"] == [PROBE_PANEL]


def test_a_whole_fleet_health_run_passes_with_the_empty_probe_panel_expected(prometheus):
    prometheus.rules["inference_probe_success"] = "empty"
    report = vdd.evaluate(
        _payload("inference-fleet-health", expected_empty_panels=[PROBE_PANEL], prometheus_url=prometheus.url)
    )
    assert report["status"] == "pass"
    assert report["panels_failing"] == []
    assert report["expected_empty_panel_titles"] == [PROBE_PANEL]
    assert next(p for p in report["panels"] if p["title"] == PROBE_PANEL)["status"] == "expected_empty"


def test_a_whole_fleet_health_run_passes_with_the_probe_sampled(prometheus):
    report = vdd.evaluate(_payload("inference-fleet-health", prometheus_url=prometheus.url))
    assert report["status"] == "pass"
    assert PROBE_PANEL in report["verified_panel_titles"]


def test_an_allowed_panel_with_a_query_error_still_fails(tmp_path, prometheus):
    dashboard = _synthetic("up")
    prometheus.rules["up"] = "error"
    report = vdd.evaluate(
        _payload(
            "synthetic",
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
            panel_titles=["Only"],
            expected_empty_panels=["Only"],
            prometheus_url=prometheus.url,
        )
    )
    assert report["status"] == "fail"
    assert report["panels"][0]["status"] == "error"
    assert report["panels_failing"] == ["Only"]


def test_an_allowed_panel_with_one_empty_target_and_one_data_target_fails(tmp_path, prometheus):
    dashboard = _synthetic("up")
    dashboard["panels"][0]["targets"].append({"refId": "B", "expr": "missing_metric"})
    prometheus.rules["missing_metric"] = "empty"
    report = vdd.evaluate(
        _payload(
            "synthetic",
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
            panel_titles=["Only"],
            expected_empty_panels=["Only"],
            prometheus_url=prometheus.url,
        )
    )
    assert report["status"] == "fail"
    assert report["panels"][0]["status"] == "partial"
    assert [target["status"] for target in report["panels"][0]["targets"]] == ["data", "empty"]


@pytest.mark.parametrize(
    "allowlist",
    ["Only", ["Only", "Only"], [""], [1]],
    ids=["not-a-list", "duplicate", "empty-title", "non-string"],
)
def test_malformed_expected_empty_panel_lists_are_refused(tmp_path, allowlist):
    dashboard = _synthetic("up")
    with pytest.raises(vdd.Refused, match="expected_empty_panels"):
        vdd.plan(
            _payload(
                "synthetic",
                dashboards_dir=_dashboard_dir(tmp_path, dashboard),
                expected_empty_panels=allowlist,
            )
        )


def test_unknown_or_unselected_expected_empty_panels_are_refused(tmp_path):
    dashboard = _synthetic("up")
    dashboard["panels"].append(
        {"title": "Other", "type": "timeseries", "targets": [{"refId": "A", "expr": "up"}]}
    )
    dashboard_dir = _dashboard_dir(tmp_path, dashboard)
    with pytest.raises(vdd.Refused, match="not on this dashboard"):
        vdd.plan(_payload("synthetic", dashboards_dir=dashboard_dir, expected_empty_panels=["Missing"]))
    with pytest.raises(vdd.Refused, match="not selected"):
        vdd.plan(
            _payload(
                "synthetic",
                dashboards_dir=dashboard_dir,
                panel_titles=["Other"],
                expected_empty_panels=["Only"],
            )
        )


@pytest.mark.parametrize(
    ("behaviour", "reported"),
    [
        ("error", "bad_data"),
        ("execution", "execution"),
        ("odd_error", "unrecognised error"),
        ("structured_error", "unrecognised error"),
    ],
)
def test_a_query_error_reports_its_type_never_its_text(prometheus, behaviour, reported):
    # An execution error's text embeds label sets: identity and model names.
    prometheus.rules['status=~"4.."'] = behaviour
    report = vdd.evaluate(_payload("agentgateway-client-view", prometheus_url=prometheus.url))
    panel = next(p for p in report["panels"] if p["title"] == "4xx request ratio")
    assert panel["status"] == "error"
    assert panel["targets"][0]["error"] == reported
    dumped = json.dumps(report)
    assert "parse error" not in dumped and "duplicate series" not in dumped
    assert not any(label in dumped for label in SECRET_LABELS)


def test_an_unreachable_prometheus_reports_the_exception_class_only():
    report = vdd.evaluate(_payload("agentgateway-client-view", panel_titles=["First-token latency p50"]))
    assert report["panels"][0]["targets"][0]["error"] == "URLError"


def test_unsupported_datasources_remain_skipped_and_do_not_count(tmp_path, prometheus):
    dashboard = _synthetic("up")
    dashboard["panels"].append(
        {
            "title": "Unsupported trace query",
            "type": "timeseries",
            "datasource": {"type": "tempo", "uid": "tempo"},
            "targets": [{"refId": "A", "expr": '{resource.service.name="api"}'}],
        }
    )
    report = vdd.evaluate(
        _payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard), prometheus_url=prometheus.url)
    )
    assert report["status"] == "pass"
    assert report["panels_skipped"] == 1
    skipped = next(p for p in report["panels"] if p["status"] == "skipped")
    assert skipped["targets"][0]["datasource"] == "tempo"
    assert skipped["targets"][0]["status"] == "skipped"


def test_whole_dashboard_skips_loki_so_empty_log_panels_do_not_fail(prometheus, loki):
    loki.rules['service="vllm"'] = "empty"
    report = vdd.evaluate(
        _payload("inference-fleet-health", prometheus_url=prometheus.url, loki_url=loki.url)
    )
    assert report["status"] == "pass"
    assert report["panels_skipped"] == 2
    assert loki.requests == []
    assert all(
        t["status"] == "skipped"
        for p in report["panels"]
        for t in p["targets"]
        if t.get("datasource") == "loki"
    )


def test_mixed_prometheus_and_loki_panels_are_both_verified(prometheus, loki):
    report = vdd.evaluate(
        _payload(
            "inference-fleet-health",
            panel_titles=["Node exporter up", "Recent vLLM journal"],
            prometheus_url=prometheus.url,
            loki_url=loki.url,
        ),
        now=1700003600,
    )
    assert report["status"] == "pass"
    assert report["panels_verified"] == 2
    assert report["panels_failing"] == []
    assert prometheus.requests[0][0] == "/api/v1/query"
    path, form = loki.requests[0]
    assert path == "/loki/api/v1/query_range"
    assert form["limit"] == str(vdd.LOKI_RESULT_LIMIT)
    assert (form["start"], form["end"]) == ("1700000000.000", "1700003600.000")
    dumped = json.dumps(report)
    assert "private log line" not in dumped
    assert not any(label in dumped for label in SECRET_LABELS)


def test_loki_stream_result_reports_only_bounded_counts(loki):
    report = vdd.evaluate(
        _payload("inference-fleet-health", panel_titles=["Recent vLLM journal"], loki_url=loki.url),
        now=1700003600,
    )
    target = report["panels"][0]["targets"][0]
    assert report["status"] == "pass"
    assert (target["series"], target["series_with_values"]) == (1, 1)
    assert loki.requests[0][1]["limit"] == "100"
    assert "private log line" not in json.dumps(report)


def test_loki_metric_logql_results_use_the_metric_result_counter(tmp_path, loki):
    dashboard = _synthetic("sum(count_over_time({service=\"api\"}[5m]))", datasource={"type": "loki", "uid": "loki"})
    loki.rules["sum("] = "metric"
    report = vdd.evaluate(
        _payload(
            "synthetic",
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
            panel_titles=["Only"],
            loki_url=loki.url,
        ),
        now=1700003600,
    )
    target = report["panels"][0]["targets"][0]
    assert report["status"] == "pass"
    assert (target["series"], target["series_with_values"]) == (1, 1)
    assert loki.requests[0][0] == "/loki/api/v1/query_range"


def test_instant_loki_metric_queries_use_query_endpoint(tmp_path, loki):
    dashboard = _synthetic(
        "sum(count_over_time({service=\"api\"}[5m]))",
        datasource={"type": "loki", "uid": "loki"},
    )
    dashboard["panels"][0]["targets"][0]["queryType"] = "instant"
    loki.rules["sum("] = "metric"
    report = vdd.evaluate(
        _payload(
            "synthetic",
            dashboards_dir=_dashboard_dir(tmp_path, dashboard),
            panel_titles=["Only"],
            loki_url=loki.url,
        ),
        now=1700003600,
    )
    assert report["status"] == "pass"
    assert loki.requests[0][0] == "/loki/api/v1/query"
    assert loki.requests[0][1]["time"] == "1700003600.000"


def test_instant_loki_raw_stream_query_is_refused(tmp_path):
    dashboard = _synthetic('{service="api"}', datasource={"type": "loki", "uid": "loki"})
    dashboard["panels"][0]["targets"][0]["queryType"] = "instant"
    with pytest.raises(vdd.Refused, match="instant log stream query"):
        vdd.plan(
            _payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard), panel_titles=["Only"])
        )


def test_empty_loki_stream_result_fails_with_counts_only(loki):
    loki.rules["service=\"vllm\""] = "empty"
    report = vdd.evaluate(
        _payload("inference-fleet-health", panel_titles=["Recent vLLM journal"], loki_url=loki.url)
    )
    target = report["panels"][0]["targets"][0]
    assert report["status"] == "fail"
    assert target["status"] == "empty"
    assert (target["series"], target["series_with_values"]) == (0, 0)


def test_loki_http_error_body_is_discarded(loki):
    loki.rules["service=\"vllm\""] = "error"
    report = vdd.evaluate(
        _payload("inference-fleet-health", panel_titles=["Recent vLLM journal"], loki_url=loki.url)
    )
    target = report["panels"][0]["targets"][0]
    assert report["status"] == "fail"
    assert target["status"] == "error" and target["error"] == "HTTP 400"
    dumped = json.dumps(report)
    assert "bad query" not in dumped
    assert not any(label in dumped for label in SECRET_LABELS)


def test_loki_query_error_text_is_discarded(loki):
    loki.rules['service="vllm"'] = "query_error"
    report = vdd.evaluate(
        _payload("inference-fleet-health", panel_titles=["Recent vLLM journal"], loki_url=loki.url)
    )
    target = report["panels"][0]["targets"][0]
    assert target["status"] == "error" and target["error"] == "unrecognised error"
    dumped = json.dumps(report)
    assert "private log" not in dumped
    assert not any(label in dumped for label in SECRET_LABELS)


@pytest.mark.parametrize("backend", ["prometheus", "loki"])
def test_backend_redirects_are_not_followed(backend, prometheus, loki):
    if backend == "prometheus":
        prometheus.rules["up"] = "redirect"
        report = vdd.evaluate(
            _payload("inference-fleet-health", panel_titles=["Node exporter up"], prometheus_url=prometheus.url)
        )
        requests = prometheus.requests
    else:
        loki.rules["service=\"vllm\""] = "redirect"
        report = vdd.evaluate(
            _payload("inference-fleet-health", panel_titles=["Recent vLLM journal"], loki_url=loki.url)
        )
        requests = loki.requests
    target = report["panels"][0]["targets"][0]
    assert target["status"] == "error" and target["error"] == "HTTP 302"
    assert len(requests) == 1
    assert requests[0][0] != "/redirect-target"


def test_unreachable_prometheus_fails():
    report = vdd.evaluate(_payload("agentgateway-client-view", panel_titles=["First-token latency p50"]))
    assert report["status"] == "fail" and report["panels"][0]["status"] == "error"


def test_count_handles_every_result_type():
    assert vdd._count({"resultType": "scalar", "result": [1, "2"]}) == (1, 1)
    assert vdd._count({"resultType": "scalar", "result": [1, "NaN"]}) == (1, 0)
    assert vdd._count({"resultType": "vector", "result": [{"value": [1, "+Inf"]}]}) == (1, 0)
    assert vdd._count({"resultType": "matrix", "result": [{"values": [[1, "NaN"], [2, "0"]]}]}) == (1, 1)


def test_native_histogram_samples_count_by_their_observation_count():
    histogram = {"count": "4", "sum": "1.5", "buckets": [[0, "0", "1", "4"]]}
    assert vdd._count({"resultType": "vector", "result": [{"histogram": [1, histogram]}]}) == (1, 1)
    assert vdd._count({"resultType": "matrix", "result": [{"histograms": [[1, histogram]]}]}) == (1, 1)
    assert vdd._count({"resultType": "matrix", "result": [{"histograms": [[1, {"count": "NaN"}]]}]}) == (1, 0)
    assert vdd._count({"resultType": "vector", "result": [{"histogram": [1, {"sum": "1"}]}]}) == (1, 0)


# ── What an override and an unsupported panel may not do ─────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ['.*"}[5m]) or vector(1)) or (x{a=~".*', ".*}", "a(b", "a)b", "a\\b", "a\nb", ""],
)
def test_an_override_that_could_restructure_the_query_is_refused(value):
    with pytest.raises(vdd.Refused, match="override for 'identity'"):
        vdd.plan(_payload("agentgateway-client-view", variables={"identity": value}))


def test_regex_characters_are_kept_inside_a_string():
    planned = vdd.plan(_payload("agentgateway-client-view", variables={"identity": "a|b.+[0-9]*?^$"}))
    assert all('identity=~"a|b.+[0-9]*?^$"' in t["expr"] for p in planned["panels"] for t in p["targets"])


def test_outside_a_string_only_a_duration_or_number_is_substituted():
    with pytest.raises(vdd.Refused, match="outside a string"):
        vdd.plan(_payload("inference-placement-comparison", variables={"compare_offset": "1d or up"}))
    assert vdd.plan(_payload("inference-placement-comparison", variables={"compare_offset": "90m"}))


@pytest.mark.parametrize(("expr", "closing"), [("x{a='$v'}", "a'b"), ("x{a=`$v`}", "a`b")])
def test_a_value_cannot_close_the_string_it_lands_in(tmp_path, expr, closing):
    dashboard = _synthetic(expr, [{"name": "v", "type": "custom", "current": {"value": "a"}}])
    with pytest.raises(vdd.Refused, match="would close the string"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard), variables={"v": closing}))


def test_an_apostrophe_in_a_comment_opens_no_string(tmp_path):
    # A comment's apostrophe once read as an opening quote, so the offset after it counted as
    # inside a string and skipped the duration check.
    dashboard = _synthetic("up # it's\n offset $o", [{"name": "o", "type": "custom", "current": {"value": "1h"}}])
    with pytest.raises(vdd.Refused, match="outside a string"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard), variables={"o": "1h or up"}))
    planned = vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))
    assert planned["panels"][0]["targets"][0]["expr"] == "up # it's\n offset 1h"


def test_a_saved_default_outside_a_string_must_be_a_duration(tmp_path):
    dashboard = _synthetic("x offset $v", [{"name": "v", "type": "custom", "current": {"value": "1d or up"}}])
    with pytest.raises(vdd.Refused, match="outside a string"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


@pytest.mark.parametrize(
    ("panel_extra", "target_extra"),
    [
        ({"maxDataPoints": 100}, {}),
        ({"interval": "1m"}, {}),
        ({"timeShift": "1d"}, {}),
        ({"repeat": "identity"}, {}),
        ({"libraryPanel": {"uid": "lib"}}, {}),
        ({}, {"interval": "30s"}),
        ({}, {"intervalFactor": 2}),
    ],
)
def test_a_panel_option_this_check_cannot_reproduce_is_refused(tmp_path, panel_extra, target_extra):
    dashboard = _synthetic("up")
    dashboard["panels"][0].update(panel_extra)
    dashboard["panels"][0]["targets"][0].update(target_extra)
    with pytest.raises(vdd.Refused, match="does not reproduce"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


def test_a_panel_in_a_repeating_row_is_refused(tmp_path):
    dashboard = _synthetic("up")
    dashboard["panels"] = [{"type": "row", "title": "Row", "repeat": "identity", "panels": dashboard["panels"]}]
    with pytest.raises(vdd.Refused, match=r"repeat \(row\)"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


def test_a_panel_after_an_expanded_repeating_row_is_refused_until_the_next_row(tmp_path):
    # An expanded row keeps panels: [] and its panels follow it at the top level.
    dashboard = _synthetic("up")
    only = dashboard["panels"][0]
    dashboard["panels"] = [
        {"type": "row", "title": "Repeating", "repeat": "identity", "panels": []},
        dict(only, title="In the row"),
        {"type": "row", "title": "Plain", "panels": []},
        dict(only, title="After the row"),
    ]
    path = _dashboard_dir(tmp_path, dashboard)
    with pytest.raises(vdd.Refused, match=r"'In the row' uses repeat \(row\)"):
        vdd.plan(_payload("synthetic", dashboards_dir=path))
    assert vdd.plan(_payload("synthetic", dashboards_dir=path, panel_titles=["After the row"]))


def test_a_library_panel_without_targets_is_refused(tmp_path):
    dashboard = _synthetic("up")
    dashboard["panels"].append({"title": "Shared", "libraryPanel": {"uid": "lib", "name": "Shared"}})
    with pytest.raises(vdd.Refused, match="'Shared' is a library panel"):
        vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


@pytest.mark.parametrize("factor", [1, "1"])
def test_empty_options_and_non_prometheus_panels_are_not_refused(tmp_path, factor):
    dashboard = _synthetic("up")
    dashboard["panels"][0].update({"interval": "", "maxDataPoints": None})
    dashboard["panels"][0]["targets"][0].update({"interval": "", "intervalFactor": factor})
    logs = {"title": "Logs", "type": "logs", "timeShift": "1d", "datasource": {"type": "loki"}}
    dashboard["panels"].append(dict(logs, targets=[{"refId": "A", "expr": '{a="b"}'}]))
    assert vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


def test_named_loki_panel_with_unsupported_options_is_refused(tmp_path):
    dashboard = _synthetic("up")
    logs = {"title": "Logs", "type": "logs", "timeShift": "1d", "datasource": {"type": "loki"}}
    dashboard["panels"].append(dict(logs, targets=[{"refId": "A", "expr": '{a="b"}'}]))
    with pytest.raises(vdd.Refused, match="timeShift"):
        vdd.plan(
            _payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard), panel_titles=["Logs"])
        )


# ── The playbook, end to end ─────────────────────────────────────────────────────────────


@pytest.fixture
def receiver(tmp_path, prometheus, loki):
    repo = tmp_path / "repo"
    (repo / "platform/services/netbox").mkdir(parents=True)
    (repo / "agents").mkdir()
    deploy = repo / "platform/services/o11y/deployment/config/grafana"
    shutil.copytree(DASHBOARDS, deploy / "dashboards")
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "[o11y_svc]\n"
        f"receiver ansible_connection=local ansible_python_interpreter=auto_silent "
        f"local_monorepo_dir={repo} monorepo_deploy_path=platform/services/o11y/deployment "
        f"o11y_prom_bind=127.0.0.1 o11y_prom_port={prometheus.server.server_address[1]} "
        f"o11y_loki_bind=127.0.0.1 o11y_loki_port={loki.server.server_address[1]}\n"
    )
    return {"inventory": inventory, "prometheus": prometheus, "loki": loki}


def _run(receiver, *extra):
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(
        ["ansible-playbook", "-i", str(receiver["inventory"]), str(PLAYBOOK), *extra],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _ipv6_loki_or_skip():
    if not socket.has_ipv6:
        pytest.skip("OS reports IPv6 support is unavailable")
    try:
        return FakeLoki("::1")
    except OSError as exc:
        unavailable = {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL}
        if exc.errno in unavailable:
            pytest.skip(f"OS cannot bind the IPv6 loopback address: {exc.strerror}")
        raise


def _set_loki_inventory(receiver, bind, loki):
    previous_port = receiver["loki"].server.server_address[1]
    inventory = receiver["inventory"]
    contents = inventory.read_text().replace("o11y_loki_bind=127.0.0.1", f"o11y_loki_bind={bind}")
    contents = contents.replace(
        f"o11y_loki_port={previous_port}", f"o11y_loki_port={loki.server.server_address[1]}"
    )
    inventory.write_text(contents)
    receiver["loki"] = loki


@needs_ansible
@pytest.mark.parametrize("check", [False, True])
def test_playbook_passes_identically_under_check(receiver, check):
    proc = _run(receiver, "-e", "dashboard_uid=agentgateway-client-view", *(["--check"] if check else []))
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert "6 panels of agentgateway-client-view verified over 1h" in proc.stdout
    assert "First-token latency p50; First-token latency p95; " in proc.stdout
    assert re.search(r"receiver\s+: ok=\d+\s+changed=0", proc.stdout)
    assert not any(label in proc.stdout for label in SECRET_LABELS)
    assert receiver["prometheus"].requests


@needs_ansible
def test_playbook_fails_naming_the_empty_panel(receiver):
    receiver["prometheus"].rules["sum by (identity)"] = "empty"
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=agentgateway-client-view",
        "-e",
        '{"panel_titles": "[\\"Request rate by identity\\", \\"First-token latency p50\\"]"}',
    )
    assert proc.returncode != 0
    assert "Panels without data over 1h: Request rate by identity" in proc.stdout
    assert len(receiver["prometheus"].requests) == 2


@needs_ansible
def test_playbook_reports_expected_empty_panel_in_readback(receiver):
    receiver["prometheus"].rules["sum by (identity)"] = "empty"
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=agentgateway-client-view",
        "-e",
        '{"panel_titles": "[\\"Request rate by identity\\"]", '
        '"expected_empty_panels": "[\\"Request rate by identity\\"]"}',
    )
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert '"status": "expected_empty"' in proc.stdout
    assert '"status": "pass"' in proc.stdout
    assert "Expected empty: Request rate by identity." in proc.stdout
    assert not any(label in proc.stdout for label in SECRET_LABELS)


@needs_ansible
def test_playbook_passes_canonical_controller_registry_to_receiver_evaluator(receiver):
    receiver["loki"].rules["unwrap state_code"] = "workflow_failure"
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=service-conformance",
        "-e",
        '{"panel_titles": "[\\"Latest step status by service\\"]", "lookback": "1d"}',
    )
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert '"lookback_seconds": 86400' in proc.stdout
    assert '"window_seconds": 2700' in proc.stdout
    assert '"scope": "latest_recorded_state"' in proc.stdout
    assert '"status": "present"' in proc.stdout, proc.stdout
    assert '"service": "netbox"' in proc.stdout
    assert '"step": "provision-vm"' in proc.stdout
    assert '"completion_age_status": "not_selected"' in proc.stdout
    assert '"completion_age_seconds": null' in proc.stdout
    assert len(receiver["loki"].requests) == 1
    assert not any(label in proc.stdout for label in SECRET_LABELS)


@needs_ansible
def test_playbook_joins_completion_age_only_when_both_exact_panels_are_selected(receiver):
    receiver["loki"].rules["unwrap state_code"] = "workflow_failure"
    receiver["loki"].rules["completed_at_seconds"] = "workflow_age"
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=service-conformance",
        "-e",
        '{"panel_titles": "[\\"Latest step status by service\\", '
        '\\"Latest workflow completion age by service and step\\"]"}',
    )
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert '"completion_age_status": "observed"' in proc.stdout
    assert '"completion_age_seconds": 42.5' in proc.stdout
    assert '"failed_pairs_with_known_completion_age": 1' in proc.stdout
    assert len(receiver["loki"].requests) == 2
    assert not any(label in proc.stdout for label in SECRET_LABELS)


@needs_ansible
@pytest.mark.parametrize(
    "forge",
    forgeries.templated_forgeries(
        "_vdd_workflow_registry", {"steps": []}, {"steps": [{"id": "forged-step"}]}
    ),
)
def test_playbook_refuses_extra_var_registry_override_before_query(receiver, forge, tmp_path):
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=service-conformance",
        "-e",
        forge(tmp_path),
    )
    assert proc.returncode != 0
    assert "Refusing to run: _vdd_workflow_registry set from outside the playbook" in proc.stdout
    assert receiver["loki"].requests == []


@needs_ansible
@pytest.mark.parametrize(
    "forge",
    forgeries.templated_forgeries(
        "_vdd_workflow_steps", [], [{"id": "forged-step"}]
    ),
)
def test_playbook_refuses_extra_var_step_override_before_query(receiver, forge, tmp_path):
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=service-conformance",
        "-e",
        forge(tmp_path),
    )
    assert proc.returncode != 0
    assert "Refusing to run: _vdd_workflow_steps set from outside the playbook" in proc.stdout
    assert receiver["loki"].requests == []


@needs_ansible
@pytest.mark.parametrize("value", ['"{}"', '"[1]"', '"not-json"'])
def test_playbook_refuses_a_malformed_expected_empty_list_before_query(receiver, value):
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=agentgateway-client-view",
        "-e",
        '{"expected_empty_panels": ' + value + "}",
    )
    assert proc.returncode != 0
    assert "Read the receiver checkout revision" not in proc.stdout
    assert receiver["prometheus"].requests == []


@needs_ansible
def test_playbook_never_prints_prometheus_error_text(receiver):
    receiver["prometheus"].rules['status=~"5.."'] = "execution"
    proc = _run(receiver, "-e", "dashboard_uid=agentgateway-client-view")
    assert proc.returncode != 0
    assert "Panels without data over 1h: 5xx request ratio" in proc.stdout
    assert '"error": "execution"' in proc.stdout
    assert "duplicate series" not in proc.stdout
    assert not any(label in proc.stdout for label in SECRET_LABELS)


@needs_ansible
def test_playbook_queries_selected_loki_panel_and_discards_log_content(receiver):
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=inference-fleet-health",
        "-e",
        '{"panel_titles": "[\\"Recent vLLM journal\\"]"}',
    )
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert "1 panel of inference-fleet-health verified over 1h" in proc.stdout
    assert receiver["loki"].requests[0][0] == "/loki/api/v1/query_range"
    assert "private log line" not in proc.stdout
    assert not any(label in proc.stdout for label in SECRET_LABELS)


@needs_ansible
@pytest.mark.parametrize("bind", ["0.0.0.0", ""])
def test_playbook_uses_loopback_for_wildcard_loki_bind(receiver, bind):
    inventory = receiver["inventory"]
    inventory.write_text(inventory.read_text().replace("o11y_loki_bind=127.0.0.1", f"o11y_loki_bind={bind}"))
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=inference-fleet-health",
        "-e",
        '{"panel_titles": "[\\"Recent vLLM journal\\"]"}',
    )
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert receiver["loki"].requests


@needs_ansible
def test_playbook_brackets_ipv6_loki_bind(receiver):
    loki = _ipv6_loki_or_skip()
    _set_loki_inventory(receiver, "::1", loki)
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=inference-fleet-health",
        "-e",
        '{"panel_titles": "[\\"Recent vLLM journal\\"]"}',
    )
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert loki.requests


@needs_ansible
def test_playbook_maps_ipv6_wildcard_loki_bind_to_ipv6_loopback(receiver):
    loki = _ipv6_loki_or_skip()
    _set_loki_inventory(receiver, "::", loki)
    proc = _run(
        receiver,
        "-e",
        "dashboard_uid=inference-fleet-health",
        "-e",
        '{"panel_titles": "[\\"Recent vLLM journal\\"]"}',
    )
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert loki.requests


@needs_ansible
def test_playbook_uses_service_overview_defaults_without_overrides(receiver):
    proc = _run(receiver, "-e", "dashboard_uid=service-overview")
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert any('service=~"agentgateway"' in form["query"] for _, form in receiver["prometheus"].requests)
    assert receiver["loki"].requests == []


@needs_ansible
def test_playbook_refuses_a_malformed_request_before_the_receiver(receiver):
    proc = _run(receiver, "-e", "dashboard_uid=agentgateway-client-view", "-e", "lookback=1h30m")
    assert proc.returncode != 0
    assert "Declare exactly one o11y_svc host" in proc.stdout
    assert receiver["prometheus"].requests == []


# The play's panel selection is a play var an extra var outranks: narrowing it to the panels
# that have data would turn a failing gate into a pass. refuse-internal-extra-vars.yml refuses
# the name, plainly and templated, before any play reads it.
@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("_vdd_panel_titles", [], ["First-token latency p50"]))
def test_playbook_refuses_a_forged_panel_selection(receiver, forge, tmp_path):
    receiver["prometheus"].rules["sum by (identity)"] = "empty"
    proc = _run(receiver, "-e", "dashboard_uid=agentgateway-client-view", "-e", forge(tmp_path))
    assert proc.returncode != 0, proc.stdout
    assert "Refusing to run: _vdd_panel_titles set from outside the playbook" in proc.stdout, proc.stdout
    assert proc.stdout.count("PLAY [") == 1, proc.stdout
    assert receiver["prometheus"].requests == []


@needs_ansible
@pytest.mark.parametrize(
    "forge", forgeries.templated_forgeries("_vdd_expected_empty_panels", [], ["First-token latency p50"])
)
def test_playbook_refuses_a_forged_expected_empty_allowance(receiver, forge, tmp_path):
    proc = _run(receiver, "-e", "dashboard_uid=agentgateway-client-view", "-e", forge(tmp_path))
    assert proc.returncode != 0, proc.stdout
    assert "Refusing to run: _vdd_expected_empty_panels set from outside the playbook" in proc.stdout
    assert proc.stdout.count("PLAY [") == 1
    assert receiver["prometheus"].requests == []


def test_dev_template_declares_the_survey():
    templates = yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(t for t in templates if t["name"] == "Verify o11y Dashboard Data (Dev)")
    assert template["playbook"] == "platform/playbooks/verify-o11y-dashboard-data.yml"
    assert template["repository"] == "agent-cloud dev"
    playbook = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    evaluator_play = next(p for p in playbook if p.get("hosts") == "o11y_svc")
    assert "from_yaml" in evaluator_play["vars"]["_vdd_workflow_registry"]
    assert "service-onboarding/registry.yml" in evaluator_play["vars"]["_vdd_workflow_registry"]
    assert "type_debug == 'list'" in (REPO / "platform/playbooks/verify-o11y-dashboard-data.yml").read_text()
    assert "_vdd_workflow_steps" in evaluator_play["tasks"][1]["ansible.builtin.command"]["stdin"]
    assert [v["name"] for v in template["survey_vars"]] == [
        "dashboard_uid",
        "lookback",
        "panel_titles",
        "expected_empty_panels",
        "dashboard_variables",
    ]
