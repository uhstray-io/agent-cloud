# Inference telemetry in production: promote o11y and ingest the DGX Spark signals

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Explored 2026-09-14 from the dgx-spark
document `docs/dgx-inference-telemetry-benchmark-gateway-ecosystem.md`.

Boundary (Joe, 2026-09-14): the two DGX Spark machines and the vLLM API are dgx-spark's
for now; everything outside them is agent-cloud's. Companion changes: dgx-spark
`node-telemetry-and-placement-benchmark` (the exporters and log shipper on the nodes,
which this change scrapes and receives) and agent-cloud `inference-gateway-agentgateway`
(the gateway, which adds its own signals to this stack once it exists). This change is
ordered first: the ecosystem document requires a baseline before any gateway or
placement change.

## Why

The platform's observability stack exists as code but not in production. The o11y
compose defines Grafana, Prometheus, Loki and Alloy; Prometheus scrapes only itself;
Alloy ships container logs only; `o11y_svc` does not appear in the production
inventory; the deployment plan (`plan/development/05-observability.md`) is PROPOSED and
its Phase 3 is the prod extension. Meanwhile the instrumentation contract
(`plan/architecture/06-observability-instrumentation.md`) describes the stack as
deployed. The ecosystem document names exactly this inconsistency and asks for a live
inventory first.

The inference endpoint has been serving the team for four days with no graph of queue
depth, cache pressure, preemptions or node memory. Its capacity ceiling was found by
saturating it; its memory thresholds were tuned through power cycles. The dgx-spark
companion change puts exporters on the nodes; without a production Prometheus and Loki
to scrape and receive them, those exporters have nowhere to report.

## What Changes

- **o11y promoted to production.** `o11y_svc` is added to the production inventory on a
  host chosen in the design; `deploy-o11y.yml` runs through Semaphore; Grafana is
  published at `o11y.uhstray.io` through Caddy (the DNS record already exists in
  `dns.tf`'s managed list as `o11y`). Retention is set from a measured day of ingestion,
  per Phase 3 of plan 05.
- **Prometheus scrapes the DGX Spark targets.** Static scrape jobs for the two nodes'
  host exporter, GPU exporter and the head's vLLM `/metrics`, on the ports and with the
  label contract the companion change defines. The Prometheus host's address is what
  the nodes' firewall admits, so it is a fixed inventory value.
- **Loki receives the node logs.** Loki's push endpoint is reachable from the two nodes
  only, through the estate firewall playbook; the nodes' Alloy pushes journal streams
  with `service`, `component`, `cluster`, `env`, `node`, `model_alias`.
- **Three dashboards as code**, per the ecosystem document: fleet and resource health
  (node memory, swap, thermal, exporter up); inference latency and capacity (running,
  waiting, KV usage, preemptions, TTFT and per-token histograms); model and placement
  comparison (per `model_alias`, with links to `results/` bundles in dgx-spark).
- **Alerts as code** for the four classes the document names: sustained inference
  failure, missing telemetry, memory or thermal risk, failed benchmark gate. Thresholds
  are set after one week of baseline; alerting never triggers an automated restart.
- **Plan documents reconciled.** Plan 05's status moves to reflect the production
  deploy; the instrumentation contract's "deployed" claim is corrected to the verified
  state at the time of writing.

No **BREAKING** changes: nothing existing is re-pointed; the local o11y profile is
unchanged.

## Capabilities

### New Capabilities
- `platform/inference-telemetry`: production metrics, logs, dashboards and alerts for
  the inference estate, with the DGX Spark nodes as the first external targets.

### Modified Capabilities
- none in this store (`specs/platform/` holds `n8n-automation` only).

## Impact

- Files: `platform/services/o11y/deployment/config/prometheus.yml` (static jobs for the
  nodes, with a per-environment include so local stays unchanged),
  `config/grafana/dashboards/inference-*.json`, `config/grafana/provisioning/alerting/`,
  `templates/env.j2` retention vars, `platform/playbooks/deploy-o11y.yml` reload step,
  `platform/tests/test_service_o11y.bats`, `plan/development/05-observability.md`,
  `plan/architecture/06-observability-instrumentation.md`.
- site-config: `o11y_svc` host entry; the Caddy route for `o11y.uhstray.io`; the two
  node addresses and exporter ports as inventory variables; firewall allow rules for the
  Loki push port from the two nodes.
- Live: one dedicated new VM (design decision 2); one Caddy route; Prometheus and Loki
  storage sized from measurement.
- Out of scope, recorded: Tempo and traces (no producer yet; the gateway change adds
  OTLP later); Mimir; the Grafana MCP triage path from the instrumentation contract;
  anything on the nodes themselves.

## Rollback Plan

- Stop without removing: disable the `o11y_svc` deploy template and stop the compose
  stack through Semaphore; the nodes' exporters keep running and buffer nothing they
  cannot send (metrics are pull, logs drop past the WAL cap by design).
- Remove: `clean-deploy-o11y.yml` in destroy mode removes containers and volumes; the
  Caddy route and firewall rules are inventory entries reverted and redeployed.
- Dashboards and alerts are provisioned files; reverting the commit removes them on the
  next deploy.
- The plan-document edits are reverted with the change; they carry dated status lines so
  a revert is visible as history.
