# O11y alert review, 2026-10-21 to 2026-11-04

**Date:** 2026-11-04  
**Status:** OPEN — review  
**Author:** Joseph A. Wisneski IV  
**Context:** Review of the production o11y alert firings from 2026-10-21 to 2026-11-04, to plan follow-up work. It separates real outages from alert noise, records what drove the noise, and lists the work that follows from it.

## 1. Scope and sources

The review window is 2026-10-21 through 2026-11-04. All times are UTC. The review reads four sources:

- Production Grafana alert state history, read from the annotations backend: 3,159 state transitions between 2026-10-24 and 2026-11-04.
- Grafana's own metrics in Prometheus, including rule evaluation duration and datasource request results.
- Production Semaphore task history: 3,176 tasks since 2026-10-21, read per template and read-only.
- Declared schedules in `platform/semaphore/templates.yml`.

The Discord alert channel itself was not cross-checked. The review's bot is not allowlisted for that channel, and a desktop read of the channel was blocked. Grafana's history covers every alert that Grafana routes to the channel. Messages from other senders, including the liveness watcher, were not reviewed.

## 2. Summary

| Category | Notifications | Notes |
|---|---|---|
| Real: vLLM outages on the DGX head | 12 | Three windows, four rules each (section 5, C1) |
| Real: scheduled job silent | 1 | Daily certificate renewal did not run on 2026-10-28 (section 5, C2) |
| Drill | 2 | Planned alert-delivery drill on 2026-10-31 (section 3) |
| False noise: query errors | 64 | "Alerting (Error)": Grafana datasource errors during evaluation stalls (section 4) |
| **Total firing notifications** | **79** | 15 "Alerting" plus 64 "Alerting (Error)" |

The 15 "Alerting" notifications are 12 from the vLLM outages, 1 from the silent job and 2 from the drill. The real total is 13, the drill total is 2 and the false-noise total is 64, which together make the 79 firings. The 64 "Alerting (Error)" notifications are not real conditions. Grafana reported that a rule's query failed, not that the monitored condition was true.

## 3. A. Alerts correlated with a scheduled or operator change

| Window (UTC) | Alerts | Change | Verdict |
|---|---|---|---|
| 2026-10-31 21:43 | "Service telemetry scrape failed" for two loopback test targets | Semaphore task 11062, "Drill o11y Active Alert Delivery (Dev)", ran 21:39–21:44, after Deploy o11y (Dev) task 11054 (21:20–21:28) | Planned drill, expected. In the channel these alerts looked identical to real ones. |
| 2026-10-28 16:29 | "Scheduled job silent" for job `renew-internal-certs` | The daily Renew Internal Certs (Dev) run, scheduled `17 4 * * *`, did not run on 2026-10-28. Task history goes from task 9536 (2026-10-27 04:17) to task 10040 (2026-10-29 04:17). Production Semaphore was in its root-disk-full outage that day. Recovery tasks ran 15:19–16:38, including Grow Semaphore Root (Dev) task 9846 at 15:23. | The alert was correct. The cause is a real outage (section 5, C2). |

Some error bursts overlap change work. The overlap is recorded here. Causation is not established (section 7).

- 2026-10-29 22:40–23:00 overlaps Provision VM tasks 10296 and 10297 (22:45–23:06). Both clone a template on Proxmox, which could load shared storage.
- 2026-10-28 18:10–19:10 overlaps Apply Firewall tasks 9903 and 9904, Ensure and Verify Service Persistence, Back Up Credentials to site-config, and Deploy console tasks 9927 and 9928.
- 2026-10-30 07:00–07:40 overlaps Read Proxmox Root CA task 10449, Provision Console Proxmox Token, and Deploy console tasks 10456 and 10457.
- 2026-11-04 18:30–20:20 overlaps Deploy Plane tasks 12278 and 12279 (18:53–19:04).

