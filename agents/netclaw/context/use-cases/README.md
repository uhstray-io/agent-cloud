# NetClaw use-cases

**NetClaw remains a placeholder/scaffold, not a completed operational agent.** Its
runtime integration and deployment are not established by this directory. The
canonical definitions of NetClaw's proposed AI workloads (use-cases) are maintained in
**skynet's catalog** — `use-case-catalog.yaml` in the `uhstray-io/skynet` repo
(private) — the single source of truth for AI workloads across the platform.

This directory previously held only a `.gitkeep`. The workload proposals were inferred from
this agent's OPA `allowed_actions`
(`platform/services/opa/deployment/policies/agentcloud/data.json`, role `netclaw`)
and its deployment README, then recorded as skynet catalog entries (e.g. the seed
`netclaw-network-reasoning`). Those catalog entries do not mean the NetClaw runtime is implemented.

> Scope: this pointer records **where the use-cases live**. NetClaw's *runtime
> model* is reframed separately in `plan/development/06-inference-skynet.md`
> (Part 2), which is gated on the netclaw↔Semaphore decision and not yet applied.

See `plan/development/06-inference-skynet.md` (Part 3 — use-case harvest).
