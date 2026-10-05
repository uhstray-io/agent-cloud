# agentgateway — the inference edge gateway

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Status: local-dev proven (2026-09-17). Prod VM (node, vmid and address declared in site-config) provisioned, keyed and
SSH-hardened through Semaphore (2026-09-18); the prod gateway rollout (Authentik app,
edge record, key seed, deploy, firewall, Caddy block) is pending.

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
| Image | `cr.agentgateway.dev/agentgateway:v1.5.0` | Chainguard glibc-dynamic base: no shell, no curl. Readiness is probed from the sibling `agentgateway-db` container over the compose network, not by a compose healthcheck |
| Config | `deployment/templates/config.yaml.j2` → `config.yaml` (rendered, gitignored, 0644) | No credential inside: upstream key as `$VLLM_API_KEY`, db URL as `$AGW_DATABASE_URL`, client keys as `keyHash: sha256:<hex>`. 0644 because the image runs non-root and 0640 was `Permission denied` (2026-09-17) |
| Env | `deployment/templates/env.j2` → `.env` (0600, gitignored) | `VLLM_API_KEY`, the budget Postgres credentials (`POSTGRES_*`, `AGW_DATABASE_URL`), `UI_READ_ONLY`, the UI's OIDC client secret and cookie secret when `agw_ui_enabled`, and the published bind/port values |
| Listener | gateway `default`, container `:4000`, published `AGW_BIND:AGW_PORT` | Caddy proxies `inference.<zone>` here. The LLM routes are ALSO attached to the `ui` gateway (:4001) so the UI's playground calls `/v1` on its own origin through Caddy; still apiKey-gated there |
| Readiness | container `:19001` `/healthz/ready` | upstream `management/readiness_server.rs`. Probed by deploy.sh and the verify play from the sibling `agentgateway-db` container (busybox `wget`) over the compose network — the gateway image has no shell, and the host loopback is the wrong vantage when the play runs inside the local Semaphore container |
| Stats | container `:19002`, published `AGW_STATS_BIND:AGW_STATS_PORT` | Prometheus text on `/metrics`; every metric carries `identity` (= `apiKey.name`). Access log adds `identity` to the default `gen_ai.*` fields and `agw.ai.time_to_first_token` (streams); prompt/completion content is never logged |
| Operator UI | gateway listener `:4001` (`gateways.ui` + `ui.gateways: ui`), published `AGW_UI_BIND:AGW_UI_PORT` in prod; `compose.local.yml` `!override`s the publish away locally | The GATEWAY authenticates it: `ui.policies.oidc` against Authentik (`agentgateway-oidc.yaml`, admin tier, client secret shared-read) + an `authorization` rule requiring `platform-admins` in the token's `groups`; `OIDC_COOKIE_SECRET` = sha256 of a stored seed. Caddy at `admin.inference.<zone>` is a plain TLS proxy. Locally the gateway trusts step-ca via `SSL_CERT_FILE`. A Caddy forward_auth gate was tried first: it left the UI warning "exposed without authentication" because the UI only recognises its own policies. Paths: `/` → 308 `/ui`, `/ui/assets/*`, `/ui/api/*`, callback `/oidc/callback` Inventory `agw_ui_enabled: false` renders the gateway with none of this: no `:4001` listener, no OIDC policy, no Authentik shared read, `/v1` on the default gateway only. It exists because v1.5.0 fetches the OIDC discovery document when it loads config, so a UI-on gateway cannot start before its Authentik provider exists (`--validate-only`, 2026-09-22). Default `true` |
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
| UI key editor | Disabled on purpose: `UI_READ_ONLY=true` in `.env` puts the config store in read-only mode (writes get 403), and `config.yaml` is mounted `:ro`; configuration is code |
| Saved keys in the playground | Local-dev only: `agw_plaintext_keys: true` renders values instead of hashes so the UI can offer them. Prod stays on hashes |
| Upstream key | `vllm_api_key` (`existing`, seeded separately) → `params.apiKey: $VLLM_API_KEY`; omitted when the upstream takes no key (LM Studio locally); rotated on vLLM's schedule (task 5.1) |

