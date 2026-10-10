#!/usr/bin/env python3
"""Evaluate one provisioned Grafana dashboard's Prometheus panels and explicitly selected Loki panels.

Read-only: it reads the dashboard JSON the receiver's Grafana provisions from, and sends
query and query_range requests to the receiver's Prometheus and Loki APIs. It prints one
JSON report on stdout carrying panel titles, target status and counts only: no label value,
log line, query expression or sample value leaves this script.

Input (stdin, JSON):
  dashboards_dir   directory Grafana's file provider loads (config/grafana/dashboards)
  dashboard_uid    the dashboard's `uid`
  lookback         Prometheus duration, one unit: 30m, 1h, 2d (the dashboard time range)
  panel_titles     optional exact panel titles; empty means every Prometheus panel, while
                   Loki panels must be explicitly selected by title
  expected_empty_panels  optional exact selected panel titles whose empty results are allowed;
                   every queried target in an allowed panel must be empty and error-free
  variables        optional {name: value} overriding a template variable's saved default;
                   substituted as written, as Grafana substitutes a custom All value, but
                   never one that could change the query's structure (see below)
  prometheus_url   base URL of the Prometheus HTTP API
  loki_url         base URL of the Loki HTTP API
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
  - Prometheus targets with `instant: true` (and no `range: true`) are queried at the end of
    the range; every other target is queried over it. Loki range queries use the same bounds
    and a fixed result limit. Instant raw log stream queries are refused; instant metric
    LogQL targets use Loki's instant endpoint
    (https://grafana.com/docs/loki/latest/reference/loki-http-api/).
  - A target or panel with no datasource uses the default one, which is Prometheus
    (datasources.yml, isDefault). Loki targets are evaluated only when their panel title is
    explicitly named in `panel_titles`; whole-dashboard runs skip Loki and unsupported data sources.
  - What this script does not reproduce, an evaluated panel may not use: a panel's own
    time range (timeFrom, timeShift), its query options (maxDataPoints, a min interval on
    the panel or a target, intervalFactor), repetition (repeat, on the panel or its row) and
    library panels (whose queries live outside the dashboard file). Each is refused. A
    library panel is refused whatever its datasource, since the file does not say.
  - A row's panels are in its own `panels` list only while it is collapsed; an expanded row
    keeps `panels: []` and its panels follow it at the top level until the next row
    (Grafana DashboardModel getRowPanels/toggleRow). Both count as the row's.
  - `#` outside a string starts a PromQL comment that runs to the end of the line.

A variable's value may not change the query's structure. Inside a quoted PromQL string it
may hold regex characters but not the closing quote, a backslash or a line break; outside
one (an offset, a range) it must be a duration or a number. An override is also refused for
any of " { } ( ), a backslash or a line break, and the report names it with the first 12
hex digits of its sha256 rather than its value.

No backend error body reaches the report: an execution error may embed label values or log
content. Only a fixed error class, an HTTP status or an exception class name is reported.

A target has data when at least one returned series carries a finite sample: Prometheus
encodes NaN as a string (https://prometheus.io/docs/prometheus/latest/querying/api/), and
histogram_quantile over a window with no traffic returns NaN, which a panel draws as a gap.
A native histogram sample (the `histogram` key of a vector series, `histograms` of a matrix
series, same page) counts when its observation count is finite.
A panel passes when every queried Prometheus or Loki target has data. A named expected-empty
panel passes only when every queried target is empty and error-free; mixed data and empty
targets still fail. The run passes when at least one panel is evaluated and every evaluated
panel passes or is explicitly expected empty. Whole-dashboard runs evaluate Prometheus
panels; Loki panels require explicit title selection.

Exit status: 0 pass, 1 a panel has no data or a query failed, 2 refused (bad input).
"""

from __future__ import annotations

import hashlib
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
LOKI_RESULT_LIMIT = 100
# The Prometheus HTTP API's errorType values (prometheus web/api/v1/api.go).
ERROR_TYPES = {"timeout", "canceled", "execution", "bad_data", "internal", "unavailable", "not_found", "not_acceptable"}
# Characters an override may never carry, wherever it lands.
OVERRIDE_FORBIDDEN = re.compile(r'["{}()\\\r\n]')
# A value substituted outside a string literal: a duration or a number, nothing else.
BARE_VALUE = re.compile(r"^[0-9]+(?:\.[0-9]+)?(?:ms|s|m|h|d|w|y)?$")
# Panel and target fields whose effect this script does not reproduce.
PANEL_UNSUPPORTED = ("maxDataPoints", "interval", "timeFrom", "timeShift", "repeat", "libraryPanel")
TARGET_UNSUPPORTED = ("interval",)


