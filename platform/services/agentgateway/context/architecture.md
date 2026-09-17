# agentgateway — the inference edge gateway

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Status: local-dev proving (2026-09-17);
prod VM not yet provisioned.

## Role

The authenticated, metered, observable `/v1` in front of the OpenAI-compatible model
API. Clients keep their base URL and request shape; what changes is the key they hold.
The gateway holds the upstream credential (vLLM's `--api-key` in prod) and decides
identity itself; Caddy in front of it keeps TLS, the path allowlist and a header-shape
Bearer check only.

Decision record: `plan/architecture/05-platform-infra.md`, "Inference gateway:
agentgateway alongside skynet" (Proposed until the operator confirms). OpenSpec change:
`plan/development/openspec/changes/inference-gateway-agentgateway`.

## Shape

| Piece | Where | Notes |
|---|---|---|
| Image | `cr.agentgateway.dev/agentgateway:v1.5.0` | Chainguard glibc-dynamic base: no shell, no curl. Readiness is probed from the host, not by a compose healthcheck |
| Config | `deployment/templates/config.yaml.j2` → `config.yaml` (rendered, gitignored, 0644) | No credential inside: upstream key as `$VLLM_API_KEY`, db URL as `$AGW_DATABASE_URL`, client keys as `keyHash: sha256:<hex>`. 0644 because the image runs non-root and 0640 was `Permission denied` (2026-09-17) |
| Env | `deployment/templates/env.j2` → `.env` (0600, gitignored) | `VLLM_API_KEY` + published bind/port values |
| Listener | gateway `default`, container `:4000`, published `AGW_BIND:AGW_PORT` | Caddy proxies `inference.<zone>` here. The LLM routes are ALSO attached to the `ui` gateway (:4001) so the UI's playground calls `/v1` on its own origin through Caddy; still apiKey-gated there |
| Readiness | container `:19001` `/healthz/ready` | upstream `management/readiness_server.rs`. Probed by deploy.sh and the verify play from the sibling `agentgateway-db` container (busybox `wget`) over the compose network — the gateway image has no shell, and the host loopback is the wrong vantage when the play runs inside the local Semaphore container |
| Stats | container `:19002`, published `AGW_STATS_BIND:AGW_STATS_PORT` | Prometheus text on `/metrics`; every metric carries `identity` (= `apiKey.name`). Access log adds `identity` to the default `gen_ai.*` fields and `agw.ai.time_to_first_token` (streams); prompt/completion content is never logged |
| Operator UI | gateway listener `:4001` (`gateways.ui` + `ui.gateways: ui`), published `AGW_UI_BIND:AGW_UI_PORT` in prod; `compose.local.yml` `!override`s the publish away locally | The GATEWAY authenticates it: `ui.policies.oidc` against Authentik (`agentgateway-oidc.yaml`, admin tier, client secret shared-read) + an `authorization` rule requiring `platform-admins` in the token's `groups`; `OIDC_COOKIE_SECRET` = sha256 of a stored seed. Caddy at `admin.inference.<zone>` is a plain TLS proxy. Locally the gateway trusts step-ca via `SSL_CERT_FILE`. A Caddy forward_auth gate was tried first: it left the UI warning "exposed without authentication" because the UI only recognises its own policies. Paths: `/` → 308 `/ui`, `/ui/assets/*`, `/ui/api/*`, callback `/oidc/callback` |
| Admin | container loopback `:15000` | Never published |
| Postgres | `agentgateway-db` (`postgres:16-alpine`), compose network only, volume `agentgateway-pg-data` | Exists for per-key budgets; v1.5.0 refuses budgets without `config.database`. Also receives request METADATA rows (`request_logs`); prompt/completion payloads are NOT stored by default (`request_log_payloads` stayed 0 after a completion, verified 2026-09-17) |

## Identity and limits

- Policy `apiKey`, `mode: strict`, header `Authorization: Bearer <key>`.
- One identity per name in inventory `agw_clients`. manage-secrets generates the field
  `client_<name>` (48 chars) once into `secret/services/agentgateway` and reuses it on
  every deploy; rotation is a deliberate separate step, never implicit.
- The key's `metadata.name` is the identity; CEL sees it as `apiKey.name` (metadata is
  flattened onto the apiKey object — upstream `http/apikey.rs` `Claims`).
- `localRateLimit`: ONE global `requests` bucket per minute
  (`agw_rate_requests_per_minute_total`) protecting the upstream's measured ceiling.
- **Per-identity control is the token budget on each key** (`budgets`: `Tokens`,
  rolling `1h` UTC-aligned window, `Block`), figure `agw_rate_tokens_per_hour`. This is
  why the service carries its own Postgres. Chosen by the operator 2026-09-17 over waiting
  for a release with `localRateLimit[].key`, after verifying against the v1.5.0 binary
  (not the published schema, which tracks main) that `key` → `unknown field`, the
  `conditional` form under `llm.policies` → `expected a sequence`, and budgets →
  `API key budgets require config.database`. A per-identity REQUEST bucket remains
  unavailable until a release ships `key`.
