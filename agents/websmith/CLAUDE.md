# CLAUDE.md — agents/websmith

This file provides guidance to Claude Code (and other LLM agents) when working inside `agents/websmith/`.

## What WebSmith is

WebSmith is a **prompt-only agent**: a structured, opinionated workflow (5 mandatory phases + 1 optional intake) that an LLM follows with a human user to design a website end-to-end and produce a signed `SPEC.md`. The framework lives entirely as markdown — no executables, no daemons, no containers.

Inside agent-cloud, WebSmith plays the role of the **AI layer** for website creation. The signed spec it produces is the input to the **automation layer** (composable Ansible playbooks that deploy the resulting service).

## Critical rules when running a WebSmith session

These override any default LLM behavior and apply for the duration of a WebSmith session:

1. **Read [`context/AGENTS.md`](./context/AGENTS.md) before doing anything else.** It is the agent's operating manual. Every rule below has a fuller treatment there.
2. **Phases run in order.** Do not skip. Do not interleave. Each phase has an exit gate that requires explicit user signoff.
3. **Ask, don't assume — and batch questions per phase.** Within a phase, gather all clarifying questions and present them once. No drip-feeding.
4. **Catalogs are non-exhaustive.** `context/catalogs/` enumerates common choices, not all possible ones. If the user wants something not in a catalog, build it — do not substitute the nearest catalog entry.
5. **No code during phases 0–5.** Phases are decision-only. No scaffolding, no `npm install`, no `go mod init`, no build tools. Only deciding.
6. **The unified `SPEC.md` is the contract.** After Phase 5, assemble the five artifacts into one signed document. Implementation begins only after dated signoff.
7. **At the end of every phase, ask the catch-all:** *"Is there anything I haven't asked about that you think matters for this site?"* This is non-optional.

## Where the spec goes (agent-cloud-specific deviation)

The framework's own KICKSTART.md tells users to build their site in a **separate working directory** outside the framework repo. Inside agent-cloud, that's overridden:

- **Spec artifacts** (`intake.md`, `purpose.md`, `template.md`, `tooling.md`, `style.md`, `considerations.md`, `SPEC.md`) are written to `platform/services/<sitename>/context/spec/` — colocated with the service they describe.
- **Implementation** lands in `platform/services/<sitename>/deployment/`, following the composable pattern documented in `plan/architecture/AUTOMATION-COMPOSABILITY.md`.
- **Do not** write spec files into `agents/websmith/`. WebSmith holds the workflow; concrete sites live under `platform/services/`.

[`agents/websmith/context/architecture/integration-with-agent-cloud.md`](./context/architecture/integration-with-agent-cloud.md) is the authoritative reference for this override. (The file is currently a skeleton; Phase 11 will fill it in with the full second-site recipe.)

## The agent-cloud preset (Phase 3 — Tooling)

When a WebSmith session reaches Phase 3 (Tooling), surface these defaults derived from agent-cloud conventions. The user can override, but they must be offered:

| Concern | agent-cloud default | Source |
|---------|--------------------|--------|
| Database | PostgreSQL | Used by NetBox, NocoDB, UhhCraft; no Postgres-incompatible service in production |
| Container runtime | Podman | Per root `CLAUDE.md` — NetBox is the only Docker exception |
| Reverse proxy | Central Caddy with CloudFlare DNS-01 | `plan/architecture/CADDY-REVERSE-PROXY.md` |
| Secrets management | OpenBao + Ansible templating | Root `CLAUDE.md` "Secrets Management" |
| CI/CD | Unified `lint-and-test.yml` with path filters | `.github/workflows/lint-and-test.yml` |
| Deployment orchestration | Semaphore running composable playbooks | `platform/playbooks/README.md` |
| Hosting | Dedicated Proxmox VM per service | `platform/hypervisor/proxmox/` |
| SSH | Per-service ed25519 key from OpenBao | `distribute-ssh-keys.yml` + `harden-ssh.yml` |

Any deviation a user requests from this preset must be captured in the site's `SPEC.md` under a `## Deviations from agent-cloud preset` section.

## Operating principles inside this directory

- **Never modify framework content for a single user session.** The `context/phases/`, `context/catalogs/`, and `context/schemas/` files are shared infrastructure. Per-user decisions go in the site's `platform/services/<sitename>/context/spec/`, not here.
- **Updates to the framework itself** (new catalog entries, refined phase prompts, schema fixes) are normal PRs to `agents/websmith/context/`. Treat them like any other shared-library change: they affect every future WebSmith session.
- **`context/use-cases/`** is where worked examples accumulate. When a new site ships, add a short walkthrough referencing its SPEC.

## Related documentation

- Root [`CLAUDE.md`](../../CLAUDE.md) — repo-wide conventions and the four-layer model.
- [`plan/development/WEBSMITH-INTEGRATION-PLAN.md`](../../plan/development/WEBSMITH-INTEGRATION-PLAN.md) — full integration plan.
- [`plan/architecture/AUTOMATION-COMPOSABILITY.md`](../../plan/architecture/AUTOMATION-COMPOSABILITY.md) — how the implementation half works once the spec is signed.
- [`platform/services/uhhcraft/`](../../platform/services/uhhcraft/) — first concrete site built with WebSmith (added in Phase 2).
