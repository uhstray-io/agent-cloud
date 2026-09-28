# agentgateway observability: the delta over the landed telemetry path

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Requested by Joe, 2026-09-27: the
production agentgateway (image `cr.agentgateway.dev/agentgateway:v1.5.0`) reports its
metrics, traces and access logs to the platform's Grafana. Rebased onto `dev` at
`7a24846` on 2026-09-28. Most of the transport now exists, so this change covers the
gateway-side decisions that code does not yet carry, and lists the o11y-side items it
depends on.

Decisions by Joe, 2026-09-27, all still binding: "Log content for our team so we can
learn from our prompts and improve them. Don't log content for any users that are not
part of the uhstray.io team, users outside of the uhstray.io team is not a feature that
has been created, but will be created in the future." "The legacy key is only use by the
uhstray.io team, same for skynet." "Let's keep prompts for 90 days." "The gateways
client-view should be a separate dashboard." Client-started traces are not continued and
new traces are sampled at 10 percent. Joe later asked for two dashboards: an operations
dashboard (the landed traffic dashboard, extended and renamed) and a separate client
view.

## What has landed

Read from this worktree at `7a24846`. The commit list comes from
`git log --oneline c9f99ca7..origin/dev` over the gateway and o11y paths.

- **Gateway policy blocks** (`13ff354`, PR #295): the gateway renders
  `frontendPolicies.accessLog` with `add.identity: apiKey.name`. When inventory sets
  `agw_otlp_host`, it also renders an `accessLog.otlp` export (fields `service` and
  `identity`) and `frontendPolicies.tracing` to the same host
  (`platform/services/agentgateway/deployment/templates/config.yaml.j2:81-107`). The
  deprecated `config.logging` and `config.tracing` blocks are gone.
- **Tempo, Alloy OTLP and a dashboard in the o11y stack** (`32db674`): Tempo `2.10.8`
  with `O11Y_TEMPO_RETENTION` defaulting to `168h`
  (`platform/services/o11y/deployment/compose.yml:92-102`). An Alloy OTLP gRPC receiver
  sends logs to the existing Loki writer and spans to `tempo:4317`
  (`config/config.alloy:108-146`). Grafana has a datasource with uid `tempo`
  (`config/grafana/provisioning/datasources/datasources.yml:22-39`) and the dashboard
  `agentgateway-traffic.json` (uid `agentgateway-traffic`), whose eight panels the o11y
  deploy asserts (`platform/playbooks/deploy-o11y.yml:476-477`).
- **Recovery-safe scrape receipt** (`5e2e36b`, PR #296): after readiness, the gateway
  deploy waits for a receiver-side sample of `agentgateway_config_synchronized` newer
  than the restart (`platform/playbooks/deploy-agentgateway.yml:279-316`).
- **Trace canary** (`a2ead67`, `9d5c609`, `74b5b24`): `Verify o11y Service` with
  `emit_agentgateway_canary=true` sends 100 anonymous `GET /v1/models` requests
  expecting 401. It then searches Tempo for an `agentgateway` trace from the
  receiver-clock start mark (`platform/playbooks/verify-o11y-service.yml:41-68`,
  `:106-132`).
- **Trace rollout gate** (`f0435e7`, `9350efd`): `tasks/assert-o11y-trace-rollout.yml`
  refuses unless the receiver records its metrics, alert-delivery, retention and
  cardinality receipts. Both deploys include it, and the gateway refuses a sampling
  fraction outside (0, 0.1] (`deploy-agentgateway.yml:103-116`,
  `deploy-o11y.yml:122-128`).
- **Self-monitoring**: Prometheus scrapes Alloy and Tempo
  (`config/prometheus.yml:27-33`). The self-monitoring dashboard charts
  `tempo_distributor_spans_received_total`
  (`config/grafana/dashboards/o11y-self-monitoring.json:95`).

## Why this change still exists

The landed path carries the signals. It does not carry the decisions above, and it
ties together two signals that need different gates.

- **One switch turns on both logs and traces.** Every OTLP step keys on `agw_otlp_host`:
  the access-log export and the tracing block (`config.yaml.j2:88`, `:98`), the sampling
  guard and the trace rollout gate (`deploy-agentgateway.yml:112-116`), the Tempo
  readiness check (`:132`), and the receiver's own inclusion of the gate
  (`deploy-o11y.yml:124-128`). Access records cannot ship until every trace receipt
  exists, although records do not need a trace store.
- **Client-started traces are continued.** `clientSampling` renders the same fraction as
  `randomSampling` (`config.yaml.j2:103-104`). The sampling default is `0.05`, not Joe's
  `0.1`.
- **Team content is neither kept nor fenced.** The budgets database is also the
  gateway's request-log store (agentgateway v1.5.0 `crates/agentgateway/src/config.rs:387`).
  No `database.llm` mode is rendered, which leaves v1.5.0's legacy behaviour in place
  (`schema/config.md:18230`). No identity declares a team, and nothing prunes rows.
- **One dashboard, not two.** The client view Joe asked for does not exist.
- **The receiver accepts plaintext on its private bind.** The production guard accepts
  a private, non-loopback OTLP bind (`deploy-o11y.yml:164-175`), and the receiver has no
  TLS block (`config.alloy:108-111`). That stays the interim until the internal CA can
  issue both leaves.

## What Changes

Gateway-side work, owned by this change:

- **Split the switch first.** `agw_otlp_host` stays the receiver address.
  `agw_otlp_logs` turns on the access-log export. `agw_otlp_traces` turns on tracing,
  and only that switch brings in the trace rollout gate, the Tempo readiness check and
  the sampling guard. The deploy refuses a host that is declared with neither switch,
  so the prod inventory, which today relies on the host alone, cannot silently lose a
  signal. The scrape receipt keys on the receiver's metrics declaration instead of the
  OTLP host.
- **Sampling.** `clientSampling: false`. `randomSampling` defaults to `0.1`, inside the
  landed guard.
- **Identity and team on every record.** `apiKey.name` and `apiKey.team` go in both
  `accessLog.add` and `accessLog.otlp.fields.add`, along with a
  `signal: "access-log"` field.
- **Team content is kept, and kept nowhere else.** The gateway renders
  `frontendPolicies.accessLog.database.llm: full` when inventory sets it. Every
  identity declares `team`, and the deploy refuses to render a non-team identity while
  content is kept. Content is read only through the gateway UI's Logs page, behind
  Authentik and the admin-group rule.
- **Content expires after 90 days.** A scheduled prune deletes old rows, and a prune
  that stops running raises an alert. The gateway deploy never gates on the prune,
  because that deploy is how a key gets revoked.
- **No forbidden reference in the template.** A BATS scan of `config.yaml.j2` checks it.
- **The gateway deploy proves its own access record reached Loki** after its keyed
  probe.
- **Metrics:** a LAN stats bind, per-port firewall sources and local scrape labels.
- **Transport:** mutual TLS, added to the landed private bind once leaves exist.

o11y-side work, owned by the o11y session per the handoff of 2026-09-28 and listed here
as dependencies:

- the operations dashboard: rename and extend `agentgateway-traffic`, and update its
  eight-panel assert
- the new `agentgateway-client-view` dashboard
- the receiver's mutual-TLS block
- the `signal` label hint
- the Alloy `cluster` label, read from the environment instead of hard-coded
  (`config.alloy:44`)
- the trace-silence alert on `tempo_distributor_spans_received_total`
- the prune-staleness alert rule
- any extension of `verify-o11y-service.yml`

Dropped from the earlier draft:

- an external Tempo and its inputs
- a second trace gate
- a receipt check that needed sampling at one, which conflicts with the landed 0.1
  ceiling
- a new Alloy scrape
- an o11y verify that reads a gateway client key or sends keyed inference
- a runtime team rule in `llm.policies.authorization`
- moving the Alloy config into a directory

## Capabilities

### New Capabilities
- `platform/agentgateway-observability`: the gateway's metrics, access records and
  traces reach the platform's Grafana. Labels stay bounded, each signal has its own
  switch, retention is declared, and no request content is exported. The team's prompt
  and completion content is kept only in the gateway's own request-log store, for team
  identities only, and it expires.

### Modified Capabilities
None. This change supplies the mechanism behind `inference-gateway-agentgateway`'s
scenario "Client-view latency on the dashboard", delivered as the separate client-view
dashboard. It also supplies the pilot for `observability-estate` task 3.3.

## Impact

- `platform/services/agentgateway/deployment/`: `templates/config.yaml.j2` (switch
  split, sampling, `team` metadata, `signal` field, `database.llm`, TLS paths),
  `compose.yml` (scrape labels, certificate mount, and the header comment at lines 7-8),
  `README.md` and `context/architecture.md`.
- `platform/playbooks/deploy-agentgateway.yml` (switch split, render guards, Loki
  receipt), a new `prune-agentgateway-request-logs.yml`, `apply-firewall.yml` (per-port
  sources) and `platform/semaphore/templates.yml` (prune schedule).
- `platform/playbooks/deploy-o11y.yml:124-128`: the receiver's gate condition reads the
  gateway's trace switch instead of its host. The variable belongs to the gateway, so
  the edit is ours, and the o11y session reviews it.
- Tests: `platform/tests/test_service_agentgateway.bats` (replaces the OTLP assertions at
  lines 346-373 and the gate assertion at line 385), `test_service_o11y.bats:1077` and
  `test_apply_firewall.bats`.
- `inference-personal-keys` task 4.2 renders `team: uhstray` on user keys.
- site-config (private): `agw_otlp_logs`, `agw_otlp_traces`, `agw_stats_bind`,
  `agw_content_logging`, `agw_request_log_retention_days`, each identity's `team`, and
  both hosts' firewall declarations.

## Rollback Plan

Every step is inventory-gated. Each one reverts by a declaration change plus a Semaphore
redeploy, and no telemetry data is deleted.

- Traces: set `agw_otlp_traces: false` and redeploy the gateway. The template renders no
  `frontendPolicies.tracing`, and access records keep flowing.
- Access records over OTLP: set `agw_otlp_logs: false` and redeploy. Stdout logging is
  unaffected throughout.
- Content logging: set `agw_content_logging: metadata` and redeploy. New rows carry no
  payload, and stored rows keep expiring through the prune. An immediate purge is the
  same prune run with a retention of zero days.
- Metrics: remove `agentgateway_metrics_address` from the o11y inventory and redeploy
  o11y, which removes the scrape file (`deploy-o11y.yml:228-233`). Set `agw_stats_bind`
  back to loopback, redeploy the gateway and re-apply the firewall.
- Dashboards: revert the provisioning commit and redeploy o11y.
