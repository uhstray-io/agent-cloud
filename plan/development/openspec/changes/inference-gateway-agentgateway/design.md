# Design: agentgateway as the inference edge gateway

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

## Context

Verified 2026-09-14:

- agentgateway: latest release `v1.5.0` (2026-08-27), Apache-2.0, default branch `main`;
  release assets include `agentgateway-linux-amd64` and `agentgateway-linux-arm64`.
  Docker install page: `cr.agentgateway.dev/agentgateway:v1.5.0`, `-p 4000:4000`,
  config as a mounted file with `-f /config.yaml`, admin on the container's own
  `localhost:15000`. README: LLM gateway with OpenAI-compatible routing, MCP and A2A
  gateways, JWT and API-key auth, RBAC with CEL, rate limiting, OpenTelemetry;
  Linux Foundation project.
- Example `examples/llm-keyed-rate-limit/config.yaml`: `llm.port: 4000`;
  `llm.policies.jwtAuth` with `issuer`, `audiences`, `jwks.file`;
  `llm.policies.localRateLimit` entries of `type: requests|tokens`, `maxTokens`,
  `tokensPerFill`, `fillInterval`, `key` (CEL over `jwt.sub`, `jwt.team`,
  `llm.requestModel`); a vLLM model entry `provider: {custom: {formats: [{type:
  completions}]}}` with `params.baseUrl: http://vllm.internal:8000/v1` and
  `params.model`. Example `llm-basic`: `config.readinessAddr`, `config.statsAddr`,
  `frontendPolicies.http.maxBufferSize`, model aliasing with `transformation.model`.
  Example `traffic-ratelimiting-local`: `config.tracing.otlpEndpoint`,
  `randomSampling`, `binds[].listeners[].routes[].policies.localRateLimit`.
  Schema reference: policies `apiKey`, `basicAuth`, `oidc`, `authorization.rules`,
  `timeout.requestTimeout`/`backendRequestTimeout`, `retry.attempts/backoff/codes`;
  `config.metrics.fields`, `config.logging`.
- Verified 2026-09-17 by running the v1.5.0 binary (the published schema at
  agentgateway.dev tracks main and misled twice): `statsAddr` serves Prometheus text on
  `/metrics`; readiness is `/healthz/ready`; the image is a Chainguard glibc-dynamic base
  (no shell, runs non-root); `localRateLimit[].key` is unreleased; the `conditional`
  rate-limit form is rejected under `llm.policies`; `budgets` require `config.database`;
  API-key metadata is flattened onto the CEL `apiKey` object; `$VAR` references expand in
  `params.apiKey` and `config.database.url`; the UI attaches to a named gateway via
  `ui.gateways` and serves `/ui`, `/ui/assets`, `/ui/api` (config dump unauthenticated).
  The `examples/llm-keyed-rate-limit` directory the previous Context cited does not exist
  at the v1.5.0 tag.
- Not verified: whether the `custom` provider passes `reasoning_effort`, `chat_template_kwargs`
  and the Responses API through unchanged (task 2.3 tests each); streaming behaviour
  with SSE keep-alive comments from vLLM (task 2.3).
- agent-cloud: Caddy `inference_api` route shape and its BATS test; production block in
  site-config with upstream the head node; `platform/services/inference/` is an empty
  stub; onboarding checklist tiers and phases in
  `plan/architecture/02-service-onboarding.md`; plan 06 states skynet is the `/v1`
  gateway and reversed "no separate gateway".
- Joe, 2026-09-14: skynet is its own custom model-serving gateway designed to interface
  with agent-cloud and orchestrate across the platform; agentgateway does not replace it.
- dgx-spark: vLLM `--api-key` covers `/v1`, `/v2`, `/inference` only; port 8000 admitted
  from the LAN; served names `nvidia/Qwen3.8-Flash-Next-NVFP4` and `qwen3.8-flash-next`;
  measured ceiling about six concurrent requests; the reliability change adds an SSE
  keep-alive on chat and completions streams and a public-path verify play.