## Shared-key retirement (gateway task 5.1)

Before the gateway, every client sent vLLM's one `--api-key`. During a dated grace period the
gateway accepts that key as one more identity, `legacy-shared`, so clients move to their own keys
one at a time; then it is retired. The deploy enforces the date rather than anyone remembering it:

| Inventory | Effect |
|---|---|
| `legacy_shared_expires` absent | No grace period declared: no `legacy-shared` identity, no check |
| today (controller UTC) before `legacy_shared_expires` | `legacy-shared` is enrolled (budget from `agw_client_policies.legacy-shared`); the deploy records the shared key's sha256 fingerprint ONCE into `secret/services/agentgateway:legacy_shared_key_sha256` and pins the identity to it |
| on or after `legacy_shared_expires` | `legacy-shared` is not rendered and Phase 2 rolls that config out; then the last play (Phase 4) fails while `vllm_api_key` still has the recorded fingerprint (or no fingerprint was ever recorded) |

The fingerprint is written by a compare-and-swap on the field's absence and is not declared
through manage-secrets, which writes declared fields back. Only booleans leave the credential task (`tasks/agw-legacy-key-check.yml`, `no_log`); neither
the key nor its hash is printed. `legacy-shared` may not appear in `agw_clients`. Rotating
`VLLM_API_KEY` at vLLM and in OpenBao belongs to dgx-spark, which owns the key; the rollback
`restore` mode does not rotate it either.

Retirement date: _not yet set_ — `legacy_shared_expires` is the route-switch date + 14 days
(task 4.7), recorded here when the route switches.

## Upstream

`custom` provider, `formats: [{type: completions}, {type: responses}]`, `params.baseUrl` from inventory
`agw_upstream_base_url`, one model entry per name in `agw_models` (an optional
`upstream_model` remaps the name the client sends to the id the upstream serves).

**Responses passthrough (2026-10-04, operator decision after production conformance task 2681,
whose `responses` case was a `semantic_diff`).** With only `completions` declared, agentgateway
translated `/v1/responses` into a chat completion, which dropped reasoning output items and echoed
the requested model. Declaring `responses` too makes the gateway forward the request to vLLM's own
`<baseUrl>/responses` unchanged. Evidence, agentgateway v1.5.0 source:
- the `formats` entries are `ProviderFormatConfig { type, path }` over the `ProviderFormat` enum,
  which includes `Responses`, and an omitted `path` falls back to the default
  (`crates/llm/src/custom.rs:189-216`);
- a custom provider's `responses` format maps to the native Responses chat format
  (`crates/agentgateway/src/llm/mod.rs:991-1000`), and the ordered translation table puts
  Responses-to-Responses passthrough first (`mod.rs:324`), so it wins over the completions fallback;
- with no `path`, the upstream path is the `baseUrl` path prefix plus `/responses`
  (`mod.rs:1338-1380`, `crates/llm/src/openai.rs:65-69`);
- the per-key model allow-list is checked on the requested model before any format choice, for
  every LLM route (`crates/agentgateway/src/llm/model_router.rs:173-183`);
- usage is read on the passthrough path for both unary responses
  (`crates/llm/src/types/responses.rs:786-822`) and streams
  (`crates/llm/src/conversion/responses.rs:22-111`), which is what the per-key token budget charges.
API-key authentication and the request rate limit are listener policies, independent of format.
Not yet proven live: the next conformance run must show the `responses` case matching; the
Responses entries in `conformance-shape-allow.json` that the passthrough makes redundant are left
in place until that run reports them.

