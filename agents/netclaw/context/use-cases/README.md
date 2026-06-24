# NetClaw use-cases

The canonical definitions of NetClaw's AI workloads (use-cases) are maintained in
**skynet's catalog** — `use-case-catalog.yaml` in the `uhstray-io/skynet` repo
(private) — the single source of truth for AI workloads across the platform.

This directory previously held only a `.gitkeep`. The workloads were inferred from
this agent's OPA `allowed_actions`
(`platform/services/opa/deployment/policies/agentcloud/data.json`, role `netclaw`)
and its deployment README, then defined as skynet catalog entries (e.g. the seed
`netclaw-network-reasoning`).

> Scope: this pointer records **where the use-cases live**. NetClaw's *runtime
> model* is reframed separately in `plan/development/SKYNET-REPLACEMENT-PLAN.md`
> (Part 2), which is gated on the netclaw↔Semaphore decision and not yet applied.

See `plan/development/SKYNET-REPLACEMENT-PLAN.md` (Part 3 — use-case harvest).
