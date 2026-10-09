"""The gateway dashboard preserves no-data semantics for absent metrics."""

import json
from pathlib import Path

DASHBOARD = (
    Path(__file__).resolve().parents[1]
    / "services/o11y/deployment/config/grafana/dashboards/agentgateway-traffic.json"
)


def test_error_ratio_zero_fills_only_the_missing_5xx_numerator():
    panels = {p["title"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}
    error = panels["Server error ratio"]
    expression = error["targets"][0]["expr"]
    assert expression.startswith("(sum(rate(agentgateway_requests_total")
    assert " or vector(0)) / clamp_min(sum(rate(agentgateway_requests_total" in expression
    assert "No data means the request denominator is absent" in error["description"]


def test_ttft_panel_explains_missing_histogram_without_synthesizing_zero():
    panels = {p["title"]: p for p in json.loads(DASHBOARD.read_text())["panels"]}
    ttft = panels["Time to first token p95"]
    assert "histogram_quantile" in ttft["targets"][0]["expr"]
    assert "vector(0)" not in ttft["targets"][0]["expr"]
    assert "does not mean zero latency" in ttft["description"]
