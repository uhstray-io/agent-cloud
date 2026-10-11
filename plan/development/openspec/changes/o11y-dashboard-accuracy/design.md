## Context

The two provisioned dashboards have stable UIDs and use existing Prometheus and Loki datasources. The conformance dashboard currently derives tracked-service count from all recent conformance streams and hard-codes `[16m]` for current-state queries (`service-conformance.json:14-22,57-105`). The collector maps inventory services and merges stored status beneath newly observed results (`collect-service-conformance.yml:109-124,241-257`).

`Verify o11y Dashboard Data` reads provisioned dashboard JSON and evaluates Prometheus and selected Loki panels read-only (`verify-o11y-dashboard-data.yml:36-49,80-124`). At baseline, its evaluator required finite data for each queried target and failed on any empty panel; OpenSpec tasks 1.1–1.3 now add selected expected-empty reporting while preserving required-empty and query-error failures (`verify-o11y-dashboard-data.py`).

Service Overview has separate metrics and log selectors (`service-overview.json:41-46,146-185,341-359`). The Alloy journal source uses `service` and `signal="container"` without a `container` label (`journal.alloy.j2:3-8`). New Dev evidence is mixed: read-only check-mode task 4243 against receiver `b1be2a7b2de884baf10d72bb09646e50dd6d63ba` returned data for “Services tracked”, “Services not yet run” and “Latest step status by service”, while “History incomplete” was empty and caused the combined required-data check to fail. Task 4244 found no logs for `log_service="o11y/alloy"` in six hours. Task 4246 succeeded for the Overview “All Loki streams” panel with `log_service=.+`, proving at least one Loki stream is queryable. Task 4247 found “Recent container logs” empty across all nonempty service labels. The earlier `.*` selector probe errored; `+` is the accepted nonempty selector. Current Dev `a052555060b4c9ca5fb59fb44fa64906fc38bd69` has no diff in relevant dashboard/verifier files from that receiver. These receipts do not prove container ingestion; the journal rollout gate remains separate.

## Goals / Non-Goals

**Goals:**

- Make the tracked count inventory-marker based and expose age of the latest source marker.
- Calibrate state lookback/freshness from measured collection timing.
- Preserve current state and make expected-empty cases explicit without suppressing errors.
- Include existing Overview signals with disjoint aggregate selectors and stable UIDs.

**Non-Goals:**

- Change collector cadence, retention, service instrumentation, labels, production inventory, or Grafana version.
- Add a diagnostic profile, backend, dependency, or new production data path.
- Claim an Overview source is live until an exact query receipt proves it.

## Decisions

1. **Extend the existing verifier.** Add a validated list of exact selected panel titles whose empty result is an expected state. Report `expected_empty` separately from `pass`, `no_data`, and query error. Reject names outside the selected dashboard panels; never convert a query error or a required panel’s empty result into success. This reuses the existing read-only diagnostic and avoids a new Semaphore profile.
2. **Use collector inventory markers for estate coverage.** Count distinct `service` values from valid `step="none"` inventory marker records, including the `no_history` marker. Store the marker time as a numeric `marker_time_seconds` field in the existing record body; keep the Loki label set unchanged. For the 12 successive successful collector runs measured on 2026-10-10 (task duration 11.7–11.9 minutes, completion gaps 14.8–15.1 minutes, latest end 23:26:50 UTC), use a 45-minute latest-marker window and mark ages at 30 minutes stale. This is a calibration for that sample, not a lifetime guarantee. No separate Loki delivery-delay measurement was supplied; adjust using future run and verifier receipts. Missing or invalid marker evidence stays unavailable, never zero.
3. **Retain the collector as state authority.** The dashboard reads latest snapshots emitted by the collector, which merges last-known NetBox state under current results (`collect-service-conformance.yml:241-257`). Do not add a second state store or erase values when history is incomplete.
4. **Use separate, disjoint stream selectors.** Include the journal stream through its existing `signal="container"` label and preserve application container, access-log, span and conformance views. The legacy container selector excludes records marked as journal, access, span or conformance; each signal selector excludes conformance records. The container log view unions legacy container and journal records, so a record with both `container` and `signal="container"` appears only through the journal branch. Keep the all-stream view as a drill-down, never a source total. Confirm selectors against actual Dev receipts before accepting live coverage.
5. **Preserve identity and bounded cardinality.** Keep UIDs `service-conformance` and `service-overview`, datasource UIDs `prometheus` and `loki`, and the existing bounded label contract. Add no task ID or arbitrary values as labels.

## Risks / Trade-offs

- **[Risk] Collection timing or Loki delivery delay changes, making the current boundary too short or too permissive.** → The 30-minute stale and 45-minute unavailable boundaries are calibrated only to the supplied 12-run sample. Recheck collector and verifier receipts and adjust the values when later measured timing requires it.
- **[Risk] A panel is empty because its source is missing, but an exception hides it.** → Allow empty only for exact named panels whose absence is documented as healthy; keep required panel emptiness and every query error failing.
- **[Risk] Overview source selectors overlap or live logs are absent.** → Test selector disjointness and inspect exact Dev query results; report absent source as unverified rather than as healthy or fixed.

## Migration Plan

No data migration is required. After implementation, run focused tests and final-head CI, obtain independent review, execute a check-mode `Deploy o11y (Dev)` through Semaphore, then deploy that reviewed head to Dev without Clean Deploy. Reuse `Verify o11y Dashboard Data (Dev)` for required panels and explicitly named expected-empty panels. Record receiver revision and exact source receipts. For acceptance, identify the promoted Dev merge commit containing the reviewed final-head content and verify the receiver SHA equals that promoted Dev commit; a squash merge may rewrite the pre-merge SHA. Roll back by reverting the implementation commit and redeploying the prior reviewed Dev revision through Semaphore.

## Open Questions

- Do the new Overview queries return data on Dev? The supplied receipt found the existing Recent container logs query empty and did not prove journal-container or other source ingestion. Keep those source results unverified until exact Dev query receipts exist.
