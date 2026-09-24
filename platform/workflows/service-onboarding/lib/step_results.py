#!/usr/bin/env python3
"""Turn Semaphore task history into per-service workflow status (the conformance collector).

Change service-deployment-workflow, platform/service-onboarding-workflow: "Every step emits
one structured result", "A failure with no result is still recorded", "Per-service
conformance is tracked and reported".

A step result is recorded by tasks/emit-step-result.yml with set_stats; the repository
ansible.cfg makes Ansible print it as one line `RUN: {...json...}` after `CUSTOM STATS:`.
A task that FAILED without recording one is still a result: the registry maps its template
to a step, and it is recorded as `fail` with the last twenty output lines as the error.

The collector calls this three times, one JSON object on stdin each, keyed by "mode":

  select     {registry, templates, groups, host_services}
                                                    -> {"template_ids": [...]}
             the workflow templates whose history is worth reading: a per-service deploy
             counts only when its service is in this inventory
  pick       {groups, host_services, histories: [[task rows]]}
                                                    -> {"tasks": [task rows + "service"]}
             the newest PICK_WINDOW finished tasks per (template, service), newest first:
             whether a task was check mode is only certain once its output is read, so the
             real run behind a newer dry run is kept for aggregate to classify
  aggregate  {registry, templates, fetched: [uri results of raw_output, item = picked row],
              groups?, host_services?, now_ns?}
                                                    -> {services, validation, failed_steps,
                                                        status_by_service, report,
                                                        loki_streams (only with now_ns)}

Check mode never sets conformance (review of PR #195): a passing `--check` after a failed real
run must not clear the failure, because nothing on the service changed. A task is check-mode
when Semaphore recorded `params.dry_run` for it, or when its step result says so; its results
land in `validation` as evidence, and `services` / `status_by_service` / `failed_steps` come
only from real runs.

An inventory group is mapped to its first host's service_name (groups + host_services, both
plain data: hostvars themselves never cross), so a service is named the way its own step
results name it (step_ca_svc -> step-ca), never by string surgery.
"""

import json
import re
import sys

