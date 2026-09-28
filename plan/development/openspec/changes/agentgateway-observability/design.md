# Design: agentgateway observability

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

## Context

See `proposal.md` for motivation. Rebased 2026-09-28 onto `origin/dev` at `7a24846`,
then brought up to `origin/dev` at `a146382` (PR #301) by merge `f92b0bf`. Repository
facts below were read in the `7a24846` tree; the line cites into `deploy-o11y.yml`,
`datasources.yml` and `config.yaml.j2` were re-read at `f92b0bf` on 2026-09-28. The
agentgateway facts were read on 2026-09-27 from tag `v1.5.0` (commit `fe6732474a96a0363dfb9822859af4e9bab360fa`, via
`gh api repos/agentgateway/agentgateway/contents/<path>?ref=v1.5.0`). Where the upstream
documentation, which tracks `main`, disagrees with v1.5.0, v1.5.0 wins.

### What has landed on dev

Commits from `git log --oneline c9f99ca7..origin/dev` over
`platform/services/o11y`, `platform/services/agentgateway`, `deploy-o11y.yml`,
`deploy-agentgateway.yml` and `verify-o11y-service.yml`:

| Commit | What it added |
|---|---|
| `32db674` | Tempo, the Alloy OTLP receiver, the Tempo datasource and the `agentgateway-traffic` dashboard |
| `0c15a22` | Tempo verified through Grafana's datasource proxy |
| `13ff354` (PR #295) | `frontendPolicies.accessLog` and `.tracing`, both gated on `agw_otlp_host` |
| `5e2e36b` (PR #296) | A receiver-side scrape receipt taken after startup, so a stopped gateway can still be recovered |
| `a2ead67`, `9d5c609`, `74b5b24` | An opt-in, anonymous gateway trace canary in `Verify o11y Service` |
| `f0435e7`, `9350efd` | The shared trace rollout gate and the gateway's sampling guard |
| `9375948` | Receipt IDs taken from Semaphore task records |
| `f87850f`, `97728e4` (PR #301) | A Loki-to-Tempo `TraceID` derived field on the Loki datasource, read back live by the o11y deploy, and runtime retention budgets verified |

The gateway side, `platform/services/agentgateway/deployment/templates/config.yaml.j2`:

- Metrics: `config.metrics.fields.add.identity: apiKey.name` (lines 44-47). The stats
  listener is `0.0.0.0:19002` inside the container (line 35). Compose publishes it on
  `${AGW_STATS_BIND:-127.0.0.1}` (`compose.yml:45`, `templates/env.j2:13-14`), and
  local-dev publishes `127.0.0.1:4402` (`compose.local.yml:37`). The gateway container
  carries no `prometheus.io/*` labels.
- Access log: `frontendPolicies.accessLog.add.identity: apiKey.name` (lines 85-87). When
  `agw_otlp_host` is set, `accessLog.otlp` goes to that host with
  `fields.add: {service: '"agentgateway"', identity: apiKey.name}` (lines 88-97). No
  `database` block is rendered.
- Tracing: under the same condition, `frontendPolicies.tracing` goes to the same host,
  with `randomSampling` and `clientSampling` both set to `agw_trace_sampling` (default
  `0.05`) and `resources.service.name: '"agentgateway"'` (lines 98-107).
- Keys: each `apiKey` entry carries `metadata.name` only (lines 142-143). The UI's
  authorization is the `platform-admins in jwt.groups` rule (lines 76-78). The admin
  listener is pinned to the container loopback (line 33).

The gateway deploy, `platform/playbooks/deploy-agentgateway.yml`:

- Lines 103-112: when `agw_otlp_host` is set, the deploy requires exactly one
  `o11y_svc` host and requires the host to equal that receiver's
  `o11y_otlp_bind:o11y_otlp_port` (line 108). It also requires `agw_trace_sampling` in
  (0, 0.1] (lines 109-110).
- Lines 114-116: under the same condition, it includes `tasks/assert-o11y-trace-rollout.yml`.
  Lines 118-132 require Tempo's `/ready` through the receiver's Grafana container, and
  lines 134-142 require a TCP connection to the declared OTLP listener.
- Lines 279-316: the receiver-side scrape receipt. It is also gated on `agw_otlp_host`
  (lines 288 and 316), even though it proves metrics, not OTLP.
- Lines 341-429: the keyed probes (`GET /v1/models`, then one chat completion) are
  `uri` calls with `no_log`, so the key never reaches any process's argv.

The o11y side:

- `config/config.alloy:108-116`: `otelcol.receiver.otlp "traces"` on gRPC
  `0.0.0.0:4317`, with no `tls` block. It sends logs through an attributes processor
  that inserts `loki.attribute.labels = "service"` (lines 118-127) into
  `otelcol.exporter.loki` and on to the existing Loki writer (lines 129-131). Spans go
  through `otelcol.processor.batch` into `otelcol.exporter.otlp` at `tempo:4317` with
  `tls { insecure = true }` (lines 133-146). That last hop stays inside the compose
  network.
- `config.alloy:10-37`: container stdout is collected over the engine socket, and the
  `service` label is copied from the container name (lines 26-30). Only the local
  overlay mounts the socket (`compose.local.yml:32-35`).
- `config.alloy:44` and `config/prometheus.yml:8`: `cluster = "agent-cloud-local"` is
  hard-coded for every environment.
- `compose.yml:81`: Alloy publishes `${O11Y_OTLP_BIND:-127.0.0.1}:${O11Y_OTLP_PORT:-4317}`
  (`templates/env.j2:23-24`). `deploy-o11y.yml:164-175` refuses, outside local-dev, an
  OTLP bind that is not a non-loopback IPv4 address equal to the gateway's
  `agw_otlp_host`.
- `compose.yml:92-102`: Tempo `2.10.8`, single node, local storage, retention
  `O11Y_TEMPO_RETENTION` defaulting to `168h` (`config/tempo-config.yml:18-20`,
  `templates/env.j2:29`). No port is published from Tempo.
- `config/grafana/provisioning/datasources/datasources.yml:21-27` (PR #301): the Loki
  datasource carries a `TraceID` derived field whose matcher is
  `"traceid":"([0-9a-f]{32})"` and whose target is datasource uid `tempo`. The o11y
  deploy reads the live Loki datasource and requires exactly one such field
  (`deploy-o11y.yml:349-372`). The Tempo datasource and its trace-to-metric and
  trace-to-log mappings are at `datasources.yml:29-46`, read back live at
  `deploy-o11y.yml:321-347`. unverified: whether the gateway's OTLP access records reach
  Loki in a form that matcher finds; task 4.2 reads one real record first.
- `config/prometheus.yml:27-33` scrapes `alloy:12345` and `tempo:3200`. The
  self-monitoring dashboard charts `tempo_distributor_spans_received_total{job="tempo"}`
  (`o11y-self-monitoring.json:95`), and the deploy asserts that panel
  (`deploy-o11y.yml:471-476`).
- `agentgateway-traffic.json`, uid `agentgateway-traffic`, has eight Prometheus panels:
  scrape up, config synchronized, requests per second, requests by status, server error
  ratio, request latency p95, tokens per second, and first-token p95. It has no template
  variables and no Loki or Tempo panel. `deploy-o11y.yml:501-502` requires exactly eight
  panels.
- `tasks/assert-o11y-trace-rollout.yml:9-29` requires, on the single receiver:
  `o11y_trace_rollout_enabled`, `o11y_alerts_enabled`, four numeric Semaphore receipt
  IDs, explicit Prometheus, Loki and Tempo retentions, a scrape sample limit, and a
  declared `agentgateway_metrics_address`.
- `verify-o11y-service.yml:41-68`: the canary sends 100 anonymous `GET /v1/models`
  requests expecting 401, straight to the gateway listener, and marks its start on the
  receiver's clock. Lines 76-100 require any `{service="<name>"}` Loki line from the last
  15 minutes. Lines 106-132 require a Tempo trace for `service.name` since the start
  mark. The canary requires `expect_traces` (line 37), so it cannot serve as a logs-only
  receipt.
- `platform/services/o11y/deployment/README.md:114-115` records that a production
  click-through opened an agentgateway trace's same-span Loki log. unverified: the
  current production values of `agw_otlp_host` and the trace switches (task 1.1 reads
  them, names only, through Semaphore).

Still absent: per-port firewall sources. `apply-firewall.yml` allows every detected port
from every `firewall_upstream_source` entry, as the product of the two lists (lines
265-268, 290 and 306).

### agentgateway v1.5.0 facts this design relies on

Carried from the 2026-09-27 reading. Every row cites the v1.5.0 source.

- `otlp.fields` replaces the parent `add` list instead of extending it ("If unset, the
  parent access log fields are used", `schema/config.md:18224`). So any field that
  both stdout and OTLP need must appear in both lists.
- `frontendPolicies.tracing.clientSampling` defaults to `true`, which continues any
  trace a caller started (`schema/config.md:78-79`; documentation page
  `/documentation/observability/traces/setup/`).
- The request-log store falls back to `config.database` (`crates/agentgateway/src/config.rs:387`).
  Its schema has `request_log_payloads` with `request_prompt_json` and
  `response_completion_json`, which reference `request_logs(id) ON DELETE CASCADE`
  (`telemetry/log_store/postgres_migrations/0001_create_request_log_schema.sql:25-29`).
- `frontendPolicies.accessLog.database.llm` takes `metadata` or `full`. When it is
  omitted, "legacy behavior is preserved: content captured by CEL expressions is also
  stored in the payload" (`schema/config.md:18230`; `log.rs:68-133`, `:1128-1131`). The
  mode is process-wide: every `accessLog` schema row sits under `frontendPolicies`, and
  no attachable policy kind carries an access-log policy (`schema/config.md:18805`
  onward).
- `full` mode stores normalised input messages, the completion text and tool calls
  (`log.rs:76-126`) without any CEL expression.
- A CEL field that evaluates to null is dropped from the record (`log.rs:555`).
- The UI reads a payload through `/api/logs/get` with `includePayload: true`
  (`ui/src/api/logsApi.ts:19-21`). The log API paths belong to the UI route, which
  carries `ui.policies` (`types/local.rs:2060-2070`, `:3968-3975`). The admin listener
  serves the same router without that login (`management/admin.rs:185-200`).
- The UI's "Include prompts and completions in logs" toggle rewrites the gateway config
  (`ui/src/pages/Logs.tsx:466-480`). The config is mounted read-only, and
  `UI_READ_ONLY=true` makes the store refuse writes (`config.rs:390-392`;
  `inference-gateway-agentgateway` task 1.10).
- Nothing in the v1.5.0 log store prunes rows (a grep for
  `retention|prune|delete|ttl|cleanup` over `telemetry/log_store.rs` and
  `telemetry/log_store/postgres.rs` returns nothing).
- `llm.policies.apiKey.keys[].metadata` is typed `any` (`schema/config.md:74083`) and is
  flattened onto the CEL `apiKey` object (`config.yaml.j2:11-13`).
- `statsAddr` takes an address only and has no TLS option (`schema/config.md:59`).
- `accessLog.otlp.policies.backendTLS` and `tracing.policies.backendTLS` take `cert`,
  `key`, `root` and `hostname` (`schema/config.md:17974-17977`, `:18548-18551`).

Alloy `v1.5.1` facts:

- `otelcol.exporter.loki` turns attributes into Loki labels only when they are named in
  the `loki.attribute.labels` or `loki.resource.labels` hint (`otelcol.exporter.loki.md`).
- The receiver's server `tls` block takes `client_ca_file`, which requires and verifies
  a client certificate. No argument matches a client certificate's name
  (`internal/component/otelcol/config_tls.go:14-18`, `57-67` at tag `v1.5.1`).

## Ownership

Per the handoff of 2026-09-28, the o11y session owns the o11y stack, including Tempo.
This change owns the gateway side and lists the o11y items as dependencies.

| Item | Owner |
|---|---|
| Splitting the OTLP switch in the gateway template and deploy | This change |
| The receiver gate condition at `deploy-o11y.yml:124-128`, which reads the gateway's variable | This change; the o11y session reviews it |
| Sampling, `clientSampling`, the `team` metadata, the `signal` field, `database.llm` | This change |
| Render guards, the BATS template scan, the gateway's Loki receipt | This change |
| The request-log prune and its schedule | This change |
| Stats bind, gateway scrape labels, per-port firewall sources | This change |
| The gateway's OTLP client leaf and `backendTLS` | This change |
| Tempo, its retention, its datasource, the Alloy pipelines | o11y session (landed) |
| The trace rollout gate and the trace canary | o11y session (landed); reused here without changes |
| The receiver's mutual-TLS block and server leaf | o11y session |
| Adding `signal` to the Loki label hint | o11y session |
| The `cluster` label read from the environment (`config.alloy:44`, `prometheus.yml:8`) | o11y session |
| Operations dashboard (rename and extend `agentgateway-traffic`, update the eight-panel assert) | o11y session |
| The `agentgateway-client-view` dashboard | o11y session |
| The trace-silence alert rule and the prune-staleness alert rule | o11y session |
| Any extension of `verify-o11y-service.yml` | o11y session |

## Goals / Non-Goals

**Goals:**
- Access records can ship without the trace receipts, and traces keep every landed gate.
- The team's prompt and completion content is kept in the gateway's own store only,
  for team identities only, for 90 days.
- No request content, header or key reaches any metric, span, OTLP record or Loki line.
  This is enforced by a template scan and by render-time refusals, not by convention.
- Two dashboards: operations and client view.
- Local-dev and production share one code path and differ only in inventory.

**Non-Goals:**
- Tempo itself, its storage, its retention and its datasource: owned by the o11y session
  and already landed.
- A second trace gate: `tasks/assert-o11y-trace-rollout.yml` is the gate.
- A runtime team rule at the gateway (decision 5).
- The cost catalog (`agentgateway_gen_ai_client_cost_usd_total`). vLLM on our own
  hardware has no per-token price to declare.
- Shipping the gateway VM's non-access stdout to Loki in production.

## Decisions

1. **Split the OTLP switch; `agw_otlp_host` stays the receiver address.** Two booleans,
   both defaulting to `false`, and both requiring `agw_otlp_host`:
   - `agw_otlp_logs` renders `accessLog.otlp`. It keeps the landed receiver checks: one
     `o11y_svc` host, the host equal to its bind and port, and a reachable port
     (`deploy-agentgateway.yml:103-108`, `:134-142`). It needs no trace receipt.
   - `agw_otlp_traces` renders `frontendPolicies.tracing`. It alone brings in the
     sampling guard (lines 109-110), the rollout gate (lines 114-116) and the Tempo
     readiness check (lines 118-132).
   - A declared `agw_otlp_host` with neither switch is refused, and the refusal names
     both switches. The production inventory relies on the host alone today, so without
     the refusal a deploy that shipped before the site-config change would silently stop
     exporting. site-config sets both switches in the same pull request that lands the
     code.
   - The scrape receipt (lines 279-316) keys on the receiver declaring
     `agentgateway_metrics_address`, which is the fact it proves.
   - The receiver's gate condition (`deploy-o11y.yml:124-128`) reads the gateway's
     `agw_otlp_traces` instead of `agw_otlp_host`. The private-bind guard
     (`deploy-o11y.yml:164-175`) keeps reading `agw_otlp_host`, because it guards the
     transport of both signals.

   Alternatives rejected: keeping `agw_otlp_host` as the single switch, because it
   holds access records hostage to trace receipts; making the host alone mean "logs" and
   adding only a trace switch, because the host would then be an address in one reading
   and a switch in another; a trace switch that defaults to on, because a new
   environment would then pull in the trace gate without asking for traces.

2. **Sampling at 10 percent, client-started traces not continued** (Joe, 2026-09-27).
   `randomSampling` defaults to `0.1`, the upper edge of the landed (0, 0.1] guard, and
   `clientSampling` renders the literal `false`. With `clientSampling` left true, any
   caller could force its requests into the trace store by sending a `traceparent`
   header, which would make trace volume a quantity the client controls. At the default
   120-per-minute request ceiling (`config.yaml.j2:175-179`), 10 percent is at most
   17,280 traced requests per day.

3. **Identity, team and signal on access records, in both lists.** `accessLog.add`
   carries `identity: apiKey.name` and `team: apiKey.team`. `accessLog.otlp.fields.add`
   repeats both, because it replaces the parent list, and adds
   `service: '"agentgateway"'` (landed) and `signal: '"access-log"'`. `signal` goes in
   the OTLP list only. Stdout lines therefore never carry it, which matters in local-dev:
   there, the docker-socket source also labels the gateway's stdout
   `service="agentgateway"` (`config.alloy:26-30`), so `{service="agentgateway"}`
   matches stdout as well as OTLP records. The o11y session adds `signal` to the label
   hint (`config.alloy:118-123`), and every query that proves an OTLP record selects on
   `signal="access-log"`. Local duplication is accepted and noted, not removed. It
   costs only local Loki volume, production has no socket mount, and dropping the
   gateway from the socket source would need a templated Alloy file for one container.
   Alternative rejected: `signal` in the parent list, because a stdout line would then
   satisfy the OTLP receipt locally.

4. **Team content is kept only in the gateway's own store.** When inventory sets
   `agw_content_logging: full`, the template renders
   `frontendPolicies.accessLog.database: {llm: full}`. Otherwise it renders
   `{llm: metadata}`, never an omitted mode, because omission is v1.5.0's legacy
   capture. Rows land in the gateway's Postgres and are read through the UI Logs page,
   behind the Authentik login and the `platform-admins` rule (`config.yaml.j2:63-78`).
   The admin listener serves the same log API without that login, inside the container
   only; decision 5 and Risks record why that is accepted.
   Metrics, spans, OTLP records and Loki stay metadata-only, because Grafana has no
   per-record access control. Team members are told in their key handout that prompts
   and completions are kept for 90 days and are readable by platform admins.
   Alternatives rejected: content in Loki or OTLP, because every Grafana viewer would
   read every prompt; an identity-conditional CEL field that selects whose content is
   stored, because the payload comes from the captured request, not from the value the
   expression returns (`log.rs:68-90`).

5. **Team membership is declared on every identity and enforced at render only.** Each
   `apiKey` entry renders `metadata: {name: <identity>, team: <team>}`. Inventory
   identities take `team` from `agw_client_policies.<name>.team`, with no default.
   Personal keys render `team: uhstray`, because their eligibility group
   `inference-users` is the team (`inference-personal-keys` task 4.2). While content
   is `full`, the deploy refuses to render any identity whose team is not `uhstray`, or
   that declares no team. Why no runtime rule in `llm.policies.authorization`: every key
   the gateway accepts comes from this one render (`config.yaml.j2:131-165`), and the
   UI cannot write it. That claim rests on three explicit controls, each enforced in
   code, not assumed:
   - **`UI_READ_ONLY=true` is a prerequisite for `full`.** v1.5.0 forces the config store
     to read-only when the variable is set (`crates/agentgateway/src/config.rs:390-392`),
     and every UI write handler then answers 403 (`ui.rs:52-60`, called at `:316`,
     `:402`, `:451`, `:603` and `:673`; read 2026-09-28). Without it, the store is in its
     default `File` mode (`lib.rs:442-450`) and a write fails only at the read-only
     mount. The same UI router is also served on the admin listener with no login
     (`management/admin.rs:199-205`), so the store's own refusal covers that
     unauthenticated copy too, instead of relying only on the mount failing the write.
     unverified: whether a write in `File` mode changes the running state before the
     file write fails; the refusal makes the question moot. `inference-gateway-agentgateway` task 1.10 sets the variable
     (PR #303, open against `dev` on 2026-09-28; this tree at `f92b0bf` does not carry
     it yet). The render guards (task 2.5) refuse `agw_content_logging: full` unless the
     rendered `.env` sets `UI_READ_ONLY=true`, and the site-config switch (task 2.12)
     waits on it.
   - **The config mount stays read-only and holds only `config.yaml`.** Today the file
     is mounted alone, read-only (`compose.yml:48`). If the gateway change's hot-reload
     drill (its task 1.12) moves the config to a directory mount, that directory is
     mounted `:ro` and contains `config.yaml` and nothing else. It is never the deploy
     directory, which holds the 0600 `.env` carrying the database URL with its password
     (`templates/env.j2:26`) and the upstream key (line 32). A BATS assertion on
     `compose.yml` holds both properties (task 2.7).
   - **The admin listener's unauthenticated log API is an accepted risk** (Risks). It
     stays pinned to the container loopback (`config.yaml.j2:33`) and unpublished.

   A runtime rule would guard against
   a path that does not exist, and it would add an unverified evaluation-order question
   about whether `apiKey.team` is populated when authorization runs. **Future users
   outside the team** get a second gateway instance in `metadata` mode, because the mode
   is process-wide at v1.5.0. That is recorded here and not built.

6. **The forbidden-reference rule is a static scan; runtime guards cover only what
   depends on inventory.** The template is the only place a CEL telemetry expression
   can come from, so a BATS scan of `config.yaml.j2` is sufficient and cheaper than a
   render-time CEL walk. It refuses `llm.prompt`, `llm.completion`, `request.headers`,
   `request.body`, and any `apiKey.` member other than `name` and `team`, in any value
   position. It extends the existing key-position scan
   (`platform/tests/test_service_agentgateway.bats:145-152`). The deploy keeps
   render-time refusals only for facts that come from inventory: a missing `team`, a
   non-team identity under `full`, the switch combinations (decision 1), and plaintext
   against a TLS-declared receiver (decision 9).

7. **Content expires after 90 days; a stopped prune raises an alert, not a deploy
   failure** (Joe, 2026-09-27: "Let's keep prompts for 90 days"). The playbook
   `prune-agentgateway-request-logs.yml` deletes `request_logs` rows whose
   `completed_at` is older than `agw_request_log_retention_days` (default 90), inside
   `agentgateway-db` through the container engine. Payload rows go with them by cascade.
   It runs daily from a `schedule:` in `platform/semaphore/templates.yml` (the form used
   at `templates.yml:446-449`), prints counts only, and a second run deletes nothing.
   It records its result with `tasks/emit-step-result.yml`, including the deleted count
   and the oldest remaining row's age in days. The o11y session's alert rule fires when
   no prune result arrived in 26 hours, or when the oldest age exceeds the retention
   plus two days. The alert reads that result from Loki: the conformance collector
   writes one Loki line per step result (`collect-service-conformance.yml:22-23`).
   unverified: whether the collector reads a template outside the service-onboarding
   registry (task 3.3 settles it; if it does not, the prune pushes the same line to Loki
   from the receiver host). Why not a gateway-deploy gate: the gateway deploy is the
   path that rotates and revokes keys (`manage-agentgateway-client-key.yml:145`
   imports it), so a stale prune must never block a revocation. Any future backup of
   this database excludes `request_log_payloads` data.

8. **Verification: each deploy proves its own signal, and no o11y verify holds a gateway
   key.**
   - The gateway deploy, when `agw_otlp_logs` is on, marks a time on the receiver clock
     (the pattern at `deploy-agentgateway.yml:279-283`) before its keyed probe. After
     the probe, it queries Loki from the receiver
     (`exec o11y-grafana wget … loki:3100/loki/api/v1/query_range`) for
     `{service="agentgateway", signal="access-log"}` carrying the verifying identity at
     or after the mark, within a bounded retry. The query carries no key, so it is not
     `no_log`. The probe used is keyed `GET /v1/models` (lines 362-375) if that record
     carries `identity`, otherwise the chat completion (lines 409-429). unverified:
     whether a `/v1/models` record carries `apiKey.name`; task 4.2 reads one real
     record first.
   - The o11y side proves the trace path with the landed canary
     (`verify-o11y-service.yml:41-68`, `:106-132`): 100 anonymous requests, sampled at
     0.1, all miss with probability 0.9^100, about 2.7e-5. The o11y verify never
     shared-reads a gateway client key and never sends keyed inference.
   - Standing trace signal (o11y session): an alert when
     `rate(tempo_distributor_spans_received_total{job="tempo"}[30m])` is zero while
     `increase(agentgateway_requests_total{job="agentgateway"}[30m]) >= 100` and traces
     are on. The 100-request floor keeps the false-alarm probability at a healthy 0.1
     sample rate at the same 2.7e-5. The counter is already scraped
     (`prometheus.yml:31-33`), so no new scrape is needed. unverified: whether any other
     service sends spans to this Tempo; if one does, the rule needs a gateway-specific
     series, which the o11y session picks.

9. **Transport: the landed plaintext private bind is the interim; mutual TLS is an
   upgrade of that bind.** Plaintext is permitted in two cases: in local-dev, and outside
   local-dev only on the landed private, non-loopback bind behind the host firewall
   (`deploy-o11y.yml:164-175`), until `production-internal-ca` can issue both leaves.
   When the o11y session turns on the receiver's `tls` block with `client_ca_file`, the
   gateway follows the receiver's declaration, which it reads from the receiver's
   hostvars the same way it reads `o11y_otlp_bind` today (`deploy-agentgateway.yml:108`).
   It then renders `backendTLS.{root, cert, key, hostname}` on both exporters and
   refuses to render plaintext against a TLS-declared receiver. unverified: the name of
   the receiver's TLS declaration, which the o11y session picks (task 6.1 records it).
   The receiver cannot check the client certificate's name at Alloy `v1.5.1` (Context),
   so the firewall rule admitting only the gateway host to the OTLP port is what binds
   the sender. That is accepted: the receiver serves nothing back, so a forged sender can
   add records but cannot read any. Alternative rejected: refusing all production
   plaintext now, because the o11y README records a production trace on this path
   (`README.md:114-115`) and the leaves do not exist yet.

10. **Metrics are pulled from a LAN-bound stats listener, with per-port firewall
    sources.** The scrape template, the receiver guard and the probe stay as landed
    (`deploy-o11y.yml:142-162`, `:220-233`). The gateway's `agw_stats_bind` becomes its
    LAN address, in the order the o11y README gives (`README.md:28-34`). The stats
    listener takes no TLS (`schema/config.md:59`), so the hop is plaintext HTTP with
    aggregate series only. `apply-firewall.yml` gains an optional
    `firewall_detected_port_sources` map (published port to a list of sources), which
    replaces `firewall_upstream_source` for the ports it names. The gateway names its
    stats port with the o11y host as the only source, and the o11y host names its OTLP
    port with the gateway host as the only source. In local-dev, the gateway container
    carries `prometheus.io/scrape`, `prometheus.io/port: "19002"` and
    `prometheus.io/path: /metrics`, so Alloy's discovery scrapes it
    (`config.alloy:49-100`). Those labels are inert in production, where no Alloy sees
    the gateway's engine. Alternative rejected: adding the o11y host to
    `firewall_upstream_source`, because that also opens the API and UI ports to it.

11. **Two dashboards, both provisioned by the o11y session** (Joe: "The gateways
    client-view should be a separate dashboard", and later: two dashboards).
    - Operations: `agentgateway-traffic` is extended and renamed to "Agentgateway
      operations". This change asks to keep the uid, so links and the landed assert keep
      resolving; the o11y session makes that call. The dashboard gains tokens by identity, model and
      `gen_ai_token_type`, a raw 4xx access-record panel from Loki using
      `{service="agentgateway", signal="access-log"}`, requests with HTTP status 429,
      `agentgateway_build_info` and a Tempo search on uid
      `tempo`. The panel-count assert (`deploy-o11y.yml:502`) moves to the new count.
      The panel does not yet group rejections by reason: gateway tasks 4.2/4.3 must
      capture an OTLP record and verify its reason field and Loki line shape first.
      Task 7.1 remains partially open until that evidence supports a reason query.
      The previously proposed `agentgateway_requests_shed_total` is not listed in the
      gateway 1.5 metric reference, and no source definition has been verified. The
      rate-limit panel uses the documented status breakdown of
      `agentgateway_requests_total`, aggregated across matching identities. A live 429
      receipt is still required before claiming that this panel captures rate-limit events.
    - Client view: `agentgateway-client-view.json`, uid `agentgateway-client-view`, with
      p50 and p95 first-token latency from
      `agentgateway_gen_ai_server_time_to_first_token_bucket`, request duration, the 4xx
      and 5xx ratio, and per-identity request rate. This is the dashboard
      `inference-gateway-agentgateway`'s scenario "Client-view latency on the dashboard"
      is proved against.
    - Both take an `identity` variable from `label_values(agentgateway_requests_total,
      identity)`, and neither queries a Kubernetes label.
    - Acceptance uses the normal Semaphore o11y deploy after traffic has been retained.
      Read back both dashboards and telemetry from before and after the deploy. A clean
      deploy or volume wipe is not part of this change; it needs a separate explicit
      request. This proves configuration reconciliation and data preservation, not
      recovery from lost volumes.

## Risks / Trade-offs

- [Prompts with pasted secrets sit in the gateway database for up to 90 days, readable
  by platform admins and by anyone holding the database password] → the prune and its
  alert (decision 7), admin-only UI access and the handout notice (decision 4). A secret
  found in a prompt is rotated, and a zero-day prune run purges the rows at once.
- [With `full`, the admin listener serves team prompts and completions without a login]
  → accepted. The admin listener merges the whole UI router with no `ui.policies`
  (`management/admin.rs:185-205` at v1.5.0), and that router carries the log API
  (`ui.rs:106-108`: `/api/logs/search`, `/api/logs/get`, `/api/logs/tail`). So any
  process inside the gateway container's network namespace can read every stored
  payload from `127.0.0.1:15000` with no Authentik login. Why this is accepted: the
  listener is bound to the container loopback (`config.yaml.j2:33`) and has no
  published port (`compose.yml:43-46`), so reaching it takes code execution inside the
  gateway container or the host account that runs its container engine. Both already
  reach the same rows without the listener: the container's environment is loaded from
  the 0600 `.env` (`compose.yml:40-41`), which carries the database URL with its
  password (`templates/env.j2:26`), and `agentgateway-db` answers on the compose
  network. The listener adds no reader who could not already query
  `request_log_payloads` directly. Alternative rejected: an image built without the
  `ui` feature, the only switch at `admin.rs:199-208` that keeps the UI router off the
  admin listener, because it also removes the authenticated operator UI. unverified:
  whether v1.5.0 can run with no admin listener at all; task 2.10 settles it, and if it
  can while the UI listener keeps working, that replaces this acceptance. The gateway
  change's task 1.6 asserts that the admin port is never published.
- [The prod inventory relies on `agw_otlp_host` alone] → the neither-switch refusal
  (decision 1) turns a missed site-config change into a named failure instead of silent
  loss.
- [Content rows and budget rows share one Postgres] → task 9.1 measures the table size
  per day. A separate `config.logging.database` is the fallback (`config.rs:374-387`).
- [Identity label cardinality grows with personal keys] → it is bounded by the number
  of enrolled people and agents, and the landed sample limit applies
  (`config.alloy:98`).
- [A plaintext OTLP hop on the LAN until leaves exist] → the private-bind guard, the
  per-port firewall rule, and the fact that records carry names and counts only.
- [Local stdout duplicates the access record in Loki] → the OTLP-only `signal` field
  (decision 3).

## Migration Plan

1. The switch split lands together with the site-config pull request that sets
   `agw_otlp_logs` and `agw_otlp_traces` where `agw_otlp_host` is declared today.
2. Sampling, team markers, the `signal` field and the template scan, proven in local-dev.
   Team markers are declared for every identity before `agw_content_logging: full`, and
   the prune schedule runs before the first production row with content exists.
3. Metrics in production: stats bind, firewall, probe, declaration.
4. o11y-side dependencies, as the o11y session delivers them: the label hint, the
   dashboards, the alerts, and receiver TLS.
5. Mutual TLS once `production-internal-ca` issues both leaves.

Rollback is in `proposal.md`.

## Open Questions

None open for Joe. The dependencies recorded as unverified are resolved by tasks that
name them: the `/v1/models` record fields (task 4.2), the collector's reach (task 3.3),
the receiver TLS declaration's name (task 6.1), and other Tempo producers (task 8.2).
