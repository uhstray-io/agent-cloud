"""Verify o11y Dashboard Data evaluates a provisioned dashboard's Prometheus and Loki panels.

The evaluator (platform/playbooks/files/verify-o11y-dashboard-data.py) is exercised directly
against the committed dashboards and fake backends, then the playbook is run end to end through
ansible-playbook with a local-connection receiver whose deploy dir holds dashboard copies.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import forgeries
import pytest
import yaml
from fake_http import DrainingHandler

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/verify-o11y-dashboard-data.yml"
GRAFANA = REPO / "platform/services/o11y/deployment/config/grafana"
DASHBOARDS = GRAFANA / "dashboards"
_spec = importlib.util.spec_from_file_location(
    "verify_dashboard_data", REPO / "platform/playbooks/files/verify-o11y-dashboard-data.py"
)
vdd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vdd)

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

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
        behaviour = next((b for key, b in self.rules.items() if key in form["query"]), "streams")
        if behaviour == "error":
            return 400, {"status": "error", "errorType": "bad_data", "error": f"bad query {SECRET_LABELS[0]}"}
        if behaviour == "query_error":
            return 200, {"status": "error", "errorType": "unknown", "error": f"parse {SECRET_LABELS[0]} private log"}
        if behaviour == "redirect":
            return 302, {"error": "redirect body must not be followed"}
        if behaviour == "empty":
            result_type, result = "streams", []
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


def test_a_query_variable_without_a_reproducible_default_is_refused_until_overridden():
    with pytest.raises(vdd.Refused, match="'service' has no default"):
        vdd.plan(_payload("service-overview"))
    planned = vdd.plan(_payload("service-overview", variables={"service": "o11y"}))
    assert planned["variables"] == {"service": "override sha256:" + hashlib.sha256(b"o11y").hexdigest()[:12]}


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
        ({"timeFrom": "6h"}, {}),
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
    logs = {"title": "Logs", "type": "logs", "timeShift": "1d", "datasource": {"type": "tempo"}}
    dashboard["panels"].append(dict(logs, targets=[{"refId": "A", "expr": '{a="b"}'}]))
    assert vdd.plan(_payload("synthetic", dashboards_dir=_dashboard_dir(tmp_path, dashboard)))


# ── The playbook, end to end ─────────────────────────────────────────────────────────────


@pytest.fixture
def receiver(tmp_path, prometheus, loki):
    deploy = tmp_path / "repo/o11y/config/grafana"
    shutil.copytree(DASHBOARDS, deploy / "dashboards")
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "[o11y_svc]\n"
        f"receiver ansible_connection=local ansible_python_interpreter=auto_silent "
        f"local_monorepo_dir={tmp_path / 'repo'} monorepo_deploy_path=o11y "
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


@needs_ansible
@pytest.mark.parametrize("check", [False, True])
def test_playbook_passes_identically_under_check(receiver, check):
    proc = _run(receiver, "-e", "dashboard_uid=agentgateway-client-view", *(["--check"] if check else []))
    assert proc.returncode == 0, proc.stdout[-4000:]
    assert "6 panels of agentgateway-client-view render data over 1h" in proc.stdout
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
    assert "1 panel of inference-fleet-health render data" in proc.stdout
    assert receiver["loki"].requests[0][0] == "/loki/api/v1/query_range"
    assert "private log line" not in proc.stdout
    assert not any(label in proc.stdout for label in SECRET_LABELS)


@needs_ansible
def test_playbook_reports_a_refusal(receiver):
    proc = _run(receiver, "-e", "dashboard_uid=service-overview")
    assert proc.returncode != 0
    assert "Refused: variable 'service' has no default" in proc.stdout
    assert receiver["prometheus"].requests == []


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


def test_dev_template_declares_the_survey():
    templates = yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(t for t in templates if t["name"] == "Verify o11y Dashboard Data (Dev)")
    assert template["playbook"] == "platform/playbooks/verify-o11y-dashboard-data.yml"
    assert template["repository"] == "agent-cloud dev"
    assert [v["name"] for v in template["survey_vars"]] == [
        "dashboard_uid",
        "lookback",
        "panel_titles",
        "dashboard_variables",
    ]