## Goals / Non-Goals

Goals: every inference request from outside the nodes passes one metered, authenticated
gateway that the platform deploys and observes; each client has its own key and limit;
client base URLs and request shapes are unchanged; the gateway is ready to front a
second upstream; the skynet relationship is written down.

Non-Goals: MCP or A2A routing (no consumer); replacing Cloudflare's edge controls;
narrowing the node firewall (dgx-spark, after this proves out); moving vLLM into the
estate; semantic routing, prompt guards, caching (features exist; none requested).

## Decisions

1. **agentgateway is the inference edge; skynet is the platform's orchestrating
   gateway.** Two authorities, two concerns: agentgateway owns transport-level
   identity, limits, routing to model backends and request telemetry for the vLLM API;
   skynet owns placement and policy across the platform's model estate and acts as
   the OPA-role-bearing orchestrator. skynet reaches the DGX Spark model through
   agentgateway like any other client (open question 1 records the alternative).
   Alternative rejected: agentgateway replaces skynet's gateway role, because Joe
   states skynet is purpose-built for agent-cloud orchestration and is kept.
   Alternative rejected: skynet fronts vLLM directly and agentgateway is skipped,
   because skynet is not the surface OpenCode, pi and team SDK clients use today, and
   the ecosystem document's request-telemetry and per-client-limit rows would stay
   empty. Recorded as an architecture record that amends plan 06.

2. **Own VM, Infrastructure tier — two containers.** The gateway is in the request path
   and holds OpenBao-sourced credentials at runtime (the vLLM upstream key, client
   keys), which the onboarding decision tree routes to a dedicated VM with its own
   AppRole. Amended 2026-09-17: the VM also runs the gateway's own internal-only
   Postgres for per-key budgets (decision 4); it publishes no host port and its
   password is generated once by manage-secrets.
   Alternative rejected: co-locate on the Caddy host, because Caddy is the front door
   for every platform hostname and a gateway fault or upgrade must not touch it.
   Alternative rejected: run on the head node, because the node's memory is the
   binding constraint and the boundary rule keeps non-vLLM software off the nodes.

3. **`custom` provider with `completions` format, one model entry per served name.**
   The verified example shape. The gateway's model name equals vLLM's served short name
   so clients change nothing. `maxBufferSize` set to 20 MiB to match Caddy's 16 MB body
   cap with headroom. `timeout.requestTimeout` unset (streams run past any fixed
   value; the upstream keep-alive handles idle), `backendRequestTimeout` unset for the
   same reason; `retry` disabled for `/v1` (a retried partial stream repeats tool side
   effects, per the ecosystem document). Alternative rejected: the `openAI` provider
   with `hostOverride`, because the `custom` provider is the documented path for a
   self-hosted OpenAI-compatible server and the example uses it for vLLM by name.

4. **API keys, not JWT, for the first rollout.** Clients are SDKs and CLIs configured
   with a bearer key today. `apiKey` policy with one key per person or agent role,
   minted by a playbook into OpenBao and handed out through the existing secret
   channel, enrolled as sha256 `keyHash` entries so the rendered config carries no
   plaintext key. Limits, as v1.5.0 allows (verified against the binary 2026-09-17):
   one GLOBAL `type: requests` bucket per minute derived from the measured ceiling, and
   a per-key `budgets` entry (`Tokens`, rolling `1h` UTC-aligned, `Block`) as the
   per-identity guard — which requires `config.database`, hence the Postgres (Joe's
   choice over waiting for the unreleased bucket `key` or rewriting in the verbose
   `binds/routes` shape). JWT via Authentik OIDC is the second step once the OIDC client for
   machine identities exists (plan 02). Alternative rejected: keep the single shared
   key at the gateway, because per-client limits are the reason the gateway exists.

