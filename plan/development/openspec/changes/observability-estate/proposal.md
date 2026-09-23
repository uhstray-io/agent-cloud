## Why

The local observability stack is defined in the repository, but its current Prometheus config scrapes only itself and the estate has no reusable metrics, alerting, and correlation path for newly onboarded services. The architecture contract requires declaration-based instrumentation and metrics and alerts before tracing; this change makes that contract executable without colliding with the active production inference telemetry change.

## What Changes

- Add a reusable, opt-in metrics discovery path for services co-located with the o11y engine, with an explicit network and label contract and a bounded series budget.
- Provision a generic service dashboard and baseline service-down and telemetry-missing alerts as code. Route notifications using an OpenBao-sourced contact point through Semaphore.
- Add a repeatable observability validation step to service onboarding and a pilot service that proves logs, metrics, dashboard selection, and alert delivery.
- After the metrics and alert gates pass, add a real Alloy OTLP pipeline and Tempo tracing with retention and cross-signal correlation. Production use requires a separate network and access review.
- Reconcile the as-built status in `05-observability.md` and `06-observability-instrumentation.md` with dated, evidence-backed notes.

No breaking service API change is planned. Existing services opt in as they are verified.

## Capabilities

### New Capabilities

- `platform/observability-estate`: declarative service telemetry, shared correlation, provisioned dashboards and alerts, and staged trace ingestion.

### Modified Capabilities

None.

## Impact

- Public repo: o11y compose/config/provisioning, composable validation tasks, onboarding documentation, and focused tests.
- Private `site-config`: inventory values for retention, permitted network paths, and notification destination; no real addresses or credentials in this repository.
- Live systems: deployments and verification only through Semaphore after code review and prerequisite checks.
- Boundary: `inference-telemetry-production` owns the production o11y host, DGX Spark scrape fragments, inference dashboards and alert rules. This change will rebase after those files land and will not edit them concurrently.

## Rollback Plan

Remove a pilot's opt-in labels and redeploy it through Semaphore to stop its scrape. Revert the o11y provisioning/config commit and redeploy o11y through Semaphore to remove its dashboard, rules, or receiver while preserving its volumes. Disable the notification route via inventory and redeploy; do not delete metric, log, or trace data as part of rollback.