| Environment | `agw_upstream_base_url` | Key |
|---|---|---|
| local-dev | `http://host.containers.internal:1234/v1` (LM Studio on the Mac) | LM Studio's API token, seeded as `vllm_api_key` with `seed-openbao-key.yml` (2026-09-17); before that, none — `params.apiKey` omitted |
| prod | `http://<spark-1>:8000/v1` (site-config) | `secret/services/agentgateway:vllm_api_key`, seeded by a separate playbook |

## Conformance against direct vLLM (tasks 2.1, 2.2)

**Results.** The first production run (Semaphore task 2592, sent as an enrolled identity for
the served model, as reported by the operator) matched 0 of 13 cases. As reported, every case
returned 200 on both sides with the same semantic fields but a different body shape (that run's
output was not kept, so this is not re-checked; the next run disproves it for `responses`). That run's report
carried only a hash of each shape, so it could not say which fields differed, and no decision
could be made from it. The comparison now names the differing key paths (`shape_diff`, below). The
second production run, task 2614, named them; the accepted differences are recorded below. That
run matched semantics in 12 of 13 cases, not 13: in `responses` the gateway's answer has no
`reasoning` output item where vLLM's has one. The third run, task 2681 (commit `80c27733`, with the
allowlist), matched 12/13; `responses` still differs on semantics only, its shape accepted: the
reasoning item, and the `model` field (the gateway reports the served name, vLLM its own id).
Cause, read from agentgateway v1.5.0's source: with only the `completions` format declared, a
`/v1/responses` request is translated to chat completions and the answer rebuilt
(`crates/llm/src/conversion/openai_compat.rs`, `to_responses::translate_response_internal`),
which emits message text and tool calls only and names the requested model. The timing deltas
are still to be recorded.

How the comparison is made: the Semaphore template `Run agentgateway Conformance`
(`platform/playbooks/run-agw-conformance.yml`) runs `deployment/tests/conformance.sh` on the gateway
VM. The script sends each case twice: once to the gateway, presenting the `agw-verifier` leaf
when listener TLS is on, and once straight to `agw_upstream_base_url`, sending the same request
body to both. The gateway is reached through the one probe path (task 6.1a): the deploy's base
URL, leaf and trust bundle, the server leaf's name resolved by the host resolver after the shared
resolution step's read-only check, and one keyless `/v1/models` through `tasks/agw-probe.yml`
that must answer 401 before any key is read or any completion is spent. The cases are the models list, thinking off
(`chat_template_kwargs.enable_thinking: false`), one request per `reasoning_effort` value (`none`,
`minimal`, `low`, `medium`, `high`, `xhigh`, `max`, the seven values dgx-spark's endpoint
contract lists), a `chat_template_kwargs` override (`reasoning_effort: low`), a tool call, a
streamed `xhigh` request and one Responses API request.

A case **matches** when both targets succeeded (curl exit 0, a 2xx status, and for the stream
an ending `[DONE]` with no error event) and returned the
same status, the same body shape (every leaf path and its JSON type) and the same semantic fields:
model, finish reason, whether content, reasoning and tool calls came back, and whether reasoning
tokens were zero. A failure on either side is an error, and the report names the status on each
side, so two identical 401s fail the run. Model names are compared through the declared mapping
(each `agw_models` name to its `upstream_model`, or to itself). The playbook writes that mapping as
a file, the gateway's names are translated through it, and the direct models list is narrowed to
the declared upstream ids. The normalised-body
hash removes ids and timestamps and sorts keys. It is reported as `exact_body_match`, but it does
not decide the verdict, because two generations can differ even at temperature 0 with a fixed
seed. Timing deltas are the gateway's value minus the direct value. The run fails when any case
does not match.

**Shape differences.** For each case, `shape_diff` lists three things, all taken from the
normalised bodies (ids and timestamps removed):
- the key paths only the gateway's body has (`only_gateway`)
- the key paths only vLLM's body has (`only_direct`)
- the paths whose JSON type differs (`type_changed`)

