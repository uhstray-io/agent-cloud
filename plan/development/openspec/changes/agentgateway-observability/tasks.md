# Tasks: agentgateway observability

Every task is idempotent and runs through Semaphore templates for live work; nothing is
done over a shell on a VM. Pull requests only when Joe asks for them (repo rule).

Relation to sibling changes (recorded here so no task is done twice):

- `inference-gateway-agentgateway` task 3.1 is **superseded** by sections 2 to 5 of this
  change (its spans-as-Loki-lines plan is replaced by export to Tempo, design decision 3).
  Its gate 3.3 is **proved here** by task 6.5, against the separate client-view dashboard
  (task 4.1) rather than a row on the inference dashboard. Its task 1.10
  (`UI_READ_ONLY=true`) stays with it and is a dependency of design decision 5.
- `observability-estate` task 3.3 (one instrumented pilot service) is **absorbed** by
  section 5, with the gateway as the pilot; the Alloy-exporter half of its task 3.2 is
  delivered here, and the Tempo half (image, retention, storage) belongs to the separate
  Tempo work. Its task 3.1 (the gate itself) and 3.4 (its own validation) stay with it;
  section 5 consumes 3.1. Its task
  4.4's agentgateway half is proved by tasks 2.8 and 3.10.
- `inference-telemetry-production` keeps the o11y host (section 1), its firewall (1.4),
  the inference dashboards (3.1) and alert groups (3.2). This change depends on its
  section 1 for production and offers task 2.1's mechanism to its task 1.4.
- `production-internal-ca` tasks 4.1 and 4.4 (consumer-side leaf issuance) are a
  dependency of task 3.8.
- `inference-personal-keys` task 4.2 (user-key rendering) renders the `team` marker this
  change requires (task 1.4); its eligibility group `inference-users` is the team.
- External dependency, not an OpenSpec change: the Tempo deployment on the o11y VM
  (separate session, Joe 2026-09-27). It supplies the Tempo OTLP ingest endpoint, its
  query endpoint, its transport statement and Grafana's Tempo datasource. Section 5
  starts only when it has published those.

Decided by Joe, 2026-09-27: keep the uhstray.io team's prompt and completion content,
never anyone else's, for 90 days; the legacy shared key and skynet are team-only;
Tempo is deployed by separate work and this change integrates with its endpoint;
`clientSampling` off with 10 percent sampling; the gateway client view is a separate
dashboard.

## 1. Gateway configuration on current blocks; team content kept, nothing else

- [ ] 1.1 Feature branch from `dev`: `feat/agentgateway-observability`
- [ ] 1.2 `templates/config.yaml.j2`: remove `config.logging.fields` and `config.tracing`;
      add `frontendPolicies.accessLog` with `add: {identity: apiKey.name}` and
      `database: {llm: <agw_content_logging>}` (`full` or `metadata`, default `metadata`);
      keep `config.metrics.fields.add.identity`. Add, inside the same block, `otlp`
      rendered only when `agw_otlp_endpoint` is set: `host`, `protocol: grpc`,
      `fields.add` = `identity`, `service: '"agentgateway"'`, `signal: '"access-log"'`,
      and `policies.backendTLS.{root, cert, key, hostname}` unless the local-only
      plaintext flag holds (design decisions 1, 4, 5, 6)
- [ ] 1.3 Same template: `frontendPolicies.tracing` rendered only when `agw_traces_enabled`
      and `agw_otlp_endpoint` are both set: `host`, `protocol: grpc`,
      `randomSampling: '{{ agw_trace_sampling | default("0.1") }}'`,
      `clientSampling: 'false'`, `resources: {service.name: '"agentgateway"',
      deployment.environment: <env from inventory>}`, the same `backendTLS` rule
      (design decision 8)
- [ ] 1.4 Team marker (design decision 12): every `apiKey` entry renders
      `metadata: {name, team}`; inventory identities take `team` from
      `agw_client_policies.<name>.team` with no default; while
      `agw_content_logging == 'full'` the template adds `llm.policies.authorization`
      with the rule `apiKey.team == "uhstray"`. Coordinate with `inference-personal-keys`
      task 4.2 so user keys render `team: uhstray` (their group `inference-users` is the
      team); append a dated pointer line to that change's task list
