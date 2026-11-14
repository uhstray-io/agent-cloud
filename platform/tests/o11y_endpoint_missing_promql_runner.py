#!/usr/bin/env python3
"""Prove the rendered endpoint missing-data rule by native PromQL evaluation (openspec 2.3c.5).

The rule expression is rendered from the committed alert template with four
declared targets, then promtool evaluates it over synthetic series. The
positive fixture must match exactly; two mutants must each fail as a PromQL
result mismatch. The endpoint drill's absent-mode readiness query is read from
the committed drill playbook and evaluated the same way: it must return nothing
while the probe series is present and one zero-valued series with the probe's
labels once it stops; a mutant without the "- 1" offset must fail as a result
mismatch. Engine, image, and cleanup handling is shared with
o11y_native_promql_runner.py, which is imported rather than copied.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
import uuid
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

_NATIVE_PATH = Path(__file__).resolve().with_name("o11y_native_promql_runner.py")
_spec = importlib.util.spec_from_file_location("o11y_native_promql_runner", _NATIVE_PATH)
if _spec is None or _spec.loader is None:
    raise SystemExit("could not load the shared native PromQL runner")
native = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(native)

ROOT = Path(__file__).resolve().parents[2]
ALERTS_TEMPLATE = ROOT / "platform/services/o11y/deployment/templates/alerts.yml.j2"
DRILL_PLAYBOOK = ROOT / "platform/playbooks/drill-o11y-endpoint-availability.yml"
RULE_UID = "o11y_endpoint_probe_missing"
TARGETS = ("alpha", "beta", "gamma", "delta")
FRESHNESS_SECONDS = 600
PROBE_VALUES = {"alpha": "1x9", "beta": "1x30"}
UP_VALUES = {"alpha": "1x30", "beta": "1x30", "gamma": "0x30"}
EVAL_17M = ("delta",)
EVAL_23M = ("alpha", "delta")
UNLESS_CLAUSE = re.compile(
    r" unless on\(job,instance,service,target_id,environment,cluster\) \(up\{[^{}]*\} == 0\)"
)
ABSENT_OVER_TIME = re.compile(r"absent_over_time\((probe_success\{[^{}]*\})\[" + str(FRESHNESS_SECONDS) + r"s\]\)")
PASS_LINE = (
    "PASS: rendered missing-data rule fires only for absent series not explained by a down scrape, "
    "including when up is absent"
)
READINESS_PASS_LINE = (
    "PASS: the absent readiness query reads one zero-valued series with the probe labels once the "
    "probe series stops, and no series while it is present"
)
READINESS_OFFSET = " - 1"
# The drill's own target, as the census would name it; its labels must equal _identity("alpha").
DRILL_TARGET = {
    "target_id": "service:alpha", "service_identity": "alpha", "environment": "prod",
    "cluster": "test-cluster", "freshness_seconds": FRESHNESS_SECONDS,
}


def _identity(target):
    return {
        "job": "o11y-availability",
        "instance": f"service:{target}",
        "service": target,
        "target_id": f"service:{target}",
        "environment": "prod",
        "cluster": "test-cluster",
    }


def _render_expression():
    env = Environment(undefined=StrictUndefined, trim_blocks=True)
    env.filters["bool"] = bool
    env.filters["to_json"] = json.dumps
    template = env.from_string(ALERTS_TEMPLATE.read_text(encoding="utf-8"))
    targets = [
        {
            "target_id": f"service:{name}", "service_identity": name, "environment": "prod",
            "cluster": "test-cluster", "freshness_seconds": FRESHNESS_SECONDS,
        }
        for name in TARGETS
    ]
    document = yaml.safe_load(template.render(
        local_mode=False,
        o11y_environment="prod",
        o11y_cluster="test-cluster",
        o11y_alerts_enabled=True,
        _o11y_availability={"enabled": True, "targets": targets},
    ))
    rules = document["groups"][0]["rules"]
    matches = [rule for rule in rules if rule.get("uid") == RULE_UID]
    if len(matches) != 1:
        raise native.RunnerError("rendered alert set does not contain exactly one missing-data rule")
    expression = matches[0]["data"][0]["model"]["expr"]
    if not isinstance(expression, str) or expression.count("absent_over_time(") != len(TARGETS):
        raise native.RunnerError("rendered missing-data expression does not hold one term per target")
    return expression


def _mutants(expression):
    without_suppression, removed = UNLESS_CLAUSE.subn("", expression)
    instant, replaced = ABSENT_OVER_TIME.subn(r"absent(\1)", expression)
    if removed != len(TARGETS) or replaced != len(TARGETS):
        raise native.RunnerError("mutants could not be derived from the rendered missing-data expression")
    if without_suppression == expression or instant == expression:
        raise native.RunnerError("mutant derivation did not change the rendered expression")
    return {"no-scrape-suppression": without_suppression, "instant-absent": instant}


def _input_rows():
    rows = []
    for target in TARGETS:
        labels = native._format_labels(_identity(target))
        if target in PROBE_VALUES:
            rows.append((f"probe_success{{{labels}}}", PROBE_VALUES[target]))
        if target in UP_VALUES:
            rows.append((f"up{{{labels}}}", UP_VALUES[target]))
    return rows


def _header_lines():
    return [
        "rule_files: []",
        "evaluation_interval: 1m",
        "tests:",
        "  - interval: 1m",
        "    input_series:",
    ]


def _input_lines():
    lines = []
    for series, values in _input_rows():
        lines.append(f"    - series: {json.dumps(series)}")
        lines.append(f"      values: {json.dumps(values)}")
    return lines


def _drill_task(name):
    """The one named task in the drill playbook, at any depth, read from the committed YAML."""
    stack = list(yaml.safe_load(DRILL_PLAYBOOK.read_text(encoding="utf-8")) or [])
    while stack:
        entry = stack.pop()
        if not isinstance(entry, dict):
            continue
        if entry.get("name") == name:
            return entry
        for key in ("tasks", "block", "rescue", "always"):
            if isinstance(entry.get(key), list):
                stack.extend(entry[key])
    raise native.RunnerError(f"drill playbook has no task named {name!r}")


def _render_text(text, context):
    return Environment(undefined=StrictUndefined).from_string(text).render(context).strip()


def _readiness_expression():
    """The absent-mode readiness query, rendered from the committed drill playbook."""
    probe_text = _drill_task("Bind the endpoint probe selector the readiness waits query")[
        "ansible.builtin.set_fact"]["_endpoint_probe_query"]
    probe = _render_text(probe_text, {"_endpoint_drill_target": DRILL_TARGET})
    labels = dict(re.findall(r'([a-z_]+)="([^"]*)"', probe))
    if not probe.startswith("probe_success{") or labels != _identity("alpha"):
        raise native.RunnerError("drill probe selector does not match the synthetic probe identity")
    ready_text = _drill_task("Bind the endpoint readiness query the drill mode waits for")[
        "ansible.builtin.set_fact"]["_endpoint_ready_query"]
    expression = _render_text(ready_text, {"availability_drill_mode": "absent", "_endpoint_probe_query": probe})
    if not (expression.startswith("absent_over_time(probe_success{") and expression.endswith("}[30s]) - 1")):
        raise native.RunnerError("drill absent readiness query is not the absent_over_time form")
    return expression


def _readiness_rules(expression, value):
    """Two evaluations: nothing while alpha's probe series is present, one labelled sample once it stops."""
    lines = _header_lines() + _input_lines()
    lines.append("    promql_expr_test:")
    lines.append(f"      - expr: {json.dumps(expression)}")
    lines.append("        eval_time: 5m")
    lines.append(native._samples_yaml([]))
    lines.append(f"      - expr: {json.dumps(expression)}")
    lines.append("        eval_time: 17m")
    lines.append(native._samples_yaml([(_identity("alpha"), value)]))
    return ("\n".join(lines) + "\n").encode()


