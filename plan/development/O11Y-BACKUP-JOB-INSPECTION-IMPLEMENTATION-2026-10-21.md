# O11Y-BACKUP-JOB-INSPECTION-IMPLEMENTATION

**Date:** 2026-10-21
**Status:** PROPOSED
**Context:** A reviewed Dev-bound backup readiness inspection already reads the declared o11y VM and backup-job list. Its candidate output exposes job identifiers and does not correlate the private declared job with current o11y membership. This plan adds a bounded, GET-only selection summary without changing backup-job mutation or schedule behavior.

---

## Problem

The existing inspect-only workflow can report candidate jobs, but durable output includes private job identifiers and forces an operator to correlate the private inventory declaration manually. It also cannot answer whether the exact declared job currently includes the o11y VM. Scheduler selectors such as `all`, pool, and exclude are not safely reducible to explicit membership by the existing mutation validator.

## Design Principles

1. **Private declaration remains authoritative** — resolve the selected job only from the existing private inventory value; add no survey input or caller-selected identity.
2. **Read-only means GET-only** — retain the current inspect-mode boundary and end the play before every apply task.
3. **No inferred selection** — use the existing explicit-VMID validator for membership and mark unsupported or malformed selector semantics unverified.
4. **Sanitized durable output** — emit fixed statuses and booleans only; never output job IDs, VMIDs, raw API bodies, or arbitrary selector values.
5. **Preserve apply safety** — existing write planning, pre-write checks, update behavior, and readback remain unchanged.

## Architecture

```mermaid
sequenceDiagram
    participant Inventory as Private inventory
    participant Playbook as Dev-bound inspect playbook
    participant Proxmox as Proxmox API
    participant Helper as Read-only inspection helper
    Inventory->>Playbook: Declared job identity and o11y VM identity
    Playbook->>Proxmox: GET current backup-job list
    Playbook->>Helper: Private declaration plus current list
    Helper->>Helper: Count exact matches; validate explicit VMID selector
    Helper-->>Playbook: Fixed match, enabled, membership, and selector statuses
    Playbook-->>Inventory: Sanitized inspect summary; then end play
```

The helper reports `unique`, `missing`, or `ambiguous` for the declared job match. A unique job reports its enabled state and a fixed selector category. Membership is `true` or `false` only when the existing membership validator proves a valid explicit VMID selector; `all`, pool, exclude, unsupported job types, and malformed selectors report unknown membership and `selection_unverified`. Candidate identifiers and raw selector values are not returned.

## Implementation Phases

### Phase 1: Selected-job helper and handoff

**Goal:** Correlate the private declared job with the existing GET response while preserving read-only semantics.

**Tasks:**
1. Add a helper operation that returns a bounded selected-job summary without private identifiers.
2. Reuse the current explicit-membership validator while retaining its strict enabled requirement for mutation callers.
3. Pass the existing private inventory declaration to the helper and use its unique-match status for detail/storage inspection GET conditions.
4. Replace candidate identity output with fixed sanitized status/count fields; keep the inspect-mode `meta: end_play` before apply tasks.

**Acceptance criteria:**
- Missing and duplicate selected identities are distinguishable from a unique current match.
- Disabled, malformed, and non-explicit selector cases never produce a false membership claim.
- Inspection makes no PUT/POST/DELETE request and does not accept a launch-time job identity.

### Phase 2: Focused regression coverage and evidence note

**Goal:** Prove sanitized helper and Ansible handoff behavior across refusal and selector cases.

**Tasks:**
1. Add actual helper tests for unique/missing/ambiguous selection, disabled jobs, unsupported selectors, private-value redaction, and malformed/forged identity inputs.
2. Extend playbook tests to verify that the private declaration feeds the helper, selected detail reads remain GET-only, and the inspect boundary precedes all mutation tasks.
3. Record source implementation and existing live evidence as separate claims in the copied standalone note.

**Acceptance criteria:**
- Focused helper and playbook tests pass.
- No test claims a backup artifact, freshness, job attribution, or restore acceptance from selection membership alone.

## Validation Criteria

| Check | Pass Condition |
|---|---|
| Focused helper tests | Match states, selector categories, enabled state, and redaction are verified |
| Playbook handoff tests | Private inventory source, exact summary gate, GET-only inspection, and end-before-mutation ordering are verified |
| YAML parse and Ansible syntax | The playbook parses and syntax validation passes |
| Secret/privacy review | New durable output contains no private job or VM identifiers, addresses, raw API data, or credentials |
| Git scope | Only the owned helper, playbook, focused test, and standalone plan/evidence notes are changed |

## Security Considerations

- Job identity and VMID are private inventory values and remain in Ansible memory under existing `no_log` boundaries.
- The helper returns a fixed allowlisted schema; input identifiers and arbitrary Proxmox fields never enter durable output.
- Membership proves configuration selection only. It does not prove a successful or fresh backup, artifact presence, PBS integrity, or restoreability.
- Unsupported scheduler selection semantics remain unverified; this plan does not broaden the mutation validator or change the selected job.

## Cross-references

| Document | Relationship |
|---|---|
| [PBS VM and restore gate tasks](openspec/changes/pbs-vm-and-restore-gate/tasks.md) | Source workflow is part of the backup readiness sequence; live backup and restore acceptance remain open |
| [PBS backup and capacity evidence](O11Y-BACKUP-CAPACITY-RECEIPT-2026-10-21.md) | Existing read-only evidence, copied byte-for-byte before this implementation |
| [Ansible automation standards](../architecture/08-ansible-automation-standards.md) | Read-only task behavior and check-mode conventions |
| [AGENTS.md](../../AGENTS.md) | Repository security, Semaphore, and testing requirements |

## Revision History

| Date | Change |
|---|---|
| 2026-10-21 | Initial bounded selected-job inspection plan |
