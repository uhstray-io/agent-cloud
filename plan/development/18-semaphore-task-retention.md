# 18 — Semaphore task-history retention and orchestrator disk headroom

**Date:** 2026-10-27
**Status:** PROPOSED
**Author:** Joseph A. Wisneski IV
**Context:** On 2026-10-27 a production task failed while cloning the repository on the
Semaphore host: `fatal: write error: No space left on device` (task 9541). The scheduled jobs
five minutes later ran normally, so the disk was full at the peak, not permanently. The same VM
filled once before (`docs/MISTAKES.md` 1.18). That time Postgres failed WAL recovery and every
API call answered 401 until the root volume was grown. The immediate fix is to grow the disk:
site-config declares 64G for the VM, Resize VM grows the Proxmox disk, and Grow Semaphore Root
(Dev) grows the guest partition, physical volume, root logical volume and ext4 online. This plan is
the follow-up. It keeps task history to 30 days so the disk stops growing without bound, and it
adds the disk alert that 1.18 proposed and nobody built.

> **Depends on:** 01 (automation model: config as code, Semaphore as the executor), 05
> (observability), 15 (the conformance collector reads Semaphore task history).
> **Retention period:** 30 days, set by Joe on 2026-10-27.

---

## Problem

1. **Task history is kept forever.** Semaphore stores every task and every output line in its
   Postgres. Line rows go in `task__output`, keyed by task id (`db/sql/task.go:243-258` at
   v2.19.11). Nothing deletes them. Production task history from 2026-10-26/27 shows the schedules alone
   starting about 288 tasks a day: liveness every 10 minutes (144), conformance every 15 (96),
   and the tududi token refresh twice an hour (48).
2. **Semaphore has no age-based retention.** Its only built-in control is
   `max_tasks_per_template` (`SEMAPHORE_MAX_TASKS_PER_TEMPLATE`, `util/config.go:567`). It keeps
   the newest N tasks of each template when a new task is created (`db/sql/task.go:170-190`).
   That is a count, not an age: one N cannot mean 30 days for a template that runs every 10
   minutes and for one that runs once a month. It is also a server setting, so changing it
   means changing and restarting the Semaphore container. That is operator-side work, never a
   Semaphore job.
3. **The disk is not measured.** Production Prometheus has node-exporter targets for three hosts.
   The Semaphore host is not one of them, so there is no usage history and no alert. Both
   fills were found by a failing task.
4. **What actually fills the disk is unmeasured.** Task output is the obvious candidate. Two
   others exist:
   - **Per-template clones.** Each template keeps its own clone of the repository (task 9541
     cloned into `repository_5_template_353`), and production has 251 templates.
   - **Per-template collection installs.** Each template has its own Ansible home with the
     collections installed into it (`repository_5_template_354_home/.ansible/collections`,
     task 9533).

## Decisions

| # | Decision | Rejected | Why |
|---|---|---|---|
| D1 | Task history (each task and its output) is kept for **30 days** after it ends | Keeping it forever. A per-template count | Joe's call. Age is what an operator reasons about ("last month's runs"). A count means a different age for every template |
| D2 | Prune through Semaphore's own API: `DELETE /api/project/{p}/tasks/{id}`, which runs `DeleteTaskWithOutputs` (`api/projects/tasks.go:418-450`, `db/sql/task.go:384-398`) | SQL straight against the database. `max_tasks_per_template` | The API is the supported path. It deletes the output rows and the task row in that order, and it refuses a queued or running task (400). SQL would bypass both and couple us to the schema. The count setting is ruled out by Problem 2 |
| D3 | The prune is a **scheduled Semaphore template**, run daily and declared with `schedule:` in `templates.yml`, like the tududi token refresh | A cron job on the host. An operator-run script | It goes through the executor like every other job, is reviewed, and leaves a task record. Deleting history is a data operation through the API. It does not restart or reconfigure Semaphore, so the "never reconfigure Semaphore from its own job" rule does not forbid it |
| D4 | It authenticates with the controller's admin token from `secret/services/semaphore` against the loopback API `http://127.0.0.1:3000`, the same source and URL as `provision-console-semaphore-access.yml:8,64-65` | A new service account | Deleting a task requires a Semaphore **system admin**. Anyone else gets 401 "is not permitted to delete task logs" (`api/projects/tasks.go:437-441`). A new admin account would add a second admin credential and buy nothing |
| D5 | Check mode reports what would go: the count of tasks older than the cutoff, per template, and the oldest end time. A real run deletes at most `semaphore_prune_max_per_run` tasks, defaulting to 2000, oldest first | Deleting the whole backlog in one run | The first run faces months of history. A cap bounds the I/O and the length of the run. The daily schedule drains the backlog over a few days and then keeps up |
| D6 | Disk space is given back to the filesystem by a **one-time `VACUUM FULL`** of `task__output` (and `task`) after the backlog is drained, run operator-side in a maintenance window | `VACUUM FULL` from a Semaphore job. Nothing | Deleted rows are reused by later inserts once autovacuum processes them. The files only shrink under `VACUUM FULL`, which takes an exclusive lock on the table. A Semaphore job would hold that lock against the output writes of every running task, its own included |
| D7 | Clones and collection homes are pruned **only if Phase 0 shows they matter** | Pruning them now | Semaphore reuses a template's clone on its next run. Deleting one under a running task breaks that task. Measure before adding a moving part |
| D8 | The Semaphore host gets a node-exporter target and a Grafana alert at 80% root usage | No alert | This is the guard 1.18 proposed. A filling disk then pages before a task or Postgres fails |

