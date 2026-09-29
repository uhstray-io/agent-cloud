# Tasks: agentgateway observability

Every task is idempotent, and live work runs through Semaphore templates; nothing is done
over a shell on a VM. Pull requests are opened only when Joe asks for them (repo rule).

Rebased 2026-09-28 onto `origin/dev` at `7a24846`, then brought up to `origin/dev` at
`a146382` (PR #301) by merge `f92b0bf`. The landed state and its commits are in
`design.md` Context.

## Ownership and relation to other work

Per the handoff of 2026-09-28, the o11y session owns the o11y stack. Tasks marked
**[o11y]** are that session's work. They are listed here as dependencies, with the task
in this change that waits on each one, and this change does not implement them. Every
other task is gateway-side and belongs to this change.

- `inference-gateway-agentgateway` task 3.1 is **superseded** by this change together
  with the landed o11y work. Its gate 3.3 is **proved here** by task 9.4, against the
  separate client-view dashboard. Its task 1.10 (`UI_READ_ONLY=true`) stays with it and
  is done in PR #303 (merged into `dev` on 2026-09-28 (merge `255b251`); `templates/env.j2:38` renders it); design decisions 4 and 5 depend
  on it, and task 2.5 here refuses `full` without it.
- `observability-estate` task 3.1 (the gate) has landed as
  `tasks/assert-o11y-trace-rollout.yml`, which this change reuses without a second gate.
  Task 3.2 is checked off there. Task 3.3 (the pilot service) is **absorbed** here, with
  the gateway as the pilot; task 8.1 proves it.
- `inference-telemetry-production` keeps the o11y host, its firewall (its task 1.4), the
  inference dashboards and the alert groups. Task 5.1 here offers the per-port mechanism
  to its task 1.4.
- `production-internal-ca` tasks 4.1 and 4.4 (consumer-side leaf issuance) are a
  dependency of section 6.
- `inference-personal-keys` task 4.2 (user-key rendering) renders the `team` marker that
  task 2.3 requires.

Decided by Joe, 2026-09-27, and still binding: keep the uhstray.io team's prompt and
completion content, and never anyone else's, for 90 days; the legacy shared key and
skynet are team-only; `clientSampling` is off and sampling is 10 percent; the client view
is a separate dashboard, with operations as a second dashboard.

## 1. Split the OTLP switch (first; design decision 1)

- [ ] 1.1 Read-only, through Semaphore: record, names and booleans only, whether the
      production and local gateway inventories declare `agw_otlp_host` and
      `agw_trace_sampling`, and whether the receiver declares `o11y_otlp_bind`. Record
      the result in `design.md` Context. unverified today: the production values
      (`platform/services/o11y/deployment/README.md:114-115` records a production trace).
      2026-09-29, read from site-config `main` (the Semaphore `production` record matched it,
      `sync-inventory.yml --check`): production declares `agw_otlp_host`, `agw_trace_sampling`
      and `o11y_otlp_bind`; `agw_trace_sampling` is pinned at 0.05, below the 10% decision;
      the correction to 0.1 is proposed in site-config#46. The local inventory is not recorded here
- [ ] 1.2 `templates/config.yaml.j2`: render `accessLog.otlp` only when `agw_otlp_host`
      is set and `agw_otlp_logs` is true (today the condition is the host alone, line
      88). Render `frontendPolicies.tracing` only when `agw_otlp_host` is set and
      `agw_otlp_traces` is true (today line 98). Both switches default to `false`
- [ ] 1.3 `deploy-agentgateway.yml` Phase 1:
      - Split the assert at lines 103-112 in two. The receiver checks (one `o11y_svc`
        host, the host format, and bind equality, lines 106-108) run when
        `agw_otlp_host` is set. The sampling guard (lines 109-110) runs when
        `agw_otlp_traces` is true.
      - Change the conditions of the rollout-gate include (lines 114-116) and of the
        Tempo readiness check (line 132) to `agw_otlp_traces`.
      - Keep the reachability check (line 142) on `agw_otlp_host`.
      - Add a refusal, placed before `Manage secrets and render env + config`: when
        `agw_otlp_host` is set with neither switch, fail and name both switches. Also
        refuse either switch without `agw_otlp_host`.
      - Update the inventory header comment at line 29.
- [ ] 1.4 `deploy-agentgateway.yml` Phase 3: the scrape receipt at lines 279-316 (its
      conditions at lines 288 and 316) runs when the receiver declares
      `agentgateway_metrics_address`, instead of when `agw_otlp_host` is set
- [ ] 1.5 `deploy-o11y.yml:124-128`: the receiver's gate include reads the gateway's
      `agw_otlp_traces` instead of `agw_otlp_host`. The private-bind guard at lines
      164-175 keeps `agw_otlp_host`. The o11y session reviews this edit
- [ ] 1.6 BATS:
      - Replace the landed OTLP test at `platform/tests/test_service_agentgateway.bats:346-373`
        with a matrix of renders: logs only renders `accessLog.otlp` and no `tracing`;
        traces only renders `tracing` and no `accessLog.otlp`; both renders both;
        neither renders neither.
      - Update the gate assertion at line 385 and `platform/tests/test_service_o11y.bats:1086`
        to the trace switch.
      - Add one playbook-level test per refusal from 1.3, each mutated once to watch it
        go red (`CONTRIBUTING.md`, "Writing BATS Tests").
- [ ] 1.7 site-config: in the pull request that accompanies this code, set
      `agw_otlp_logs: true` and `agw_otlp_traces: true` wherever 1.1 found
      `agw_otlp_host` declared
- [ ] 1.8 Validation gate: in local-dev, through the local Semaphore, a gateway deploy
      with `agw_otlp_logs: true`, `agw_otlp_traces: false` and the receiver's trace
      rollout flag unset reaches the keyed probes. A deploy with only `agw_otlp_host`
      set fails with the named refusal. Together these prove scenarios "Access records
      ship without trace receipts" and "A receiver address alone is refused"

## 2. Gateway configuration deltas: sampling, team, content, guards

- [ ] 2.1 `config.yaml.j2` tracing (design decision 2): `randomSampling` defaults to
      `0.1` instead of `0.05` (line 103), and `clientSampling` renders the literal
      `false` (line 104). The landed guard (0, 0.1] is unchanged
- [ ] 2.2 `config.yaml.j2` access log (design decision 3): `accessLog.add` carries
      `identity: apiKey.name` and `team: apiKey.team`; `accessLog.otlp.fields.add`
      carries `service`, `identity`, `team` and `signal: '"access-log"'`. `signal` goes
      in the OTLP list only
- [ ] 2.3 Team marker (design decision 5): every `apiKey` entry renders
      `metadata: {name, team}`. Inventory identities take `team` from
      `agw_client_policies.<name>.team`, with no default. Coordinate with
      `inference-personal-keys` task 4.2 so user keys render `team: uhstray`, and append
      a dated pointer line to that change's task list
- [ ] 2.4 Content mode (design decision 4): render
      `frontendPolicies.accessLog.database.llm` from `agw_content_logging`, which is
      `full` or `metadata` and defaults to `metadata`. The mode is always rendered and
      never omitted
- [ ] 2.5 `deploy-agentgateway.yml` render guards, placed before
      `Manage secrets and render env + config` and not `no_log`, because they read
      inventory only (design decision 6):
      - refuse any identity without a `team`, naming it
      - while `agw_content_logging == 'full'`, refuse any identity whose team is not
        `uhstray`, naming it
      - refuse an `agw_content_logging` value outside `full` and `metadata`
      - refuse a team value outside `^[a-z0-9][a-z0-9-]*$`, the charset the playbook
        already enforces for names (lines 86-93)
      - while `agw_content_logging == 'full'`, refuse unless the gateway environment sets
        `UI_READ_ONLY=true` (design decision 5; `inference-gateway-agentgateway` task
        1.10, PR #303). The guard runs before the render like the others, so it reads
        the committed `templates/env.j2` on the controller (a `lookup('file')`), not an
        inventory flag, and requires the literal line `UI_READ_ONLY=true`; removing
        that line from the template fails a `full` deploy
- [ ] 2.6 BATS scan of `config.yaml.j2` (design decision 6): extend the key-position scan
      at `test_service_agentgateway.bats:145-152` so it fails on `llm.prompt`,
      `llm.completion`, `request.headers`, `request.body` or any `apiKey.` member other
      than `name` and `team`, anywhere outside a Jinja comment. Mutate it once with a
      planted `request.headers.authorization` field and watch it go red
- [ ] 2.7 BATS render tests:
      - `clientSampling` is `false`, and the sampling default is `0.1`.
      - `identity` and `team` appear in both field lists; `signal` appears only in the
        OTLP list.
      - Every key renders a `team`.
      - `database.llm` renders `metadata` when the variable is unset and `full` when it
        is set.
      - Each refusal in 2.5 fires, including `full` against an environment without
        `UI_READ_ONLY=true`.
      - `compose.yml` mounts the gateway config `:ro`, and the mount source is
        `config.yaml` or a directory that holds only `config.yaml`, never the deploy
        directory or `.env` (design decision 5; it stays true if
        `inference-gateway-agentgateway` task 1.12 moves the config to a directory
        mount).
- [ ] 2.8 `compose.yml`: rewrite the header comment that says the database holds "nothing
      a client needs back" (lines 7-8) so it names the request-log content and its
      retention
- [ ] 2.9 Local proof through `Deploy agentgateway (Local)` with
      `agw_content_logging: full`: readiness 200; a keyed round-trip 200 for a team
      identity, and a payload row for it (counts only, inside `agentgateway-db`). A
      temporary identity declared with a non-team value is refused at render. Record
      the results in `platform/services/agentgateway/context/architecture.md`
- [ ] 2.10 Content access check, local: the log detail request for a stored entry
      through `https://admin.inference.<local zone>/api/logs/get` is refused without the
      Authentik login, and refused for a signed-in user outside `platform-admins`; the
      conversation view renders for an admin. unverified today: that `ui.policies`
      covers the `/api/logs` paths, as the source reads (`types/local.rs:2060-2070`,
      `:3968-3975`). Also record, from the v1.5.0 source and one local start, whether
      the gateway can run with no admin listener while the UI listener keeps working;
      the design's accepted risk on the admin listener's unauthenticated log API
      (Risks) stands until this says it can
- [ ] 2.11 Handout notice: the document the team's keys are handed out with (the gateway
      change's task 4.4 updates dgx-spark `docs/TEAM-ENDPOINT.md`; personal keys use
      their own handout) states that prompts and completions are kept for 90 days and
      are readable by platform admins
- [ ] 2.12 site-config: declare `team: uhstray` for every `agw_clients` identity,
      including the enrolled legacy shared key and `skynet`, which Joe confirmed as
      team-only on 2026-09-27. Only after 3.2's schedule is live and
      `inference-gateway-agentgateway` task 1.10 (PR #303) is merged and deployed, set
      `agw_content_logging: full` and `agw_request_log_retention_days: 90`. (#303 merged
      2026-09-28; the deploy is still to run)
- [ ] 2.13 Validation gate: 2.6's planted field fails the scan, which proves scenario
      "Content-capturing configuration is refused". 2.9 proves scenarios "A non-team
      identity is refused at render" and "A team request's content is stored". 2.10
      proves scenario "Content is readable only by platform admins". 2.7's refusal of `full`
      without `UI_READ_ONLY=true` proves scenario "Content logging needs a read-only UI"

## 3. Content retention (design decision 7)

- [ ] 3.1 `platform/playbooks/prune-agentgateway-request-logs.yml`: run
      `DELETE FROM request_logs WHERE completed_at < now() - interval '<N> days'` inside
      `agentgateway-db` through the container engine, with `N` taken from
      `agw_request_log_retention_days` (default 90, integer asserted). Payload rows go by
      cascade (`0001_create_request_log_schema.sql:26`). Report the deleted count and
      the oldest remaining row's age in days, nothing else. A second run deletes
      nothing. Record the result with `tasks/emit-step-result.yml`
- [ ] 3.2 `platform/semaphore/templates.yml`: add `Prune agentgateway Request Logs`, with
      `dev_variant: true` and a daily `schedule:` in the form at `templates.yml:446-449`.
      BATS asserts the schedule, the integer guard, and that no statement selects a
      payload column
- [ ] 3.3 unverified: whether `collect-service-conformance.yml` reads this template's
      step result and writes its Loki line (`collect-service-conformance.yml:22-23`
      describes one line per workflow step result). Read the collector's template
      selection. If it does, the alert in 3.4 reads that line. If it does not, the prune
      pushes the same fields to Loki from the receiver host, labelled
      `service="agentgateway", signal="request-log-prune"`, and BATS asserts the push
      carries counts only. Resolved 2026-09-29: it does not. The collector selects only
      templates that map to a registry step (`platform/workflows/service-onboarding/lib/step_results.py`,
      `select`), so the prune pushes its own Loki line (the second branch)
- [ ] 3.4 **[o11y]** Grafana alert rule: fire when no prune result has arrived in 26 hours,
      or when the reported oldest age exceeds the retention plus two days. Task 3.6
      waits on it
- [ ] 3.5 Backup exposure: read-only, through Semaphore, find whether any Proxmox backup
      job covers the gateway VM (unverified today), and record its retention in
      `design.md`. Add to `platform/services/agentgateway/deployment/README.md` that any
      database backup excludes `request_log_payloads` data
- [ ] 3.6 Validation gate: a local prune run with retention `0` removes the rows and a
      second run deletes nothing, which proves scenario "An expired row is removed". With
      the local schedule paused past the alert window, 3.4's rule fires, which proves
      scenario "A stopped prune is detected". A gateway deploy during that window still
      succeeds, which proves scenario "A stale prune never blocks a gateway deploy"

## 4. Access records in Loki (design decisions 3 and 8)

- [x] 4.1 **[o11y]** Add `signal` to the label hint at `config.alloy:118-123`, so the
      hint names `service,signal`. Tasks 4.3 and 7.1 wait on it
- [ ] 4.2 unverified: the line format `otelcol.exporter.loki` v1.5.1 produces for a
      gateway record, whether a keyed `GET /v1/models` record carries `identity`, and
      the field v1.5.0 uses for a rejection reason. In local-dev with `agw_otlp_logs: true`,
      record one real line each for a keyed `/v1/models`, a keyed chat completion and a
      401, with the identity reduced to its name. Record them in
      `platform/services/o11y/deployment/README.md`, and write 4.3's query against them
- [ ] 4.3 `deploy-agentgateway.yml` Phase 3, when `agw_otlp_logs` is true:
      - Before the keyed probe, mark a time on the receiver clock, as lines 279-283 do.
      - After the probe, query Loki from the receiver (`exec o11y-grafana wget` to
        `loki:3100/loki/api/v1/query_range`) for `{service="agentgateway", signal="access-log"}`
        carrying the verifying identity at or after the mark, with bounded retries.
      - The probe is keyed `/v1/models` if 4.2 shows that record carries `identity`;
        otherwise it is the chat completion.
      - Print the record's status and timestamp only. The query holds no key, so it is
        not `no_log`.
      - A 429 from the gateway's own bucket still satisfies the check if 4.2 shows a
        refused request leaves a record.
- [ ] 4.4 BATS: the Loki receipt selects on `signal="access-log"` (a stdout line cannot
      satisfy it), runs only when `agw_otlp_logs` is true, and its task carries no
      `Authorization` header
- [ ] 4.5 Local note: with the socket source (`config.alloy:10-37`), the gateway's stdout
      also lands in local Loki as `service="agentgateway"`. The landed log check at
      `verify-o11y-service.yml:76-100` matches either source. Record in
      `platform/services/o11y/deployment/README.md` that locally only the
      `signal="access-log"` stream proves OTLP delivery
- [ ] 4.6 Validation gate: in local-dev, then production, 4.3 passes. This proves
      scenarios "A request is findable by identity" and "Identity is not an index label"
      (the stream's label set holds no `identity`, `team`, model or token label). One
      keyless request and one request burst past the bucket, both found in Loki with
      their status and reason, prove scenario "A rejected request still leaves a record"

## 5. Metrics (design decision 10)

- [ ] 5.1 `apply-firewall.yml`: an optional `firewall_detected_port_sources` (published
      port to a list of sources) that replaces `firewall_upstream_source` for the ports
      it names, leaving unnamed ports unchanged (today the product at lines 290 and
      306). `platform/tests/test_apply_firewall.bats` covers a named port, an unnamed
      port, and a named port that is not published (refused with its name)
- [ ] 5.2 unverified: whether `apply-firewall.yml` removes an allow rule it added on an
      earlier run when the source list for that port shrinks. Read the play and run it
      with `--check` against the gateway host. If it does not prune, add pruning of rules
      that carry the play's own comment tag, with a BATS case. Resolved 2026-09-29: it
      does, since #327. Every rule carries an `agent-cloud:` tag, and a tagged rule that is
      no longer declared is pruned; an untagged rule is only reported until a run with the
      declaration that produced it tags it. Left: the `--check` run on the gateway host,
      recording its would-delete list
- [ ] 5.3 `compose.yml` (gateway): add the labels `prometheus.io/scrape: "true"`,
      `prometheus.io/port: "19002"` and `prometheus.io/path: /metrics`. BATS asserts
      them
- [ ] 5.4 Local: deploy the gateway and o11y through the local Semaphore, and confirm the
      local Prometheus holds `agentgateway_requests_total` with the verifying identity's
      label (Alloy discovery, `config.alloy:49-100`)
- [ ] 5.5 site-config: set the gateway's `agw_stats_bind` to its LAN address, and set
      `firewall_detected_port_sources` naming the stats port with the o11y host as its
      only source. Run `Deploy agentgateway (Dev)`, then `Apply Firewall (Dev)` on the
      gateway host
- [ ] 5.6 `Probe o11y Metrics Endpoint (Dev)` with `probe_target=agentgateway`: HTTP 200
      from the o11y host, in the order `platform/services/o11y/deployment/README.md:28-34`
      gives. A probe from the controller to the same port is refused
- [ ] 5.7 site-config declares `agentgateway_metrics_address` and
      `agentgateway_metrics_port` in its own pull request, then `Deploy o11y (Dev)`. The
      gateway's scrape receipt (1.4) then runs
- [ ] 5.8 Validation gate: 1.4's receipt passes after a keyed request through the public
      route, which proves scenario "Metrics target is up and carries the identity". 5.6's
      refused probe proves scenario "Metrics listener refuses other hosts"

## 6. Transport: mutual TLS on the landed bind (design decision 9)

- [ ] 6.1 **[o11y]** The receiver's `tls { cert_file, key_file, client_ca_file }` block,
      its server leaf, and the name of the receiver-side declaration that turns it on.
      unverified today: that name. Record it in `design.md` when the o11y session picks
      it. 6.2 waits on it
- [ ] 6.2 `config.yaml.j2`: when the receiver declares TLS (read from
      `hostvars[groups['o11y_svc'][0]]`, as line 108 of the deploy reads
      `o11y_otlp_bind`), render `policies.backendTLS.{root, cert, key, hostname}` on
      `accessLog.otlp` and on `tracing`. `compose.yml`: mount a deploy-created
      `./certs/otlp` directory read-only. The deploy refuses to render plaintext against
      a TLS-declared receiver
- [ ] 6.3 Issue the gateway's OTLP client leaf through `production-internal-ca` task 4.1
      (the key is generated on the gateway host). unverified: how v1.5.0 reports an
      unreadable client key. Observe it once in local-dev, with the key's mode
      deliberately wrong, and record the log line
- [ ] 6.4 BATS: `backendTLS` renders exactly when the receiver declares TLS, and the
      plaintext-against-TLS refusal fires
- [ ] 6.5 Validation gate: a Semaphore-run TLS handshake to the receiver without a
      client certificate fails, which proves scenario "A sender without a client
      certificate is rejected". A render with a TLS-declared receiver and no leaf path
      fails, which proves scenario "Plaintext export is refused once the receiver
      requires TLS"
- [ ] 6.6 Follow-up, recorded and not built: Alloy `v1.5.1`'s receiver cannot check a
      client certificate's name, so the firewall rule is the only binding of sender to
      gateway. On each Alloy upgrade, read the receiver's server TLS arguments at the new
      tag, and record the version read in `design.md`

## 7. Dashboards and other o11y-side dependencies (design decision 11)

- [ ] 7.1 **[o11y]** Operations dashboard: extend `agentgateway-traffic.json` and rename
      it "Agentgateway operations". This change asks to keep the uid. Add tokens by
      identity, model and `gen_ai_token_type`, raw 4xx access records (reason grouping
      remains gated on a captured OTLP record and verified reason field/line shape from
      gateway tasks 4.2/4.3), a `Rate-limited requests (429)` panel using the status
      selector on `agentgateway_requests_total`, `agentgateway_build_info`, access records
      (`{service="agentgateway", signal="access-log"}`, which needs 4.1) and a Tempo
      search on uid `tempo`. Update the panel-count assert at `deploy-o11y.yml:502`.
      The upstream [Prometheus guide](https://agentgateway.dev/docs/standalone/latest/documentation/observability/metrics/prometheus/)
      documents the v1.5.0 metrics endpoint and says `agentgateway_requests_total`
      is broken down by status; task 4.6 must still confirm a real 429 appears in
      that series before claiming rate-limit coverage. The panel-count and query
      implementation is present, but leave this task open until tasks 4.2/4.3 establish
      the access-record stream and reason extraction, and task 4.6 confirms live 429 data.
- [x] 7.2 **[o11y]** Client-view dashboard: `agentgateway-client-view.json`, uid
      `agentgateway-client-view`, with p50 and p95 first-token latency, request
      duration, the 4xx and 5xx ratio, and per-identity request rate, plus an `identity`
      variable. The o11y deploy asserts it the way lines 480-504 assert the traffic
      dashboard
- [x] 7.3 **[o11y]** Replace the hard-coded `cluster = "agent-cloud-local"` at
      `config.alloy:44` and `config/prometheus.yml:8` with a value rendered from
      inventory
- [ ] 7.4 Validation gate: `Deploy o11y (Local)` without removing containers' persistent
      volumes, then one keyed request. The local
      client-view and operations dashboards show that identity in metrics and in an
      access record, which proves scenario "Local deploy shows metrics and access
      records"

## 8. Traces (landed path; the deltas are sampling and the standing signal)

- [ ] 8.1 Validation gate: with 1.2 and 2.1 deployed and `agw_otlp_traces: true`, run
      the landed `Verify o11y Service` with `expected_service=agentgateway`,
      `expect_traces=true` and `emit_agentgateway_canary=true`
      (`verify-o11y-service.yml:41-68`, `:106-132`). A trace found proves scenario "A
      sampled request is searchable in Grafana". No sampling-at-one window is used,
      because the landed guard caps sampling at 0.1. The same run proves
      `observability-estate` task 3.3's pilot
- [ ] 8.2 **[o11y]** Trace-silence alert (design decision 8): fire when
      `rate(tempo_distributor_spans_received_total{job="tempo"}[30m]) == 0` while
      `increase(agentgateway_requests_total{job="agentgateway"}[30m]) >= 100`, and only
      while the gateway's `agw_otlp_traces` is true. The counter is already scraped
      (`config/prometheus.yml:31-33`), so no new scrape is needed. unverified: whether
      another service sends spans to this Tempo. If one does, the o11y session picks a
      gateway-specific series
- [ ] 8.3 Client-started traces: in local-dev, send 20 requests, each carrying a distinct
      `traceparent` with the sampled flag set, and search Tempo for each of those trace
      ids. Fewer than 20 are found, where `clientSampling: true` would find all 20. This
      proves scenario "Caller-supplied trace context does not force a trace". BATS in 2.7
      asserts the rendered `false`
- [ ] 8.4 Validation gate: a gateway deploy with `agw_otlp_traces: true` against a
      receiver without its receipts is refused by the landed gate, which proves scenario
      "Trace enablement waits for the rollout gate"

## 9. Measure, record, reconcile

- [ ] 9.1 After seven days in production, record in `design.md`: spans received per day
      (`tempo_distributor_spans_received_total`), gateway access lines per day in Loki,
      active gateway series in Prometheus, the `request_logs` row count, and the size of
      `request_log_payloads`. If the payload table's size slows budget accounting, move
      the request log to its own `config.logging.database` in a follow-up change
- [ ] 9.2 Docs: update `platform/services/agentgateway/context/architecture.md` (signals,
      switches, ports, content rule) and `platform/services/agentgateway/deployment/README.md`
      (switches, prune, backup exclusion), plus the root `AGENTS.md` rows the branch
      workflow requires
- [ ] 9.3 Append dated pointer lines, never edits, to the sibling changes' task lists:
      `inference-gateway-agentgateway` 3.1 (superseded) and its design decision 6 (the
      Tempo deferral is reversed by the landed o11y work); `observability-estate` 3.3
      (delivered by 8.1); `inference-telemetry-production` 1.4 (per-port sources
      available after 5.1)
- [ ] 9.4 Validation gate:
      - After a private site-config inventory change is reviewed and merged, run the
        code-managed operator-side `platform/semaphore/sync-inventory.yml` check and
        apply from the reviewed agent-cloud worktree against Semaphore. Set
        `inventory_source` to the production inventory file from the reviewed
        site-config revision, then verify Semaphore readback matches the source before
        a Dev-bound deploy relies on the values. Pin that deploy to the exact reviewed
        and pushed `dev` SHA. A site-config merge alone does not update Semaphore's
        static inventory copy.
      - After an hour of production traffic, run the normal `Deploy o11y (Dev)`
        Semaphore template from the merged `dev` revision, preserving Prometheus, Loki,
        Tempo and Grafana volumes. Read back both dashboards and the previously recorded
        telemetry timestamps, then confirm the client view renders first-token
        percentiles and per-identity counts from new traffic. This proves scenario
        "Dashboard survives a non-destructive redeploy" and the gateway change's
        "Client-view latency on the dashboard". A volume-wipe recovery drill requires
        a separate explicit request and is not an acceptance condition here.
      - A team request carrying a unique marker string leaves no match in Loki or in
        Tempo, which proves scenario "Loki holds no prompt text".
      - Neither the caller's key nor its Authorization value appears in the database,
        Loki or Tempo, which proves scenario "Header and key are never stored".
      - On archive, retain the outcome (worked / dead end / corrected) into bank
        `agent-cloud-750a33b9`.
