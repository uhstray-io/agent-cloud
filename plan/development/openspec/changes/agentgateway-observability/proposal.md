# agentgateway observability: metrics, traces and access logs in Grafana

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Requested by Joe, 2026-09-27: the
production agentgateway (image `cr.agentgateway.dev/agentgateway:v1.5.0`) reports its
metrics, traces and access logs to the platform's Grafana.

Decisions by Joe, 2026-09-27: "Log content for our team so we can learn from our prompts
and improve them. Don't log content for any users that are not part of the uhstray.io
team, users outside of the uhstray.io team is not a feature that has been created, but
will be created in the future." "The tempo deployment for the VM will be handled by a
different session, this session should only focus on integrating to the tempo endpoint
and configuring agentgateway." "The legacy key is only use by the uhstray.io team, same
for skynet." "Let's keep prompts for 90 days." "The gateways client-view should be a
separate dashboard." Client-started traces are not continued and new traces are sampled
at 10 percent.

External dependency: a Tempo instance with an OTLP gRPC ingest endpoint and a query
endpoint, deployed on the o11y VM by separate work (no OpenSpec change exists for it
under `plan/development/openspec/changes/` on 2026-09-27). That work owns the Tempo
container, its storage, its retention and Grafana's Tempo datasource; this change owns
the gateway's trace configuration and the hop that carries spans to that endpoint.

Companion changes: `inference-gateway-agentgateway` (owns the gateway; this change
supersedes its telemetry task 3.1 and reverses the Tempo deferral in its design
decision 6), `inference-telemetry-production` (owns the production o11y host, its
firewall and the inference dashboards), `observability-estate` (owns the trace rollout
gate; this change delivers the Alloy side of its Alloy-to-Tempo path and its pilot
service, the gateway) and `production-internal-ca` (issues the certificates the OTLP hop uses).

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
  there (`crates/agentgateway/src/telemetry/log.rs:1128-1131`). The storage mode is
  process-wide: `frontendPolicies` is the only place an access-log policy can be set at
  v1.5.0, so one gateway either keeps content for every identity or for none.

## What Changes

- **Metrics.** Bind the gateway's stats listener to its LAN address, open it to the
  o11y host alone, and declare the existing `scrape-agentgateway.yml.j2` target through
  the gate `deploy-o11y.yml` already enforces (lines 122-142). Local-dev collects the same
  endpoint through Alloy's existing opt-in container labels (`config.alloy:49-101`).
- **Traces.** The gateway exports spans through `frontendPolicies.tracing` at 10 percent,
  with client-initiated trace continuation off, to the same Alloy OTLP receiver the
  access records use; Alloy forwards them to the Tempo OTLP endpoint named in inventory
  (no default host). Deploying Tempo is out of scope (external dependency above). Trace
  export waits for that endpoint and for the `observability-estate` gate.
- **Access logs.** The gateway exports one OTLP log record per request over the same
  receiver; Alloy converts them into the existing Loki writer with a `service` label.
  Stdout stays for `podman logs`. The identity field moves from the deprecated
  `config.logging.fields` to `frontendPolicies.accessLog`.
