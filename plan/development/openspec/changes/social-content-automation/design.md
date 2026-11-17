## Context

See `proposal.md` — Why. Plan 28 (`plan/development/28-social-content-automation.md`) holds the full pipeline, the decision table D1 to D12, the risks R1 to R7 with their mitigations, and the implementation phases. This document records the decisions the spec depends on and the items still open. The requirements are in `specs/platform/social-content-automation/spec.md`.

Three constraints shape the design. The first two come from the platform's own records and the third from the upstream services.

1. The publishing service keeps scheduling and publishing (plan 14 D1, and the publishing service's own use-case record). Activepieces only generates, checks and hands off.
2. Approval is a rule of this platform, not a preference of one flow. Generation may create drafts without approval. Every schedule and every publish needs a recorded approval that names the approver and the revision (AC-795).
3. The upstream services are not fully documented for the edition and the pinned version this platform runs. Each upstream fact that the plan relies on was read from upstream sources checked on 2026-11-16, and it is re-verified on the pinned version before the task that depends on it closes.

## Goals / Non-Goals

Goals:

- Generate text drafts for the instance's brands through the inference gateway, under the client identity `activepieces`.
- Keep a person's approval in front of every schedule and publish, recorded with the approver and the revision.
- Keep one publisher. The publishing service schedules and publishes, and no flow publishes immediately.
- Keep flows and connections as code, with no credential in any committed definition.

Non-Goals:

- Images. They follow in a later phase, after the image path of plan 27 and the licence decision (AC-870).
- The studio. It is the intended long-term interface (AC-810), its decision is open, and it is outside the v0.17.0 scope.
- Changing the publishing service's own capability.
- Deploying Activepieces. Change `deploy-activepieces` owns that.
- Any brand value in this repository.

## Decisions

Each decision below names its plan 28 entry. The reasoning is in plan 28.

1. **Publishing stays with the publishing service** (plan 28 D1). Activepieces generates, checks and hands off. It never publishes.
2. **Approval rule** (plan 28 D2, AC-795). Spec Requirement "Drafts need no approval; scheduling and publishing need a recorded approval".
3. **No immediate publication** (plan 28 D2). Flows never send the immediate post type. The only write made without an approval record is a draft. Spec Requirement "Flows never publish immediately".
4. **Exactly once** (plan 28 D7). A created post's identifier is recorded against its draft, keyed by a stored external identifier, and checked before every create or schedule. Spec Requirement "Handoff to the publishing service is exactly once".
5. **No credential in flow definitions** (plan 28 D4). A `no_log` playbook reads each credential from OpenBao and writes it into an Activepieces connection. Spec Requirement "Flows and connections are declared as code and applied by playbook".
6. **Gateway through the HTTP piece** (plan 28 D5). The OpenAI piece has a fixed base URL, and the AI provider setting that would allow another base URL is plan-gated. Structured output uses the `response_format` json_schema form, which the gateway deploy already proves. Spec Requirement "Generation reaches the model only through the inference gateway under its own identity".
7. **Brand isolation by declared mapping** (plan 28 D8 and D9). The mapping from brand to publishing integration lives in site-config. Spec Requirement "Each draft targets exactly one mapped account of its brand".
8. **Deterministic guardrail step** (plan 28 D10). The guardrail is code inside the flow, so it needs no package installation and fits the sandbox mode that AC-768 proposes. Spec Requirement "Guardrail checks run before approval".
9. **Failure alerts through the observability alert route** (plan 28 D11). Spec Requirement "A failed content flow run raises an alert".
10. **Order of work** (plan 28 D12). Everything that does not need a connected social account is built and proven first.

## Open Decisions

- **Approval surface**, decided by the spike in task 4.4. The options are:
  - (a) A person schedules the draft in the publishing service's interface, signed in through the platform's identity provider. Plan 28 recommends it for Gate A only if the spike shows that the publishing service records who changed a post's state.
  - (b) An Activepieces approval step or form behind the platform's forward-auth gate, which records the approver and the revision and then sends the status change.
  - (c) The studio (AC-810). It is outside the v0.17.0 scope.
- **Edition risk R1**, decided by the spike in task 3.1. If the Community edition does not support the flow API path that D3 needs, the decision returns to the operator (the edition, AC-768). No workaround is designed in this change.

## Risks / Trade-offs

The full risk table with mitigations is in plan 28. The summary:

- **R1** The Community edition may not support the flow API path that flows-as-code and connections-as-code need. Spike first, in task 3.1.
- **R2** A publishing-service draft may need a connected account, and the local publishing service has none. Spike in task 4.4. If it does, the draft lives in the flow until approval, and the publishing leg is proven at Gate A.
- **R3** The HTTP piece may not be able to take its key from a stored connection. Spike in task 3.2. A key in a flow definition is never an acceptable fallback.
- **R4** Outbound client certificates may not be supported. Spike AC-826 (plan 27), with an egress proxy as the fallback.
- **R5** The publishing service's API key covers the whole organisation, so any holder can schedule a post or send an immediate one. The approval guarantee therefore rests on flow logic. The planned mitigation is a static test over the committed flow definitions.
- **R6** The publishing service's post-creation ceiling bounds burst generation. The documented ceiling conflicts between the configuration reference and the shipped defaults, so the scheduled cadence must stay under the lower figure until the pinned version is checked.
- **R7** Model output may name internal systems or make unbacked claims. The guardrail, the forbidden-claims list in the brand profile and the human approval address this together.

Trade-off accepted: the guardrail and the approval rule are enforced in flow logic and in a static test, not by the publishing service, because the publishing service's key does not scope what a caller may do.

## Rollout

The rollout follows the phases in `tasks.md`.

1. Phases 2 and 3 prove the brand profiles, the flows as code and the connections in the local environment first.
2. Phase 4 proves the text pipeline end to end before any account is connected, except where R2 requires one.
3. Phase 5 is Gate A. It publishes one AI-drafted, human-approved text post to the first brand's two accounts through the publishing service in production. It is the first live publication.
4. Phase 6 is Gate B. It adds an approved AI-generated brand image, and it starts only after the image path and the licence decision are settled.

Production use of Activepieces waits for the validation gate of change `deploy-activepieces` (AC-781).
