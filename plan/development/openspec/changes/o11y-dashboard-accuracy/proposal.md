## Why

The Service deployment conformance dashboard can report no data during a healthy collector delay, and its verifier treats expected-empty exception panels as failures. The Service Overview also omits a known container journal stream. Correcting the data contract and both dashboards will make the views useful without hiding missing required data.

## What Changes

- Count conformance coverage from inventory markers and display collector source freshness.
- Show collector marker age separately from latest workflow completion age; display numeric task references when known and explicit unknown retained/missing provenance.
- Reduce the latest state across pass, fail and skip before projecting current failures, so a later pass replaces an older failure. Keep a bounded, clearly historical failure drilldown that is separate from current service state.
- Let the existing dashboard verifier explicitly classify selected exception panels with empty results as `expected_empty`; query errors and required no-data remain failures.
- Show the independently selected Metrics service and Logs service identities in Service Overview. Report observed records or no records for the selected legacy container, journal container, access, span, conformance, and unclassified sources, with neutral absence styling and disjoint aggregate selectors. Unclassified identifies missing source classification only; it does not infer access records or service health.
- Include existing container, access, span, conformance and unclassified log signals in Service Overview without overlapping aggregate counts; retain existing Prometheus metrics and dashboard UIDs.
- Validate the final reviewed head through CI and non-destructive Dev-bound Semaphore deployment and live query receipts.

## Capabilities

### New Capabilities

- `platform/o11y-dashboard-accuracy`: accurate conformance and Service Overview dashboards, explicit empty-panel semantics, and evidence-based freshness.

### Modified Capabilities

None.

## Impact

Dashboard JSON for `service-conformance` and `service-overview`; existing dashboard evaluator coverage; focused dashboard and conformance tests; `deploy-o11y.yml` live read-back assertions; implementation documentation. No new service, dependency, production configuration, or destructive deployment is in scope.

## Rollback Plan

Revert the dashboard/evaluator implementation commit and redeploy the prior reviewed receiver revision through the existing Semaphore Dev deployment. The change has no data migration and does not alter retained collector state or production inventory.
