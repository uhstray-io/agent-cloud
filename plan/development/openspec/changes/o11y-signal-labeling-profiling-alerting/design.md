## Context

The current stack has Prometheus, Loki, Alloy, Tempo, and Grafana. The service overview uses Docker container names, while remote metrics and OTLP logs already use a `service` hint. Grafana rules notify the Discord contact directly. An open estate-wide instrumentation change inventories future producers, but its signal contract and profile budget need a concrete baseline.

## Goals / Non-Goals

Goals: one bounded service identity, honest cross-signal coverage, a private profile backend with a measured opt-in pilot, and alerts routed with stable context and restrained repeat cadence. Preserve state and keep deployment reproducible from `dev` through Semaphore.

Non-goals: instrumenting every service in this change, replacing the alert policy tree, introducing Grafana Cloud IRM, importing the Docker LGTM demo image into production, or forcing profiling on workloads without a supported producer and budget.

## Decisions

1. **Identity contract.** Use `service` as the Prometheus/Loki/alert key and `service.name` in OTLP traces; map profiles to a stable `service_name`. `cluster` and `environment` are inventory-bounded. `owner` and `severity` are alert labels. Keep instance and container details as drill-down context, not routing dimensions. Maintain an explicit allowlist of low-cardinality fields and a test for forbidden dynamic labels.
2. **Visibility.** Change the service overview selector from `container` to `service`; use declared metrics targets for a missing-telemetry view and show logs independent of container naming. Retain the existing Loki-to-Tempo derived field. Add a bounded cardinality/ingestion panel from collector self-metrics and verify the query against live metric names before claiming runtime acceptance.
3. **Profiling.** Add Pyroscope to the existing Compose stack with an isolated persistent volume and internal-only port. Add the Grafana datasource. An Alloy `pyroscope.scrape` and `pyroscope.write` pipeline is enabled only for a verified in-stack pprof producer through inventory. Use a small initial retention/resource budget and explicit measurement before other runtimes join. A normal deploy never calls the clean workflow.
4. **Alerting.** Keep the existing Discord contact. Add stable labels, dashboard/runbook annotations, and rule-level `notification_settings` grouping/timing. Do not file-provision the shared policy tree, whose replacement semantics could overwrite unrelated routes. Read back the rule settings and run a bounded delivery drill.
5. **Estate adoption.** Amend the open estate-wide instrumentation plan with a per-service signal/label/profile/owner declaration and a phased capacity gate. Service onboarding references that contract. Avoid duplicating source truth in dashboard JSON or a second inventory.

## Risks / Trade-offs

- A new Pyroscope process and volume consume memory and disk; production enablement is gated on measured VM headroom, retention, and one verified sample. A local config check alone does not prove an end-to-end profile.
- A service selector derived from Prometheus cannot enumerate log-only services. The dashboard must state this scope, while Logs Drilldown remains available for log-only producers and the estate inventory records coverage gaps.
- Grouping delays delivery by the configured wait period. The delivery drill must allow for that delay and verify both firing and resolution.
- Adding indexed labels increases series and stream counts. Establish baseline and compare before broad rollout; reject unbounded values at the collector or producer boundary.

## Migration Plan

1. Land the spec and code from an isolated branch after one independent review and green CI; merge into `dev`.
2. Run the normal non-destructive Semaphore deploy from `dev`, verify five existing components plus Pyroscope, data sources, dashboard, alerts, and preserved volumes.
3. Enable one verified profile producer only after measuring VM headroom and checking privacy; confirm a profile in Grafana and record ingestion/cardinality deltas.
4. Expand service onboarding in batches under the estate-wide change; size the VM from measured load before enabling high-volume producers.

Rollback: disable the pilot and restore the prior reviewed config through `dev` and Semaphore. Preserve all named volumes for a separate retention decision.

## Producer and rollout gate

Use Alloy's documented self-profiling endpoint on port 12345 as the first producer, under `service_name="alloy"`; pin Pyroscope v2.2.0. Keep the scrape disabled until a config check confirms the pinned Alloy version exposes valid pprof profiles and a controlled deployment probe measures resource headroom. Failure at either gate leaves profile collection disabled and is documented in the rollout evidence.
