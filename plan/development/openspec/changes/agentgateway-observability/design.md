# Design: agentgateway observability

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

## Context

See `proposal.md` for motivation. Verified 2026-09-27 from the repository, the
agentgateway source at tag `v1.5.0` (commit `fe6732474a96a0363dfb9822859af4e9bab360fa`,
read with `gh api repos/agentgateway/agentgateway/contents/<path>?ref=v1.5.0`), and the
upstream documentation, which tracks `main` rather than the release
(`https://agentgateway.dev/docs/standalone/latest/...`, fetched as Markdown by appending
`.md`). Where the documentation and v1.5.0 disagree, v1.5.0 wins and the difference is
recorded.

### What the gateway already renders

- Metrics labels: `config.metrics.fields.add.identity: apiKey.name`
  (`platform/services/agentgateway/deployment/templates/config.yaml.j2:44-47`).
- Access-log field: `config.logging.fields.add.identity: apiKey.name` (lines 53-56).
- Tracing, only when inventory sets `agw_otlp_endpoint`: `config.tracing.otlpEndpoint`
  and `randomSampling` defaulting to `0.05` (lines 58-64). No inventory sets it today:
  the only other reference is the playbook header comment
  (`platform/playbooks/deploy-agentgateway.yml:29`).
- Listeners: `statsAddr: 0.0.0.0:19002` inside the container (line 35); compose publishes
  it at `${AGW_STATS_BIND:-127.0.0.1}:${AGW_STATS_PORT:-19002}`
  (`platform/services/agentgateway/deployment/compose.yml:45`), and `env.j2:13` defaults
  the bind to loopback. Local-dev replaces the publish with `127.0.0.1:4402:19002`
  (`compose.local.yml:34-37`).
- `config.database.url: $AGW_DATABASE_URL` for per-key budgets (`config.yaml.j2:39-40`).
- The stats listener serves Prometheus text on `/metrics`, recorded on the running v1.5.0
  container on 2026-09-17 (`platform/services/agentgateway/context/architecture.md:85-91`);
  the source routes `/metrics` and `/stats/prometheus` to the handler
  (`crates/agentgateway/src/management/metrics_server.rs:39`).

### What the o11y stack already does

- Four containers: Prometheus, Loki, Alloy, Grafana
  (`platform/services/o11y/deployment/compose.yml:15-113`). Alloy publishes no port
  (lines 60-84). Prometheus accepts remote write (line 25).
- Alloy discovers containers over the engine socket, ships their stdout to Loki with
  `container` and `service` labels, and scrapes containers that carry
  `prometheus.io/scrape=true` plus a `prometheus.io/port` label, forwarding samples by
  remote write (`config/config.alloy:11-107`). The socket is mounted only by the local
  overlay (`compose.local.yml:32-35`), so in production Alloy sees no gateway container.
- The Loki writer stamps `cluster = "agent-cloud-local"` as an external label
  (`config.alloy:44-46`), hard-coded for every environment.
- The OTLP receiver is deliberately absent: "an OTLP receiver with no output errors at
  boot" (`config.alloy:7-9`).
- Prometheus loads `scrape.d/*.yml` (`config/prometheus.yml:10-11`). The gateway scrape
  template exists: job `agentgateway`, path `/metrics`, labels `{service: agentgateway,
  component: gateway, env: prod}` (`templates/scrape-agentgateway.yml.j2:1-7`).
  `deploy-o11y.yml` renders it only when `agentgateway_metrics_address` is declared, and
  refuses a target that differs from the gateway's declared `agw_stats_bind` and
  `agw_stats_port` (`platform/playbooks/deploy-o11y.yml:122-142`, `:187-200`).
  `probe-o11y-metrics-endpoint.yml:54-74` already probes that endpoint from the receiver.
  The o11y README records the order: bind the stats listener to the LAN, firewall it to
  the receiver, probe, then declare (`platform/services/o11y/deployment/README.md:28-34`).
- Grafana datasources are Prometheus (`uid: prometheus`) and Loki (`uid: loki`) only
  (`config/grafana/provisioning/datasources/datasources.yml:6-20`); dashboards load from
  committed JSON with UI edits blocked (`provisioning/dashboards/dashboards.yml:5-17`).
- The generic service-down rule already fires for any `up{service!=""}` target
  (`templates/alerts.yml.j2:12-23`), so a declared gateway scrape is covered by it.
- Alloy `run` loads every `*.alloy` file in a directory as one configuration (Alloy
  v1.5.1 `docs/sources/reference/cli/run.md:26`); the pinned image is
  `grafana/alloy:v1.5.1` (`compose.yml:61`).

