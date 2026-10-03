# Tasks: agentgateway as the inference edge gateway

## 0. Branch, decision record, host
- [x] 0.1 Feature branch from `dev`: `feat/inference-gateway-agentgateway`. Pull requests
      only when Joe asks for them (repo rule). 2026-09-17: the exact name was held by a
      stale codex worktree (one superseded proposal commit), so the work is on
      `feat/inference-gateway-agentgateway-impl`; the stale branch is Joe's to delete
- [x] 0.2 Draft the `plan/architecture/` record (agentgateway inference edge; skynet
      orchestrating gateway; distinct authorities; alternatives) as Proposed; Joe confirms
      the text before it is Accepted. Drafted 2026-09-17 as a Proposed section in
      `plan/architecture/05-platform-infra.md` (the repo's decision convention: numbered
      docs, no separate ADR directory)
- [x] 0.3 Allocate the gateway VM in site-config `proxmox/vm-specs.yml` (Infrastructure
      tier, Podman); provision through onboarding phases 1 to 2; AppRole
      `agentgateway` with read on `secret/services/agentgateway`
      2026-09-17 PARTIAL: declared on site-config branch `feat/agentgateway-host` —
      vm-specs entry on one hypervisor node (2c/4 GB/20G, podman; node and vmid in site-config) and the `agentgateway_svc`
      inventory group (upstream = the current Caddy inference upstream, four
      identities, firewall vars). Address chosen from the inventory's declared set
      because NetBox (IPAM) was unavailable; recorded as a future feature in
      `plan/architecture/02-service-onboarding.md` Known Gaps. NOT yet done, blocked
      off-LAN (prod Semaphore is 403 from outside): NetBox reserve, Provision VM,
      SSH key generate/distribute/verify/harden, Apply Firewall, AppRole.
      2026-09-18 (on-LAN, via the prod Semaphore API with the operator token): inventory
      synced to record `production` (sync-inventory.yml, operator-side); `vm_*` added to the
      host because the runner never sees vm-specs.yml. NetBox reservation NOT done: the
      IPAM read needs `secret/services/netbox:automation_api_token`, which is absent, and
      `Provision NetBox Automation Token` (task 1058) fails inside its no_log Django-shell
      mint — NetBox side, out of this change; the address keeps its provenance note.
      vmid moved to a free one: the first choice and its neighbour were the GitHub runners',
      absent from the ledger (adopted into vm-specs); provision-vm.yml gained a foreign-VM refusal guard (MISTAKES 3.5).
      Provisioning on that node then hit Proxmox's rule that a cross-node clone needs SHARED
      source storage (the template sits on another node's local LVM storage; task 1064) —
      fixed as clone-on-template-node + offline migrate in agent-cloud PR #188 (from dev,
      Joe's call); `Provision VM (Dev)` runs it once merged. SSH keypair minted (task 1061)
      and backed up to site-config branch `backup/ssh-agentgateway-20260918T115852Z-6a5828`
      (task 1063, dev-bound template — the playbook is not on main yet).
      Provision VM (Dev) with the clone-then-migrate path then created the VM
      (task 1068), but at an address that turned out to be gh-runner-01's: the runners
      were declared only on site-config's unmerged `feat/apply-firewall` (MISTAKES 3.6).
      Joe's decisions: destroy the VM AS CODE (`destroy-vm.yml`, PR #189, with an
      address-answers refusal in provision-vm), re-provision at another address (network-swept,
      undeclared on every branch), and fold the runner declaration into the inventory
      (done; Semaphore record re-synced). PR #189 merged; `Destroy VM (Dev)` stopped the VM
      but Proxmox's unreferenced-disk scan aborted on its node ("no such logical volume
      pve/data": the cluster-wide local-lvm storage is absent on that node). Joe rejected
      skipping the scan (dead disks waste space); PR #190 makes the play sweep the node's
      ACTIVE image storages for leftover volumes itself. Merged; `Destroy VM (Dev)` task 1075
      destroyed the VM cleanly: the node's image storages swept, zero leftovers, vmid gone.
      DONE 2026-09-18: `Provision VM (Dev)` task 1076 re-created the VM at the new address (address
      guard passed, clone on the template's node + offline migrate); cloud-init done; Distribute SSH
      Keys 1078; Verify Host Access 1079 (controller side) + workstation key-only login;
      Harden SSH 1080 (password REJECTED, key CONFIRMED, NOPASSWD sudo). Apply Firewall waits
      for the gateway deploy so port auto-detection sees the containers. Per-service AppRole
      not created: the composable deploy runs under the controller AppRole like every other
      service (onboarding step 9 is optional)
- [x] 0.4 Validation gate: `openspec validate inference-gateway-agentgateway --store
      agent-cloud` passes; record file exists as Proposed; VM answers over the
      distributed key. 2026-09-18: all three hold (validate passes; plan 05 section is
      Proposed; key-only SSH to the VM from both the controller and a workstation)

## 1. Service onboarding
- [x] 1.1 `platform/services/agentgateway/deployment/compose.yml`: image
      `${AGW_IMAGE:-cr.agentgateway.dev/agentgateway:v1.5.0}`, command `-f
      /config.yaml`, config mounted read-only, publish `${AGW_BIND:-127.0.0.1}:${AGW_PORT:-4000}:4000`,
      `readinessAddr` health check, no admin port published; `compose.local.yml`
      overlay per the o11y pattern
      2026-09-17: done, plus `agentgateway-db` (Postgres for per-key budgets, operator's choice) — see 4.2; no compose healthcheck on the gateway (image has no shell), readiness probed from the sibling db container
- [x] 1.2 `templates/config.yaml.j2`: `llm.port: 4000`; one model entry per served name
      with `provider: {custom: {formats: [{type: completions}]}}`,
      `params.baseUrl: http://{{ agw_vllm_upstream }}/v1`, `params.apiKey: $VLLM_API_KEY`;
      `frontendPolicies.http.maxBufferSize: 20971520`; `config.statsAddr` on the LAN bind;
      `config.tracing.otlpEndpoint` to the o11y host; no `retry`, no `requestTimeout`
      2026-09-17: done; `config.database.url: $AGW_DATABASE_URL` added; limits shape per 4.2
      2026-09-28 correction (the checked text above is kept as written): two names in it
      are not what landed. The upstream variable is `agw_upstream_base_url`, rendered
      whole as `params.baseUrl: {{ agw_upstream_base_url }}` (`config.yaml.j2:190`;
      asserted at `deploy-agentgateway.yml:76-77`); `agw_vllm_upstream` appears in no
      commit under `platform/` (`git log -S`, 2026-09-28). Tracing is
      `frontendPolicies.tracing` (`config.yaml.j2:99-106`, PR #295), not
      `config.tracing.otlpEndpoint`. Also as landed: the listener is
      `gateways.default.port: 4000` (lines 56-58), and `statsAddr` is `0.0.0.0:19002`
      inside the container (line 35), published on `AGW_STATS_BIND`, which defaults to
      loopback (`templates/env.j2:13`). The same applies to 1.8's
      `config.logging.fields.add.identity`, now `frontendPolicies.accessLog.add.identity`
      (lines 85-87)
- [x] 1.3 `templates/env.j2` (gitignored `.env` at deploy): `VLLM_API_KEY` from OpenBao
      `secret/services/agentgateway:vllm_api_key`; client keys rendered into the
      `apiKey` policy from `secret/services/agentgateway/clients/*`. 2026-09-17: client
      keys live as fields `client_<name>` on the SAME secret path (manage-secrets reads
      one path per service and already does generate-once/reuse), rendered as sha256
      hashes — the rendered config carries no plaintext key
      2026-09-17: done (see the note above on where client keys live)
- [x] 1.4 Read `statsAddr` once on the running container and record what it serves in
      `context/architecture.md`; if it is not Prometheus text, find the documented
      metrics endpoint and update task 3.1. 2026-09-17: Prometheus text on `/metrics`
      (`/` and `/stats` 404); task 3.1 scrapes `/metrics` on the stats port
- [x] 1.5 `deploy-agentgateway.yml` (place-monorepo, manage-secrets, deploy.sh, verify
      readiness and one `/v1/models` through the gateway) and `clean-deploy-agentgateway.yml`
      2026-09-17: done; carries the zero-hosts pre-flight and the OpenBao transport guard (both found missing on the first runs — MISTAKES 2.21); verify runs over the compose network, no keyed request (a key on an exec argv is a token on a command line — the keyed proof is task 2's conformance script)
- [x] 1.6 `platform/tests/test_service_agentgateway.bats`: env-param image, pinned tag,
      readiness health check, no published admin port, config template has no literal
      key and no `retry` block, `deploy.sh` container-only
      2026-09-17: 18 tests green; full suite 576 BATS + 120 pytest
- [x] 1.7 Validation gate: second deploy reports no changes and readiness answers,
      proving scenario "Deploy converges"; `nc` to the admin port from a LAN host is
      refused, proving scenario "Admin interface is not exposed"

      2026-09-17 LOCAL (LM Studio upstream, local Semaphore tasks 595 then 596): second run `changed=2` — the deploy.sh shell step (`changed_when: true` by the repo's convention) and the monorepo copy; every other task unchanged, readiness 200 before and after, secrets reused. Admin port: zero published mappings and connection refused from the Mac; the LAN-host refusal is re-proven on the prod VM
- [x] 1.8 Operator UI (design §9): `gateways.ui` on :4001 + `ui.gateways: ui` +
      `ui.policies.oidc` (issuer/redirect from inventory, `$AGW_OIDC_CLIENT_SECRET`) +
      `authorization` admin-group rule in the config; `OIDC_COOKIE_SECRET` derived from a
      stored seed; compose publishes `AGW_UI_BIND:AGW_UI_PORT` (local overlay removes it);
      Authentik catalog entry `agentgateway` (oidc, admin tier, `prod_required`
      redirect/launch vars) + `agentgateway-oidc.yaml` with `!Env
      AGENTGATEWAY_OIDC_CLIENT_SECRET`, shared-read by the gateway deploy; step-ca trust
      via `SSL_CERT_FILE` locally; local Caddy route `admin.inference.<zone>` ->
      `agentgateway:4001` as a PLAIN proxy in the example, working inventory and the
      control plane's route table; BATS. Prod: site-config `caddy_managed_sites` block +
      `agentgateway_external_host` on the authentik host + `agentgateway` in `authentik_apps`
      Playground (Joe, 2026-09-17): `llm.gateways: [default, ui]` so the UI calls /v1 on
      its own origin through Caddy (verified apiKey-gated, not OIDC-gated; completion
      round-trips at admin.inference.agent-cloud.test/v1). Observability per upstream:
      `config.metrics.fields.add.identity` + `config.logging.fields.add.identity`
      (first-token latency is already a default log field on streams). Upstream key per
      the api-keys doc: `params.apiKey: $VLLM_API_KEY` env reference, omitted when the
      upstream takes no key; proven 2026-09-17 with LM Studio switched to a required token,
      seeded via `seed-openbao-key.yml` (task 616: completions 200, no plaintext in config)
- [ ] 1.9 Validation gate: unauthenticated browser to `https://admin.inference.<zone>` is
      redirected to Authentik; after `agent-cloud-admin` login the UI renders and
      `/ui/api/config_dump` is reachable only through that path, proving scenario "UI
      requires an admin login"; on prod, `nc` to :4001 from a non-Caddy LAN host is refused.
      2026-09-17 LOCAL: `admin.inference.agent-cloud.test` resolves (wildcard), the served
      cert carries `*.inference.agent-cloud.test`, an unauthenticated GET is a 302 to
      Authentik's authorize endpoint, the outpost ping answers 204, the worker applied
      the blueprint; the local overlay removes the UI host publish so no unauthenticated
      path exists. Browser login as `agent-cloud-admin` and the prod `nc` are Joe's/prod

- [x] 1.10 `UI_READ_ONLY=true` in the gateway environment, and a BATS assertion that it is
      rendered; the UI must refuse writes itself, not only fail at the read-only mount
      (design "Decisions recorded 2026-09-27"). v1.5.0 reads the variable and switches the
      config store to read-only (`crates/agentgateway/src/config.rs:390-392` at tag
      v1.5.0), and the UI refuses writes in that mode (`crates/agentgateway/src/ui.rs:53`).
      This is the only UI read-only task; `inference-personal-keys` relies on it. Proves
      scenario "The UI refuses configuration writes"
      2026-09-28: done in PR #303 (`templates/env.j2` renders `UI_READ_ONLY=true`
      unconditionally, plus a BATS assertion on the rendered env). Merged into `dev` on
      2026-09-28 (merge `255b251`). `agentgateway-observability`
      decision 5 makes it a prerequisite for `agw_content_logging: full`
- [ ] 1.11 Operator UI in production: Cloudflare record `admin.inference` (applied
      2026-09-27, Apply Cloudflare Tofu (Dev) task 1616, zero-diff 1617), the Authentik skip
      rule (PR #289) applied, `agw_ui_enabled: true` (site-config #35), then Deploy
      agentgateway (Dev); proves gate 1.9
- [ ] 1.12 Change-aware deploy (design decision 11). Today `deploy.sh:47-53` runs
      `compose up -d --force-recreate` on every run, so every deploy, and every playbook
      that imports it, drops in-flight streams. First settle hot reload: v1.5.0 watches a
      file config source and reloads it on change (`crates/agentgateway/src/state_manager.rs:141-142`
      and `150-180` at tag v1.5.0, read 2026-09-28), but `compose.yml:48` mounts
      `config.yaml` as a single file, and a template write replaces the inode the container
      still holds (the lesson at `manage-caddy-sites.yml:14-20`). In local-dev, mount the
      config through a directory, change one `apiKey` entry, and record whether the running
      process serves the change with no restart and whether a failed reload keeps the old
      state (`state_manager.rs:301-311` logs the error and keeps the previous state). Then:
      - **Hot reload confirmed:** the config becomes a directory mount; a `config.yaml`
        change is left to the watch and proven by the verify phase's keyed probe (6.1a);
        only an `.env` change recreates, because a process's environment is fixed at start.
        The directory stays mounted `:ro` and holds `config.yaml` only: the deploy renders
        into a dedicated subdirectory, never mounts the deploy directory, and never places
        `.env` (0600, database URL and upstream key, `templates/env.j2:26`, `:32`) where
        the container can read it as a file. The drill's directory mount follows the same
        rule. `agentgateway-observability` decision 5 depends on this, and its task 2.7
        asserts it in BATS.
      - **Not confirmed:** `deploy.sh` computes the sha256 of the rendered `config.yaml` and
        `.env`, reads the value recorded in a label on the running `agentgateway`
        container, and passes `--force-recreate` only when the two differ; otherwise it
        runs plain `compose up -d`, which creates a missing container and starts a stopped
        one. unverified: that the podman compose provider starts a stopped container on
        plain `up -d`; the local drill checks it.
      Either way the deploy task's `changed_when` reports a recreate or a reload, not the
      constant `true` it carries today (`deploy-agentgateway.yml:206`). BATS: the recreate
      flag is conditional and the label is written. Validation: a second deploy with no
      input change leaves the container's start time unchanged; a rotated key is served
      after one deploy; a deploy whose `deploy.sh` is made to fail after the render is
      converged by the next plain deploy, because the label still names the old hash;
      with the container removed, a plain deploy brings it back. Together these prove
      scenario "An unchanged deploy does not restart the gateway"

## 2. Conformance against direct vLLM
      Added 2026-09-22 (security review): the gateway's `platform-admins in jwt.groups` rule
      has never been shown DENYING anyone — log in as a platform-developers member and
      require a refusal at the gateway (the IdP-side admin binding is the other half).
- [ ] 2.1 Conformance script `platform/services/agentgateway/deployment/tests/conformance.sh`
      (curl + jq): models list; chat completion thinking off; `reasoning_effort` each of
      the seven values; `chat_template_kwargs` override; tool call; streamed request with
      `xhigh` and timing of first token and inter-chunk gaps; one Responses API request;
      each sent to the gateway and to vLLM directly from the gateway VM
      2026-10-02: code landed, not yet run in production. The playbook is
      `run-agw-conformance.yml` (Semaphore `Run agentgateway Conformance`). The runner reads
      the keys from OpenBao and places them as 0600 files in a 0700 directory on the VM,
      removed in an `always:`. curl gets each key through a pipe, never argv. Under listener
      TLS the gateway is reached by its SAN with `--resolve` and the `agw-verifier` leaf.
      Output is one JSON line per case and target: status, a normalised-body sha256, a shape
      sha256, semantic fields, and timings, including first token and gap statistics for the
      stream. Thinking off is `chat_template_kwargs.enable_thinking: false`; the override is
      `chat_template_kwargs.reasoning_effort: low`. The seven efforts are dgx-spark's
      (`plans/development/openspec/specs/inference-endpoint/spec.md`, `vllm/public_probe.py`).
      `platform/tests/test_agw_conformance.py` runs it against stub servers.
- [ ] 2.2 Diff bodies (ignoring ids and timestamps); record first-token and gap deltas
      in `context/architecture.md`
      2026-10-02: `conformance.sh diff` is the comparison, and the playbook runs it. A case
      matches only when both sides succeeded (curl exit 0, 2xx) and agree on status, shape and
      semantic fields; any failure is an error naming each side's status (PR 409 review).
      Model names go through the inventory's name-to-`upstream_model` map, passed as a file. The exact-body hash is reported but does
      not decide the verdict. The playbook fails on any case that does not match. Recording
      in `architecture.md` waits for the first production run; the section is in place and
      says the results are pending.
- [ ] 2.3 Confirm SSE keep-alive comment lines from vLLM pass through unchanged and
      unbuffered (needs dgx-spark `inference-endpoint-reliability` deployed)
- [ ] 2.3a Streams and the budget: confirm whether a streamed completion is charged to the
      per-key budget, and whether a model `overrides: {stream_options: {include_usage: true}}`
      entry makes vLLM report usage on streams without changing the client-visible contract
      (the budget is otherwise best-effort for streams; PR 191 review)
- [ ] 2.4 Validation gate: 2.2 proves scenario "Conformance against direct vLLM"; 2.3
      with a stream past 130 s proves scenario "Stream is not buffered"

## 3. Telemetry
- [ ] 3.1 o11y: `scrape.d/agentgateway.yml.j2` for the stats endpoint (`/metrics` on the
      stats port; series `agentgateway_requests_total`, `agentgateway_request_duration_seconds_*`,
      `agentgateway_gen_ai_server_request_duration_*`, `agentgateway_gen_ai_client_token_usage_*`
      — read on the running container 2026-09-17); Alloy
      `otelcol.receiver.otlp` on the o11y host forwarding spans as structured log lines to
      Loki (Tempo deferred); inference dashboard gains the client-view row
      2026-10-02 code state: the scrape job (`scrape-agentgateway.yml.j2`, rendered only
      when inventory declares the stats endpoint) and the OTLP receiver already existed,
      with spans going to Tempo rather than being deferred. Added: inventory-gated
      `o11y_gateway_span_logs_enabled` (default off) span-to-Loki lines via
      `otelcol.connector.spanlogs`, the model request-duration series on the client view,
      and a link to it from the inference latency dashboard. Open until the private
      inventory declares the endpoint and 3.3 proves it on live traffic
- [x] 3.2 Check whether `localRateLimit` has a log-only mode in the v1.5.0 schema; record
      the answer in `design.md` and set the first-week policy accordingly. 2026-09-17: no
      such mode (`RateLimitSpec` has only `maxTokens`, `tokensPerFill`, `fillInterval`,
      `type`); first week runs with loose figures tightened from observed rates
- [ ] 3.3 Validation gate: one hour of traffic renders p50 and p95 first-token latency and
      per-identity counts, proving scenario "Client-view latency on the dashboard"

## 4. Identities, limits, re-route
- [ ] 4.1 Virtual-key lifecycle (design §10). DONE 2026-09-17 in code: the deploy mints
      `client_<name>` once per `agw_clients` entry (fields on the service's secret path,
      not a `clients/*` subpath); `manage-agentgateway-client-key.yml` rotates (name must
      be declared) or revokes (name must be removed first; merge-patch null) one identity
      and re-renders/reloads; per-identity `allowedModels` + budget overrides via
      `agw_client_policies`; Semaphore templates shared + local; BATS. Local rotate drill
      run through the local Semaphore 2026-09-17 (task 615 after the Authentik tombstone,
      MISTAKES 6.5): the morning's key answers 401, the rotated value 200, gateway 0 restarts. LEFT for prod: mint the
      first set (`stray`, `opencode`, `pi`, `skynet` — declared in site-config) by the first
      deploy, hand each out with `backup-credentials-to-site-config.yml`, and enrol the
      legacy shared key as identity `legacy-shared` with the tightest budget and
      `legacy_shared_expires: <date>` in inventory (task 5.1 enforces the expiry)
- [ ] 4.2 `localRateLimit` per identity: `type: requests` per minute and `type: tokens`
      per hour, figures derived from the measured ceiling and written as comments.
      2026-09-17: v1.5.0 has no per-identity REQUEST bucket under `llm.policies` (`key`
      unreleased, `conditional` rejected); Joe chose per-key token BUDGETS backed by the
      service's own Postgres (design.md Open Questions). Shipped in the template: one
      global request bucket (`agw_rate_requests_per_minute_total`) + `hourly-tokens`
      budget per key (`agw_rate_tokens_per_hour`). Left for this task: derive both
      figures from the measured ceiling and write them into site-config inventory
- [ ] 4.3 site-config: production Caddy block upstream to the gateway; `Caddyfile.local.j2`
      `inference_api` unchanged in shape (upstream already comes from `r.upstream`);
      redeploy Caddy through Semaphore
- [ ] 4.4 Run dgx-spark's public-path verify play against the live hostname; update
      dgx-spark `docs/TEAM-ENDPOINT.md` (key changes, base URL does not)
- [ ] 4.5 Validation gate: an unenrolled key gets 401 at the gateway with no vLLM log
      line, proving scenario "Unknown key is rejected"; a burst from one identity gets
      429 while another identity is served, proving scenario "One client cannot exceed its
      share"; the gateway access log shows the public request, proving scenario "Public
      path traverses the gateway"; one inventory change and a Caddy redeploy restores the
      direct path, proving scenario "Rollback is one value during the grace period"
- [ ] 4.6 `rollback-inference-route.yml` with `mode=gateway-config|direct|restore` as in the
      proposal's Rollback Plan, each mode idempotent (re-run converges, no duplicate key
      publication, `restore` is a no-op once the upstream is the gateway and the key is
      rotated); BATS asserts the three modes exist and that `direct` never prints a key;
      drill `direct` then `restore` against the live route in a window Joe names, proving
      scenario "Rollback after retirement is the playbook"
      2026-10-02: code landed (PR #385); the drill is still owed. `restore` does not rotate
      the vLLM key (dgx-spark owns it); it refuses to retire the copies until the key is
      rotated and the running gateway holds the new one, proving scenario "Restore refuses
      while the published key is still live". One-step rotation needs dgx-spark to read the
      key from OpenBao (cross-repository, dgx-spark handoff). The tests are pytest, not
      BATS, because only a real run proves that no key is printed and that re-runs converge.
      `gateway-config` needs the deploy to keep the config it replaces as
      `config.yaml.previous`; until it does, that mode refuses

- [ ] 4.7 `legacy_shared_expires` = the route-switch date + 14 days (operator decision
      2026-09-27), set in site-config in the same change that switches the route

## 5. Retire the shared key, records
- [ ] 5.1 Retirement, enforced by the deploy rather than remembered: the config template
      renders `legacy-shared` only while today is before `legacy_shared_expires`; a deploy
      on or after that date drops the identity and fails if the vLLM key in OpenBao still
      equals the pre-rotation value, so a rerun cannot leave the shared key valid. Rotate
      `VLLM_API_KEY` at vLLM (dgx-spark `secrets/vllm_api_key` and a two-rank restart) and in
      OpenBao; record the retirement date in `context/architecture.md`; hand dgx-spark the
      gateway address for narrowing `vllm_api_allowed_cidr`
      2026-10-02: the deploy half landed (`config.yaml.j2` renders `legacy-shared` only before
      `legacy_shared_expires`; `tasks/agw-legacy-key-check.yml` records the shared key's sha256
      fingerprint once at `legacy_shared_key_sha256` during the grace period and, on or after the
      date, fails the deploy in its last play, after the gateway is recreated without the
      identity, while `vllm_api_key` still matches it; tests
      `platform/tests/test_agw_legacy_retirement.py`). Left: the rotation at vLLM and in OpenBao
      and the `vllm_api_allowed_cidr` narrowing are dgx-spark's and wait on the dgx-spark handoff;
      the retirement date is recorded in `context/architecture.md` once 4.7 sets it
- [ ] 5.2 Accept the architecture record; append a dated pointer line to
      `plan/development/06-inference-skynet.md`; `platform/services/inference/` stub
      gains a README pointing at the gateway service and the dgx-spark roadmap record
- [ ] 5.3 Validation gate: 5.1 proves scenario "Grace period ends"; the record and the
      plan 06 line prove scenario "Decision is findable and plan 06 is amended"; on
      archive, retain the outcome (worked / dead end / corrected) into bank
      `agent-cloud-750a33b9`

## 6. Transport security (decisions of 2026-09-27; needs `production-internal-ca`)
- [ ] 6.1 Gateway API and UI listeners serve HTTPS from step-ca-issued certificates in a
      mounted directory (`current/`; the image has no shell); `tls.root` = the step-ca
      root, so a client certificate is required. This task is the single owner of the
      client allowlist (design decision 12): both gateways carry the `require` rule
      `source.subjectAltNames.exists(n, n in [<allowlist>])`, because `root` alone admits
      any leaf from the CA. The list renders from inventory `agw_client_cert_allowlist`,
      whose entries are leaf names from `production-internal-ca` design decision 4's table,
      resolved to their declared SANs (default `caddy` only; production adds
      `agw-verifier`, and `bench` once the benchmark VM exists); an entry naming no
      declared client-profile leaf fails the deploy before restart, naming the entry.
      In local-dev first: confirm the rule renders beside the `llm` shortcut and the gateway
      starts with it, and record the HTTP status a failed rule returns and whether the rule
      is evaluated before API-key authentication (both unverified). If the rule cannot be
      rendered there, fall back to a dedicated client-issuing hierarchy with the gateway's
      `root` set to it alone, and record why as an amendment to decision 12. BATS asserts
      both listeners render `tls` with `root` and the rule, that the default list is
      `caddy` alone, and that an undeclared entry is refused
- [ ] 6.1a The one gateway probe path. Every check that sends a request to the gateway
      from outside Caddy uses it: this deploy's own verify, the personal-key 401 gates
      (`inference-personal-keys`), the renewal proof for the client leaves that are probed
      directly (`production-internal-ca`), the benchmark VM's attribution check
      (`inference-benchmarking`) and the access-record verify of `agentgateway-observability`
      (its requirement "Every gateway request produces an access record in Loki"). Extract
      it from `deploy-agentgateway.yml` into a new `platform/playbooks/tasks/agw-probe.yml`
      whose inputs are the host it runs from (default the gateway host), the client leaf it
      presents (default `agw-verifier`; the benchmark VM passes `bench`), the path, an
      optional key (absent means keyless) and the expected status; the key-bearing call is
      `no_log` and its result is never printed. What changes when 6.1 lands, per probe in
      today's deploy: the readiness probe (`deploy-agentgateway.yml:266`, the busybox
      `wget` defined at line 227, from the sibling db container to `:19001`) is unaffected,
      because `readinessAddr` is a separate plain listener (`config.yaml.j2:34`), not a
      `gateways` listener. The keyless 401 probe (`deploy-agentgateway.yml:318-326`, the same
      `wget` over `http://` to `:4000`) cannot survive, because busybox `wget` presents no
      client certificate and the handshake fails before the key check; it becomes the
      shared task with no key, expecting 401. The keyed probes (`_verify_url`,
      `deploy-agentgateway.yml:349-352`) become the shared task over `https://` with
      `client_cert`/`client_key` = the presented leaf and `ca_path` = the internal bundle.
      The `uri` module at ansible-core 2.21.0 offers `ca_path`, `client_cert`, `client_key`
      and `validate_certs` and no server-name override (`ansible-doc uri`, run 2026-09-27;
      the Semaphore image's ansible-core version is unverified), so the URL names the
      gateway server leaf's SAN; `validate_certs: false` is never used. Interim name
      resolution: an idempotent `lineinfile` on the probing host maps that SAN to the
      published bind, with the trailing marker `# agent-cloud-managed: agw-probe (interim,
      task 7.2)` and a `regexp` on that marker, so code finds, updates and later removes the
      line. Local-dev keeps `agw_verify_base_url` and sets it to the SAN form; unverified:
      how the Semaphore container resolves that name on the `local-dev` network, settled
      before 6.1a lands
- [ ] 6.2 Caddy's `inference` and `admin.inference` blocks proxy to `https://` with
      `transport http { tls_server_name <gateway SAN>; tls_trust_pool file <root>;
      tls_client_auth <cert> <key> }` (Caddy 2.11.4), the leaf files read from the Caddy
      host's mounted `current/` directory. The architecture document's deprecated
      directive is corrected by `production-internal-ca` task 9.1, not here
- [ ] 6.3 The model's `tls: {root, hostname, cert, key}` (the `agw-upstream` client leaf,
      production-internal-ca task 5.4a; mutual TLS decided 2026-09-28) and an `https://`
      base URL, once dgx-spark
      serves vLLM over HTTPS (dgx-spark session: `--ssl-certfile`, `--ssl-keyfile`,
      `--enable-ssl-refresh`)
- [ ] 6.4 Validation gate: a request without a client certificate is refused at the
      gateway, proving scenario "A request without a client certificate is refused"; a
      throwaway client-profile leaf declared with a SAN on no allowlist entry (issued as in
      `production-internal-ca` task 4.7, then removed) completes the handshake and its
      request is refused, proving scenario "Another client leaf is refused at the gateway";
      a request with the `agw-verifier` leaf from the gateway host through 6.1a reaches the
      key check, proving scenario "An allowlisted non-Caddy client is served"; the BATS
      refusal of an undeclared entry proves scenario "An undeclared allowlist entry is
      refused at render"; `Deploy agentgateway (Dev)` still passes its whole verify phase
      after group 6 (readiness, the keyless 401, the keyed `/v1/models` and chat
      round-trip, all through 6.1a); the gateway refuses a vLLM certificate not issued by
      the internal CA; the public path works end to end with every hop encrypted

## 7. Internal name resolution (follow-up; needs hickory-dns in production)
- [ ] 7.1 Server-side OIDC off the Cloudflare path: a split-horizon record for
      `auth.uhstray.io` answering with the Caddy host (declared in the site-config
      split-horizon list, answered only to the internal DNS's declared clients:
      `internal-dns-naming` decision 15 as amended 2026-09-29), so the gateway's discovery, JWKS and
      token calls (and OpenBao's, once `inference-personal-keys` adds its mount) reach
      Authentik through Caddy without transiting Cloudflare. The Cloudflare skip rule for
      Authentik's machine endpoints (`platform/infra/cloudflare/waf.tf:129-135`) is the
      interim fix and stays until this record is proven; then decide, as a recorded
      amendment, whether it is retired. hickory-dns runs in local-dev only today; production
      is planned (`platform/services/dns/context/architecture.md:7`, `:45`), so this task
      waits on it
- [ ] 7.2 Replace 6.1a's interim `lineinfile` entries with records in the internal zone
      for the gateway server leaf's SAN, and remove every line carrying the
      `agent-cloud-managed: agw-probe` marker through the same task
- [ ] 7.3 Validation gate: with the Cloudflare skip rule temporarily disabled in a declared
      window, a gateway restart loads Authentik's discovery document and serves `/v1`; no
      probing host carries the interim marker line