For a stream, the shape is the union over all of its chunks. The diff carries key paths and type
names only, never a value. The playbook prints it per case as "shape differences (paths only)".

The allowlist `deployment/tests/conformance-shape-allow.json` holds the differences the gateway
is accepted to introduce: `gateway_may_add`, `gateway_may_drop` and `gateway_may_retype`, each a
list of paths written exactly as `shape_diff` prints them. All three lists must be present, though
each may be empty. A file that lacks one, carries any other key, or has a non-string `_comment` is
refused. An allowlisted difference is still
reported, under `shape_allowed`. Only the rest (`shape_unaccepted`) fails a case. The top-level
lists apply to every case. An optional `cases` object, keyed by case name, holds the same three lists
for that case only (any subset of them, nothing else); a case is judged against the top-level lists
plus its own entry, so a path accepted for the stream does not excuse it in a plain completion. Adding a path is the operator's
decision after reading a run's diff; record it and its reason in this section when making it.

**Accepted shape differences (operator decision, 2026-10-03).** The second production run, Semaphore
task 2614 (commit `1f8057aa`), matched semantics in 12 of 13 cases (`responses` differs, see
Results) and shape in none. An earlier version of this line said all 13; the operator's
decision was made on that wrong summary (`docs/MISTAKES.md` 1.16). The operator
accepted every difference it reported, for the case it was seen in. The run's per-case paths are kept
as `deployment/tests/conformance-shape-t2614.json` (paths only), and a test holds the allowlist to
exactly that record. No path appeared in every case, so the top-level lists stay empty and all 78
entries sit under `cases`, across all 13 cases. They are accepted, not
hidden: each is still listed under `shape_allowed` on every run. The client-visible ones below are
candidates for passthrough upstream in agentgateway; a later release that passes a field through
makes its entry redundant, not wrong.

Client-visible differences, as task 2614 reported them:
- **Cached-token counts.** Chat completions through the gateway lack `usage.prompt_tokens_details`,
  so a client reading cached-prompt token counts gets none. `service_tier` is also dropped.
- **Responses reasoning continuity.** On `/v1/responses` the gateway's output items lack
  `output.[].encrypted_content`, `output.[].summary` and `output.[].phase`. A client that carries
  reasoning state between turns, or shows reasoning summaries, loses them through the gateway.
- **Responses request echo.** The gateway's Responses body lacks the fields vLLM echoes back from
  the request and its own bookkeeping: `instructions`, `tools`, `tool_choice`, `reasoning.*`
  (`context`, `effort`, `generate_summary`, `mode`, `summary`), `metadata`, `temperature`, `top_p`,
  `text`, `truncation`, `previous_response_id`, `user` and others, plus the per-turn usage details
  (`usage.*_per_turn`, `usage.output_tokens_details.tool_output_tokens`).
- **Responses item status retyped.** `output.[].status` is `null` or a string from vLLM and always a
  string through the gateway.
- **Models list.** `/v1/models` through the gateway lacks `data.[].max_model_len`, so a client that
  reads the context length from the models list loses it; `data.[].parent`, `data.[].root` and
  `data.[].permission.[].*` are also dropped.
- **Tool-call message content.** For the tool-call case the gateway's body lacks
  `choices.[].message.content` that vLLM's carries.
- **Stream chunks.** Over the stream's chunks, the gateway's union has a top-level `choices` path
  and `usage.prompt_tokens`, `usage.completion_tokens`,
  `usage.completion_tokens_details.reasoning_tokens` and `usage.total_tokens` that vLLM's does not:
  the gateway emits a usage chunk vLLM does not. vLLM's union has `choices.[].delta`, which the
  gateway's lacks. The report gives the paths only; why the gateway's chunks are shaped this way was
  not established from it.