## Phases

### Phase 0 — Measure (read-only)

A new read-only template, `Survey Semaphore Storage (Dev)`, runs against `semaphore_svc`. It
reports sizes and counts only, never any content:

- Root filesystem size, used and free.
- The live container layout. Production runs the legacy standalone project from the
  directory in its inventory `compose_working_dir`, not the monorepo path. The monorepo compose file names the
  volumes `semaphore_db_data`, `semaphore_tmp`, `semaphore_data` and `semaphore_config`, but the
  live names must be read, not assumed.
- Bytes in each of:
  - the Postgres data directory,
  - the repository clones under the tmp volume,
  - the per-template `_home` directories,
  - container images.
- From Postgres, read-only:
  - `pg_total_relation_size` for `task__output`, `task` and `event`,
  - the count of tasks older than 30 days and their output rows,
  - `SHOW autovacuum`.

**Gate:** the numbers are recorded in this plan, and Phases 1–3 are re-ranked by bytes.

### Phase 1 — Prune task history to 30 days

`prune-semaphore-task-history.yml` and the template `Prune Semaphore Task History`, scheduled
daily:

1. Read the admin token (`no_log`, credential task only).
2. List the project's tasks, page by page, until the end time passes the cutoff.
3. Select tasks whose `end` is older than now − `semaphore_task_retention_days` (default 30) and
   whose status is terminal (`success`, `error`, `stopped`).
4. Under check mode, report and stop.
5. Delete the selection oldest first, up to the per-run cap. A 400 for a task that became active
   is counted and skipped, never retried in the same run. Any other status fails the run.
6. Report deleted and skipped counts and the oldest remaining end time. Record a step result.

Tests:

- Selection is pure: a fixed clock and a task list give exactly the expected ids.
- Non-terminal tasks are never selected.
- The cap holds.
- A 401 fails the run.
- No visible task loops over a registered result from a request that carried the token
  (`platform/tests/test_no_request_in_loop_items.py`).

### Phase 2 — Give the space back

After Phase 1 has drained the backlog, check how much the database files shrink if reclaimed.
If that is worth a short write outage, run `VACUUM FULL task__output, task` operator-side,
in a window with no schedules due, and record the before and after sizes here.

### Phase 3 — Clones and collection homes (conditional)

Only if Phase 0 ranks them first. The candidate is a scheduled prune of the clone and `_home` of
templates that have not run in 30 days, skipping any template with an active task. Design it
then, against the measured layout.

### Phase 4 — Alert

Add the Semaphore host as a node-exporter target in the o11y stack (inventory plus the existing
scrape configuration path), and add an alert rule at 80% root usage that routes to the existing
Discord contact point.

## Open questions

- **O1. Task numbers cited as evidence.** 1,162 citations of a task number appear across 110
  committed files (`git grep -E '[Tt]asks? #?[0-9]{3,5}'` over docs, plan, platform and
  AGENTS.md, 2026-10-27). Once retention runs, each citation older than 30 days points at a task
  that no longer exists.
  - **Recommended: accept it.** The committed text already states the finding, and the task
    number is a pointer, not the evidence.
  - **Alternative:** before deleting, export each task's output to a compressed archive in
    durable storage. That is a new storage dependency, so it needs its own decision.
- **O2.** Should the cutoff be shorter for the high-frequency schedules (liveness, conformance)?
  Their value decays in hours, not weeks. Decide with Phase 0's numbers.

## Acceptance

- `Prune Semaphore Task History` runs daily and green. After the backlog drains, no terminal task
  older than 30 days remains.
- The root filesystem usage figure is recorded before and after Phases 1–2.
- The disk alert fires in a test against a lowered threshold, then is reset to 80%.

## Changelog

| Date | Change |
|---|---|
| 2026-10-27 | Proposed after task 9541 hit a full disk. The disk is grown to 64G separately (site-config, Resize VM, Grow Semaphore Root). 30-day retention set by Joe |
