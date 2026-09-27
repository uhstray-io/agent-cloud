# agentgateway observability: metrics, traces and access logs in Grafana

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Requested by Joe, 2026-09-27: the
production agentgateway (image `cr.agentgateway.dev/agentgateway:v1.5.0`) reports its
metrics, traces and access logs to the platform's Grafana.

Companion changes: `inference-gateway-agentgateway` (owns the gateway; this change
supersedes its telemetry task 3.1 and reverses the Tempo deferral in its design
decision 6), `inference-telemetry-production` (owns the production o11y host, its
firewall and the inference dashboards), `observability-estate` (owns the trace rollout
gate; this change delivers its Alloy-to-Tempo path with the gateway as the pilot
service) and `production-internal-ca` (issues the certificates the OTLP hop uses).

## Why

The gateway already renders per-identity metric and access-log fields
(`platform/services/agentgateway/deployment/templates/config.yaml.j2:44-56`) and an
optional OTLP trace block (lines 58-64), but none of it reaches Grafana: the stats
listener publishes on loopback unless inventory says otherwise
(`platform/services/agentgateway/deployment/templates/env.j2:13`), the o11y host has no
OTLP receiver (`platform/services/o11y/deployment/config/config.alloy:7-9`) and no trace
store (`platform/services/o11y/deployment/compose.yml` defines only Prometheus, Loki,
Alloy and Grafana), and the o11y Alloy collects container stdout only from the engine
socket on its own host (`config.alloy:11-38`, socket mounted by `compose.local.yml:32-35`),
so a gateway on another VM ships no logs at all. The gateway now carries personal and
agent identities with token budgets; operators cannot see who used what, how slow the
first token was, or why a request failed without SSH, which the platform forbids.

Two findings from reading the v1.5.0 source make this more than wiring:

- The two telemetry blocks the template uses, `config.logging.fields` and
  `config.tracing`, are deprecated at v1.5.0: the loader converts them into
  `frontendPolicies.accessLog` and `frontendPolicies.tracing`, refuses to combine a
  deprecated block with its replacement, and the converted trace exporter cannot carry a
  resource name, a span filter or a private CA root (agentgateway `v1.5.0`
  `crates/agentgateway/src/types/local.rs:200-282`).
- Setting `config.database` for token budgets also turns on the gateway's request-log
  store in that same Postgres (`crates/agentgateway/src/config.rs:387`), whose schema has
  a prompt and completion payload table
  (`crates/agentgateway/src/telemetry/log_store/postgres_migrations/0001_create_request_log_schema.sql:25-29`).
  With the storage mode left unset, content that any CEL expression captures is persisted
  there (`crates/agentgateway/src/telemetry/log.rs:1128-1131`). Nothing in the template
  captures content today, but nothing pins that either.

## What Changes

- **Metrics.** Bind the gateway's stats listener to its LAN address, open it to the
  o11y host alone, and declare the existing `scrape-agentgateway.yml.j2` target through
  the gate `deploy-o11y.yml` already enforces (lines 122-142). Local-dev collects the same
  endpoint through Alloy's existing opt-in container labels (`config.alloy:49-101`).
- **Traces.** Add Grafana Tempo to the o11y compose stack with a declared retention, an
  Alloy OTLP gRPC receiver that forwards spans to it, and a provisioned Tempo datasource
  linked to Loki and Prometheus. The gateway exports spans through
  `frontendPolicies.tracing` at a declared sampling rate, with client-initiated trace
  continuation off. Trace enablement waits for the `observability-estate` gate.
- **Access logs.** The gateway exports one OTLP log record per request over the same
  receiver; Alloy converts them into the existing Loki writer with a `service` label.
  Stdout stays for `podman logs`. The identity field moves from the deprecated
  `config.logging.fields` to `frontendPolicies.accessLog`.
- **Content never leaves the gateway.** Prompts, completions, the Authorization header
  and API keys are never added to any metric, span, log or database field; the request
  log store is pinned to `metadata` mode. Prompt logging stays off.