RUN = re.compile(r"^\s*RUN:\s*(\{.*\})\s*$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
VARIANT = re.compile(r" \((Dev|Local)\)$")
PER_SERVICE = "Deploy {service}"
FINISHED = {"success", "error"}
TAIL = 20
# How many finished tasks per (template, service) reach aggregate. A check-mode run is only
# certain from its OUTPUT (a history row may lack params.dry_run), so pick cannot discard
# older tasks; aggregate classifies them. ponytail: fixed window - a real run older than the
# newest PICK_WINDOW tasks (all dry runs) drops out; raise it if that ever happens.
PICK_WINDOW = 5


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
    base = VARIANT.sub("", template or "")
    for step in registry:
        if base in {step.get("executor"), step.get("snapshot")}:
            return step["id"]
        if step.get("executor") == PER_SERVICE and base.startswith("Deploy "):
            return step["id"]
    return None


def select(registry: list[dict], templates: list[dict], by_group: dict) -> list[int]:
    def wanted(t: dict) -> bool:
        step = _step_for(t["name"], registry)
        if step is None:
            return False
        per_service = next(s for s in registry if s["id"] == step).get("executor") == PER_SERVICE
        return not per_service or _service_of({"tpl_playbook": t.get("playbook")}, by_group) is not None
    return [t["id"] for t in templates if wanted(t)]


def _service_of(task: dict, by_group: dict) -> str | None:
    """The service a task ran for: the launch's target_service (a group such as tududi_svc,
    or a service name such as tududi, as provision-vm and the address steps take it), else
    the group a per-service deploy playbook is named after. None when neither names an
    inventory group (a phantom such as deploy-all.yml)."""
    try:
        target = json.loads(task.get("environment") or "{}").get("target_service")
    except (ValueError, AttributeError):
        target = None
    if isinstance(target, str) and target:
        return by_group.get(target) or by_group.get(target + "_svc")
    match = re.search(r"(?:^|/)deploy-([a-z0-9-]+)\.yml$", task.get("tpl_playbook") or "")
    return by_group.get(match.group(1).replace("-", "_") + "_svc") if match else None


def group_services(groups: dict, host_services: dict) -> dict:
    return {g: host_services[hosts[0]] for g, hosts in groups.items() if hosts and hosts[0] in host_services}


def is_check_mode(task: dict) -> bool:
    """Semaphore records Ansible check mode on the task as params.dry_run (v2.17 db/Task.go)."""
    return bool((task.get("params") or {}).get("dry_run"))


def pick(histories: list[list[dict]], by_group: dict) -> list[dict]:
    """The newest PICK_WINDOW finished tasks per (template, service), oldest first. aggregate
    reads each one's output and decides which were check mode; pick never discards the real
    run behind a newer dry run, because a dry run's history row may not say it is one
    (review of PR #229)."""
    by_key: dict[tuple, list[dict]] = {}
    for history in histories:
        for task in history:
            if task.get("status") in FINISHED:
                by_key.setdefault((task["template_id"], _service_of(task, by_group)), []).append(task)
    kept = [dict(t, service=key[1]) for key, tasks in by_key.items()
            for t in sorted(tasks, key=lambda t: t["id"], reverse=True)[:PICK_WINDOW]]
    return sorted(kept, key=lambda t: t["id"])


def aggregate(registry: list[dict], templates: list[dict], tasks: list[dict],
              inventory_services: list[str] | None = None) -> dict:
    names = {t["id"]: t["name"] for t in templates}
    services: dict[str, dict] = {}
    validation: dict[str, dict] = {}
    for task in sorted(tasks, key=lambda t: t["id"]):
        lines = ANSI.sub("", task.get("output") or "").splitlines()
        results = results_in(lines)
        if not results and task["status"] == "error":
            step = _step_for(names.get(task["template_id"], ""), registry)
            if step and task.get("service"):
                results = [{
                    "service": task["service"], "step": step, "status": "fail",
                    "check_mode": False, "evidence": {},
                    "error": "\n".join(lines[-TAIL:]) or "task failed with no output",
                }]
        for result in results:
            service = result.get("service") or task.get("service")
            if not service or not result.get("step"):
                continue
            check = is_check_mode(task) or bool(result.get("check_mode"))
            (validation if check else services).setdefault(service, {})[result["step"]] = {
                "status": result.get("status"),
                "task_id": task["id"],
                "end": task.get("end"),
                "check_mode": check,
                "error": result.get("error") or None,
                "evidence": result.get("evidence", {}),
            }
    failed = {
        service: sorted(step for step, r in steps.items() if r["status"] == "fail")
        for service, steps in services.items()
    }
    status = {s: {step: r["status"] for step, r in steps.items()} for s, steps in services.items()}
    agg = {"services": services, "validation": validation, "failed_steps": failed,
           "status_by_service": status}
    agg["report"] = report(agg, registry, inventory_services or [])
    return agg


def report(agg: dict, registry: list[dict], inventory_services: list[str] = ()) -> dict:
    """The read-only failure report: per service, each failed step with the criteria it did
    not meet, its error context, whether an undo exists and the Semaphore task, plus every
    step whose review has not passed (spec scenario "Unreviewed step is visible").

    Every inventoried service gets a row, including one with no workflow history yet
    (`no_history: true`), so a service that has never run a step is visible rather than
    absent (review of PR #195)."""
    steps = {s["id"]: s for s in registry}
    unreviewed = [s["id"] for s in registry if not s.get("reviewed")]
    out = {}
    for service in sorted(set(agg["services"]) | set(inventory_services)):
        results = agg["services"].get(service, {})
        failed = [
            {"step": step, "criteria": steps.get(step, {}).get("criteria", []),
             "error": results[step]["error"], "task_id": results[step]["task_id"],
             "undo": steps.get(step, {}).get("undo") or "none"}
            for step in agg["failed_steps"].get(service, [])
        ]
        out[service] = {"failed": failed, "unreviewed": unreviewed, "no_history": not results}
    return out


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
    mode = data["mode"]
    by_group = group_services(data.get("groups", {}), data.get("host_services", {}))
    if mode == "select":
        out = {"template_ids": select(data["registry"], data["templates"], by_group)}
    elif mode == "pick":
        out = {"tasks": pick(data["histories"], by_group)}
    elif mode == "aggregate":
        tasks = [dict(r["item"], output=r.get("content") or "") for r in data["fetched"]]
        out = aggregate(data["registry"], data["templates"], tasks, sorted(set(by_group.values())))
        if data.get("now_ns"):
            out["loki_streams"] = loki_streams(out, int(data["now_ns"]))
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    json.dump(out, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
