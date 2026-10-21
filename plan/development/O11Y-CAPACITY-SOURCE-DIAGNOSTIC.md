# O11Y CAPACITY SOURCE DIAGNOSTIC

**Date:** 2026-10-20  
**Status:** IMPLEMENTED  
**Context:** The Dev-bound seven-day capacity survey returned its existing `source_series_ambiguous` refusal. This plan adds bounded, sanitized evidence to help diagnose that refusal without changing source selection or capacity acceptance.

---

## Problem

The survey requires exactly one source series and correctly refuses when Prometheus returns more than one. Its fixed refusal receipt does not currently say how many series were returned or whether their evaluation/event histories overlap. That limits diagnosis while preserving the gate.

## Design Principles

1. Preserve the single-source requirement and all current refusal categories.
2. Emit only fixed schema keys, counts, and booleans; never emit metric label values, raw samples, or arbitrary label names.
3. Bound diagnostic work by the existing response size, query range, and returned rows.
4. Keep this read-only evidence separate from forecast, retention, and capacity decisions.

## Architecture

```mermaid
flowchart LR
    PROM["Bounded Prometheus response"] --> PARSE["Existing strict survey parser"]
    PARSE --> GATE["Existing one-source and history gate"]
    PARSE --> DIAG["Fixed-key, value-free diagnostics"]
    GATE --> RECEIPT["Sanitized success or refusal receipt"]
    DIAG --> RECEIPT
```

Diagnostics count returned source series, fixed allowlisted label variation, series present at the latest evaluation, overlapping evaluation timestamps, and overlapping sampled source-event timestamps. Any diagnostic parse limitation produces fixed unavailable fields and does not replace the existing gate result.

## Implementation Phases

### Phase 1: Additive diagnostics

**Goal:** Add the same bounded diagnostic object to survey success and refusal receipts.

**Tasks:**
1. Compute only aggregate counts and fixed allowlisted label-variation booleans from the source matrix.
2. Preserve the current `source_series_ambiguous` refusal for every multi-series response.
3. Add tests for disjoint and overlapping series, output secrecy, and unchanged readiness/refusal behavior.

**Acceptance criteria:**
- Two returned series still produce `status=insufficient` and `reason=source_series_ambiguous`.
- Diagnostics distinguish disjoint from overlapping evaluations/events without exposing values or label names.
- Existing source-history, backend, host-metric, and transport gates remain unchanged.

## Validation Criteria

| Check | Pass condition |
|---|---|
| Focused capacity history tests | Existing gate cases plus disjoint/overlapping diagnostic receipts pass |
| Ruff | Modified Python files pass repository Ruff checks |
| Output inspection | Receipts contain fixed field names and aggregate values only |
| OpenSpec status | Capacity task remains open pending Dev-bound live readback and seven-day evidence |

## Security Considerations

The diagnostic operates on an already bounded read-only query response. It must not print metric bodies, label values, timestamps, URLs, addresses, or unknown label names. It does not join or filter series, alter query selectors, prune history, or authorize retention/growth changes. The existing exact-revision controller and receiver guards remain authoritative.

## Cross-references

- [OpenSpec observability signal and capacity change](openspec/changes/o11y-signal-labeling-profiling-alerting/design.md)
- [OpenSpec capacity task 5.3](openspec/changes/observability-estate/tasks.md)
- [Testing and linting plan](../architecture/TESTING-AND-LINTING-PLAN.md)
- [Repository principles](../../PRINCIPLES.md)
- [Repository architecture](../../ARCHITECTURE.md)

## Revision History

| Date | Change |
|---|---|
| 2026-10-20 | Implemented bounded diagnostics for existing ambiguous-source refusal; no live cause established. |
