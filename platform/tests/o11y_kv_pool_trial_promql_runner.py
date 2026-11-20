#!/usr/bin/env python3
"""Prove the rendered KV pool trial stop-line rule by native PromQL evaluation.

The rule is rendered from the committed alert template with the DGX scrape on,
and promtool evaluates it over one day of synthetic vLLM series. Grafana's
threshold condition B (gt 0, reducer last) is applied in PromQL as "> 0", so a
case "fires" when the query returns one sample and is silent when it returns
none. Two mutants must each fail as a PromQL result mismatch: the expression
without its count() wrap (a zero hit ratio on a busy day no longer passes gt 0)
and the expression without its prompt-token condition (a low ratio on a quiet
day would fire). The engine, image, and cleanup helpers are imported from
o11y_native_promql_runner.py; main() follows o11y_endpoint_missing_promql_runner.py.
"""

from __future__ import annotations

import importlib.util
import json
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
GROUP = "kv-pool-trial"
RULE_UID = "inference_kv_pool_trial_stop_line"
TOKEN_CLAUSE = ' and (sum(increase(vllm:prompt_tokens_total{job="dgx-spark-vllm"}[1d])) >= 1e6)'
LABELS = {"job": "dgx-spark-vllm", "model_name": "trial-model"}
# One day of 5-minute samples (288 steps), evaluated at 1d. Healthy: no preemption,
# pool at 0.5, hit ratio 0.9, 1.44 million prompt tokens (a busy day).
HEALTHY = {
    "vllm:num_preemptions_total": "0x288",
    "vllm:kv_cache_usage_perc": "0.5x288",
    "vllm:prefix_cache_hits_total": "0+900x288",
    "vllm:prefix_cache_queries_total": "0+1000x288",
    "vllm:prompt_tokens_total": "0+5000x288",
}
LOW_RATIO = {"vllm:prefix_cache_hits_total": "0+500x288"}
QUIET = {"vllm:prompt_tokens_total": "0+100x288"}
# (name, overrides of HEALTHY, fires)
CASES = (
    ("healthy", {}, False),
    ("preemption", {"vllm:num_preemptions_total": "0+1x288"}, True),
    ("pool-peak", {"vllm:kv_cache_usage_perc": "0.5x200 0.95 0.5x87"}, True),
    ("low-ratio-busy-day", LOW_RATIO, True),
    ("zero-ratio-busy-day", {"vllm:prefix_cache_hits_total": "0x288"}, True),
    ("low-ratio-quiet-day", {**LOW_RATIO, **QUIET}, False),
)
# Each mutant runs against the one case that must kill it.
MUTANT_CASES = {"no-count-wrap": "zero-ratio-busy-day", "no-prompt-token-condition": "low-ratio-quiet-day"}
PASS_LINE = (
    "PASS: rendered KV pool trial stop line fires on each branch alone and on a zero hit ratio on a "
    "busy day, and stays silent when healthy and on a quiet day"
)


def _rule():
    env = Environment(undefined=StrictUndefined, trim_blocks=True)
    env.filters["bool"] = bool
    env.filters["to_json"] = json.dumps
    template = env.from_string(ALERTS_TEMPLATE.read_text(encoding="utf-8"))
    document = yaml.safe_load(template.render(
        local_mode=False, o11y_alerts_enabled=True, o11y_expected_metrics_targets=[],
        dgx_spark_scrape_enabled=True,
    ))
    groups = [group for group in document["groups"] if group["name"] == GROUP]
    rules = [rule for group in groups for rule in group["rules"]]
    if len(rules) != 1 or rules[0].get("uid") != RULE_UID:
        raise native.RunnerError("rendered alert set does not hold exactly the one KV pool trial rule")
    return rules[0]


def queries():
    """The rendered rule as a PromQL query with its Grafana threshold, plus both mutants."""
    rule = _rule()
    threshold = rule["data"][1]["model"]
    condition = threshold["conditions"][0]
    if (
        rule["condition"] != "B"
        or threshold["expression"] != "A"
        or condition["reducer"]["type"] != "last"
        or condition["evaluator"] != {"type": "gt", "params": [0]}
    ):
        raise native.RunnerError(
            "rendered condition is not B = last(A) gt 0; the PromQL encoding below would be wrong"
        )
    expression = rule["data"][0]["model"]["expr"]
    if not (expression.startswith("count(") and expression.endswith(")")):
        raise native.RunnerError("rendered expression is not wrapped in count()")
    if expression.count(TOKEN_CLAUSE) != 1:
        raise native.RunnerError("rendered expression does not hold the prompt-token condition once")
    unwrapped = expression[len("count("):-1]
    return {
        "positive": f"({expression}) > 0",
        "no-count-wrap": f"({unwrapped}) > 0",
        "no-prompt-token-condition": f"({expression.replace(TOKEN_CLAUSE, '')}) > 0",
    }


def _test_lines(query, overrides, fires):
    labels = native._format_labels(LABELS)
    lines = ["  - interval: 5m", "    input_series:"]
    for name, values in {**HEALTHY, **overrides}.items():
        lines.append(f"    - series: {json.dumps(f'{name}{{{labels}}}')}")
        lines.append(f"      values: {json.dumps(values)}")
    lines.append("    promql_expr_test:")
    lines.append(f"      - expr: {json.dumps(query)}")
    lines.append("        eval_time: 1d")
    lines.append(native._samples_yaml([({}, 1)] if fires else []))
    return lines


def _rules_file(query, cases):
    lines = ["rule_files: []", "evaluation_interval: 1m", "tests:"]
    for _name, overrides, fires in cases:
        lines.extend(_test_lines(query, overrides, fires))
    return ("\n".join(lines) + "\n").encode()


def build_cases():
    """(case name, promtool rules file) in run order: the positive set, then each mutant."""
    built = queries()
    cases = [("positive", _rules_file(built["positive"], CASES))]
    for mutant, target in MUTANT_CASES.items():
        cases.append((mutant, _rules_file(built[mutant], [case for case in CASES if case[0] == target])))
    return cases


def main():
    started = time.monotonic()
    deadline = started + native.TOTAL_SECONDS
    cleanup_deadline = deadline
    owner = uuid.uuid4().hex
    names = []
    engine = None
    cleanup_completed = False
    try:
        image = native._fallback_image()
        cases = build_cases()
        names = [f"o11y-kvpool-promql-{owner[:16]}-{index}" for index in range(len(cases))]
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

        for index, (case_name, case_rules) in enumerate(cases):
            argv = native._evaluator_command(engine, image, names[index], owner)
            available = deadline - native.CLEANUP_RESERVE_SECONDS - time.monotonic()
            if available <= 0:
                raise native.RunnerError("insufficient time remains for evaluator and owned cleanup")
            run_seconds = min(45, available / (len(cases) - index))
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
                        f"rendered stop-line rule failed its native fixture; bounded evaluator output: {detail}"
                    )
                print(PASS_LINE)
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
