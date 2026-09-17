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
- [ ] 0.3 Allocate the gateway VM in site-config `proxmox/vm-specs.yml` (Infrastructure
      tier, Podman); provision through onboarding phases 1 to 2; AppRole
      `agentgateway` with read on `secret/services/agentgateway`
      2026-09-17 PARTIAL: declared on site-config branch `feat/agentgateway-host` —
      vm-specs vmid 216 on apollo (2c/4 GB/20G, podman) and the `agentgateway_svc`
      inventory group (upstream = the current Caddy inference upstream, four
      identities, firewall vars). Address chosen from the inventory's declared set
      because NetBox (IPAM) was unavailable; recorded as a future feature in
      `plan/architecture/02-service-onboarding.md` Known Gaps. NOT yet done, blocked
      off-LAN (prod Semaphore is 403 from outside): NetBox reserve, Provision VM,
      SSH key generate/distribute/verify/harden, Apply Firewall, AppRole
- [ ] 0.4 Validation gate: `openspec validate inference-gateway-agentgateway --store
      agent-cloud` passes; record file exists as Proposed; VM answers over the
      distributed key

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
      upstream takes no key (LM Studio locally); prod renders it from OpenBao
- [ ] 1.9 Validation gate: unauthenticated browser to `https://admin.inference.<zone>` is
      redirected to Authentik; after `agent-cloud-admin` login the UI renders and
      `/ui/api/config_dump` is reachable only through that path, proving scenario "UI
      requires an admin login"; on prod, `nc` to :4001 from a non-Caddy LAN host is refused.
      2026-09-17 LOCAL: `admin.inference.agent-cloud.test` resolves (wildcard), the served
      cert carries `*.inference.agent-cloud.test`, an unauthenticated GET is a 302 to
      Authentik's authorize endpoint, the outpost ping answers 204, the worker applied
      the blueprint; the local overlay removes the UI host publish so no unauthenticated
      path exists. Browser login as `agent-cloud-admin` and the prod `nc` are Joe's/prod

## 2. Conformance against direct vLLM
- [ ] 2.1 Conformance script `platform/services/agentgateway/deployment/tests/conformance.sh`
      (curl + jq): models list; chat completion thinking off; `reasoning_effort` each of
      the seven values; `chat_template_kwargs` override; tool call; streamed request with
      `xhigh` and timing of first token and inter-chunk gaps; one Responses API request;
      each sent to the gateway and to vLLM directly from the gateway VM
- [ ] 2.2 Diff bodies (ignoring ids and timestamps); record first-token and gap deltas
      in `context/architecture.md`
- [ ] 2.3 Confirm SSE keep-alive comment lines from vLLM pass through unchanged and
      unbuffered (needs dgx-spark `inference-endpoint-reliability` deployed)
- [ ] 2.4 Validation gate: 2.2 proves scenario "Conformance against direct vLLM"; 2.3
      with a stream past 130 s proves scenario "Stream is not buffered"

## 3. Telemetry
- [ ] 3.1 o11y: `scrape.d/agentgateway.yml.j2` for the stats endpoint (`/metrics` on the
      stats port; series `agentgateway_requests_total`, `agentgateway_request_duration_seconds_*`,
      `agentgateway_gen_ai_server_request_duration_*`, `agentgateway_gen_ai_client_token_usage_*`
      — read on the running container 2026-09-17); Alloy
      `otelcol.receiver.otlp` on the o11y host forwarding spans as structured log lines to
      Loki (Tempo deferred); inference dashboard gains the client-view row
- [x] 3.2 Check whether `localRateLimit` has a log-only mode in the v1.5.0 schema; record
      the answer in `design.md` and set the first-week policy accordingly. 2026-09-17: no
      such mode (`RateLimitSpec` has only `maxTokens`, `tokensPerFill`, `fillInterval`,
      `type`); first week runs with loose figures tightened from observed rates
- [ ] 3.3 Validation gate: one hour of traffic renders p50 and p95 first-token latency and
      per-identity counts, proving scenario "Client-view latency on the dashboard"

## 4. Identities, limits, re-route
- [ ] 4.1 `mint-agentgateway-client-key.yml`: idempotent per client name: if
      `secret/services/agentgateway/clients/<name>` exists the key is kept, otherwise one
      is generated; rotation is an explicit `-e rotate=true` run, never implicit; every
      run re-renders and reloads the gateway. Mint the first set (Joe names it; default one
      per current user, OpenCode, pi, skynet) and enrol the legacy shared key as identity
      `legacy-shared` with the tightest limit and `legacy_shared_expires: <date>` in inventory
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

## 5. Retire the shared key, records
- [ ] 5.1 Retirement, enforced by the deploy rather than remembered: the config template
      renders `legacy-shared` only while today is before `legacy_shared_expires`; a deploy
      on or after that date drops the identity and fails if the vLLM key in OpenBao still
      equals the pre-rotation value, so a rerun cannot leave the shared key valid. Rotate
      `VLLM_API_KEY` at vLLM (dgx-spark `secrets/vllm_api_key` and a two-rank restart) and in
      OpenBao; record the retirement date in `context/architecture.md`; hand dgx-spark the
      gateway address for narrowing `vllm_api_allowed_cidr`
- [ ] 5.2 Accept the architecture record; append a dated pointer line to
      `plan/development/06-inference-skynet.md`; `platform/services/inference/` stub
      gains a README pointing at the gateway service and the dgx-spark roadmap record
- [ ] 5.3 Validation gate: 5.1 proves scenario "Grace period ends"; the record and the
      plan 06 line prove scenario "Decision is findable and plan 06 is amended"; on
      archive, retain the outcome (worked / dead end / corrected) into bank
      `agent-cloud-750a33b9`