**Accepted shape differences, second record (2026-10-04).** Production run Semaphore task 2698
(commit `79a8eb76`, after the `/v1/responses` passthrough deploy, task 2696) matched 12 of 13.
`responses` now matches: the gateway passes the request through to vLLM, and the semantic fields
agree. Its shape still differs on three paths, all accepted from task 2614: vLLM's output items carry
`output.[].encrypted_content` and `output.[].phase` that the gateway's lack, and `output.[].status`
changes type.
`stream-xhigh` matched semantics but failed shape on `choices.[].delta` appearing only on the
gateway's side, where task 2614 had it only on vLLM's side. The chunk union of a stream varies by
run: a chunk whose `delta` is empty or absent on one side flips which union carries the path. Under
the 2026-10-03 rule the path is accepted in both directions for that case. Client-visible: a
streaming client may see chunks with an empty or absent `delta` from either target. Task 2698's
per-case paths are kept as `deployment/tests/conformance-shape-t2698.json`; the test now holds the
allowlist to exactly the union of both records. Of the 38 `responses` entries accepted from task 2614,
task 2698 used only those three; the other 35 (among them the request echo and per-turn usage paths) went
unused. They stay (operator decision); they may be pruned once several consecutive production runs
show `responses` matching without them.

Also found from these runs: the conformance model was not stable. Task 2614 and 2698 tested one
served model and 2681 another, because the model was the first item of a set intersection of the
identity's allowed models and the declared models, and that order follows per-process string
hashing. The selection now keeps `allowed_models` order (else the declared `agw_models` order), as
does the deploy's keyed verification probe, and a repository test refuses taking the first item of a
set-operation result in any playbook or task.

Timing measures (recorded 2026-10-04). `conformance.sh diff` reports each measure as gateway
minus direct, in seconds (`timing_delta`; positive means the gateway was slower). The report
carries only the delta, not the two absolute values. `stream-xhigh` is the one streamed case,
so it is the only case with first-token and gap measures. Task 2709 (commit `21891372`) is the
first run to pass 13/13, but its saved output is truncated before the `stream-xhigh` entry, so
its stream deltas are not recorded here; the three earlier production runs below are.

| Measure (`stream-xhigh`, gateway minus direct) | Task 2614 (`1f8057aa`) | Task 2681 (`80c27733`) | Task 2698 (`79a8eb76`) |
|---|---|---|---|
| Time to first token (s) | +0.023 | +0.026 | +0.029 |
| Inter-chunk gap p95 (s) | -0.001 | +0.001 | +0.001 |
| Inter-chunk gap max (s) | -0.002 | -0.001 | +0.001 |
| Total (s) | +0.072 | +0.020 | -0.030 |
| Cases matched | 0/13 | 12/13 | 12/13 |

Task 2681 tested a different served model from the other two (see the paragraph above).
Reading: the gateway adds roughly 25 ms to the first token and nothing measurable to the gaps
between chunks. Cases matched at task 2709: **13/13, PASS**.

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
- 2026-10-03 — task 2.2, production conformance run Semaphore task 2614 (commit `1f8057aa`):
  12/13 cases semantic match (corrected from 13/13: `responses` lacks the reasoning output item
  through the gateway), 0/13 shape match. Every reported shape difference accepted by the
  operator and committed to `conformance-shape-allow.json` (78 paths, each scoped to the case it was seen in); the
  client-visible ones are listed under "Accepted shape differences". Timing deltas not yet recorded.
- 2026-10-04 — task 2.2, production conformance run Semaphore task 2698 (commit `79a8eb76`):
  12/13. `responses` matches after the passthrough deploy; `stream-xhigh` failed only on
  `choices.[].delta` from the gateway side, now accepted (union of the task 2614 and 2698 records).
  Model selection made deterministic (see "Accepted shape differences, second record").
- 2026-10-04 — task 2.2, production conformance run Semaphore task 2709 (commit `21891372`):
  PASS, 13/13 cases match. Stream timing deltas from tasks 2614, 2681 and 2698 recorded in the
  table under "Conformance against direct vLLM".