- [ ] 1.5 `deploy-agentgateway.yml` render guard, after templating and before `deploy.sh`:
      load the rendered YAML; collect every CEL value under `config.metrics.fields.add`,
      `frontendPolicies.accessLog.add`, `frontendPolicies.accessLog.otlp.fields.add`,
      `frontendPolicies.accessLog.database.add`, `frontendPolicies.tracing.attributes` and
      `.resources`; fail naming the field if any references `llm.prompt`,
      `llm.completion`, `request.headers`, `request.body` or any `apiKey` member other
      than `apiKey.name` and `apiKey.team`; fail naming the identity if any key has no
      `team`, or if `database.llm` is `full` and any key's team is not `uhstray`; fail
      when `database.llm` is `full` and the team authorization rule is absent; fail when
      an OTLP block lacks `backendTLS` and `local_mode` is false or the plaintext flag is
      unset. Not `no_log` (the config holds no credential: `config.yaml.j2:4-10`)
- [ ] 1.6 `deploy-agentgateway.yml` verify: probe `http://<gateway>:19002/metrics` from the
      sibling db container as the readiness probe does; require `agentgateway_build_info`;
      after the keyed chat completion, require an `agentgateway_requests_total` series with
      `identity="<verify client>"`. Database checks inside the db container, counts only:
      with `full`, a payload row exists for the verify request; with `metadata`, the
      payload table gained no row; in both modes the oldest `request_logs` row is younger
      than `agw_request_log_retention_days` plus two days, else fail naming the breach
- [ ] 1.7 `compose.yml`: labels `prometheus.io/scrape: "true"`, `prometheus.io/port:
      "19002"`, `prometheus.io/path: /metrics` on the gateway; read-only mount of a
      deploy-created `./certs/otlp` directory for the OTLP client leaf, key and CA root;
      update the header comment that says the database holds "nothing a client needs
      back" (`compose.yml:6-7`) to name the request-log content and its retention
- [ ] 1.8 `platform/tests/test_service_agentgateway.bats`: the deprecated blocks are absent;
      `identity` appears in both the parent and the OTLP field lists; `database.llm`
      renders from `agw_content_logging` with default `metadata`; every key renders a
      `team`; the team rule renders exactly when the mode is `full`; `clientSampling` is
      `false`; both exporters declare `protocol: grpc`; the scrape labels exist. One
      render test per refusal in 1.5 (each forbidden expression, a missing team, a
      non-team identity under `full`), each mutated once to watch it go red
      (`CONTRIBUTING.md`, "Writing BATS Tests"). Replace the existing `config.logging`
      assertion at lines 145-151 rather than keeping both
- [ ] 1.9 Local proof through the local Semaphore (`Deploy agentgateway (Local)`), with
      `agw_content_logging: full`: readiness 200; keyed round-trip 200 for a team
      identity and a payload row for it; a temporary identity declared with a non-team
      value is refused at render (1.5); a key enrolled with `team` other than `uhstray`
      by a test-only local override of the guard gets HTTP 403 at the gateway and leaves
      no payload row. unverified today: that v1.5.0 accepts `frontendPolicies.accessLog`
      and `.tracing` alongside the `llm` shortcut, and that `llm.policies.authorization`
      sees `apiKey.team` after API-key authentication; this task proves both. Record the
      results in `platform/services/agentgateway/context/architecture.md`
- [ ] 1.10 Content access check, local: the log detail request for a stored entry through
      `https://admin.inference.<local zone>/api/logs/get` is refused without the
      Authentik login, and refused for a signed-in user outside `platform-admins`; the
      conversation view renders for an admin (`ui/src/api/logsApi.ts:19-21`). unverified
      today: that the UI's OIDC and authorization policy covers the `/api/logs` paths as
      the source reads (`types/local.rs:2060-2070`, `:3968-3975`)
- [ ] 1.11 `platform/playbooks/prune-agentgateway-request-logs.yml` (design decision 13):
      `DELETE FROM request_logs WHERE completed_at < now() - interval '<N> days'` inside
      `agentgateway-db` through the container engine, `N` from
      `agw_request_log_retention_days` (default 90, integer asserted); payload rows go by
      cascade (`0001_create_request_log_schema.sql:26`); report the deleted count only;
      a second run deletes nothing. `templates.yml`: `Prune agentgateway Request Logs`
      with `dev_variant: true` and a daily `schedule:`; BATS asserts the schedule, the
      integer guard and that no statement selects payload columns