- No log-only mode exists for `localRateLimit` in v1.5.0; the first week runs with a
  loose figure and tightens from observed rates (design open question, answered).

## Virtual-key lifecycle (aligned with vLLM's single key)

vLLM accepts one static `--api-key` and knows nothing about clients; the gateway owns the
per-client layer. Everything is inventory-driven code (design §10):

| Operation | How |
|---|---|
| Add a client | Add the name to `agw_clients` (site-config / local inventory) and deploy: manage-secrets mints `client_<name>` once and reuses it; the config enrols its sha256 hash |
| Per-client policy | `agw_client_policies.<name>.allowed_models` and `.tokens_per_hour` render `allowedModels` and the budget amount |
| Rotate | Semaphore `Manage agentgateway Client Key`, `client=<name> action=rotate` (name must be declared); merges a new value, re-renders, reloads |
| Revoke | Remove the name from `agw_clients` FIRST, then `action=revoke`: deletes the field (KV-v2 merge-patch null), re-renders, reloads; the old key is 401 |
| Hand out | `Back Up Credentials to site-config` with `credential_service=agentgateway credential_fields=client_<name>`: a site-config branch, names only in the task output |
| UI key editor | Inert on purpose: the config is read-only in the container; configuration is code |
| Saved keys in the playground | Local-dev only: `agw_plaintext_keys: true` renders values instead of hashes so the UI can offer them. Prod stays on hashes |
| Upstream key | `vllm_api_key` (`existing`, seeded separately) → `params.apiKey: $VLLM_API_KEY`; omitted when the upstream takes no key (LM Studio locally); rotated on vLLM's schedule (task 5.1) |

## Upstream

`custom` provider, `formats: [{type: completions}]`, `params.baseUrl` from inventory
`agw_upstream_base_url`, one model entry per name in `agw_models` (an optional
`upstream_model` remaps the name the client sends to the id the upstream serves).

| Environment | `agw_upstream_base_url` | Key |
|---|---|---|
| local-dev | `http://host.containers.internal:1234/v1` (LM Studio on the Mac) | LM Studio's API token, seeded as `vllm_api_key` with `seed-openbao-key.yml` (2026-09-17); before that, none — `params.apiKey` omitted |
| prod | `http://<spark-1>:8000/v1` (site-config) | `secret/services/agentgateway:vllm_api_key`, seeded by a separate playbook |

## Verification log

- 2026-09-17 — structural: BATS `platform/tests/test_service_agentgateway.bats` 17/17;
  shellcheck, yamllint, ansible-lint clean.
- 2026-09-17 — task 1.4, read on the running v1.5.0 container: `statsAddr` serves
  **Prometheus text on `/metrics`** (200; `/` and `/stats` are 404). Process metrics
  (`agentgateway_config_synchronized`, tokio and cgroup gauges) are present at start;
  request-level series appear after traffic: `agentgateway_requests_total`,
  `agentgateway_request_duration_seconds_*`, `agentgateway_upstream_call_duration_seconds_*`,
  `agentgateway_gen_ai_server_request_duration_*`, `agentgateway_gen_ai_client_token_usage_*`,
  `agentgateway_requests_shed_total`, `agentgateway_response_bytes_total` (all histograms
  as `_bucket/_count/_sum`). Task 3.1 scrapes these.
- 2026-09-17 — smoke against LM Studio (`openai/gpt-oss-20b`) with the rendered config in a
  throwaway container, before the Semaphore-driven deploy: `/healthz/ready` 200; `/v1/models`
  with no key → 401 `no API Key found`, with an unknown key → 401 `invalid credentials`,
  with the enrolled hash's key → 200 listing `gpt-oss-20b`; `/v1/chat/completions` → 200
  with usage and a `reasoning` field passed through from the upstream. With the Postgres
  attached: `budget_usage` gained one row charging the completion's 91 tokens to the
  `hourly-tokens` budget of the enrolled key hash; `$AGW_DATABASE_URL` expands inside
  `config.database.url`, so the rendered config still carries no credential.
- 2026-09-17 — upstream-key path proven end to end locally: LM Studio switched to
  requiring a token (401 upstream, surfaced by the gateway as `invalid_api_key`); the token
  was seeded with `seed-openbao-key.yml` (value as an environment secret, Mac-direct) into
  `vllm_api_key`; redeploy (task 616) rendered `params.apiKey: $VLLM_API_KEY` with no
  plaintext in config.yaml; completions succeed on :4000 and through the UI origin; no-key
  is still 401. This is the exact prod shape with vLLM's key.
- Task 2 (conformance against the direct upstream): pending.