5. **Grace period for the shared key.** The gateway accepts the old shared key as one
   more `apiKey` identity with the tightest limit for a dated period, then it is
   removed from the gateway and rotated at vLLM. Clients migrate one at a time with no
   flag day. Alternative rejected: a cut-over date with no overlap, because three
   client types and several people share the key today.

6. **Telemetry: traces to Alloy OTLP, metrics scraped, logs via container stdout.**
   This change adds the OTLP receiver to the o11y host's Alloy (the first producer)
   and a Tempo decision is deferred: traces are sampled at `randomSampling` low and go
   to Loki as structured log lines until Tempo has an owner (plan 05 Phase 3).
   Alternative rejected: deploy Tempo now, because one producer does not justify a
   store with no retention owner.

7. **Caddy stays the TLS front door.** Caddy terminates TLS, keeps the path allowlist
   and the Bearer 401, and proxies to the gateway over the LAN. The gateway does not
   terminate TLS for the public hostname. Alternative rejected: expose the gateway's
   own listener publicly, because the Cloudflare skip rule, the rate limit and the
   origin lockdown are all written against the Caddy origin.

   Authentication boundary: Caddy's 401 checks only the header's shape (`Authorization:
   Bearer <non-empty>`, the existing `header_regexp`), so every enrolled client key passes
   Caddy unchanged and reaches the gateway, which is the only component that decides
   identity. The vLLM key is held by the gateway alone and is never sent by a client.

8. **Health is the gateway's readiness plus a synthetic completion.** `readinessAddr`
   (`/healthz/ready`) for the deploy's health check, probed from the sibling database
   container over the compose network — the gateway image has no shell for a compose
   healthcheck, and the host loopback is the wrong vantage when the play runs inside
   the local control plane (found 2026-09-17); the o11y synthetic probe (telemetry change) continues
   to go through the public hostname, so it now proves Cloudflare, Caddy, gateway and
   vLLM together.

9. **Operator UI: the gateway runs its own OIDC login against Authentik, on its own
   listener (Joe, 2026-09-17; CORRECTED the same day).** v1.5.0 serves its built-in UI
   on the admin interface by default and lets `ui.gateways` attach it to a named
   gateway; upstream's "Secure the UI" page puts the login in `ui.policies` (oidc, jwt,
   basic, apikey, authorization). The UI has no login of its own and
   `/ui/api/config_dump` answers unauthenticated (verified on the binary), so it is an
   admin surface: a dedicated listener on :4001, `ui.policies.oidc` against an
   Authentik OAuth2 provider (`agentgateway-oidc.yaml`, catalog tier `admin`,
   client secret owned by Authentik and shared-read by the gateway deploy), an
   `authorization` rule requiring the platform admin group in the token's `groups`
   claim, the session cookie key derived (sha256) from a stored seed, reached at
   `admin.inference.<zone>` through Caddy as a PLAIN TLS proxy. Locally the gateway
   trusts step-ca via `SSL_CERT_FILE` (rustls-native-certs) and the compose overlay
   `!override`s the UI publish away, so the only path is through Caddy. The admin
   interface itself stays on the container loopback (decision 2 unchanged).
   *Corrected:* the first cut gated the route with Caddy + Authentik forward_auth,
   which authenticated the browser but is invisible to the gateway — the UI kept
   warning "UI is exposed without authentication", because it only recognises its own
   policies. Alternative rejected: keep forward_auth AND add native OIDC (two logins,
   no gain). Alternative rejected: publish the admin port (carries more than the UI).

## Risks / Trade-offs

- [Gateway strips or rewrites fields vLLM needs] → task 2.3 sends `reasoning_effort`,
  `chat_template_kwargs`, tools, streaming and a Responses API request through the
  gateway and diffs against direct vLLM; any loss blocks the Caddy re-route.
- [Gateway buffers the stream and defeats the keep-alive] → the same task times first
  token and inter-token gaps through the gateway against direct; a buffered stream is
  a blocker.
- [A new hop in the request path fails] → Caddy's upstream is one inventory value;
  rollback is a Caddy redeploy. Health and the synthetic probe surface it.