def _rules(expression):
    lines = _header_lines() + _input_lines()
    lines.append("    promql_expr_test:")
    lines.append(f"      - expr: {json.dumps(expression)}")
    lines.append("        eval_time: 17m")
    lines.append(native._samples_yaml([(_identity(name), 1) for name in EVAL_17M]))
    lines.append(f"      - expr: {json.dumps(expression)}")
    lines.append("        eval_time: 23m")
    lines.append(native._samples_yaml([(_identity(name), 1) for name in EVAL_23M]))
    return ("\n".join(lines) + "\n").encode()


def main():
    started = time.monotonic()
    deadline = started + native.TOTAL_SECONDS
    cleanup_deadline = deadline
    owner = uuid.uuid4().hex
    names = [f"o11y-missing-promql-{owner[:16]}-{index}" for index in range(5)]
    engine = None
    cleanup_completed = False
    try:
        image = native._fallback_image()
        expression = _render_expression()
        mutants = _mutants(expression)
        readiness = _readiness_expression()
        readiness_mutant = readiness[: -len(READINESS_OFFSET)]
        candidates = native._engine_candidates()
        if not candidates:
            raise native.RunnerError("no supported container engine executable is installed")
        for candidate in candidates:
            ready, _output = native._engine_ready(
                candidate, min(deadline - native.CLEANUP_RESERVE_SECONDS, time.monotonic() + 20),
            )
            if ready:
                engine = candidate
                break
        if engine is None:
            raise native.RunnerError("no existing container engine is usable; no daemon or VM was started")
        present_deadline = min(deadline - native.CLEANUP_RESERVE_SECONDS, time.monotonic() + 10)
        if not native._image_present(engine, image, present_deadline):
            pull_deadline = min(deadline - native.CLEANUP_RESERVE_SECONDS, time.monotonic() + 120)
            code, _output, timed_out, _truncated = native._bounded_process(
                [engine, "pull", image], deadline=pull_deadline,
            )
            if code != 0 or timed_out:
                raise native.RunnerError("one permitted image acquisition failed")
        if not native._image_present(engine, image, deadline - native.CLEANUP_RESERVE_SECONDS):
            raise native.RunnerError("fallback Prometheus image is unavailable after acquisition")

        cases = [
            ("positive", _rules(expression)),
            ("no-scrape-suppression", _rules(mutants["no-scrape-suppression"])),
            ("instant-absent", _rules(mutants["instant-absent"])),
            ("readiness-absent", _readiness_rules(readiness, 0)),
            ("readiness-without-offset", _readiness_rules(readiness_mutant, 0)),
        ]
        for index, (case_name, case_rules) in enumerate(cases):
            argv = native._evaluator_command(engine, image, names[index], owner)
            runs_left = len(cases) - index
            available = deadline - native.CLEANUP_RESERVE_SECONDS - time.monotonic()
            if available <= 0:
                raise native.RunnerError("insufficient time remains for evaluator and owned cleanup")
            run_seconds = min(45, available / runs_left)
            code, output, timed_out, output_truncated = native._bounded_process(
                argv,
                deadline=min(deadline - native.CLEANUP_RESERVE_SECONDS, time.monotonic() + run_seconds),
                input_bytes=case_rules,
            )
            if timed_out:
                raise native.RunnerError(f"{case_name} evaluator timed out")
            if output_truncated:
                raise native.RunnerError(
                    f"{case_name} evaluator output exceeded the cap; result classification refused"
                )
            detail = output.decode("utf-8", errors="replace")
            failure = native._classify_evaluator_failure(output)
            if case_name == "positive":
                if code != 0:
                    raise native.RunnerError(
                        "rendered missing-data expression failed its native positive fixture; "
                        f"bounded evaluator output: {detail}"
                    )
                print(PASS_LINE)
            elif case_name == "readiness-absent":
                if code != 0:
                    raise native.RunnerError(
                        "rendered absent readiness query failed its native fixture; "
                        f"bounded evaluator output: {detail}"
                    )
                print(READINESS_PASS_LINE)
            else:
                if code == 0 or failure != "expected_result_mismatch":
                    raise native.RunnerError(
                        f"{case_name} mutant failure was not a PromQL result mismatch; "
                        f"bounded evaluator output: {detail}"
                    )
                print(f"KILLED: {case_name} mutant failed during PromQL evaluation ({failure})")
        native._cleanup(engine, owner, names, cleanup_deadline)
        cleanup_completed = True
        return 0
    except native.RunnerError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        if engine is not None and not cleanup_completed:
            try:
                native._cleanup(engine, owner, names, cleanup_deadline)
            except native.RunnerError as exc:
                print(f"CLEANUP LIMITATION: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