- Task 2 (conformance against the direct upstream): shape decision and timing deltas recorded.
- 2026-10-05 — tasks 2.2 and 2.3a, production: `Deploy agentgateway (Dev)` Semaphore task 2880
  recreated the gateway with the stream-usage transformation; `Run agentgateway Conformance (Dev)`
  task 2882 PASS, 14/14 cases match, `stream_usage.gateway` true on `stream-xhigh` and
  `stream-options-without-usage`. Both tasks launched by the operator's coordinating session.

## Streamed completions and the token budget (task 2.3a)

Operator decision 2026-10-04: a streamed call must be charged to the caller's per-key budget.
Line references are agentgateway at tag `v1.5.0` (commit `fe673247`).

How v1.5.0 charges a stream:

- On `/v1/chat/completions` with `stream: true` and NO `stream_options`, the gateway sets
  `stream_options: {include_usage: true}` before forwarding
  (`crates/agentgateway/src/llm/mod.rs:1549-1560`, `process_completions_request`).
- The chat stream reader overwrites the recorded token counts each time a chunk carries `usage`
  (`crates/llm/src/conversion/completions.rs:1207-1219`). The budget is settled from the last
  recorded figures when the request's log completes (`crates/agentgateway/src/telemetry/log.rs:1279-1280`).
  A response with no usage is skipped, not charged (`crates/agentgateway/src/http/budget/mod.rs:478-494`).

Two bypasses followed, both measured on the v1.5.0 image on 2026-10-04 against a recording upstream:

1. A client sending `{"include_usage": false}` is forwarded as sent, so the stream has no usage
   chunk and is never charged. (`stream_options: {}` is refused by the gateway: 400, missing
   field `include_usage`.)
2. vLLM sends the answer before a separate final usage chunk. A client that disconnects after the
   answer leaves before usage arrives, so nothing is charged. Measured: budget 10 tokens, a
   50-token stream dropped after the answer, then the next request was still 200.

Closed in `config.yaml.j2`. Every model carries a CEL body `transformation`
(`crates/agentgateway/src/types/local.rs:807-809`, applied by
`crates/agentgateway/src/llm/policy/mod.rs:799-835`; a CEL error removes the key). It sends every
streamed chat request with `include_usage: true` and `continuous_usage_stats: true`, and keeps the
client's other `stream_options` keys. vLLM then puts cumulative usage on every chunk (vLLM
`entrypoints/serve/utils/api_utils.py` `should_include_usage`, read on vLLM main 2026-10-04; the
prod vLLM version was not checked). So the gateway holds the latest usage whenever the client
leaves. Measured with the transformation: the same dropped stream was charged, and the next request
got 429. `{}`, `{"include_usage": false, ...}` and `null` all reach the upstream forced.

- **Scope.** Chat only, guarded on `has(llmRequest.messages)`. A Responses request (`input`) keeps
  its own `stream_options` untouched; OpenAI defines `include_obfuscation` there, not
  `include_usage`. Non-streamed requests keep what they sent.
- **Client-visible change.** Every chunk of a chat stream carries a `usage` object, plus the
  final usage chunk.
- **Residual.** A client disconnecting before the first token is charged nothing. The Responses
  stream records usage from the `response.created` and `response.completed` events
  (`crates/llm/src/conversion/responses.rs:62-72, 102-111`), so a Responses client that drops
  before completion is not charged for its output. Not closed here: vLLM's server flag `--enable-force-include-usage` (named in vLLM `docs/features/per_request_metrics.md`) forces usage on every
  stream (same vLLM function), but it is dgx-spark's to set.

The conformance `stream-xhigh` case sends no `stream_options`. `stream-options-without-usage` sends
`{"include_usage": false}`, and vLLM directly gets the forced form, which is what the gateway should
forward. Either case fails when the gateway's stream carries no usage chunk, and the report shows
`stream_usage` for both targets. vLLM's own stream is not required to carry usage, and that flag is
not part of the semantic comparison.
