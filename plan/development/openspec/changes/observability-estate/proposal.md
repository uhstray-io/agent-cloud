## Why

The local observability stack is running, but its current Prometheus config scrapes only itself and the estate has no reusable metrics, alerting, and correlation path for newly onboarded services. The architecture contract requires declaration-based instrumentation and metrics and alerts before tracing. The production inference telemetry branch supplies inventory-driven DGX targets, but no production receiver or end-to-end receipt has been verified. This change integrates that groundwork and carries the estate through local validation, review, and guarded production rollout.

## What Changes

- Add a reusable, opt-in metrics discovery path for services co-located with the o11y engine, with an explicit network and label contract and a bounded series budget.
- Provision a generic service dashboard and baseline service-down and telemetry-missing alerts as code. Route notifications using an OpenBao-sourced contact point through Semaphore.
- Add a repeatable observability validation step to service onboarding and a pilot service that proves logs, metrics, dashboard selection, and alert delivery.
- Validate the isolated branch through local Semaphore, including Grafana's Authentik login, TLS, live metrics/log queries, and a notification drill, before opening a PR to `dev`. Submit every PR required for this rollout and obtain one completed CodeRabbit review round on each; address actionable findings before treating a PR as review-ready.
- Deploy the production o11y receiver through Semaphore from reviewed code and private inventory. Prove reachable DGX host/vLLM scrapes and Loki log receipt; enable GPU metrics only after the exporter is proven. Coordinate agentgateway's telemetry contract with its owning deployment work.
- After the metrics and alert gates pass, add a real Alloy OTLP pipeline and Tempo tracing with retention and cross-signal correlation.
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
- Integration: the clean, committed `inference-telemetry-production` branch was merged into this isolated branch. The original branch/worktree remains untouched; this change owns the combined review and rollout. DGX Spark and agentgateway host-side collectors remain with their respective owners.

## Rollback Plan

Remove a pilot's opt-in labels and redeploy it through Semaphore to stop its scrape. Revert the o11y provisioning/config commit and redeploy o11y through Semaphore to remove its dashboard, rules, or receiver while preserving its volumes. Disable the notification route via inventory and redeploy; do not delete metric, log, or trace data as part of rollback.