class Refused(Exception):
    """An input the script will not evaluate; the message names the reason."""


def duration_seconds(text: Any) -> int:
    match = DURATION.fullmatch(str(text))
    if not match:
        raise Refused(f"lookback must be one Prometheus duration unit (30m, 1h, 2d), got {text!r}")
    return int(match.group(1)) * UNIT_SECONDS[match.group(2)]


def flatten_panels(panels: list[Any], repeated_row: bool = False) -> list[dict[str, Any]]:
    """Every panel, including those a collapsed row holds in its own `panels` list and those
    following an expanded row up to the next row. A panel of a repeating row is marked,
    since the row repeats it."""
    out: list[dict[str, Any]] = []
    in_repeating_row = repeated_row
    for panel in panels or []:
        if not isinstance(panel, dict):
            continue
        if panel.get("type") == "row":
            in_repeating_row = repeated_row or bool(panel.get("repeat"))
            out.extend(flatten_panels(panel.get("panels", []), in_repeating_row))
        else:
            out.append(dict(panel, _repeated_row=True) if in_repeating_row else panel)
    return out


def _set(value: Any) -> bool:
    return value not in (None, "", [], {}, False)


def refuse_unsupported(panel: dict[str, Any], targets: list[dict[str, Any]]) -> None:
    title = panel.get("title")
    fields = [f for f in PANEL_UNSUPPORTED if _set(panel.get(f))]
    if panel.get("_repeated_row"):
        fields.append("repeat (row)")
    for target in targets:
        fields += [f"{f} (target {target.get('refId', '')})" for f in TARGET_UNSUPPORTED if _set(target.get(f))]
        if target.get("intervalFactor") not in (None, 1, "1"):
            fields.append(f"intervalFactor (target {target.get('refId', '')})")
    if fields:
        raise Refused(f"panel {title!r} uses {', '.join(fields)}, which this check does not reproduce")


def string_spans(expr: str) -> list[tuple[int, int]]:
    """(start, end) of each PromQL string literal, quotes included: "..." and '...' with
    backslash escapes, `...` raw. A comment (`#` to the end of the line) opens no string."""
    spans, i = [], 0
    while i < len(expr):
        quote = expr[i]
        if quote == "#":
            end = expr.find("\n", i)
            i = len(expr) if end < 0 else end
        elif quote in "\"'`":
            j = i + 1
            while j < len(expr) and expr[j] != quote:
                j += 2 if quote != "`" and expr[j] == "\\" else 1
            spans.append((i, j + 1))
            i = j + 1
        else:
            i += 1
    return spans


def datasource_type(target: dict[str, Any], panel: dict[str, Any]) -> str:
    source = target.get("datasource") or panel.get("datasource")
    if source is None:
        return "prometheus"
    kind = str((source.get("type") or source.get("uid") or "") if isinstance(source, dict) else source)
    if kind.startswith("$"):
        raise Refused(f"panel {panel.get('title')!r} takes its datasource from a variable")
    return kind.lower()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not follow a server-supplied location to another endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
            value = str(overrides[name])
            if not value or OVERRIDE_FORBIDDEN.search(value):
                raise Refused(
                    f"override for {name!r} is empty or holds a quote, brace, parenthesis, backslash or line break"
                )
            values[name] = value
            sources[name] = "override sha256:" + hashlib.sha256(value.encode()).hexdigest()[:12]
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
    spans = string_spans(expr)

    def replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bracket") or match.group("bare")
        if match.group("bformat") is not None or match.group("kformat") is not None:
            raise Refused(f"variable {name!r} uses a format option, which this check does not reproduce")
        if name in builtins:
            return builtins[name]
        if name in values:
            value = values[name]
            quote = next((expr[a] for a, b in spans if a < match.start() < b), None)
            if quote is None and not BARE_VALUE.fullmatch(value):
                raise Refused(f"variable {name!r} is used outside a string, where only a duration or number is safe")
            if quote is not None and (quote in value or "\\" in value or "\n" in value or "\r" in value):
                raise Refused(f"variable {name!r} would close the string it is substituted into")
            return value
        if name.startswith("__"):
            raise Refused(f"built-in variable {name!r} is not supported by this check")
        raise Refused(f"variable {name!r} has no default this check can reproduce; pass it in dashboard_variables")

    return REFERENCE.sub(replace, expr)


def plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve every selected panel's queries without contacting either backend."""
    lookback = duration_seconds(payload.get("lookback"))
    scrape = int(payload.get("scrape_interval_seconds") or 0)
    if scrape <= 0:
        raise Refused("scrape_interval_seconds must be a positive integer")
    titles = payload.get("panel_titles") or []
    overrides = payload.get("variables") or {}
    expected_empty = payload.get("expected_empty_panels", [])
    if not isinstance(titles, list) or not all(isinstance(t, str) and t for t in titles):
        raise Refused("panel_titles must be a list of exact panel titles")
    if not isinstance(expected_empty, list) or not all(isinstance(t, str) and t for t in expected_empty):
        raise Refused("expected_empty_panels must be a list of exact panel titles")
    if len(expected_empty) != len(set(expected_empty)):
        raise Refused("expected_empty_panels must not contain duplicates")
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
        if _set(panel.get("libraryPanel")):
            raise Refused(
                f"panel {panel.get('title')!r} is a library panel: its queries live outside this file, "
                "which this check does not reproduce"
            )
        targets = [t for t in panel.get("targets", []) or [] if isinstance(t, dict)]
        if not targets:
            continue
        evaluated = [
            t
            for t in targets
            if not t.get("hide")
            and (
                datasource_type(t, panel) == "prometheus"
                or (titles and datasource_type(t, panel) == "loki")
            )
        ]
        if evaluated:
            refuse_unsupported(panel, evaluated)
        entry: dict[str, Any] = {"title": panel.get("title", ""), "targets": []}
        for target in targets:
            kind = datasource_type(target, panel)
            ref = target.get("refId", "")
            if kind not in ("prometheus", "loki"):
                entry["targets"].append({"ref": ref, "datasource": kind, "status": "skipped"})
                continue
            if kind == "loki" and not titles:
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
            if kind == "loki":
                query_type = str(target.get("queryType", "")).lower()
                if query_type and query_type not in ("range", "instant"):
                    raise Refused(f"panel {entry['title']!r} target {ref!r} uses an unsupported Loki query type")
                if query_type == "instant":
                    modes = ["instant"]
                elif query_type == "range":
                    modes = ["range"]
                if "instant" in modes and expr.lstrip().startswith("{"):
                    raise Refused(
                        f"panel {entry['title']!r} target {ref!r} uses an unsupported instant log stream query"
                    )
            entry["targets"].append(
                {"ref": ref, "datasource": kind, "modes": modes, "expr": interpolate(expr, values, builtins)}
            )
        planned.append(entry)
    if not any(t.get("expr") for p in planned for t in p["targets"]):
        raise Refused("no supported Prometheus or Loki query is selected on this dashboard")
    dashboard_titles = {p.get("title") for p in panels}
    missing_expected = sorted(set(expected_empty) - dashboard_titles)
    if missing_expected:
        raise Refused(f"expected_empty_panels {missing_expected} are not on this dashboard")
    queryable_titles = {p["title"] for p in planned if any("expr" in t for t in p["targets"])}
    unselected_expected = sorted(set(expected_empty) - queryable_titles)
    if unselected_expected:
        raise Refused(f"expected_empty_panels {unselected_expected} are not selected for evaluation")
    return {
        "dashboard": dashboard,
        "lookback": lookback,
        "step": step,
        "variables": sources,
        "expected_empty_panels": expected_empty,
        "panels": planned,
    }