- [ ] 1.12 Backup exposure: read-only through Semaphore, find whether any Proxmox backup
      job covers the gateway VM (unverified today) and record its retention in
      `design.md`; add to `platform/services/agentgateway/deployment/README.md` that any
      database backup excludes `request_log_payloads` data
- [ ] 1.13 Handout notice: the document the team's keys are handed out with (the
      gateway change's task 4.4 updates dgx-spark `docs/TEAM-ENDPOINT.md`; personal keys
      use their own handout) states that prompts and completions are kept for 90 days and
      readable by platform admins
- [ ] 1.14 site-config: declare `team: uhstray` for every `agw_clients` identity, including
      the enrolled legacy shared key (`legacy-shared`) and `skynet`, both confirmed
      team-only by Joe on 2026-09-27; then `agw_content_logging: full` and
      `agw_request_log_retention_days: 90`; the prune schedule is live before the first
      production deploy with `full`
- [ ] 1.15 Validation gate: 1.8's planted `llm.prompt` field fails before any restart,
      proving scenario "Content-capturing configuration is refused"; 1.9 proves scenarios
      "A non-team identity is refused at render", "A non-team key is refused at request
      time" and "A team request's content is stored"; 1.10 proves scenario "Content is
      readable only by platform admins"; a local prune run with retention `0` removes the
      rows and a second run deletes nothing, proving scenario "An expired row is removed";
      a verify run against a row back-dated past the window fails, proving scenario "A
      stopped prune is detected"

## 2. Metrics in local-dev and production

- [ ] 2.1 `apply-firewall.yml`: optional `firewall_detected_port_sources` (published port to
      a list of sources) that replaces `firewall_upstream_source` for the ports it names;
      unnamed ports unchanged; `platform/tests/test_apply_firewall.bats` covers a named port,
      an unnamed port and a named port that is not published (refused with its name)
- [ ] 2.2 unverified: whether `apply-firewall.yml` removes an allow rule it added on an
      earlier run when the source list for that port shrinks. Read the play and a
      `--check` run against the gateway host; if it does not prune, add pruning of rules
      carrying the play's own comment tag, with a BATS case
- [ ] 2.3 Local: deploy the gateway and o11y through the local Semaphore; confirm the local
      Prometheus holds `agentgateway_requests_total{service="agentgateway"}` with the
      verifying identity's label (Alloy discovery, `config.alloy:49-101`)
- [ ] 2.4 Read-only through Semaphore: record the gateway host's current firewall
      variables and `agw_stats_bind` from the synced inventory (names only) in
      `design.md` Context
- [ ] 2.5 site-config: gateway `agw_stats_bind` = its LAN address and
      `firewall_detected_port_sources` naming the stats port with the o11y host as its only
      source; `Deploy agentgateway (Dev)`, then `Apply Firewall (Dev)` on the gateway host
- [ ] 2.6 `Probe o11y Metrics Endpoint (Dev)` with `probe_target=agentgateway`: HTTP 200
      from the o11y host (the order in `platform/services/o11y/deployment/README.md:28-34`);
      a probe from the controller to the same port is refused
- [ ] 2.7 `deploy-o11y.yml` verify: when `agentgateway_metrics_address` is declared, require
      `up{job="agentgateway"} == 1` within three scrape intervals, extending
      `tasks/verify-o11y-metrics.yml`; site-config declares `agentgateway_metrics_address`
      and `agentgateway_metrics_port` in its own PR; `Deploy o11y (Dev)`
- [ ] 2.8 Validation gate: 2.7 plus a keyed request through the public route proves
      scenario "Metrics target is up and carries the identity"; 2.6's refused probe proves
      scenario "Metrics listener refuses other hosts". Needs the production o11y host
      (`inference-telemetry-production` section 1)

## 3. Access records over OTLP

