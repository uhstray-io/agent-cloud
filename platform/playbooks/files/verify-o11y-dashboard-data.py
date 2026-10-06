#!/usr/bin/env python3
"""Evaluate one provisioned Grafana dashboard's Prometheus panels and report series counts.

Read-only: it reads the dashboard JSON the receiver's Grafana provisions from, and sends
query and query_range requests to the receiver's Prometheus. It prints one JSON report on
stdout carrying names and counts only: no label value and no sample value leaves this
script, so a per-identity panel reports how MANY identities answered, never which.

Input (stdin, JSON):
  dashboards_dir   directory Grafana's file provider loads (config/grafana/dashboards)
  dashboard_uid    the dashboard's `uid`
  lookback         Prometheus duration, one unit: 30m, 1h, 2d (the dashboard time range)
  panel_titles     optional list of exact panel titles; empty means every panel
  variables        optional {name: value} overriding a template variable's saved default;
                   substituted verbatim, as Grafana substitutes a custom All value
  prometheus_url   base URL of the Prometheus HTTP API
  scrape_interval_seconds  the Prometheus datasource's timeInterval (datasources.yml: 15s)

Grafana semantics reproduced, and where they come from:
  - $__rate_interval = max($__interval + scrape_interval, 4 * scrape_interval);
    $__range is the dashboard time range, _s and _ms in seconds and milliseconds
    (https://grafana.com/docs/grafana/latest/datasources/prometheus/template-variables/).
  - $__interval is "calculated ... based on time range and panel width"; here it is the
    query step, max(scrape_interval, ceil(lookback / 1000)), about a 1000-pixel panel.
  - $var, ${var} and the deprecated [[var]] are the plain syntaxes; ${var:format} is
    refused (https://grafana.com/docs/grafana/latest/dashboards/variables/variable-syntax/).
  - A variable saved with All selected takes its Custom all value
    (https://grafana.com/docs/grafana/latest/dashboards/variables/add-template-variables/);
    one without a Custom all value would be the regex of every option, which this script
    cannot compute without the variable's own query, so it is refused.
  - A target with `instant: true` (and no `range: true`) is an instant query at the end of
    the range; every other target is a range query over it.
  - A target or panel with no datasource uses the default one, which is Prometheus
    (datasources.yml, isDefault). Loki, Tempo and other datasources are reported skipped.

A target has data when at least one returned series carries a finite sample: Prometheus
encodes NaN as a string (https://prometheus.io/docs/prometheus/latest/querying/api/), and
histogram_quantile over a window with no traffic returns NaN, which a panel draws as a gap.
A panel passes when every visible Prometheus target has data; the run passes when every
selected Prometheus panel passes.

Exit status: 0 pass, 1 a panel has no data or a query failed, 2 refused (bad input).
"""

from __future__ import annotations

import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DURATION = re.compile(r"^([1-9][0-9]*)(s|m|h|d)$")
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
UID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# ${name}, ${name:format}, [[name]], [[name:format]], $name — in that order, so the braced
# forms are matched before the bare one.
REFERENCE = re.compile(
    r"\$\{(?P<braced>[A-Za-z0-9_]+)(?::(?P<bformat>[^}]*))?\}"
    r"|\[\[(?P<bracket>[A-Za-z0-9_]+)(?::(?P<kformat>[^\]]*))?\]\]"
    r"|\$(?P<bare>[A-Za-z0-9_]+)"
)
# Types whose saved current value is the literal text Grafana interpolates.
LITERAL_TYPES = {"custom", "constant", "interval", "textbox"}
POINTS_PER_PANEL = 1000
QUERY_TIMEOUT = 30
ERROR_TEXT = 300


class Refused(Exception):
    """An input the script will not evaluate; the message names the reason."""


def duration_seconds(text: Any) -> int:
    match = DURATION.fullmatch(str(text))
    if not match:
        raise Refused(f"lookback must be one Prometheus duration unit (30m, 1h, 2d), got {text!r}")
    return int(match.group(1)) * UNIT_SECONDS[match.group(2)]


def flatten_panels(panels: list[Any]) -> list[dict[str, Any]]:
    """Every panel, including those a collapsed row holds in its own `panels` list."""
    out: list[dict[str, Any]] = []
    for panel in panels or []:
        if not isinstance(panel, dict):
            continue
        if panel.get("type") != "row":
            out.append(panel)
        out.extend(flatten_panels(panel.get("panels", [])))
    return out


