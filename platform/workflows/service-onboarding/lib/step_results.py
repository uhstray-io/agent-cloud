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
                                                    -> {"tasks": [task rows + "service"],
                                                        "window_full": [template ids]}
             per (template, service): the newest finished REAL task, always, plus the newest
             check-mode task when it is newer - however many dry runs follow a real run,
             the real run survives. A history as long as HISTORY_WINDOW may have cut older
             runs off, so its template is listed in window_full; the collector keeps a
             service's last recorded status rather than erasing what it could not read
  aggregate  {registry, templates, fetched: [uri results of raw_output, item = picked row],
              groups?, host_services?, now_ns?, window_full?, retained?}
                                                    -> {services, validation, inputs,
                                                        failed_steps, status_by_service,
                                                        report, loki_streams (only with now_ns)}

Check mode never sets conformance (review of PR #195): a passing `--check` after a failed real
run must not clear the failure, because nothing on the service changed. The run mode comes
from ONE signal, the task row: Semaphore records `params.dry_run` whenever check mode is
chosen (UI or API; v2.17 db/Task.go). It is the only way check mode reaches a task here: no
template passes arguments or allows a per-task override (guarded by test_step_results.py), so
the row is known before any output is read (reviews of PR #229). Check-mode results land in
`validation`; `services` / `status_by_service` / `failed_steps` come only from real runs. A
step result whose own check_mode contradicts its row is listed under `anomalies` and kept out
of conformance, never trusted silently.

A snapshot template's result means its reasoning step's INPUT was captured, not that the
assessment passed: nothing has validated a proposal against the step's criteria yet. Routed by
the template's role in the registry, not by status, a snapshot's pass or skip lands in `inputs`,
never in conformance; a snapshot that fails is still the step's failure, since the
assessment then has no input (PR 195 Codex review).

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
# Semaphore's GET /project/{p}/templates/{t}/tasks returns the newest 1000 tasks by id
# (GetAllTasks, params.Count = 1000; db/sql/task.go orders "id desc"), in v2.17.0 and
# v2.19.11 alike. /tasks/last stops at 200.
HISTORY_WINDOW = 1000
RETAINED_ERROR = "last run is older than the collector's history window; status kept from NetBox"


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


def _step_for(template: str, registry: list[dict], deploy_templates: frozenset = frozenset()) -> str | None:
    """The registry step a template executes. "Deploy {service}" is the CLOSED list of
    application-service deploys OPA grants (data.json service_deploy_templates), never a
    "Deploy " prefix, which also matched the platform's foundation such as Deploy NetBox
    (PR 195 Codex review)."""
    base = VARIANT.sub("", template or "")
    for step in registry:
        if base in {step.get("executor"), step.get("snapshot")}:
            return step["id"]
        if step.get("executor") == PER_SERVICE and base in deploy_templates:
            return step["id"]
    return None


def incomplete_services(window_full: list, templates: list[dict], registry: list[dict], by_group: dict,
                        deploy_templates: frozenset = frozenset()) -> set:
    """The services a full history window can hide runs of: a per-service deploy template's own
    service, or every service for a template shared across them (PR 258 Codex review)."""
    by_id = {t["id"]: t for t in templates}
    steps = {s["id"]: s for s in registry}
    out: set = set()
    for tid in window_full:
        tpl = by_id.get(tid, {})
        step = _step_for(tpl.get("name", ""), registry, deploy_templates)
        if step and steps[step].get("executor") == PER_SERVICE:
            svc = _service_of({"tpl_playbook": tpl.get("playbook")}, by_group)
            out |= {svc} if svc else set()
        else:
            out |= set(by_group.values())
    return out


def select(registry: list[dict], templates: list[dict], by_group: dict,
           deploy_templates: frozenset = frozenset()) -> list[int]:
    def wanted(t: dict) -> bool:
        step = _step_for(t["name"], registry, deploy_templates)
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
    """Per (template, service): the newest finished real task, always, and the newest
    check-mode task when it is newer than that. The row says which is which (see module doc),
    so no number of dry runs can push the last real run out."""
    newest: dict[tuple, dict] = {}
    for history in histories:
        for task in history:
            if task.get("status") not in FINISHED:
                continue
            key = (task["template_id"], _service_of(task, by_group), is_check_mode(task))
            if key not in newest or task["id"] > newest[key]["id"]:
                newest[key] = task
    kept = {k: t for k, t in newest.items()
            if not k[2] or t["id"] > newest.get((k[0], k[1], False), {"id": -1})["id"]}
    return [dict(t, service=key[1]) for key, t in sorted(kept.items(), key=lambda kv: kv[1]["id"])]


def aggregate(registry: list[dict], templates: list[dict], tasks: list[dict],
              inventory_services: list[str] | None = None, deploy_templates: frozenset = frozenset(),
              window_full: list | tuple = (), retained: dict | None = None,
              incomplete: frozenset | set = frozenset()) -> dict:
    names = {t["id"]: t["name"] for t in templates}
    services: dict[str, dict] = {}
    validation: dict[str, dict] = {}
    inputs: dict[str, dict] = {}
    snapshot_of = {s["id"]: s.get("snapshot") for s in registry}
    anomalies: list[dict] = []
    for task in sorted(tasks, key=lambda t: t["id"]):
        template = names.get(task["template_id"], "")
        lines = ANSI.sub("", task.get("output") or "").splitlines()
        results = results_in(lines)
        if not results and task["status"] == "error":
            step = _step_for(template, registry, deploy_templates)
            if step and task.get("service"):
                results = [{
                    "service": task["service"], "step": step, "status": "fail",
                    # From the row, the one run-mode signal: a dry run that failed before
                    # recording a result is validation, not an anomaly (review of PR #229).
                    "check_mode": is_check_mode(task), "evidence": {},
                    "error": "\n".join(lines[-TAIL:]) or "task failed with no output",
                }]
        for result in results:
            service = result.get("service") or task.get("service")
            if not service or not result.get("step"):
                continue
            check = is_check_mode(task)
            if bool(result.get("check_mode")) != check:
                # The row is authoritative; a contradiction means check mode arrived by an
                # unsupported path. Keep it out of conformance and make it visible.
                anomalies.append({"task_id": task["id"], "service": service, "step": result["step"],
                                  "row_check_mode": check, "result_check_mode": bool(result.get("check_mode"))})
                check = True
            bucket = validation if check else services
            if result.get("status") != "fail" and VARIANT.sub("", template) == snapshot_of.get(result["step"]):
                bucket = inputs
            bucket.setdefault(service, {})[result["step"]] = {
                "status": result.get("status"),
                "task_id": task["id"],
                "end": task.get("end"),
                "check_mode": check,
                "error": result.get("error") or None,
                "evidence": result.get("evidence", {}),
            }
    # What NetBox already holds, merged UNDER this run's results: a step whose last run is
    # older than the history window keeps its status here, so NetBox, the report and Loki all
    # agree (PR 258 Codex review). Only real-run statuses are ever written, so they are real.
    # A step whose snapshot succeeded in this run drops what was retained: the failure that
    # snapshot recorded before is superseded, though the success is still no assessment
    # (PR 258 Codex review).
    for service, steps in (retained or {}).items():
        for step, state in (steps or {}).items():
            if step in services.get(service, {}) or step in inputs.get(service, {}):
                continue
            if state in ("pass", "fail", "skip"):
                services.setdefault(service, {})[step] = {
                    "status": state, "task_id": None, "end": None, "check_mode": False,
                    "error": RETAINED_ERROR if state == "fail" else None, "evidence": {}, "retained": True,
                }
    # Every inventoried service is tracked, run or not, so the report, the NetBox fields and
    # the dashboard all show a service before its first step (review of PR #229).
    tracked = sorted(set(services) | set(inventory_services or []))
    failed = {
        service: sorted(step for step, r in services.get(service, {}).items() if r["status"] == "fail")
        for service in tracked
    }
    status = {s: {step: r["status"] for step, r in services.get(s, {}).items()} for s in tracked}
    agg = {"services": services, "tracked": tracked, "validation": validation, "inputs": inputs,
           "anomalies": anomalies, "history_window_full": sorted(window_full),
           "history_incomplete": sorted(set(incomplete) & set(tracked)),
           "failed_steps": failed, "status_by_service": status}
    agg["report"] = report(agg, registry, inventory_services or [])
    return agg


def report(agg: dict, registry: list[dict], inventory_services: list[str] = ()) -> dict:
    """The read-only failure report: per service, each failed step with the criteria it did
    not meet, its error context, whether an undo exists and the Semaphore task, plus every
    step whose review has not passed (spec scenario "Unreviewed step is visible").

    Every inventoried service gets a row, including one with no workflow history yet
    (`no_history: true`), so a service that has never run a step is visible rather than
    absent (review of PR #195). A service a full history window can affect is marked
    `history_incomplete` and never claims `no_history`: its older runs were not read, and
    what NetBox held for them is carried in as `retained` (PR 195 and PR 258 Codex reviews)."""
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
        incomplete = service in agg.get("history_incomplete", [])
        out[service] = {"failed": failed, "unreviewed": unreviewed,
                        "no_history": not results and not incomplete, "history_incomplete": incomplete}
    return out


def loki_streams(agg: dict, now_ns: int) -> list[dict]:
    """One Loki stream per (service, step), labelled so the dashboard can filter on them, and
    one `no_history` stream for a tracked service that has not run a step yet. A service a full
    history window can affect gets a `history_incomplete` stream instead: its missing steps may
    be older than the window, not absent. A retained status streams like any other, flagged."""
    incomplete = set(agg.get("history_incomplete", []))
    streams = []
    for service in agg.get("tracked", []):
        marker = "history_incomplete" if service in incomplete else (
            "no_history" if service not in agg["services"] else None)
        if marker:
            streams.append({"stream": {"job": "agent-cloud-conformance", "service": service, "step": "none",
                                       "status": marker},
                            "values": [[str(now_ns), json.dumps({marker: True})]]})
    for service, steps in sorted(agg["services"].items()):
        for step, result in sorted(steps.items()):
            payload = {"task_id": result["task_id"], "error": result["error"], "check_mode": result["check_mode"]}
            if result.get("retained"):
                payload["retained"] = True
            line = json.dumps(payload, sort_keys=True)
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
    deploys = frozenset(data.get("deploy_templates", []))
    if mode == "select":
        out = {"template_ids": select(data["registry"], data["templates"], by_group, deploys)}
    elif mode == "pick":
        out = {"tasks": pick(data["histories"], by_group),
               "window_full": sorted({h[0]["template_id"] for h in data["histories"]
                                      if len(h) >= HISTORY_WINDOW})}
    elif mode == "aggregate":
        tasks = [dict(r["item"], output=r.get("content") or "") for r in data["fetched"]]
        full = data.get("window_full", [])
        out = aggregate(data["registry"], data["templates"], tasks, sorted(set(by_group.values())), deploys,
                        full, data.get("retained") or {},
                        incomplete_services(full, data["templates"], data["registry"], by_group, deploys))
        if data.get("now_ns"):
            out["loki_streams"] = loki_streams(out, int(data["now_ns"]))
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    json.dump(out, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