- [ ] 3.1 Move `config/config.alloy` to `config/alloy/containers.alloy`; the Loki
      `cluster` external label reads `sys.env("O11Y_CLUSTER")`, rendered from inventory in
      `templates/env.j2`; compose mounts the directory and runs `alloy run /etc/alloy/`;
      `.gitignore` the rendered `otlp.alloy`; BATS asserts the directory mount and that no
      committed `.alloy` file declares `otelcol.receiver.otlp`
- [ ] 3.2 `templates/otlp.alloy.j2`, rendered by `deploy-o11y.yml` only when
      `o11y_otlp_enabled`: `otelcol.receiver.otlp` gRPC on 4317 with `tls { cert_file,
      key_file, client_ca_file }` unless the local plaintext flag holds; logs through
      `otelcol.processor.batch`, a processor that sets `loki.attribute.labels` to
      `service, signal`, and `otelcol.exporter.loki` into the existing
      `loki.write.default`; removed when disabled, Alloy restarted only on change
- [ ] 3.3 `compose.yml`: Alloy publishes
      `${O11Y_OTLP_BIND:-127.0.0.1}:${O11Y_OTLP_GRPC_PORT:-4317}:4317` and mounts a
      deploy-created `./certs/otlp` directory read-only; `templates/env.j2` renders both
      values
- [ ] 3.4 `deploy-o11y.yml` guards: refuse `o11y_otlp_plaintext` unless `local_mode`;
      refuse `o11y_otlp_enabled` without the three certificate files present when not
      plaintext; the gateway deploy refuses an `agw_otlp_endpoint` whose host is not the
      declared o11y host
- [ ] 3.5 `deploy-o11y.yml` verify: when OTLP is enabled, the verify first generates its
      own traffic, so an idle gateway cannot fail it: it sends one keyed chat completion
      through the gateway as the verifying identity (`agw_verify_client` on the gateway
      host, else its first `agw_clients` entry — the gateway deploy's own selection; the
      key comes through `_shared_reads` from `agentgateway`, `client_<name>`, in the
      `no_log` credential step, and the request is a `uri` call delegated to the gateway
      host, to its published port, whose result is never printed) and records the send
      time. Once the gateway listeners require client certificates (gateway change task
      6.1), the call is `https://` and presents the `agw-verifier` client leaf on the
      gateway host (`client_cert`/`client_key`, `ca_path` the internal bundle;
      `production-internal-ca` decision 4). It then requires, within a bounded retry, a
      Loki record under `{service="agentgateway", signal="access-log"}` whose body names
      that identity and whose timestamp is at or after the send time, and requires the
      label set of that stream to contain no `identity`, model or token label. A 429 from
      the gateway's request bucket still satisfies the check, because a rejected request
      also leaves a record. It prints the record's status and timestamp, never the key
- [ ] 3.5a Rejected-request record: the same verify sends one request with no key (from
      the gateway host, presenting the `agw-verifier` leaf once group 6 lands, so the
      refusal is the key check and not the TLS handshake) and
      requires a record at or after its send time carrying HTTP status 401 and a
      rejection reason. unverified: the access-log field name v1.5.0 uses for the
      rejection reason; read it from the line 3.7 records before writing the query
- [ ] 3.6 Local: `o11y_otlp_plaintext: true`, `agw_otlp_endpoint` = `o11y-alloy:4317` on the
      shared `local-dev` network; deploy both through the local Semaphore
- [ ] 3.7 unverified: the line format `otelcol.exporter.loki` v1.5.1 produces for a gateway
      record (body plus attributes). Record one real line for a served request and one for
      a request refused with 401, each with the identity redacted to its name only, in
      `platform/services/o11y/deployment/README.md`; write the dashboard's and the
      verify's log queries against them
- [ ] 3.8 Production: declare the o11y receiver's server leaf and the gateway's client
      leaf through `production-internal-ca` task 4.1's issuance (names from site-config,
      keys generated on each host); site-config sets `o11y_otlp_enabled`,
      `agw_otlp_endpoint`, and `firewall_detected_port_sources` on the o11y host naming the
      OTLP port with the gateway host as its only source; `Deploy o11y (Dev)`,
      `Apply Firewall (Dev)`, `Deploy agentgateway (Dev)`. unverified: how v1.5.0 reports
      an unreadable client key; observe it once with the key's mode deliberately wrong in
      local-dev and record the log line