def datasource_type(target: dict[str, Any], panel: dict[str, Any]) -> str:
    source = target.get("datasource") or panel.get("datasource")
    if source is None:
        return "prometheus"
    kind = str((source.get("type") or source.get("uid") or "") if isinstance(source, dict) else source)
    if kind.startswith("$"):
        raise Refused(f"panel {panel.get('title')!r} takes its datasource from a variable")
    return kind.lower()


def load_dashboard(dashboards_dir: str, uid: str) -> dict[str, Any]:
    if not UID.fullmatch(uid):
        raise Refused(f"dashboard_uid {uid!r} is not a Grafana uid")
    found = []
    for path in sorted(Path(dashboards_dir).glob("*.json")):
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(document, dict) and document.get("uid") == uid:
            found.append(document)
    if len(found) != 1:
        raise Refused(f"{len(found)} provisioned dashboards carry uid {uid!r}; expected exactly one")
    return found[0]


def template_values(dashboard: dict[str, Any], overrides: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Each template variable's value, and where it came from (default or override)."""
    declared = {}
    for variable in dashboard.get("templating", {}).get("list", []) or []:
        if isinstance(variable, dict) and variable.get("name"):
            declared[variable["name"]] = variable
    unknown = sorted(set(overrides) - set(declared))
    if unknown:
        raise Refused(f"variables {unknown} are not declared by this dashboard")
    values, sources = {}, {}
    for name, variable in declared.items():
        if name in overrides:
            values[name], sources[name] = str(overrides[name]), "override"
            continue
        current = (variable.get("current") or {}).get("value")
        if isinstance(current, list) and len(current) == 1:
            current = current[0]
        if current == "$__all" and isinstance(variable.get("allValue"), str) and variable["allValue"]:
            values[name], sources[name] = variable["allValue"], "all_value"
        elif (
            isinstance(current, str)
            and current
            and not current.startswith("$")
            and variable.get("type") in LITERAL_TYPES
        ):
            values[name], sources[name] = current, "saved_value"
        # Anything else is resolved only if a query uses it, and refused there.
    return values, sources


def interpolate(expr: str, values: dict[str, str], builtins: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bracket") or match.group("bare")
        if match.group("bformat") is not None or match.group("kformat") is not None:
            raise Refused(f"variable {name!r} uses a format option, which this check does not reproduce")
        if name in builtins:
            return builtins[name]
        if name in values:
            return values[name]
        if name.startswith("__"):
            raise Refused(f"built-in variable {name!r} is not supported by this check")
        raise Refused(f"variable {name!r} has no default this check can reproduce; pass it in dashboard_variables")

    return REFERENCE.sub(replace, expr)


def plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve every selected panel's queries without contacting Prometheus."""
    lookback = duration_seconds(payload.get("lookback"))
    scrape = int(payload.get("scrape_interval_seconds") or 0)
    if scrape <= 0:
        raise Refused("scrape_interval_seconds must be a positive integer")
    titles = payload.get("panel_titles") or []
    overrides = payload.get("variables") or {}
    if not isinstance(titles, list) or not all(isinstance(t, str) and t for t in titles):
        raise Refused("panel_titles must be a list of exact panel titles")
    if not isinstance(overrides, dict) or not all(VARIABLE_NAME.fullmatch(str(k)) for k in overrides):
        raise Refused("dashboard_variables must map variable names to values")

    dashboard = load_dashboard(str(payload.get("dashboards_dir", "")), str(payload.get("dashboard_uid", "")))
    values, sources = template_values(dashboard, overrides)
    step = max(scrape, math.ceil(lookback / POINTS_PER_PANEL))
    builtins = {
        "__interval": f"{step}s",
        "__interval_ms": str(step * 1000),
        "__rate_interval": f"{max(step + scrape, 4 * scrape)}s",
        "__range": f"{lookback}s",
        "__range_s": str(lookback),
        "__range_ms": str(lookback * 1000),
    }

    panels = flatten_panels(dashboard.get("panels", []))
    missing = [t for t in titles if t not in {p.get("title") for p in panels}]
    if missing:
        raise Refused(f"panels {missing} are not on this dashboard")
    selected = [p for p in panels if not titles or p.get("title") in titles]

    planned = []
    for panel in selected:
        targets = [t for t in panel.get("targets", []) or [] if isinstance(t, dict)]
        if not targets:
            continue
        entry: dict[str, Any] = {"title": panel.get("title", ""), "targets": []}
        for target in targets:
            kind = datasource_type(target, panel)
            ref = target.get("refId", "")
            if kind != "prometheus":
                entry["targets"].append({"ref": ref, "datasource": kind, "status": "skipped"})
                continue
            if target.get("hide"):
                entry["targets"].append({"ref": ref, "datasource": kind, "status": "hidden"})
                continue
            expr = str(target.get("expr") or "").strip()
            if not expr:
                raise Refused(f"panel {entry['title']!r} target {ref!r} has no expression")
            modes = []
            if target.get("range") or not target.get("instant"):
                modes.append("range")
            if target.get("instant"):
                modes.append("instant")
            entry["targets"].append(
                {"ref": ref, "datasource": kind, "modes": modes, "expr": interpolate(expr, values, builtins)}
            )
        planned.append(entry)
    if not any(t.get("expr") for p in planned for t in p["targets"]):
        raise Refused("no Prometheus query is selected on this dashboard")
    return {
        "dashboard": dashboard,
        "lookback": lookback,
        "step": step,
        "variables": sources,
        "panels": planned,
    }


def _query(base: str, mode: str, expr: str, end: float, lookback: int, step: int) -> dict[str, Any]:
    if mode == "range":
        path = "/api/v1/query_range"
        form = {"query": expr, "start": f"{end - lookback:.3f}", "end": f"{end:.3f}", "step": f"{step}s"}
    else:
        path = "/api/v1/query"
        form = {"query": expr, "time": f"{end:.3f}"}
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=QUERY_TIMEOUT) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read())
        except ValueError:
            return {"error": f"HTTP {error.code}"}
    except (urllib.error.URLError, OSError, ValueError) as error:
        return {"error": f"{type(error).__name__}: {error}"[:ERROR_TEXT]}
    if not isinstance(body, dict):
        return {"error": "response is not a JSON object"}
    if body.get("status") != "success":
        return {"error": f"{body.get('errorType', 'error')}: {body.get('error')}"[:ERROR_TEXT]}
    return {"data": body.get("data") or {}}


