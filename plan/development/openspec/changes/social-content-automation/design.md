## Context

Plan 28 (`plan/development/28-social-content-automation.md`) holds the pipeline, the decision table D1 to D13, the risks R1 to R7 and the implementation phases. This document maps each decision to the requirement that tests it; the requirements are in `specs/platform/social-content-automation/spec.md`. It does not restate the plan.

Every upstream fact the design relies on was read on upstream main on 2026-11-16 and is re-verified on the pinned version before the task that depends on it closes (plan 28, "Upstream facts used").

## Goals / Non-Goals

Goals: the requirements in the spec, in the order of plan 28's phases.

Non-Goals:

- Images before plan 27's image path (AC-792). The image-model decision (AC-870) was taken on 2026-11-16: plan 27 records the switch and the dgx-spark fit test it waits on.
- The studio (AC-810): its decision is open and outside the v0.17.0 scope.
- Changing the publishing service's own capability (change `deploy-postiz-social-publishing`).
- Deploying Activepieces (change `deploy-activepieces`).
- Any brand value in this repository.

## Decisions

| Plan 28 | Requirement |
|---|---|
| D1 Publishing stays with the publishing service | "Flows never publish immediately" |
| D2 Approval rule and revision identity (AC-795) | "Drafts need no approval; scheduling and publishing need a recorded approval" |
| D3, D4 Flows, connections and credentials as code | "Flows and connections are declared as code and applied by playbook" |
| D5 Gateway through the HTTP piece | "Generation reaches the model only through the inference gateway under its own identity" |
| D7 Exactly once, record written before the create | "Handoff to the publishing service is exactly once" |
| D8 Brand profiles | "Brand profiles follow the schema and hold no brand value in the repository" |
| D9 Brand isolation | "Each draft targets exactly one mapped account of its brand" |
| D10 Guardrail as a tested repository module | "Guardrail checks run before approval" |
| D11 Failure alerting | "A failed content flow run raises an alert" |
| D13 Kill switch | "A kill switch stops content flows" |


Trade-off accepted: the approval rule and brand isolation are enforced in flow logic and, for the approval step, a static check, not by the publishing service, because the publishing service's API key does not scope what a caller may do (R5). Plan 28 Open Question 6 records the alternative.

## Risks / Trade-offs

R1 to R7 with their mitigations are in plan 28, "Risks". R1 (Community-edition API access) comes first: if its spike in task 3.1 fails, work stops and the decision returns to the operator.

## Open Questions

Plan 28, "Open Questions".

## Migration Plan

Nothing is migrated. The phases in `tasks.md` run in order; rollback is in `proposal.md`. Production use of Activepieces waits for change `deploy-activepieces`'s validation gate (AC-781).