- **Team prompt and completion content is kept, in one place only.** The request-log
  store (the gateway's own Postgres) runs in `database.llm: full` mode, and the team reads
  it through the gateway UI's Logs page, which sits behind the gateway's Authentik login
  and admin-group rule. Metrics, spans, OTLP exports and Loki stay metadata-only, because
  Grafana has no per-record access control.
- **Team-only, enforced.** Every enrolled identity declares its team. While content is
  kept, the gateway deploy refuses to render any identity not marked as a uhstray.io
  team member, and the gateway itself refuses requests from any key without that mark.
  Users outside the team are a future feature; their planned path is a separate gateway
  instance in `metadata` mode, recorded here and not built.
- **Content expires.** Stored rows are pruned after a declared retention (90 days) by a
  scheduled Semaphore job, because v1.5.0 has no pruning of its own and prompts can
  carry pasted secrets. Any future backup of that database excludes the payload table.
- **Credentials never leave the gateway.** No request header (the Authorization header
  included), request body expression or API key material is ever added to any metric,
  span, log or database field, and `llm.prompt` / `llm.completion` never appear in a
  telemetry field expression; content reaches the database only through `full` mode.
- **Transport.** The OTLP hop is mutual TLS with certificates from the internal CA; a
  plaintext hop is refused outside local-dev. The metrics scrape stays plaintext HTTP,
  restricted by host firewall, because v1.5.0's stats listener takes only an address.
- **Firewall mechanism.** `apply-firewall.yml` gains per-port sources for detected
  ports, so the gateway's stats port and the o11y host's OTLP port admit only their one
  peer instead of every declared upstream.
- **Dashboards.** Two provisioned Grafana dashboards adapted from the upstream v1.5.0
  dashboard (whose variables are Kubernetes-only): a separate client-view dashboard
  (first-token latency, request duration, failures, per-identity counts), and an
  operations dashboard (tokens, rejections, process health, access records, trace
  search). Neither is a row on the inference dashboard.
- **Verification.** The gateway deploy proves its stats endpoint answers and carries the
  verifying identity's label after the keyed round-trip; the o11y deploy proves the
  scrape target is up and, once enabled, that a gateway span and log line arrived.

## Capabilities

### New Capabilities
- `platform/agentgateway-observability`: the gateway's metrics, traces and access logs
  reach the platform's Grafana with bounded labels, declared retention, an authenticated
  transport and no request content; the team's prompt and completion content is kept
  only in the gateway's own request-log store, for team identities only, and expires.

### Modified Capabilities
None. The companion changes' requirements are unchanged; this change supplies the
mechanism behind the gateway change's scenario "Client-view latency on the dashboard"
(delivered as a separate client-view dashboard, not a row on the inference dashboard)
and the estate change's trace pilot.

## Impact

- `platform/services/agentgateway/deployment/`: `templates/config.yaml.j2` (move logging
  and tracing to `frontendPolicies`, OTLP log export, `database.llm: full`, the `team`
  key metadata and the runtime team rule, TLS paths), `templates/env.j2`, `compose.yml`
  (certificate mount, scrape labels), `compose.local.yml`.
- `platform/services/o11y/deployment/`: `compose.yml` (Alloy OTLP publish and
  certificate mount), `compose.local.yml`, `config/config.alloy` (becomes a directory;
  receiver, Loki conversion, Tempo exporter to the inventory-named endpoint, cluster
  label from the environment), new `config/grafana/dashboards/agentgateway-client-view.json`
  and `agentgateway.json`, `templates/env.j2`. No Tempo container, volume, config or
  datasource is added here.
- `platform/playbooks/`: `deploy-agentgateway.yml` (render guards, stats verify),
  `deploy-o11y.yml` (OTLP and trace guards, receipt verify), `apply-firewall.yml`
  (per-port sources), new `prune-agentgateway-request-logs.yml` with its scheduled
  Semaphore template in `platform/semaphore/templates.yml`.
- Tests: `platform/tests/test_service_agentgateway.bats`,
  `platform/tests/test_service_o11y.bats`, `platform/tests/test_apply_firewall.bats`.
- `inference-personal-keys`: its user-key rendering (its task 4.2) carries the `team`
  metadata this change requires.
- site-config (private): gateway `agw_stats_bind`, OTLP endpoint and sampling, content
  mode, retention and each identity's team; o11y OTLP bind, the Tempo ingest and query
  endpoints, trace enablement; both hosts' firewall declarations; leaf declarations for
  the internal CA.
- Docs: agentgateway `context/architecture.md`, o11y `README.md` (Alloy configuration is
  a directory; the Tempo endpoint is an input from separate work).

## Rollback Plan

Every step is inventory-gated and reverts by a declaration change plus a Semaphore
redeploy; no telemetry data is deleted.

- Content logging: set the gateway's content mode to `metadata` and redeploy; new rows
  carry no payload. Stored payload rows keep expiring through the scheduled prune; an
  immediate purge is the same prune run with a retention of zero days.
- Traces: unset the gateway's trace enablement and redeploy the gateway (the template
  renders no `frontendPolicies.tracing`); unset trace enablement on the o11y host and
  redeploy o11y (Alloy drops the trace pipeline). Tempo itself is not touched.
- Access logs over OTLP: unset the gateway's OTLP endpoint and redeploy; stdout logging
  is unaffected throughout.
- Metrics: remove `agentgateway_metrics_address` from the o11y inventory and redeploy
  o11y (the scrape file is removed, `deploy-o11y.yml:195-200`); set `agw_stats_bind` back
  to loopback and redeploy the gateway; re-apply the firewall.
- The `frontendPolicies` migration is a template change; reverting the commit and
  redeploying restores the deprecated blocks, which v1.5.0 still accepts.
- Dashboards: revert the provisioning commit and redeploy o11y.