def _finite(sample: Any) -> bool:
    try:
        return math.isfinite(float(sample[1]))
    except (TypeError, ValueError, IndexError):
        return False


def _count(data: dict[str, Any]) -> tuple[int, int]:
    """(series, series with at least one finite sample) for a vector, matrix or scalar result."""
    kind, result = data.get("resultType"), data.get("result")
    if kind in ("scalar", "string"):
        return 1, int(kind == "scalar" and _finite(result))
    if not isinstance(result, list):
        return 0, 0
    with_values = 0
    for series in result:
        if not isinstance(series, dict):
            continue
        samples = series.get("values") if kind == "matrix" else [series.get("value")]
        if any(_finite(sample) for sample in samples or []):
            with_values += 1
    return len(result), with_values


def evaluate(payload: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    planned = plan(payload)
    end = time.time() if now is None else now
    base = str(payload.get("prometheus_url", ""))
    panels = []
    for entry in planned["panels"]:
        targets = []
        for target in entry["targets"]:
            if "expr" not in target:
                targets.append({k: target[k] for k in ("ref", "datasource", "status")})
                continue
            for mode in target["modes"]:
                answer = _query(base, mode, target["expr"], end, planned["lookback"], planned["step"])
                row = {"ref": target["ref"], "mode": mode}
                if "error" in answer:
                    row.update(status="error", error=answer["error"])
                else:
                    series, with_values = _count(answer["data"])
                    row.update(
                        status="data" if with_values else "empty",
                        series=series,
                        series_with_values=with_values,
                    )
                targets.append(row)
        queried = [t for t in targets if t["status"] in ("data", "empty", "error")]
        if not queried:
            status = "skipped"
        elif all(t["status"] == "data" for t in queried):
            status = "pass"
        elif any(t["status"] == "error" for t in queried):
            status = "error"
        else:
            status = "empty"
        panels.append({"title": entry["title"], "status": status, "targets": targets})

    verified = [p for p in panels if p["status"] != "skipped"]
    failing = [p["title"] for p in verified if p["status"] != "pass"]
    return {
        "status": "pass" if verified and not failing else "fail",
        "dashboard_uid": planned["dashboard"].get("uid"),
        "dashboard_title": planned["dashboard"].get("title"),
        "lookback_seconds": planned["lookback"],
        "step_seconds": planned["step"],
        "variables": planned["variables"],
        "panels_verified": len(verified),
        "panels_skipped": len(panels) - len(verified),
        "panels_failing": failing,
        "panels": panels,
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise Refused("input must be a JSON object")
        report = evaluate(payload)
    except Refused as refusal:
        print(json.dumps({"status": "refused", "reason": str(refusal)}))
        return 2
    except ValueError:
        print(json.dumps({"status": "refused", "reason": "input is not valid JSON"}))
        return 2
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
