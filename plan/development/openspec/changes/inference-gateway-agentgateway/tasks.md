# Tasks: agentgateway as the inference edge gateway

## 0. Branch, decision record, host
- [ ] 0.1 Feature branch from `dev`: `feat/inference-gateway-agentgateway`. Pull requests
      only when Joe asks for them (repo rule)
- [ ] 0.2 Draft the `plan/architecture/` record (agentgateway inference edge; skynet
      orchestrating gateway; distinct authorities; alternatives) as Proposed; Joe confirms
      the text before it is Accepted
- [ ] 0.3 Allocate the gateway VM in site-config `proxmox/vm-specs.yml` (Infrastructure
      tier, Podman); provision through onboarding phases 1 to 2; AppRole
      `agentgateway` with read on `secret/services/agentgateway`
- [ ] 0.4 Validation gate: `openspec validate inference-gateway-agentgateway --store
      agent-cloud` passes; record file exists as Proposed; VM answers over the
      distributed key

## 1. Service onboarding
- [ ] 1.1 `platform/services/agentgateway/deployment/compose.yml`: image
      `${AGW_IMAGE:-cr.agentgateway.dev/agentgateway:v1.5.0}`, command `-f
      /config.yaml`, config mounted read-only, publish `${AGW_BIND:-127.0.0.1}:${AGW_PORT:-4000}:4000`,
      `readinessAddr` health check, no admin port published; `compose.local.yml`
      overlay per the o11y pattern
- [ ] 1.2 `templates/config.yaml.j2`: `llm.port: 4000`; one model entry per served name
      with `provider: {custom: {formats: [{type: completions}]}}`,
      `params.baseUrl: http://{{ agw_vllm_upstream }}/v1`, `params.apiKey: $VLLM_API_KEY`;
      `frontendPolicies.http.maxBufferSize: 20971520`; `config.statsAddr` on the LAN bind;
      `config.tracing.otlpEndpoint` to the o11y host; no `retry`, no `requestTimeout`
- [ ] 1.3 `templates/env.j2` (gitignored `.env` at deploy): `VLLM_API_KEY` from OpenBao
      `secret/services/agentgateway:vllm_api_key`; client keys rendered into the
      `apiKey` policy from `secret/services/agentgateway/clients/*`
- [ ] 1.4 Read `statsAddr` once on the running container and record what it serves in
      `context/architecture.md`; if it is not Prometheus text, find the documented
      metrics endpoint and update task 3.1
- [ ] 1.5 `deploy-agentgateway.yml` (place-monorepo, manage-secrets, deploy.sh, verify
      readiness and one `/v1/models` through the gateway) and `clean-deploy-agentgateway.yml`
- [ ] 1.6 `platform/tests/test_service_agentgateway.bats`: env-param image, pinned tag,
      readiness health check, no published admin port, config template has no literal
      key and no `retry` block, `deploy.sh` container-only
- [ ] 1.7 Validation gate: second deploy reports no changes and readiness answers,
      proving scenario "Deploy converges"; `nc` to the admin port from a LAN host is
      refused, proving scenario "Admin interface is not exposed"

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
- [ ] 3.1 o11y: `scrape.d/agentgateway.yml.j2` for the stats endpoint; Alloy
      `otelcol.receiver.otlp` on the o11y host forwarding spans as structured log lines to
      Loki (Tempo deferred); inference dashboard gains the client-view row
- [ ] 3.2 Check whether `localRateLimit` has a log-only mode in the v1.5.0 schema; record
      the answer in `design.md` and set the first-week policy accordingly
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
      per hour, figures derived from the measured ceiling and written as comments
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
      direct path, proving scenario "Rollback is one value"

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