A daily burst appears at about 07:00 UTC on 2026-10-28, 10-29, 10-30 and 10-31, then at 08:00 UTC on 2026-11-02. The one-hour move is consistent with a fixed 03:00 schedule in US Eastern time, which left daylight saving time on 2026-11-01 (EDT on 2026-10-31, EST on 2026-11-02). The Proxmox host's time zone was not read. No Semaphore task falls in these windows. The likely source is the Proxmox backup job for the o11y VM. The private inventory declares that job, and its schedule lives only on Proxmox. This is unverified (section 7).

## 4. B. Flapping and false noise

Of the 79 firing notifications, 64 were "Alerting (Error)". Grafana uses that status when a rule's query fails, a DatasourceError. The notifications arrive in bursts where every rule fails together:

- 2026-10-29 at 14:18 and 17:38.
- 2026-10-30 from 13:34 to 15:29, when all 11 scrape targets failed at once, twice.
- 2026-11-03 from 15:58 to 17:05, when all 10 missing-telemetry groups failed.
- 2026-11-04 at 13:49, when all 11 targets failed, and at 19:18.

The window holds 1,154 "Pending (Error)" transitions. The noisiest rules are:

| Rule | Pending (Error) transitions | Status |
|---|---|---|
| Service metrics endpoint down | 748 | Retired. Not in the current alert rule template. |
| Service telemetry scrape failed | 556 | Live |
| Internal CA leaf certificate expires within seven days | 166 | Live |
| DGX Spark scrape target down | 156 | Live |

Grafana's rule evaluation p99 is about one second normally. In every burst it rises to 25–31 seconds (`grafana_alerting_rule_evaluation_duration_seconds`), close to Grafana's 30-second evaluation timeout, as Grafana's documentation on no-data and error states describes. Every live rule sets `execErrState: Alerting`, so an evaluation that errors fires the rule. The tombstone rules kept for retired rules use `OK` and cannot error, because they evaluate a constant. Most rules also use a two-minute pending period (`for: 2m`), so a stall that lasts past about two minutes fires every affected rule. A few rules use five or fifteen minutes instead (`platform/services/o11y/deployment/templates/alerts.yml.j2`). Because every rule stalls at once, each burst pages the whole rule set together.

Why the queries stall:

- The receiver host, which has 4 vCPUs, showed I/O wait rising from about 0.16 cores to about 0.73 cores during bursts. User and system CPU fell in the same windows, and about 84% of memory stayed available. The stall is storage wait, not compute.
- Grafana's datasource requests and Prometheus's HTTP handlers show no errors. Their requests return 200.
- The o11y stack had one full restart in 14 days, on 2026-10-31 at 19:00. Restarts do not explain the bursts.
- The receiver's node exporter collects no disk or pressure metrics, so the storage side cannot be observed today.
- Most bursts have no Semaphore task: 2026-11-03, most of 2026-11-04, the morning of 2026-10-29 and the daily 03:00-local burst. The contention source appears to lie outside Semaphore, for example in shared Proxmox storage or backups. This is unverified (section 7).

**Fix proposed in PR #873 (open, not yet merged or deployed).** The PR is open against `dev`. It makes two changes. First, it sets `execErrState: KeepLast` on every live rule, so a query error keeps the last known state instead of firing. Second, it adds one meta rule, "Alert rule evaluations failing", with severity `warning`, `for: 15m` and no-data state `OK`. A long stall then pages once, not once per rule per burst. The change takes effect only after the PR merges and Deploy o11y (Dev) runs. Grafana 11.4.0, the pinned version, accepts `KeepLast` for the error state (Grafana source at tag v11.4.0, `pkg/services/ngalert/models/alert_rule.go:105`). Grafana's documentation on no-data and error states recommends Keep Last State against temporary data source issues, paired with a separate rule for prolonged disruption, which is the role of the meta rule. The external liveness watcher, Check o11y Liveness (Dev), runs every ten minutes and still covers Grafana and Prometheus being fully down. It reports through Discord, not through Grafana.

## 5. C. Real outages to address