### What agentgateway v1.5.0 offers

Metrics:
- `config.statsAddr` takes an address only — "ip:port", "localhost:port",
  "unix:/path/to/socket", or "off" (`schema/config.md:59`); no TLS option exists for it.
  The compiled default is port 15020 (`crates/agentgateway/src/config.rs:328-334`), the
  port the documentation uses throughout ("Agentgateway exposes a Prometheus-compatible
  metrics endpoint on port 15020", `/documentation/observability/metrics/overview/`); this
  platform pins 19002.
- `config.metrics.fields.add` ("Map of field name to a CEL expression that computes the
  value to add to metrics") and `config.metrics.remove` ("Metric names to exclude from
  collection") (`schema/config.md:91-94`). These are not deprecated.
- Custom fields attach to the HTTP and gen-AI label sets (`telemetry/metrics.rs:103`,
  `:117`); gen-AI labels are `gen_ai_operation_name`, `gen_ai_system`,
  `gen_ai_request_model`, `gen_ai_response_model` (`metrics.rs:107-111`) plus
  `gen_ai_token_type` on token usage (`metrics.rs:121-122`); HTTP labels include
  `status` and `reason` (`metrics.rs:96-97`).
- Metric names at v1.5.0 (`schema/metrics.md`): `agentgateway_requests_total`,
  `agentgateway_request_duration_seconds`, `agentgateway_requests_shed_total`,
  `agentgateway_upstream_call_duration_seconds`, `agentgateway_gen_ai_client_token_usage`,
  `agentgateway_gen_ai_server_request_duration`,
  `agentgateway_gen_ai_server_time_to_first_token`,
  `agentgateway_gen_ai_server_time_per_output_token`, `agentgateway_build_info`,
  `agentgateway_config_synchronized`. The "latest" metrics reference omits
  `agentgateway_requests_shed_total`, which v1.5.0 has; the counters "only appear in the
  output after the first request of that type"
  (`/documentation/observability/metrics/reference/`).

Traces:
- `config.tracing` is the deprecated form. The loader turns it into
  `frontendPolicies.tracing`, refuses to use both ("cannot use deprecated config.tracing
  together with frontendPolicies.tracing"), sets `resources` and `filter` to nothing
  ("Not supported in the old config"), and attaches only a default TLS policy when the
  endpoint scheme is `https` (`crates/agentgateway/src/types/local.rs:225-282`).
- `frontendPolicies.tracing` at v1.5.0 has `host`, `policies.backendTLS.{cert, key, root,
  hostname}`, `attributes`, `resources`, `remove`, `randomSampling`, `clientSampling`,
  `filter`, `path` and `protocol`; `protocol` "Defaults to HTTP", possible values `grpc`,
  `http` (`schema/config.md:18521-18551`, `:18797-18804`). The deprecated
  `config.tracing.otlpProtocol` defaults to gRPC (`crates/agentgateway/src/telemetry/trc.rs:141-145`).
- Sampling: `randomSampling` "Defaults to `false` (no new traces initiated)";
  `clientSampling` defaults to `true`, so "agentgateway continues a trace that a client
  already started even when `randomSampling` is `false`"
  (`/documentation/observability/traces/setup/`; same defaults in `schema/config.md:78-79`).
- `service.name` "defaults to `agentgateway` if not set" (traces setup page).
- Default span attributes include `gateway`, `route`, `http.method`, `http.path`,
  `http.status`, `src.addr`, `duration` and, for LLM traffic, `gen_ai.operation.name`,
  `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`,
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`
  (`/documentation/observability/traces/attribute-reference/`). No header and no message
  content is a default attribute.

Access logs:
- `config.logging.fields` and `config.logging.filter` are the deprecated form; the loader
  moves them into `frontendPolicies.accessLog` and refuses to combine them ("cannot use
  deprecated config.logging together with frontendPolicies.accessLog")
  (`types/local.rs:200-223`). `config.logging.format` and `level` are not part of that
  check (`local.rs:200-202`).
- `frontendPolicies.accessLog` has `filter`, `add`, `remove`, `otlp` (`host`, `policies`
  including `backendTLS.{cert, key, root, hostname}`, `filter`, `fields`, `protocol`,
  `path`) and `database.llm` (`schema/config.md:17943-17977`, `:18223-18231`).
  `otlp.fields` is "OTLP-specific access log fields. If unset, the parent access log
  fields are used" (`:18224`), so an OTLP field list replaces, rather than extends, the
  parent `add`.
- Default access-log fields (`crates/agentgateway/src/telemetry/log.rs`): `gateway`,
  `route`, `endpoint`, `src.addr`, `http.method`, `http.host`, `http.path`,
  `http.version`, `trace.id`, `span.id`, `jwt.sub`, `protocol` (lines 1493-1535),
  `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`,
  `gen_ai.response.model` (1614-1632), `gen_ai.usage.input_tokens`,
  `gen_ai.usage.output_tokens`, `gen_ai.usage.reasoning_tokens` (1637-1661),
  `agw.ai.time_to_first_token` (1668), and `error`, `reason`, `duration` (1772-1774).
  No request header is a default field.
- "Log export happens in addition to stdout output"
  (`/documentation/observability/access-logs/export/`).
- Request-log database: `config.logging.database` falls back to `config.database`
  (`config.rs:387`), so the budgets database is also the request-log store. Its schema
  has a `request_log_payloads` table with `request_prompt_json` and
  `response_completion_json` (`postgres_migrations/0001_create_request_log_schema.sql:25-29`).
  `frontendPolicies.accessLog.database.llm`: "`metadata` stores request metadata, usage,
  timing, and cost without prompt or completion content ... When omitted, legacy behavior
  is preserved: content captured by CEL expressions is also stored in the payload"
  (`schema/config.md:18230`; code at `log.rs:68-133`, `:1128-1131`).
- The UI's Logs settings drawer offers "Include prompts and completions in logs",
  described as "Stores prompt and completion content in the database payload", and saves
  it by rewriting the gateway configuration (`ui/src/pages/Logs.tsx:466-480`, `:536-548`).
  `UI_READ_ONLY=true` forces the config store read-only (`config.rs:390-392`); the
  gateway change's task 1.10 sets it.
- The storage mode is one of two values, `Metadata` (the enum default) and `Full`
  (`crates/agentgateway/src/types/frontend.rs:327-333`), and it is independent of CEL
  attribute capture (`log.rs:1128-1131`). In `full` mode the payload holds the normalized
  input messages, the completion text and structured tool calls (`log.rs:76-126`).
  Every schema row that mentions `accessLog` sits under `frontendPolicies` (all 290
  matching rows of `schema/config.md`, counted by grepping for `accessLog` and grouping
  on the first key segment), and the attachable `policies[].policy`
  kinds include no access-log or tracing policy (`schema/config.md:18805` onward, 31
  kinds listed), so the mode applies to the whole process: per gateway, listener or
  identity is not expressible at v1.5.0.
- A CEL field that evaluates to null is dropped from the record (`log.rs:555`). In the
  legacy mode (storage mode omitted) the payload is filled from whatever content the
  request context captured, not from the value an expression returned
  (`log.rs:68-90`), so an identity-conditional expression cannot limit what is stored.
- Reading content: the UI's log detail request asks for the payload
  (`ui/src/api/logsApi.ts:19-21`, `includePayload: true`; the field is
  `GetRequest.include_payload`, `telemetry/log_store.rs:452-458`) and renders a
  conversation view (`ui/src/pages/Logs.tsx:1514-1518`). The log API paths
  (`/api/logs/search`, `/api/logs/get`, `/api/logs/tail`, `ui.rs:106-108`) are part of
  the UI route (`types/local.rs:2060-2070`), which carries `ui.policies`
  (`local.rs:3968-3975`); this platform's `ui.policies` are the Authentik OIDC login and
  the `platform-admins in jwt.groups` rule (`config.yaml.j2:81-96`). The admin listener
  merges the same UI router without that login (`management/admin.rs:185-200`); it is
  bound to the container loopback and never published (`config.yaml.j2:33`,
  `compose.yml:28`). Anyone holding the database password
  (`secret/services/agentgateway:agw_db_password`, `env.j2:25-26`) can also read the
  rows directly.
- Pruning: no retention, prune, delete, TTL or cleanup logic exists in the v1.5.0 log
  store (`grep -niE 'retention|prune|delete|ttl|cleanup'` over
  `telemetry/log_store.rs` and `telemetry/log_store/postgres.rs` returns nothing).
  Payload rows reference `request_logs(id) ON DELETE CASCADE` (`0001_...sql:26`), so
  deleting a request row removes its payload.
- Backup: no playbook in this repository dumps the gateway's database (`grep -rln
  agentgateway platform/playbooks | xargs grep -l -iE 'backup|pg_dump'` matches only
  unrelated lines in `destroy-vm.yml` and `manage-agentgateway-client-key.yml`).
  unverified: whether a Proxmox backup job covers the gateway VM's disk.
- Identity metadata: `llm.policies.apiKey.keys[].metadata` is typed `any`
  (`schema/config.md:74083`) and is flattened onto the CEL `apiKey` object
  (`config.yaml.j2:11-13`). `llm.policies.authorization` exists at v1.5.0
  (`schema/config.md:72849`).
- Identities today: inventory `agw_clients` (the agent identities and the enrolled
  shared key, gateway change task 4.1), and, from `inference-personal-keys`, one
  `user-<username>` identity per active member of a new `inference-users` group
  (its `design.md` decisions 2 and 3); that group admits uhstray.io team members only.

Upstream dashboard: the documentation's pre-built dashboard is "for Kubernetes
deployments" (`/documentation/observability/metrics/grafana/`). At v1.5.0 it lives at
`controller/install/helm/agentgateway/files/agentgateway-dashboard.json` (uid
`agentgateway`); its template variables query `namespace` and
`gateway_networking_k8s_io_gateway_name` labels and several panels query
`container_cpu_usage_seconds_total` and `container_memory_working_set_bytes`
(read with `gh api ... ?ref=v1.5.0` and parsed with `python3 json`). A standalone
gateway exports none of those labels, so the variables resolve empty.

Loki, Tempo and Alloy:
- Alloy v1.5.1 ships `otelcol.receiver.otlp` (gRPC default `0.0.0.0:4317`, HTTP default
  `0.0.0.0:4318`), `otelcol.exporter.otlp`, `otelcol.exporter.loki`,
  `otelcol.processor.batch`, `otelcol.processor.transform` and
  `otelcol.processor.attributes` (Alloy `v1.5.1` component docs). The receiver's server
  `tls` block has `cert_file`, `key_file` and `client_ca_file`, which "sets the `ClientCA`
  and `ClientAuth` to `RequireAndVerifyClientCert`"
  (`docs/sources/shared/reference/components/otelcol-tls-server-block.md`).
- `otelcol.exporter.loki` does not turn OTLP attributes into Loki labels unless the record
  carries the `loki.resource.labels` or `loki.attribute.labels` hint attributes
  (`otelcol.exporter.loki.md`).
- Tempo releases: `v3.0.3` is the latest, `v2.10.8` the newest 2.x
  (`gh release list -R grafana/tempo`). Tempo 3.0 notes: "Single-binary mode: push
  distributor local ingest directly to live-store and metrics-generator without Kafka"
  and "This release contains breaking configuration and deployment changes"
  (`gh release view v3.0.0 -R grafana/tempo`).

### Sibling changes

- `inference-gateway-agentgateway` task 3.1 plans the scrape file, an Alloy OTLP receiver
  that forwards spans "as structured log lines to Loki (Tempo deferred)", and a
  client-view row on the inference dashboard; task 3.3 is its gate
  (`openspec/changes/inference-gateway-agentgateway/tasks.md:155-167`). Its design
  decision 6 deferred Tempo because "one producer does not justify a store with no
  retention owner" (`design.md:125-135`).
- `inference-telemetry-production` owns the o11y host's promotion, its firewall (task
  1.4), the inference dashboards (3.1) and the inference alert groups (3.2), and lists
  traces as out of scope because no producer existed (`proposal.md:81-82`).
- `observability-estate` requires that trace ingestion wait for the metrics, alert
  delivery, retention and cardinality gates, and that trace data stay self-hosted and
  expire within a declared retention (`specs/platform/observability-estate/spec.md:48-53`);
  its tasks 3.1-3.4 plan the gate, the Alloy-to-Tempo path and a one-service pilot
  (`tasks.md:16-21`).
- `production-internal-ca` issues server and client leaves on consumer hosts (tasks
  4.1-4.4, `openspec/changes/production-internal-ca/tasks.md:70-91`).

### Firewall

`apply-firewall.yml` allows every detected published port from every entry in
`firewall_upstream_source`, as the product of the two lists
(`platform/playbooks/apply-firewall.yml:30-43`, `:290`, `:306`). There is no per-port
source. Publishing the stats port on the LAN therefore also admits the Caddy host to it,
and publishing Alloy's OTLP port on the o11y host would admit every o11y upstream.

## Goals / Non-Goals

**Goals:**
- Every signal the gateway produces reaches Grafana in production and local-dev by the
  same code path, differing only in inventory.
- Labels stay bounded: identity, model, status and reason on metrics; only
  `service`, `component`, `signal` and `cluster` as Loki labels.
- Team prompt and completion content is kept for learning, in the gateway's own
  request-log store only, for team identities only, for a declared retention.
- No request content in any metric, span, OTLP export or Loki line, and no header or
  key in any signal or store, enforced by render-time refusal and tests, not by
  convention.

**Non-Goals:**
- Gateway alert rules beyond the generic service-down rule; the inference alert groups
  are `inference-telemetry-production` task 3.2.
- Tempo's metrics generator (span metrics and service graphs). The gateway's own metrics
  already give request rate, errors and duration per identity and model; generated span
  series would spend the series budget on a duplicate.
- Object storage for Tempo, long trace retention and Mimir; plan 05 phase 3 owns them.
- The cost catalog (`agentgateway_gen_ai_client_cost_usd_total`); vLLM on our own
  hardware has no per-token price to declare.
- Shipping the gateway VM's non-access stdout (startup, errors) to Loki. Access records
  travel by OTLP; process logs stay on the host until a host log shipper exists.

## Decisions

1. **Move to `frontendPolicies.tracing` and `frontendPolicies.accessLog`; keep
   `config.metrics`.** Both deprecated blocks lose features this design needs: the
   converted trace exporter cannot set `resources`, a `filter`, a private CA root or a
   client certificate (`local.rs:258-278`), and a deprecated `config.logging.fields` block
   cannot coexist with the `accessLog` block that carries OTLP export and
   `database.llm` (`local.rs:209-214`). `config.metrics.fields` is current and stays.
   `protocol: grpc` is written explicitly on both exporters because the replacement
   defaults to HTTP (`schema/config.md:18804`) while the old block defaulted to gRPC.
   Alternative rejected: keep `config.tracing` and add `frontendPolicies.accessLog`
   alongside, because the result mixes a deprecated and a current block and still cannot
   verify the OTLP server's certificate against the internal CA.

2. **Metrics are pulled by Prometheus from a LAN-bound stats listener.** The existing
   scrape template, receiver guard and probe are kept as they are
   (`deploy-o11y.yml:122-142`, `probe-o11y-metrics-endpoint.yml:54-74`). In local-dev the
   gateway container carries `prometheus.io/scrape: "true"`, `prometheus.io/port:
   "19002"` and `prometheus.io/path: /metrics` so Alloy's existing discovery scrapes it
   over the shared `local-dev` network; those labels sit in the base compose and are
   inert in production, where no Alloy sees the gateway's engine. The hop is plaintext
   HTTP: v1.5.0's stats listener takes only an address (`schema/config.md:59`), and the
   series are aggregates (identity names, model names, counts) with no request detail.
   The firewall admits only the o11y host (decision 7). Alternative rejected: OTLP metric
   push, because v1.5.0 documents no metrics exporter (`schema/config.md:91-94` has
   `remove` and `fields` only).

3. **Tempo joins the o11y compose stack; traces arrive through Alloy** (confirmed by
   Joe, 2026-09-27). A pinned Tempo
   single-binary container on the `o11y` network, filesystem storage in a named volume,
   block retention from inventory with a default of 7 days to match Loki's default
   (`compose.yml:46`), so a trace's log link never outlives its logs. Alloy receives OTLP
   gRPC on 4317, batches, and exports to Tempo on the private network. Grafana gains a
   Tempo datasource (`uid: tempo`) with trace-to-logs into Loki and trace-to-metrics into
   Prometheus. Tempo, its datasource and Alloy's trace pipeline ship as an inventory-gated
   overlay (`compose.traces.yml`, appended through the `COMPOSE_OVERLAYS` mechanism
   `deploy.sh:25-27` already uses) plus rendered, gitignored provisioning and Alloy files,
   so a host with traces off runs exactly today's stack. The pin is Tempo 3.0.3: this is
   a new install with no 2.x data to migrate, and 3.0 runs single-binary without Kafka.
   Alternatives rejected: spans as Loki log lines (the gateway change's decision 6),
   because Grafana cannot render a span tree from log lines and the gateway emits real
   OTLP spans, and the retention-owner objection is now answered by the estate change's
   declared-retention requirement; a separate `platform/services/tempo` service as
   sketched in `plan/architecture/06-observability-instrumentation.md` (Tempo
   section), because it would need its own host or a cross-project network to reach
   Alloy and Grafana, for one producer; Jaeger as the documentation's example uses,
   because the platform standardizes on the Grafana stack (06, "Tracing (Tempo)").

4. **Access records travel by OTLP, not by a log shipper on the gateway VM.** The o11y
   Alloy cannot see the gateway's stdout in production (Context), and the OTLP receiver
   exists anyway for traces. Alloy converts records with `otelcol.exporter.loki` and hands
   them to the existing Loki writer, so the `cluster` external label and retention apply
   unchanged. The gateway adds OTLP-only fields `service: '"agentgateway"'` and
   `signal: '"access-log"'`, and Alloy sets the `loki.attribute.labels` hint for exactly
   those two, so they become labels and everything else (identity, model, tokens, path,
   status) stays in the line. Because `otlp.fields` replaces the parent list
   (`schema/config.md:18224`), the OTLP field list repeats `identity: apiKey.name`. The
   receiver starts with the log pipeline alone, which gives it a consumer before traces
   are enabled (`config.alloy:7-9`). Alternatives rejected: an Alloy agent on the gateway
   VM tailing the engine socket, because it adds a container with engine access to an
   Infrastructure-tier host to carry what OTLP already carries; Alloy's
   `otelcol.exporter.otlphttp` to Loki's native `/otlp` endpoint, because Loki maps
   `service.name` to a `service_name` label (Loki v3.3.2
   `docs/sources/send-data/otel/_index.md:116`), breaking the platform's `service` label
   contract.

5. **Every request is logged; the team's content is kept only in the gateway's store.**
   Decided by Joe, 2026-09-27: keep content for the uhstray.io team so prompts can be
   studied and improved; keep none for anyone outside the team.
   - No access-log filter: the global request ceiling bounds volume
     (`agw_rate_requests_per_minute_total`, default 120, `config.yaml.j2:167-171`), and a
     complete per-identity record is the point of the gateway.
   - Content store: `frontendPolicies.accessLog.database.llm: full`, rendered only when
     inventory sets `agw_content_logging: full` (default `metadata`). Rows land in the
     gateway's own Postgres (`config.rs:387`) and are read through the UI Logs page,
     which requires the Authentik login and the `platform-admins` group on the UI
     gateway (Context, "Reading content"). The admin listener's unauthenticated copy
     stays on the container loopback.
   - Everything else stays metadata-only. The template MUST NOT reference
     `llm.prompt`, `llm.completion`, any `request.headers` value, `request.body`, or any
     part of the `apiKey` object other than `apiKey.name` and `apiKey.team` in any
     `add`, `attributes`, `resources` or `fields` expression. `full` mode needs no such
     expression (`log.rs:1128-1131`), so content reaches the database and nothing else.
     The render step refuses a config that breaks the rule.
   - The UI's own toggle cannot change the mode: it writes the gateway config
     (`Logs.tsx:466-480`), which is mounted read-only (`compose.yml:48`) and, with the
     gateway change's `UI_READ_ONLY`, refused by the store (`config.rs:390-392`).
   - Team members are told, in the handout document for their key, that their prompts
     and completions are kept for 30 days and are readable by platform admins.
   Alternatives rejected: never logging content (this design's first draft), because
   Joe wants the team's prompts to learn from; content in Loki or in OTLP log records,
   because Grafana has no per-record access control and every Grafana viewer would read
   every prompt; an identity-conditional CEL field to select whose content is stored,
   because the stored payload comes from the captured request, not from the
   expression's value (`log.rs:68-90`).

6. **The OTLP hop is mutual TLS; plaintext only in local-dev by explicit flag.** Access
   records name the identity, model, path and source address of every request, which is
   more than the aggregate metrics. Alloy's receiver presents a server leaf and requires a
   client certificate from the internal CA (`client_ca_file`); the gateway presents a
   client leaf and verifies the receiver with `backendTLS.root` and `hostname`
   (`schema/config.md:18548-18551`, `:17974-17977`). Leaves come from the
   `production-internal-ca` issuance task (its 4.1 and 4.4), generated on each consumer.
   No bearer token is used, so no new OpenBao secret exists. Until that task can issue
   both leaves, production OTLP stays off. Local-dev may set `o11y_otlp_plaintext: true`
   (honoured only with `local_mode`, the pattern `config.yaml.j2:126` uses for
   `agw_plaintext_keys`) so logs and traces can be developed before local leaves exist;
   the deploy refuses the flag outside local-dev. Alternative rejected: plaintext on the
   LAN with a firewall rule, because every other credential-adjacent hop in the platform
   is being moved to internal TLS (gateway change section 6) and a firewall rule is not
   integrity protection.

7. **Per-port sources in `apply-firewall.yml`.** A new optional
   `firewall_detected_port_sources` map (published port to a list of sources) replaces
   `firewall_upstream_source` for the ports it names; unnamed detected ports keep today's
   behaviour. The gateway declares its stats port with the o11y host as the only source;
   the o11y host declares its OTLP port with the gateway host as the only source (and
   `inference-telemetry-production` task 1.4 can use the same map for Grafana and Loki).
   Alternatives rejected: turning detection off on both hosts and listing static rules,
   because detection is the platform's default and a static list drifts from what the
   containers publish; adding the o11y host to `firewall_upstream_source`, because that
   also opens the API and UI ports to it.

8. **Sampling: 10 percent of new traces, client-started traces not continued**
   (confirmed by Joe, 2026-09-27).
   `randomSampling` defaults to `0.1` from inventory (`agw_trace_sampling`, the variable
   the template already reads) and `clientSampling` is `false`. At the 120-per-minute
   request ceiling, 10 percent is at most 17,280 traced requests per day. With
   `clientSampling` left at its default `true`, any caller could force its requests into
   the trace store by sending a `traceparent` header, which turns the trace volume into a
   client-controlled quantity. Errors do not need tracing to be found: every request has
   an access record. The first week's Tempo block growth decides whether to raise the
   rate (task 6.2).

9. **A platform dashboard adapted from the v1.5.0 upstream dashboard.** File
   `config/grafana/dashboards/agentgateway.json`, uid `agentgateway` (no collision with
   the three existing dashboards). The Kubernetes variables are replaced by `identity`,
   `gen_ai_request_model` and `env`; the pod CPU and memory panels are dropped. Rows:
   client view (p50 and p95 first-token latency from
   `agentgateway_gen_ai_server_time_to_first_token_bucket`, request duration, 4xx and 5xx
   ratio, per-identity request rate), tokens (by identity, model and
   `gen_ai_token_type`), rejections (by `reason`, which covers budget and rate-limit
   refusals), process (`agentgateway_config_synchronized`, `agentgateway_build_info`,
   `agentgateway_requests_shed_total`), access records (Loki
   `{service="agentgateway", signal="access-log"}`) and traces (Tempo search for
   `service.name=agentgateway`, present only when traces are enabled). The JSON records
   the upstream path and commit it was adapted from (Apache-2.0). This dashboard carries
   the client-view row that the gateway change's scenario "Client-view latency on the
   dashboard" names; the inference dashboard from `inference-telemetry-production` task
   3.1 links to it rather than duplicating its panels. Alternative rejected: importing the
   upstream JSON unchanged, because its variables resolve empty on a standalone gateway.

10. **The Alloy configuration becomes a directory.** `config/config.alloy` moves to
    `config/alloy/containers.alloy` unchanged except that the `cluster` external label
    reads `sys.env("O11Y_CLUSTER")` (the file already reads the environment,
    `config.alloy:99`); `deploy-o11y.yml` renders a gitignored `config/alloy/otlp.alloy`
    only when OTLP is enabled, with the trace pipeline inside it only when traces are
    enabled. Alloy loads the directory (`run.md:26`). This keeps one committed file for
    both environments and satisfies "no receiver without a consumer".

11. **Verification lives in the deploys, not in a manual check.** The gateway deploy
    probes `/metrics` from the sibling database container over the compose network (the
    same path its readiness probe uses, `deploy-agentgateway.yml:172-230`), requires
    `agentgateway_build_info`, and after its keyed chat completion requires a
    `agentgateway_requests_total` series carrying the verifying identity's label; it prints
    series names and counts, never a key. With content mode on, it also requires a
    payload row for its own keyed request (queried by the request's id in
    `request_logs`, counts only) and a `request_logs` oldest row inside the retention
    window; with content mode off, it requires the payload table to gain no row. The o11y
    deploy, when the gateway target is
    declared, requires `up{job="agentgateway"} == 1`; when OTLP logs are enabled, a Loki
    line under `{service="agentgateway", signal="access-log"}` within ten minutes of the
    gateway's last deploy; when traces are enabled, a Tempo search hit for
    `service.name=agentgateway`. Each check extends the existing
    `tasks/verify-o11y-metrics.yml` pattern.

12. **Team membership is declared on every identity and enforced twice.** Each
    `apiKey` entry renders `metadata: {name: <identity>, team: <team>}`. Inventory
    identities declare it in `agw_client_policies.<name>.team`, with no default, so an
    undeclared identity cannot slip in; personal keys render `team: uhstray` because
    their eligibility group `inference-users` is the team (`inference-personal-keys`
    decision 3; that change's task 4.2 renders it). While `agw_content_logging` is
    `full`, the deploy refuses to render any identity whose team is not `uhstray`, and
    the template adds `llm.policies.authorization` with the rule
    `apiKey.team == "uhstray"`, so a key enrolled by any other path is refused at the
    gateway before it reaches the upstream. unverified: that `llm.policies.authorization`
    evaluates after API-key authentication so `apiKey.team` is populated (task 1.9 proves
    it with a local non-team key expecting HTTP 403). The runtime rule also applies to
    `/v1` on the UI gateway, since `llm.gateways` covers both (`config.yaml.j2:113`).
    **Future users outside the team (planned, not built):** because the storage mode is
    process-wide at v1.5.0 (Context), they get a second gateway instance with its own
    config in `metadata` mode, its own identities and listener, and no team rule;
    a second listener on this instance cannot differ in mode. When a later release
    scopes access-log policy per gateway, that path is re-evaluated. Alternatives
    rejected: trusting the identity-name prefix (`user-`), because agent identities are
    team identities too and a prefix says nothing about membership; a single
    `agw_team_identities` list, because a per-identity field travels with the identity
    through rotation and revocation.

13. **Content expires after 30 days, by a scheduled prune.** v1.5.0 prunes nothing
    (Context), and prompts can carry pasted secrets, so rows cannot live forever.
    `prune-agentgateway-request-logs.yml` deletes `request_logs` rows whose
    `completed_at` is older than `agw_request_log_retention_days` (default 30); the
    payload rows go with them by cascade. It runs daily from a `schedule:` declared in
    `platform/semaphore/templates.yml`, the way the tududi token refresh does
    (`templates.yml:404-405`), executes inside the database container through the
    engine, prints counts only, and is idempotent (a second run deletes nothing). The
    gateway deploy verify fails when the oldest row is older than the retention plus
    two days, which catches a silently stopped schedule. Any future backup of this
    database excludes `request_log_payloads` data (for example `pg_dump
    --exclude-table-data`); a VM-level backup that captures the disk is checked and its
    retention recorded (task 1.12). Alternatives rejected: a longer retention, because
    thirty days is enough to review a month of prompting and a leaked secret lives no
    longer than that; pruning only payload rows, because the metadata row is also
    per-request personal data with no use after the budget window.

## Risks / Trade-offs

- [The `frontendPolicies` migration changes where two existing features live] → the
  local Semaphore deploy runs the rendered config on the pinned binary before any
  production deploy; BATS asserts the deprecated blocks are gone and the identity field
  is present in both the parent and OTLP field lists.
- [Prompts with pasted secrets sit in the gateway database for up to 30 days, readable by
  platform admins and by anyone holding the database password] → retention and prune
  (decision 13), admin-only UI access, the handout notice (decision 5); a secret found in
  a prompt is rotated, and the row can be purged at once with a zero-day prune run.
- [Content rows and budget rows share one Postgres; a large payload volume could slow
  budget accounting] → task 6.1 measures table size per day; a separate
  `config.logging.database` (supported, `config.rs:374-387`) is the fallback.
- [A non-team identity is enrolled while content mode is on] → refused at render and,
  for any key the render guard did not see, at request time (decision 12).
- [Identity label cardinality grows with personal keys (`inference-personal-keys`)] →
  bounded by the number of enrolled people and agents; the dashboard's queries
  aggregate by identity only where that is the question; the estate change's sample
  limit applies to local scraping (`config.alloy:99`).
- [A traced request whose trace is dropped by sampling leaves no span] → every request
  still has an access record with `trace.id` when one exists; latency percentiles come
  from metrics, not traces.
- [Private key files mounted into a non-root container must be readable by its user] →
  the issuance task sets ownership for the gateway's container user. unverified: how
  v1.5.0 reports an unreadable client key (task 3.8 observes it); the o11y deploy's
  log-receipt check fails closed either way, because no record arrives.
- [Tempo 3.0 is a new major line] → pinned by digest; retention and storage keys are
  checked against the 3.0.3 reference before the first deploy (task 5.1).
- [`otelcol.exporter.loki` line format is not yet observed] → task 3.7 records the line
  a real record produces before the dashboard's log queries are written.

## Migration Plan

1. Firewall mechanism and gateway template migration (sections 1 and 2 of `tasks.md`),
   proven in local-dev; the team markers are declared for every identity before
   `agw_content_logging: full` is set in production, and the prune schedule runs before
   the first production row with content exists.
2. Metrics in production: stats bind, firewall, probe, declaration, dashboard.
3. OTLP logs: local-dev plaintext first, then production over mutual TLS once the
   internal CA issues both leaves.
4. Traces: after the estate change records its gates, enable Tempo and the gateway's
   tracing, locally then in production; measure a week; set the rate and retention.

Rollback is in `proposal.md`.

## Open Questions

Decided 2026-09-27 by Joe (no longer open): Tempo lives in the o11y stack (decision 3);
`clientSampling` is off and new traces are sampled at 10 percent (decision 8); team
content is kept, non-team content is not (decisions 5, 12, 13).

Still open:

- Whether the `agentgateway` dashboard's client-view row satisfies the gateway change's
  scenario wording ("the inference dashboard's client-view row") or the row must also be
  embedded in `inference-telemetry-production`'s dashboard. Either answer leaves this
  change's tasks unchanged except task 4.3's link-versus-embed step.