- [ ] 3.9 BATS in `platform/tests/test_service_o11y.bats`: the rendered `otlp.alloy`
      carries `client_ca_file` whenever the plaintext flag is unset; the plaintext guard
      refuses outside `local_mode`; the label hint names exactly `service` and `signal`
- [ ] 3.10 Validation gate: 3.5 in production proves scenarios "A request is findable by
      identity" and "Identity is not an index label"; 3.5a plus one request burst past
      the verifying identity's request bucket, both found in Loki, proves scenario "A
      rejected request still leaves a record"; a Semaphore-run TLS handshake to the
      receiver without a client certificate fails, proving scenario "A sender without a
      client certificate is rejected"; a production render with the plaintext flag set
      fails in 3.4, proving scenario "Plaintext export is refused in production"
- [ ] 3.11 Follow-up, recorded not built: Alloy `v1.5.1`'s OTLP receiver cannot check a
      client certificate's name (design decision 6), so the firewall declaration in 3.8 is
      the only binding of sender to gateway. On each Alloy upgrade, read the receiver's
      server TLS arguments at the new tag; when a name check exists, require the gateway's
      OTLP client name there; otherwise record the version read in `design.md`

## 4. Dashboards

- [ ] 4.1 Two dashboards adapted from agentgateway `v1.5.0`
      `controller/install/helm/agentgateway/files/agentgateway-dashboard.json` (commit
      `fe6732474a96a0363dfb9822859af4e9bab360fa`, Apache-2.0, recorded in each JSON
      description), variables `identity`, `gen_ai_request_model`, `env`, datasource uids
      `prometheus` and `loki` (design decision 9):
      `config/grafana/dashboards/agentgateway-client-view.json`, uid
      `agentgateway-client-view`, the separate client-view dashboard; and
      `config/grafana/dashboards/agentgateway.json`, uid `agentgateway`, operations, whose
      trace panel uses a datasource variable of type `tempo`
- [ ] 4.2 BATS: both JSON files parse; their uids are unique among the dashboards; every
      metric name they query is in agentgateway v1.5.0 `schema/metrics.md` or carries a
      `_bucket`/`_sum`/`_count` suffix of one; no query references `namespace`, `pod` or
      a `gateway_networking_k8s_io_*` label; no panel names a fixed Tempo datasource uid
- [ ] 4.3 Validation gate: `Clean Deploy o11y (Local)` then one keyed request; the local
      client-view and operations dashboards show that identity in metrics and in an
      access record, proving scenario "Local deploy shows metrics and access records"

## 5. Traces to the separately deployed Tempo, after the estate gate

- [ ] 5.1 Inputs from the separate Tempo work, recorded in `design.md` before any code in
      this section: the OTLP gRPC ingest address, whether it is host-local to Alloy, its
      TLS server name and CA if not, the query endpoint the receipt check uses, and the
      Grafana Tempo datasource uid. unverified today: all of them (the Tempo work has not
      published them)
- [ ] 5.2 `templates/otlp.alloy.j2`: a trace pipeline rendered only when
      `o11y_traces_enabled` and `o11y_tempo_otlp_endpoint` are both set:
      `otelcol.processor.batch` into `otelcol.exporter.otlp` to that endpoint, TLS against
      the internal CA root unless `o11y_tempo_otlp_host_local` is true. No default host
- [ ] 5.3 Gate wiring: `deploy-o11y.yml` refuses `o11y_traces_enabled` without
      `o11y_tempo_otlp_endpoint`, refuses a plaintext endpoint not declared host-local,
      and refuses until the estate gate (its task 3.1) is recorded as passed, naming what
      is missing; `deploy-agentgateway.yml` refuses `agw_traces_enabled` unless the
      declared o11y host has `o11y_traces_enabled`
- [ ] 5.4 `deploy-o11y.yml` verify: when traces are enabled and `o11y_tempo_query_url` is
      declared, the verify reuses 3.5's own probe request instead of waiting for traffic:
      it reads the trace id from that probe's access record and requires a lookup of that
      trace id on the query endpoint to return a `service.name=agentgateway` trace. A
      caller cannot force sampling (the spec's caller-context requirement), so the check
      is required only while the gateway host's declared `agw_trace_sampling` is `1` (the
      5.6 test window); at any lower fraction a missing trace is reported as "not run:
      probe not sampled", never as passed or failed. With no query endpoint declared, the
      verify reports the check as not run rather than passed. unverified: the access-record
      field v1.5.0 uses for the trace id; read it from the line 3.7 records