- **Transport.** The OTLP hop is mutual TLS with certificates from the internal CA; a
  plaintext hop is refused outside local-dev. The metrics scrape stays plaintext HTTP,
  restricted by host firewall, because v1.5.0's stats listener takes only an address.
- **Firewall mechanism.** `apply-firewall.yml` gains per-port sources for detected
  ports, so the gateway's stats port and the o11y host's OTLP port admit only their one
  peer instead of every declared upstream.
- **Dashboard.** A provisioned `agentgateway` Grafana dashboard adapted from the
  upstream v1.5.0 dashboard (whose variables are Kubernetes-only), with a client-view
  row (first-token latency, request duration, failures, per-identity counts), a log
  panel and a trace search panel.
- **Verification.** The gateway deploy proves its stats endpoint answers and carries the
  verifying identity's label after the keyed round-trip; the o11y deploy proves the
  scrape target is up and, once enabled, that a gateway span and log line arrived.

## Capabilities

### New Capabilities
- `platform/agentgateway-observability`: the gateway's metrics, traces and access logs
  reach the platform's Grafana with bounded labels, declared retention, an authenticated
  transport and no request content.

### Modified Capabilities
None. The companion changes' requirements are unchanged; this change supplies the
mechanism that proves the gateway change's scenario "Client-view latency on the
dashboard" and the estate change's trace pilot.

## Impact

- `platform/services/agentgateway/deployment/`: `templates/config.yaml.j2` (move logging
  and tracing to `frontendPolicies`, OTLP log export, `database.llm: metadata`, TLS
  paths), `templates/env.j2`, `compose.yml` (certificate mount, scrape labels),
  `compose.local.yml`.
- `platform/services/o11y/deployment/`: `compose.yml` (Tempo service and volume, Alloy
  OTLP publish), `compose.local.yml`, `config/config.alloy` (receiver, Tempo exporter,
  Loki conversion, cluster label from the environment), new `config/tempo.yaml`,
  `config/grafana/provisioning/datasources/datasources.yml`, new
  `config/grafana/dashboards/agentgateway.json`, `templates/env.j2`.
- `platform/playbooks/`: `deploy-agentgateway.yml` (render guards, stats verify),
  `deploy-o11y.yml` (OTLP and trace guards, receipt verify), `apply-firewall.yml`
  (per-port sources).
- Tests: `platform/tests/test_service_agentgateway.bats`,
  `platform/tests/test_service_o11y.bats`, `platform/tests/test_apply_firewall.bats`.
- site-config (private): gateway `agw_stats_bind`, OTLP endpoint and sampling; o11y
  OTLP bind, trace enablement and retention; both hosts' firewall declarations; leaf
  declarations for the internal CA.
- Docs: agentgateway `context/architecture.md`, o11y `README.md`, a dated amendment to
  `plan/architecture/06-observability-instrumentation.md` (Tempo lives in the o11y
  stack rather than its own service directory).

## Rollback Plan

Every step is inventory-gated and reverts by a declaration change plus a Semaphore
redeploy; no data is deleted.

- Traces: unset the gateway's trace enablement and redeploy the gateway (the template
  renders no `frontendPolicies.tracing`); unset trace enablement on the o11y host and
  redeploy o11y (Alloy drops the trace pipeline). The Tempo volume is kept.
- Access logs over OTLP: unset the gateway's OTLP endpoint and redeploy; stdout logging
  is unaffected throughout.
- Metrics: remove `agentgateway_metrics_address` from the o11y inventory and redeploy
  o11y (the scrape file is removed, `deploy-o11y.yml:195-200`); set `agw_stats_bind` back
  to loopback and redeploy the gateway; re-apply the firewall.
- The `frontendPolicies` migration is a template change; reverting the commit and
  redeploying restores the deprecated blocks, which v1.5.0 still accepts.
- Dashboard and datasource: revert the provisioning commit and redeploy o11y.