- [Per-key limits trip a legitimate agent] → the global request bucket and the per-key
  token budget are derived from the measured ceiling; no log-only mode exists in v1.5.0
  (task 3.2, answered), so the first week runs with loose figures tightened from the
  metrics and the budget rows.
- [The operator UI leaks configuration] → it is reached only through the admin-tier
  forward_auth gate; the listener is published for the Caddy host alone (firewall
  auto-detect) and the admin interface stays on the container loopback (decision 9).
- [Two gateways confuse the platform] → decision 1's record; agentgateway's config
  carries no placement or policy logic, and skynet's docs gain a pointer to the record.

## Migration Plan

1. Onboard the service (VM, AppRole, secrets, compose, playbook, BATS) with the
   gateway on the LAN only; conformance test through the gateway against direct vLLM.
   Done first in local-dev against LM Studio on the developer's Mac (2026-09-17), which
   is where the v1.5.0 findings above surfaced.
1a. Operator UI: Authentik app + Caddy block + Cloudflare record for
   `admin.inference.uhstray.io`, proven locally at `admin.inference.agent-cloud.test`.
2. Add OTLP receiver and scrape job on the o11y host; confirm gateway signals on the
   inference dashboard.
3. Mint per-client keys; enrol the shared key as one identity; document the client
   change in dgx-spark `docs/TEAM-ENDPOINT.md` (the base URL stays; the key changes).
4. Re-route the production Caddy block to the gateway; run dgx-spark's public-path
   verify play; watch one week.
5. Retire the shared key from the gateway; rotate at vLLM; hand dgx-spark the go for
   narrowing the node API CIDR to the gateway host. Write the record; amend plan 06;
   archive; retain the outcome into bank `agent-cloud-750a33b9`.

## Open Questions

- Does skynet call the DGX Spark model through agentgateway, or directly over the LAN
  with its own identity? Default if unanswered: through the gateway, so one place
  meters every request and the node firewall can narrow to one source.
- ~~VM id and address for the gateway (site-config `vm-specs.yml`).~~ Answered 2026-09-17:
  vmid 216 on apollo, 2 cores / 4 GB / 20G; address picked from the inventory's declared
  set because NetBox was down — to be reserved in NetBox before provisioning.
- ~~Whether `localRateLimit` supports a log-only mode.~~ Answered 2026-09-17: no such
  mode in v1.5.0; the first week runs with the figure set high and tightened from
  observed rates.
- **Per-identity limits (decision 4) do not exist in v1.5.0 without a database.**
  Found 2026-09-17 by running the binary, not by reading the published schema (which
  tracks main): `localRateLimit[].key` is unreleased, the `conditional` form is not
  accepted under the `llm` shortcut, and per-key `budgets` require `config.database`
  (Postgres). The example the Context cites (`examples/llm-keyed-rate-limit`) does not
  exist at the v1.5.0 tag. Shipped for now: strict per-client keys (identity +
  revocation) and one global request bucket. Options for fairness were (a) wait for the
  release that ships `key` (present on main); (b) rewrite the template in the full
  `binds/listeners/routes` shape where `conditional` per identity is accepted today;
  (c) add a Postgres and use per-key token budgets. **Joe chose (c), 2026-09-17:** the
  service gains its own internal-only Postgres (`agentgateway-db`), decision 2's "own
  VM" now hosts two containers, and the gateway is no longer stateless — the state is
  budget usage plus request metadata rows (no payloads by default). (a) remains the
  path to a per-identity request bucket.
- ~~Per-client key list.~~ Declared 2026-09-17 in site-config: `stray`, `opencode`, `pi`,
  `skynet` (local-dev: `dev-local`). `legacy-shared` joins in task 4.1.
- Streamed completions were not charged to the budget in the local test (only the
  non-stream request's 93 tokens appeared in `budget_usage`); whether LM Studio omits
  `usage` in stream mode or the gateway charges late is for task 2's conformance run.