- [ ] 5.4a Standing trace-receipt signal (design decision 11): add a scrape of Alloy's own
      metrics (Alloy serves HTTP on 12345, `compose.yml:66`; nothing scrapes it today) and,
      in `templates/alerts.yml.j2`, a rule rendered only when traces are enabled: the rate
      of Alloy's exported-span counter is zero for `o11y_trace_silence_minutes` (default
      30) while `rate(agentgateway_requests_total[5m]) > 0`. unverified: the counter's
      name and the metrics path at Alloy `v1.5.1`; read both from the running local Alloy
      and record them in `design.md` before writing the rule; task 6.1 measures the same
      counter. BATS: the rule is absent when traces are disabled; drill: stop the trace
      exporter's endpoint for the window in local-dev and see the alert fire
- [ ] 5.5 BATS: the trace pipeline is absent from the rendered Alloy file when disabled or
      when no endpoint is declared; each refusal in 5.3 fires; the rendered exporter
      carries TLS whenever the endpoint is not host-local
- [ ] 5.6 Each environment where the Tempo work has published an endpoint, through its
      Semaphore: enable traces with `agw_trace_sampling` at `1` for a test window, then
      back to the declared default `0.1`
- [ ] 5.7 Validation gate: an enablement attempted before the estate gate, or with no
      endpoint declared, is refused, proving scenario "Trace enablement waits for the
      rollout gate"; the test window's trace is found on the query endpoint with the same
      trace id as its access record, proving scenario "A sampled request is searchable in
      Grafana"; with sampling at `0`, a request sent with a `traceparent` header leaves no
      trace, proving scenario "Caller-supplied trace context does not force a trace"

## 6. Measure, record, reconcile

- [ ] 6.1 After seven days in production: spans exported per day, measured at Alloy
      (unverified: the metric name Alloy v1.5.1 exposes for exported spans, which task
      5.4a records and its alert uses), gateway
      access lines per day in Loki, active gateway series in Prometheus, the `request_logs` row count
      and the size of `request_log_payloads`; record them in `design.md`
- [ ] 6.2 Set `agw_trace_sampling` from 6.1, with the Tempo owner's storage figure, and
      the arithmetic in an `env.j2` comment; if the payload table's size slows budget
      accounting, move the request log to its own `config.logging.database` in a follow-up change
- [ ] 6.3 Docs: `platform/services/agentgateway/context/architecture.md` (signals, ports,
      content rule), `platform/services/o11y/deployment/README.md` (OTLP, the Tempo
      endpoint as an input from separate work, the order of enablement, Alloy config is a
      directory), and the root `AGENTS.md` rows the branch workflow requires
- [ ] 6.4 Append dated pointer lines, never edits, to the sibling changes' task lists:
      `inference-gateway-agentgateway` 3.1 (superseded here) and its design decision 6
      (Tempo deferral reversed, design decision 3 here); `observability-estate` 3.3
      (delivered here) and 3.2 (Alloy half here, Tempo half separate work);
      `inference-telemetry-production` 1.4 (per-port sources available)
- [ ] 6.5 Validation gate: after an hour of production traffic, a wipe and redeploy of the
      production o11y stack through Semaphore restores both dashboards (production has no
      clean-deploy template on 2026-09-27: `platform/semaphore/templates.yml` declares only
      `Deploy o11y (Dev)`, line 36, and `Clean Deploy o11y (Local)` lives in
      `templates-local.yml:130`; declare one first, or reuse the one
      `inference-telemetry-production` task 3.5 needs), the client-view dashboard rendering
      first-token percentiles and per-identity counts, proving scenario "Dashboard survives
      a rebuild" (and the gateway change's "Client-view latency on the dashboard"); a
      team request carrying a unique marker string leaves no match in a Loki search or in
      the trace store, proving scenario "Loki holds no prompt text", and neither the
      caller's key nor its Authorization value appears in the database, Loki or the trace
      store, proving scenario "Header and key are never stored"; on archive, retain the
      outcome (worked / dead end / corrected) into bank `agent-cloud-750a33b9`
