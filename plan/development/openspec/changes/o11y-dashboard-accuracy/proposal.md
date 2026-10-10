## Why

The Service deployment conformance dashboard can report no data during a healthy collector delay, and its verifier treats expected-empty exception panels as failures. The Service Overview also omits a known container journal stream. Correcting the data contract and both dashboards will make the views useful without hiding missing required data.

## What Changes

- Count conformance coverage from inventory markers and display collector source freshness.
- Preserve the latest workflow state across collection gaps and calibrate its display window using observed schedule, task duration and delivery evidence.
- Let the existing dashboard verifier explicitly classify selected exception panels with empty results as `expected_empty`; query errors and required no-data remain failures.
- Include existing container, access, span and conformance log signals in Service Overview without overlapping aggregate counts; retain existing Prometheus metrics and dashboard UIDs.
- Validate the final reviewed head through CI and non-destructive Dev-bound Semaphore deployment and live query receipts.

## Capabilities

### New Capabilities

- `platform/o11y-dashboard-accuracy`: accurate conformance and Service Overview dashboards, explicit empty-panel semantics, and evidence-based freshness.

### Modified Capabilities

None.

## Impact

Dashboard JSON for `service-conformance` and `service-overview`; the existing Verify o11y Dashboard Data playbook/evaluator and tests; o11y dashboard read-back assertions; implementation documentation. No new service, dependency, production configuration, or destructive deployment is in scope.

## Rollback Plan

Revert the dashboard/evaluator implementation commit and redeploy the prior reviewed receiver revision through the existing Semaphore Dev deployment. The change has no data migration and does not alter retained collector state or production inventory.
