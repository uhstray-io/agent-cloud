# plan/architecture

Current-state architecture and the decisions that produced it, as **numbered
documents (00–09)** read in order (`00-foundation-standards.md` is the index):

- [00 — Architecture Foundation & Doc Standards](00-foundation-standards.md)
- [01 — Automation Model](01-automation-model.md)
- [02 — Service Onboarding](02-service-onboarding.md)
- [03 — Testing & CI Quality](03-testing-ci-quality.md)
- [04 — Credentials & Access](04-credentials-access.md)
- [05 — Platform Infrastructure](05-platform-infra.md)
- [06 — Observability Instrumentation](06-observability-instrumentation.md)
- [07 — Website-Building Agent](07-website-building-agent.md)
- [08 — Ansible Automation Standards](08-ansible-automation-standards.md)
- [09 — Console UI Principles](09-console-ui-principles.md)
- [Skills Recommendation](skills-recommendation.md)

This is the repo's own decision-record convention and it is authoritative here.
It is deliberately *not* the `DECISIONS.md` + `YYYY/YYYY-MM.md` layout used by
some other Uhstray repos: these numbered documents are already referenced from
`AGENTS.md`, `README.md`, and throughout `plan/development/`, and adding a
second structure beside them would create two competing homes for one
architecture decision.

Scope split:

- **Here** — how the platform is built and why: standards, the automation
  model, onboarding, testing/CI, credentials, infrastructure, observability.
- **`plan/development/`** — what we are building next (implementation plans),
  plus the OpenSpec store.
- **`ARCHITECTURE.md`** (repo root) — the short current-state summary that
  points into this directory.

Deliberation that did *not* make it into a ratified document — what was feared,
tried first, or abandoned, and why the rejected option lost — belongs in the
Hindsight bank, not in a file here. See `AGENTS.md` → "Memory & specs".