**C1. The vLLM endpoint on the DGX head went down three times.** The Alerting windows, in UTC, were 2026-10-24 05:04–05:15, 2026-10-26 20:15–20:30 and 2026-10-26 21:14–21:25. The pending period began about five minutes before each first Alerting state. Each window fired the same four rules together: Service metrics endpoint down on the DGX head vLLM endpoint (a rule since retired), vLLM metric families absent, DGX Spark scrape target down, and Synthetic inference probe failing. These are the rule titles in the production Grafana rule list; the template builds the inference titles through a macro.

No agent-cloud Semaphore task touched the DGX in these windows. The agentgateway redeploys (tasks 9411–9413, 2026-10-26 20:53–21:01) do not explain a vLLM metrics outage. The cause is unverified. Check the DGX-side records for vLLM restarts. The alerts worked as designed. Each fired about five minutes after onset and resolved on its own.

**C2. The production Semaphore root disk filled on 2026-10-27 and 2026-10-28.** The orchestrator missed the daily certificate renewal on 2026-10-28, and the Scheduled job silent alert caught it (section 3). Grow Semaphore Root (Dev) task 9846 grew the root at 15:23 on 2026-10-28.

An alert on the orchestrator host's disk is not built yet. Plan 18, `plan/development/18-semaphore-task-retention.md`, has status PROPOSED (line 4). Its decision D8 (line 89) proposes a node-exporter target on the Semaphore host and a Grafana alert at 80% root usage. No Semaphore-host rule exists in the o11y alert template.

**C3. Receiver storage stalls are a capacity or placement problem.** Fixing the noise does not fix them. While evaluations stall, rules cannot update their state, so a real condition that starts during a stall stays invisible until the stall ends. PR #873 reduces the noise but leaves the stall in place. The existing disk rule, "Receiver host root filesystem below 10%" (`alerts.yml.j2`, line 143), covers free space on the receiver's root filesystem. It does not cover I/O wait.

## 6. Plan items

1. Merge PR #873, run Deploy o11y (Dev), and watch the new meta rule. The fix takes effect only after both steps, and the meta rule is the signal that a stall is happening.
2. Make storage visible by enabling disk and pressure collectors on the receiver's node exporter. The storage side is not observable today. This needs a decision, because the bounded collector set is deliberate.
3. Read the Proxmox task log (backup and clone tasks) for the burst windows. Consider moving the o11y VM disk off contended storage or moving the backup window. The bursts have no Semaphore task, so Proxmox is the next source to check.
4. Put change markers in Grafana. Each Semaphore deploy or provision task writes a start and an end annotation. The next review then becomes a dashboard query instead of a join of logs.
5. Make drills announce themselves. Alert-delivery drills carry a drill label or title prefix, so the channel can tell a drill from a real alert.
6. Mark planned DGX and vLLM restarts. The DGX side records restarts, or silences vLLM alerts during planned work and marks them expected. The three vLLM windows cannot yet be told apart from unplanned outages.
7. Build the orchestrator root-disk alert from plan 18, decision D8. The root filled in October, and the only signal was a missed job.
8. Allowlist the alerts channel for the Discord bot. Future reviews can then include messages from senders other than Grafana.

## 7. Unverified, and how to verify

| Claim | How to verify |
|---|---|
| The daily burst (07:00 UTC, then 08:00 UTC from 2026-11-02) is the Proxmox backup job for the o11y VM | Read the Proxmox task log for the backup job. The job id is declared in the private inventory and was not read in this review. |
| The burst source is shared storage contention | Receiver disk collectors (none today), or Proxmox storage metrics. |
| The error bursts were caused by the change tasks in section 3 | Overlap only. Read the Proxmox task log for the same windows. |
| The cause of the vLLM windows on 2026-10-24 and 2026-10-26 | DGX-side records of vLLM restarts. |
| The contents of the Discord alert channel, including messages from senders other than Grafana | Allowlist the review bot for the channel, then read it. |