def _query(base: str, backend: str, mode: str, expr: str, end: float, lookback: int, step: int) -> dict[str, Any]:
    if mode == "range":
        if backend == "loki":
            path = "/loki/api/v1/query_range"
            form = {
                "query": expr,
                "start": f"{end - lookback:.3f}",
                "end": f"{end:.3f}",
                "step": f"{step}s",
                "limit": str(LOKI_RESULT_LIMIT),
            }
        else:
            path = "/api/v1/query_range"
            form = {"query": expr, "start": f"{end - lookback:.3f}", "end": f"{end:.3f}", "step": f"{step}s"}
    else:
        path = "/loki/api/v1/query" if backend == "loki" else "/api/v1/query"
        form = {"query": expr, "time": f"{end:.3f}"}
    request = urllib.request.Request(
        base.rstrip("/") + path,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=QUERY_TIMEOUT) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as error:
        if backend == "prometheus":
            try:
                body = json.loads(error.read())
            except (OSError, ValueError):
                return {"error": f"HTTP {error.code}"}
            kind = body.get("errorType") if isinstance(body, dict) else None
            if isinstance(body, dict) and body.get("status") == "error":
                return {"error": kind if isinstance(kind, str) and kind in ERROR_TYPES else "unrecognised error"}
        return {"error": f"HTTP {error.code}"}
    except (urllib.error.URLError, OSError, ValueError) as error:
        # The class name only: an exception's text is not ours to vouch for.
        return {"error": type(error).__name__}
    if not isinstance(body, dict):
        return {"error": "response is not a JSON object"}
    if body.get("status") != "success":
        # errorType only, never `error`: execution errors embed label sets.
        kind = body.get("errorType")
        return {"error": kind if isinstance(kind, str) and kind in ERROR_TYPES else "unrecognised error"}
    return {"data": body.get("data") or {}}


def _finite(sample: Any) -> bool:
    try:
        value = sample[1]
        return math.isfinite(float(value["count"] if isinstance(value, dict) else value))
    except (TypeError, ValueError, IndexError, KeyError):
        return False


def _count(data: dict[str, Any]) -> tuple[int, int]:
    """(result groups, groups with data) without retaining or reporting sample contents."""
    kind, result = data.get("resultType"), data.get("result")
    if kind == "streams":
        if not isinstance(result, list):
            return 0, 0
        with_values = sum(isinstance(stream, dict) and bool(stream.get("values")) for stream in result)
        return len(result), with_values
    if kind in ("scalar", "string"):
        return 1, int(kind == "scalar" and _finite(result))
    if not isinstance(result, list):
        return 0, 0
    with_values = 0
    for series in result:
        if not isinstance(series, dict):
            continue
        if kind == "matrix":
            samples = (series.get("values") or []) + (series.get("histograms") or [])
        else:
            samples = [series.get("value"), series.get("histogram")]
        if any(_finite(sample) for sample in samples):
            with_values += 1
    return len(result), with_values


def evaluate(payload: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    planned = plan(payload)
    end = time.time() if now is None else now
    bases = {"prometheus": str(payload.get("prometheus_url", "")), "loki": str(payload.get("loki_url", ""))}
    panels = []
    for entry in planned["panels"]:
        targets = []
        for target in entry["targets"]:
            if "expr" not in target:
                targets.append({k: target[k] for k in ("ref", "datasource", "status")})
                continue
            for mode in target["modes"]:
                answer = _query(
                    bases[target["datasource"]], target["datasource"], mode, target["expr"], end,
                    planned["lookback"], planned["step"],
                )
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
        elif all(t["status"] == "empty" for t in queried) and entry["title"] in planned["expected_empty_panels"]:
            status = "expected_empty"
        else:
            status = "empty" if all(t["status"] == "empty" for t in queried) else "partial"
        panels.append({"title": entry["title"], "status": status, "targets": targets})

    verified = [p for p in panels if p["status"] != "skipped"]
    failing = [p["title"] for p in verified if p["status"] not in ("pass", "expected_empty")]
    return {
        "status": "pass" if verified and not failing else "fail",
        "dashboard_uid": planned["dashboard"].get("uid"),
        "dashboard_title": planned["dashboard"].get("title"),
        "lookback_seconds": planned["lookback"],
        "step_seconds": planned["step"],
        "variables": planned["variables"],
        "panels_verified": len(verified),
        "verified_panel_titles": [p["title"] for p in verified],
        "panels_skipped": len(panels) - len(verified),
        "expected_empty_panel_titles": [p["title"] for p in verified if p["status"] == "expected_empty"],
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
