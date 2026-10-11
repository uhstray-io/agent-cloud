# Tasks: O11y dashboard accuracy

Implementation is gated on review and green CI. All live reads and deployment use the existing Dev-bound Semaphore workflows; no production target or destructive reset is in scope.

## 1. Verifier contract and regression coverage

- [x] 1.1 Add exact selected-panel empty allowances to the existing dashboard evaluator and playbook input; report `expected_empty` separately and refuse unknown or unselected panel names.
- [x] 1.2 Add evaluator tests for expected-empty panels, required no-data, query failure, malformed allowlists and non-empty required results.
- [x] 1.3 Run focused evaluator tests and strict OpenSpec validation. **Gate:** `Dashboard verification distinguishes expected empty panels` passes; query failures remain failures.

## 2. Dashboard and source coverage

- [x] 2.1 Replace recent-event-based tracked count with distinct valid inventory markers; add latest-marker age using the numeric marker body timestamp. Calibrate to the measured 12-run sample: task duration 11.7–11.9 minutes, completion gaps 14.8–15.1 minutes, latest completion 2026-10-10 23:26:50 UTC; use a 45-minute latest-marker window and 30-minute stale threshold, subject to future receipt-based adjustment.
- [x] 2.2 Preserve latest pass/fail/skip state across the 45-minute collection window and keep history-incomplete markers distinct from current failures. Name expected-empty panels explicitly when requesting verifier results.
- [x] 2.3 Update Overview queries to include the existing journal container signal and keep legacy container, journal container, access, span, and conformance aggregate selectors disjoint. Keep the all-stream view out of aggregate counts.
- [x] 2.4 Update panel assertions and fixtures for stable dashboard and datasource UIDs, valid inventory-marker counts, freshness/no-data behavior, bounded labels, disjoint selectors, and empty/no-data outcomes. Do not require live Deployment read-back changes where that workflow has no assertions for these dashboards.
- [x] 2.5 Run focused dashboard, evaluator and playbook tests plus relevant static checks: 226 pytest cases passed; the focused BATS dashboard read-back passed; Ruff, Python compilation, Ansible syntax check, strict validation of an isolated mirror of the exact change artifacts, and `git diff --check` passed. Three pytest temporary-directory cleanup warnings remain. **Gate scenarios:** `Collector source is stale or unavailable`, `Collector recovers after a gap`, `Journal container records are visible`, and `A selected signal has no live records` pass local source assertions; live outcomes remain for Phase 3.

## 3. Reviewed Dev acceptance

- [ ] 3.1 Obtain independent review and green CI on the exact final head; address feedback and re-run required CI on the updated head.
- [ ] 3.2 Run `Deploy o11y (Dev)` through Semaphore in check mode against the reviewed head; confirm the task is non-destructive and binds the expected revision.
- [ ] 3.3 Deploy the reviewed change to Dev through Semaphore without Clean Deploy; record the task ID and receiver revision.
- [ ] 3.4 Run `Verify o11y Dashboard Data (Dev)` for required panels and exact expected-empty exceptions; capture exact Prometheus/Loki receipts, source freshness and outcomes. Keep absent Loki coverage unverified until a matching record is queryable.
- [ ] 3.5 Review the final evidence against every spec scenario before any promotion is proposed. **Gate scenarios:** `An exception panel has no matching rows`, `Required panel is empty or its query fails`, `Dashboard identity remains stable`, and `A selected signal has no live records` pass. Identify the promoted Dev merge commit containing the reviewed final-head content and verify the receiver SHA equals that promoted Dev commit; GitHub squash merges rewrite the pre-merge SHA, so direct equality with the reviewed head is not required.
