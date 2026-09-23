#!/usr/bin/env python3
"""Turn Semaphore task output into per-service workflow status (the conformance collector).

Change service-deployment-workflow, platform/service-onboarding-workflow: "Every step emits
one structured result", "A failure with no result is still recorded", "Per-service
conformance is tracked and reported".

A step result is recorded by tasks/emit-step-result.yml with set_stats; the repository
ansible.cfg makes Ansible print it as one line `RUN: {...json...}` after `CUSTOM STATS:`.
A task that FAILED without recording one is still a result: the registry maps its template
to a step, and it is recorded as `fail` with the last twenty output lines as the error.

stdin: {"registry": [<registry steps>],
        "tasks": [{"id", "status", "end", "template_name", "service", "output": [lines]}]}
        optional "now_ns": a timestamp in nanoseconds, to also emit Loki push streams
stdout: {"services": {service: {step: {status, task_id, end, check_mode, error, evidence}}},
         "failed_steps": {service: [step, ...]},
         "loki_streams": [...]  (only with now_ns; POST /loki/api/v1/push body "streams")}
Only the LATEST result per (service, step) is kept, by task id.
"""

import json
import re
import sys

RUN = re.compile(r"^\s*RUN:\s*(\{.*\})\s*$")
FINISHED = {"success", "error"}
TAIL = 20


def results_in(lines: list[str]) -> list[dict]:
    """Every step_result recorded in one task's output (normally one)."""
    found = []
    for line in lines:
        match = RUN.match(line)
        if not match:
            continue
        try:
            stats = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(stats.get("step_result"), dict):
            found.append(stats["step_result"])
    return found


def _step_for(template: str, registry: list[dict]) -> str | None:
    base = re.sub(r" \((Dev|Local)\)$", "", template or "")
    for step in sorted(registry, key=lambda s: s["order"]):
        names = {step.get("executor"), step.get("snapshot")}
        if base in names or (step.get("executor") == "Deploy {service}" and base.startswith("Deploy ")):
            return step["id"]
    return None


def aggregate(registry: list[dict], tasks: list[dict]) -> dict:
    services: dict[str, dict] = {}
    for task in sorted(tasks, key=lambda t: t["id"]):
        if task.get("status") not in FINISHED:
            continue
        results = results_in(task.get("output", []))
        if not results and task["status"] == "error":
            step = _step_for(task.get("template_name", ""), registry)
            if step and task.get("service"):
                results = [{
                    "service": task["service"], "step": step, "status": "fail",
                    "check_mode": False, "evidence": {},
                    "error": "\n".join(task.get("output", [])[-TAIL:]) or "task failed with no output",
                }]
        for result in results:
            service = result.get("service") or task.get("service")
            if not service or not result.get("step"):
                continue
            services.setdefault(service, {})[result["step"]] = {
                "status": result.get("status"),
                "task_id": task["id"],
                "end": task.get("end"),
                "check_mode": result.get("check_mode"),
                "error": result.get("error") or None,
                "evidence": result.get("evidence", {}),
            }
    failed = {
        service: sorted(step for step, r in steps.items() if r["status"] == "fail")
        for service, steps in services.items()
    }
    return {"services": services, "failed_steps": failed}


def loki_streams(agg: dict, now_ns: int) -> list[dict]:
    """One Loki stream per (service, step), labelled so the dashboard can filter on them."""
    streams = []
    for service, steps in sorted(agg["services"].items()):
        for step, result in sorted(steps.items()):
            line = json.dumps({"task_id": result["task_id"], "error": result["error"],
                               "check_mode": result["check_mode"]}, sort_keys=True)
            streams.append({
                "stream": {"job": "agent-cloud-conformance", "service": service, "step": step,
                           "status": str(result["status"])},
                "values": [[str(now_ns), line]],
            })
    return streams


def main() -> int:
    data = json.load(sys.stdin)
    agg = aggregate(data["registry"], data["tasks"])
    if data.get("now_ns"):
        agg["loki_streams"] = loki_streams(agg, int(data["now_ns"]))
    json.dump(agg, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
