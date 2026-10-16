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
- One identity per name in `agw_identities`: inventory `agw_clients` plus one personal identity
  per entry of the declared admin list `platform_admins` (the list the Authentik admin entries are
  rendered from, the shared `platform/playbooks/vars/platform-admins.yml`; the identity is the entry's `agw_identity` when set,
  else its `username`, service accounts included). The derivation is GATED by
  `agw_admin_identities_enabled` (default false): the list is non-empty by default, so with the
  gate off `agw_identities` equals `agw_clients` exactly, the list is not read and the render is
  unchanged; the deploy, the key-management playbook, the rollback, the conformance run and
  check-secrets load the file only when it is on, and say so when it is missing. With the gate on,
  `platform_admins` must be declared for the gateway host: the shared file's own default is built
  from per-user vars that exist only on the Authentik host, so the loader refuses the run, in every
  play that includes it, before anything is read or written. The deploy, the key-management
  playbook and the rollback also refuse a derived name that repeats, is reserved or is malformed.
  `agw_identities` is computed once in `vars/secret-declarations/agentgateway.yml` and read by the
  secret declaration, the name checks, the key loop in `config.yaml.j2` and
  `manage-agentgateway-client-key.yml`. `platform_admins` absent or empty leaves it equal to
  `agw_clients`. The rollback's `direct` mode publishes a `direct_<name>` copy for every enrolled
  identity, personal ones included, by design: a personal identity is a client the direct path
  must serve like any other, and its copy is withdrawn only when the identity is removed.
  manage-secrets generates the field `client_<name>` (48 chars) once into
  `secret/services/agentgateway` and reuses it on every deploy; rotation is a deliberate
  separate step, never implicit.
- A derived name that repeats within `platform_admins`, falls outside `^[a-z0-9][a-z0-9-]*$`, or is
  `legacy-shared` or `mcp-self` fails the deploy before anything is rendered, naming the entry (set
  `agw_identity` to rename). A name in both lists is one identity (the same `client_<name>`, so
  nothing is minted twice) and is reported by name as a migration overlap. After the render the
  deploy checks that the key entries written equal `agw_identities` (plus `legacy-shared` while its
  grace period runs), naming a missing or extra identity, never a hash.
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

## MCP (change `agentgateway-mcp`, gateway side)

The gateway serves MCP as a second surface of the same process, behind the inventory flag
`agw_mcp_enabled` (default `false`). With the flag false nothing below renders and the verify asserts
`/mcp` is not served. Spike findings (v1.5.0, measured 2026-10-14) are in
`plan/development/openspec/changes/agentgateway-mcp/design.md`, "Findings". The edge (Caddy hostname,
Cloudflare record and rules) is summarised in "Operator operations" below; the harness client entry is
under "MCP clients".

| Piece | Where | Notes |
|---|---|---|
| Endpoint | `/mcp` on gateway `default` only (`mcp.gateways: [default]`) | Not on the `ui` listener. `statefulMode: stateless`: no session id and no held server stream, `GET /mcp` is 405. `prefixMode: conditional`: a tool is named by its OpenAPI `operationId` while there is one target, prefixed with the target name once there are several. `dnsRebindingProtection` unset (the gateway is reached by a non-local Host) |
| Authentication | `mcp.policies.apiKey`, `mode: strict`, the same header as `/v1`, over the admin-derived identities only | Rendered by the one `apikey_policy` macro in `config.yaml.j2` that `llm` also calls, so the key loop is written once. Over the admin-derived personal identities ONLY (`_agw_admin_identities`, never an `agw_clients` service client: the chat tool's self-call runs as `mcp-self`, so a service client admitted here would escape its own `allowedModels` and hourly budget; an admin's model spend through MCP is bounded by `mcp-self`'s budget, not their own). Never `legacy-shared`, never `mcp-self`, and with no `budgets` or `allowedModels` (a budget under the MCP policy is accepted and never charged, spike 1.4). An `mcp` block with no key policy answers every request (spike 1.1), so the deploy asserts the policy is rendered whenever the flag is true |
| Loopback gateway | `gateways.loopback`, container port `agw_loopback_port` (default `4002`), rendered only with the flag | Plain HTTP, attached to `llm` only (`llm.gateways` gains `loopback`; `mcp` and `ui` never do), `authorization` rule `source.address == "127.0.0.1"` because v1.5.0 gateways have no bind address (a neighbour container is 403 with or without a key, spike 1.5). Never in any compose file's `ports:`. Needs no client certificate, so `agw_client_cert_allowlist` is not widened for the self-call |
| Target | `agw_mcp_openapi_targets`: a list of `{name, schema}` | Rendered as an `openapi` target whose `host` is `127.0.0.1:<agw_loopback_port>` and whose `schema` is the shipped file `deployment/mcp-schemas/<schema>` read by the deploy and embedded in `config.yaml` as one compact JSON string (nothing to mount; a schema edit is a config change the change-aware deploy sees). Its `backendAuth` is the `mcp-self` key by environment reference |
| Shipped schema | `deployment/mcp-schemas/agentgateway-v1.json` | `/v1/models` (`list_models`) and `/v1/chat/completions` (`chat_completions`) only, no `servers` |
| Internal identity | `mcp-self` in `agw_clients`, budget from `agw_client_policies.mcp-self` | Its key is `AGW_MCP_SELF_KEY` in the 0600 `.env`; `config.yaml` carries `$AGW_MCP_SELF_KEY` only. It is a hash even under the local-dev `agw_plaintext_keys` flag. Refused on `/mcp` (401), honoured on the `/v1` request the self-call makes, so a model request through MCP is charged to and logged as `mcp-self`. Never handed out: hand out keys with `Back Up Credentials to site-config` and an explicit `credential_fields` list |

What the deploy refuses, before anything is rendered or restarted, naming the entry
(`_agw_mcp_problems` in `vars/secret-declarations/agentgateway.yml`): an allowlist, not a denylist.
A target is exactly `name` and `schema`: an `mcp`, `sse` or `stdio` kind, a `host` and a `port` are
not accepted, because a target aimed at the gateway's own external listener or at `/mcp` recurses
without a bound (spike 1.5) and a string check cannot enumerate every alias of that listener. The
schema is a bare `*.json` file name, so it cannot leave `mcp-schemas/`. The loopback port must be an
integer from 1024 to 65535 other than `4000`, `4001`, `15000`, `19001`, `19002`. The flag true needs
at least one target, `mcp-self` in the merged identities with `agw_client_policies.mcp-self.tokens_per_hour`
set, a non-empty admin-derived identity list (`agw_admin_identities_enabled` and `platform_admins`; the
refusal names both), and no `allowed_models` on an admin identity (MCP would widen it). The rendered file is checked against the
flag (no `mcp` block and no loopback gateway with the flag false; a strict key policy on any `mcp`
block; exactly the admin-derived identities on it), then validated by the pinned image as for every render.

Verify (`deploy-agentgateway.yml`, Phase 3, through the one probe path): flag true, a request with no
key is 401, an authenticated `initialize` returns a session, and `tools/list` lists at least one tool
(the validator accepts an `mcpAuthorization` rule naming a wrong field, which then denies every tool
silently). Flag false, a keyed `initialize` must not be an MCP reply (keyless is 401 either way). The
verdicts read the response body: the gateway's own rate-limit refusal is HTTP 200 with JSON-RPC error
`-32003`, reported as unproven, which fails the deploy unless `-e agw_verify_allow_unproven=true`.
The probing identity is `agw_verify_client`, else the first merged identity, never `mcp-self`; with MCP on it is
an admin-derived identity (`agw_verify_client` if it is one, else the first admin). After `tools/list` the verify makes one chat
call and requires the gateway's access log to show the `/v1` request as `mcp-self` on the loopback listener.

Rollback across the flag: with `agw_mcp_enabled` OFF live, a previous config that has the `mcp` block is refused by the
validator (it references `$AGW_MCP_SELF_KEY`, which the flag-off `.env` does not carry); the reverse, flag ON live and a
previous config without the block, validates and is put back, which drops `/mcp`. Both measured with `--validate-only`
on the pinned image. Crossing back needs a redeploy of the older flag state (`agw_mcp_enabled`), not a
`rollback-inference-route.yml` gateway-config rollback.

### MCP clients (change `agentgateway-mcp`, task 7)

Claude Code and pi reach `/mcp` through one user-level entry named `agentgateway`, installed by `make`:

```bash
make agw-mcp-client HOST=<mcp hostname> IDENTITY=<your gateway identity> KEY_FILE=<site-config clone>/secrets/agentgateway/client_<identity>.txt
make agw-mcp-client DRY_RUN=1 HOST=... IDENTITY=...   # which entries would change, nothing written
make agw-mcp-client-remove                            # deletes exactly the agentgateway entry
```

`scripts/agw-mcp-client.py` renders the templates in `context/clients/` (placeholder host, no key) for the
hostname given and merges the one entry: Claude Code through its own CLI (`claude mcp add-json -s user`), pi by
a merge-only edit of `~/.pi/agent/mcp.json` (atomic, mode kept, an unparseable file refused and untouched).
Re-running is a no-op; every other server entry is left as parsed. `KEY_FILE` is optional; without it the helper
reads the Keychain only.
Install from a permanent checkout: each entry stores the helper's absolute path, so after moving or deleting
the checkout re-run the installer. `make` hands the values to the installer as environment variables
(`AGW_MCP_HOST`, `AGW_MCP_IDENTITY`, `AGW_MCP_KEY_FILE`, `AGW_MCP_HARNESS`), never as text in a shell line.
Keep a handout file mode 0600; the helper still reads a group- or other-readable one but warns by name on stderr.
If replacing an entry fails after the old one was removed, the installer says so and exits non-zero; re-run it.

The key never sits in either file. Each entry runs `scripts/agw-mcp-key` at connect time (Claude Code
`headersHelper`, pi `!command`), which reads the macOS Keychain item first, then the handout file. Store the
key once, prompted (the value never appears on a command line):

```bash
security add-generic-password -s agentgateway-mcp -a <identity> -U -w
```

Verify with `claude mcp list` and `pi mcp list`. The fallback `${AGW_MCP_KEY}` in a client's `headers` is weaker
(an exported variable is visible to every process the user starts) and is for a harness without a
command-based header only. Never commit a repo-root `.mcp.json`: Claude Code loads a project one without
asking in `claude -p`, Agent SDK and cloud sessions, so a live one would connect every automated session here
to the gateway (it is gitignored, and a test asserts it does not exist).

Client facts re-verified for task 7.1:

| Fact | Source | Date | Matches design.md |
|---|---|---|---|
| `claude mcp add-json [options] <name> <json>`, `-s/--scope local\|user\|project`; `add-json` refuses an existing name (exit 1); `remove -s user` refuses an absent one (exit 1) | `claude mcp add-json --help`, `remove --help`, and a run in a temporary HOME + `CLAUDE_CONFIG_DIR`, Claude Code 2.1.299 | 2026-10-14 | Differs: `claude mcp add` has no `headersHelper` option, so the installer uses `add-json` |
| `headersHelper`: runs in a shell, prints a JSON object of string pairs, gives up after 10 s, runs fresh on each connect, working directory is the configuration directory at user scope (absolute path needed), credential-named variables stripped for project and plugin servers only | https://code.claude.com/docs/en/mcp | 2026-10-14 | Yes |
| `${VAR}` and `${VAR:-default}` expand in `url` and `headers`; a credential-named variable reads as empty in a remote server's `url`/`headers`, `:-default` ignored | same | 2026-10-14 | Yes (this is why no template uses an expanded variable) |
| A project `.mcp.json` loads without a prompt in `claude -p`, Agent SDK and cloud sessions | same | 2026-10-14 | Yes |
| User-scope Claude config lives at `$CLAUDE_CONFIG_DIR/.claude.json`, else `~/.claude.json` | run with a temporary HOME, with and without `CLAUDE_CONFIG_DIR` | 2026-10-14 | n/a (installer reads it to detect a no-op) |
| pi 1.1.0 reads `~/.pi/agent/mcp.json` (`mcpServers`); `pi mcp add`, `remove`, `list` | `pi mcp --help`; pi-mcp-adapter 5.1.0 `docs/configuration.md` | 2026-10-14 | Yes |
| pi `headers` value starting `!` runs a command at connect (shell), stdout trimmed is the value, non-empty within 10 s, `!!` escapes; `${VAR}` interpolates | adapter `docs/servers.md`, `docs/auth.md`, `bearer-command-resolver.ts` (`shell: true`) | 2026-10-14 | Yes |
| pi `url` is "StreamableHTTP with SSE fallback" | adapter `docs/servers.md` | 2026-10-14 | Differs from "SSE rejected": the adapter falls back to SSE; the gateway serves streamable HTTP, so nothing depends on it |
| Keychain read `security find-generic-password -s <service> -a <account> -w`; add with `-w` last to be prompted, `-U` to update | `security find-generic-password -h`, `security add-generic-password -h` | 2026-10-14 | Yes |

## Operator operations (changes `agentgateway-mcp` and `agentgateway-openrouter`)

One row per operation. Every template below has a generated `(Dev)` variant (`dev_variant: true` in
`platform/semaphore/templates.yml`); run a dry run (`--check`) before the real run. Every name in the
table was found by search in the repository for task 8.7 of `agentgateway-mcp`; the search results are in
that change's `tasks.md`.

| Operation | How | Source |
|---|---|---|
| Add an admin identity | Add an entry to `platform_admins` in site-config `agent_cloud.vars` (the scope both the Authentik and the gateway hosts see). With `agw_admin_identities_enabled: true` the gateway derives the identity from it (the entry's `agw_identity`, else its `username`). Run `Deploy Authentik` (creates the account and the `platform-admins` membership), then `Deploy agentgateway` (mints `client_<name>` once and reuses it). Then hand the key out (below) | `platform/playbooks/vars/platform-admins.yml`; "Identity and limits"; `../../authentik/deployment/context/architecture.md` "Platform administrators" |
| Remove an admin identity | In ONE site-config change: remove the entry from `platform_admins` and add its Authentik username to `platform_retired_accounts` (the deploy refuses a name that is both). Run `Deploy Authentik` (deletes the account; its verify reads the name gone), then `Manage agentgateway Client Key` with `client=<identity> action=revoke` (the identity must already be out of the list, or the next deploy mints it back); it reloads the gateway and the old key answers 401 | `platform/playbooks/deploy-authentik.yml`; `platform/playbooks/manage-agentgateway-client-key.yml`; "Virtual-key lifecycle" |
| Set the MCP hostname | `agw_mcp_hostname` is declared in inventory but no gateway playbook reads it: the hostname lives in the places that serve it. Caddy: a site block in site-config `caddy_managed_sites` placed by `Manage Caddy Sites` (local-dev: a `caddy_routes` entry with `mcp_api: true`, `Caddyfile.local.j2`). Cloudflare: the `mcp` record in `dns.tf`, the skip rule `mcp-api-bypass-challenge` in `waf.tf` and the rate-limit rule `mcp-api-ratelimit-log` in `ratelimit.tf`, all matching `mcp.<var.zone_name>` through `local.mcp_endpoint`, applied by `Apply Cloudflare Tofu`. The rate-limit rule is in `log` mode until the bucket is measured | `platform/infra/cloudflare/`; `platform/inventory/local-dev.yml.example` (`agw_mcp_hostname`, `mcp_api: true`) |
| Enable or disable MCP | `agw_mcp_enabled` (default `false`) in site-config, then `Deploy agentgateway`. Enabling needs `agw_admin_identities_enabled` with `platform_admins` declared for the gateway host, at least one `agw_mcp_openapi_targets` entry, `mcp-self` in `agw_clients` with `agw_client_policies.mcp-self.tokens_per_hour`, and no `allowed_models` on an admin identity; the deploy refuses otherwise, before rendering. Disabling renders no `mcp` block and no loopback gateway, and the verify then asserts `/mcp` is not served. Rolling back across the flag is a redeploy of the older flag state, not a `Rollback Inference Route` (see "Rollback across the flag") | "MCP (change `agentgateway-mcp`, gateway side)" |
| Hand out a key | `Back Up Credentials to site-config` with `credential_service=agentgateway` and `credential_fields=client_<identity>`. Always list the field: an empty `credential_fields` copies every field at the path, including the upstream key. The task output names fields, never values | `platform/playbooks/backup-credentials-to-site-config.yml` header; "Virtual-key lifecycle" |
| Install or remove client configuration | `make agw-mcp-client HOST=<mcp hostname> IDENTITY=<identity> [KEY_FILE=<handout file>]` merges one `agentgateway` entry into Claude Code and pi; the key is read at connect time from the Keychain item `agentgateway-mcp` (account `<identity>`), then from `KEY_FILE`. `make agw-mcp-client-remove` deletes exactly that entry. Both take `HARNESS=` and `DRY_RUN=1` | `Makefile` (`agw-mcp-client`, `agw-mcp-client-remove`); "MCP clients" |
| Roll back | `Rollback Inference Route` with `mode=gateway-config` (put the last live config back, gateway stays in the path), `mode=direct` (publish the vLLM key as `direct_<name>` copies and point the route at vLLM) or `mode=restore` (point back at the gateway, retire the copies once rotated). A previous config on the other side of `agw_mcp_enabled` is refused or drops `/mcp`; redeploy that flag state instead | `platform/playbooks/rollback-inference-route.yml`; "Rollback across the flag" |
| Serve an OpenRouter model | Add an `agw_models` entry `{ name, provider: openrouter, upstream_model: <vendor/model> }` and deploy. Name who may call it with `agw_openrouter_identities` and, to include every admin-derived identity, `agw_openrouter_admins: true`; the deploy refuses an empty union. `-e agw_verify_openrouter=true` adds one 8-token completion to the verify | "OpenRouter models" |
| Seed the OpenRouter key | `Seed OpenBao Key` with `bao_path=services/agentgateway` and `bao_key=openrouter_api_key`; the value arrives as the `BAO_VALUE` environment secret, staged with `scripts/semaphore-seed-input.py`, never as an extra var. Required only while an OpenRouter model is declared; the deploy refuses its absence | `platform/playbooks/seed-openbao-key.yml` header; "OpenRouter models" |

## Virtual-key lifecycle (aligned with vLLM's single key)

vLLM accepts one static `--api-key` and knows nothing about clients; the gateway owns the
per-client layer. Everything is inventory-driven code (design §10):

| Operation | How |
|---|---|
| Add a client | Add the name to `agw_clients` (site-config / local inventory), or declare the admin in `platform_admins`, and deploy: manage-secrets mints `client_<name>` once and reuses it; the config enrols its sha256 hash |
| Per-client policy | `agw_client_policies.<name>.allowed_models` and `.tokens_per_hour` render `allowedModels` and the budget amount |
| Rotate | Semaphore `Manage agentgateway Client Key`, `client=<name> action=rotate` (name must be declared); merges a new value, re-renders, reloads |
| Revoke | Remove the name from `agw_clients` and `platform_admins` FIRST, then `action=revoke`: deletes the field (KV-v2 merge-patch null), re-renders, reloads; the old key is 401 |
| Hand out | `Back Up Credentials to site-config` with `credential_service=agentgateway credential_fields=client_<name>`: a site-config branch, names only in the task output |
| pi client | Lives in the harness repository, not here: `harnesses/pi/extensions/uhstray-provider/` in `uhstray-io/harness`. `/login uhstray` validates the key against `GET /v1/models` and pi stores it in `auth.json`; the model list is the gateway's own, per key, discovered live and kept as pi's persisted snapshot; streaming is pi's built-in OpenAI Chat Completions client. No secret in the package; `UHSTRAY_INFERENCE_BASE_URL` points it at the local-dev gateway |
| UI key editor | Disabled on purpose: `UI_READ_ONLY=true` in `.env` puts the config store in read-only mode (writes get 403), and `config.yaml` is mounted `:ro`; configuration is code |
| Saved keys in the playground | Local-dev only: `agw_plaintext_keys: true` renders values instead of hashes so the UI can offer them. Prod stays on hashes |
| Upstream key | `vllm_api_key` (`existing`, seeded separately) → `params.apiKey: $VLLM_API_KEY`; omitted when the upstream takes no key (LM Studio locally); rotated on vLLM's schedule (task 5.1) |

### Admin UI (production v1.5.0)

- The Routes page shows nothing (operator report, 2026-10-14). This config declares no `binds:` or
  routes: it uses the top-level `llm:` block, which the upstream LLM docs contrast with "traditional
  HTTP routing configuration" (https://agentgateway.dev/docs/standalone/latest/llm/). An empty
  Routes page does not mean traffic is
  unrouted; the keyed deploy verify proves the `/v1` path end to end.
- The Playground needs a pasted raw key: production config holds sha256 hashes only. Local-dev
  sets `agw_plaintext_keys: true`, which renders values, so the local UI lists saved keys.
- Two `agw_models` entries (`nvidia/Qwen3.8-Flash-Next-NVFP4` and `qwen3.8-flash-next`) are
  declared; the operator asked (2026-10-14) to keep only the one active for the DGX
  deployment; which name stays waits on dgx-spark.

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

## OpenRouter models (change `agentgateway-openrouter`)

An `agw_models` entry with `provider: openrouter` is served by agentgateway's native OpenRouter
provider instead of the vLLM upstream, on the same `/v1`, behind the same per-identity keys and
budgets. Entries without `provider` render exactly as before.

```yaml
agw_models:
  - { name: gpt-oss-120b }                                                  # vLLM, as ever
  - { name: or-mini, provider: openrouter, upstream_model: openai/gpt-4o-mini }  # name -> OpenRouter id
agw_openrouter_identities: [skynet]   # who may call OpenRouter models
agw_openrouter_admins: false          # true: also every admin-derived identity (default false)
```

- **Provider.** In v1.5.0 `provider: openrouter` (lowercase string) is a preset of the custom
  provider: it supplies the base URL `https://openrouter.ai/api/v1`, so backend TLS follows from
  the `https` scheme, and the formats completions, messages, responses, embeddings and rerank
  (`crates/llm/src/custom.rs:52-67,81,144-153`; `crates/agentgateway/src/types/local.rs:872-875,
  1020-1022,1062-1064`). Verified 2026-10-14 on the pinned image with `--validate-only`;
  `openRouter` and `Openrouter` are rejected. `name` is what clients send, `params.model` the
  OpenRouter id (`vendor/model`).
- **Key.** `secret/services/agentgateway:openrouter_api_key`, an `existing` secret like
  `vllm_api_key`: seeded with `Seed OpenBao Key`, never generated. It is declared and required only
  while an OpenRouter model is declared (manage-secrets refuses its absence before any write),
  rendered into the 0600 `.env` as `OPENROUTER_API_KEY`, and referenced from `config.yaml` as
  `$OPENROUTER_API_KEY`; the config holds no credential.
- **Cost guard.** OpenRouter is billed per token on the operator's account, so it is opt-in twice.
  The deploy refuses a wildcard name, an empty or malformed `upstream_model`, an unsupported
  `provider` value, a duplicated name, and an empty union of who may call them. The union is the
  explicit `agw_openrouter_identities` list plus, with `agw_openrouter_admins: true` (default
  false), every admin-derived identity (`_agw_admin_identities`, from `platform_admins` once
  `agw_admin_identities_enabled` derives them; service clients in `agw_clients` are not in it), each
  name once. `config.yaml.j2` then removes every OpenRouter name from the
  `allowedModels` of each identity outside the union, including `legacy-shared`, starting
  from its `agw_client_policies` allow-list or, with none, every declared model. The per-key
  allow-list is checked on the requested model before any routing
  (`crates/agentgateway/src/llm/model_router.rs:173-183`).
- **Budget charging.** Each OpenRouter model carries the same streamed-usage transformation as the
  vLLM models but with `include_usage` only: `continuous_usage_stats` is a vLLM extension and
  OpenRouter's tolerance of it is unverified. Usage then arrives on the final chunk, so a client
  that disconnects before it is not charged on an OpenRouter stream (the vLLM models keep the
  every-chunk behaviour above).
- **Verify.** The keyed `/v1/models` listing must include every OpenRouter model the verifying
  identity may use, and the default completion is always a non-OpenRouter model. `-e
  agw_verify_openrouter=true` (or the inventory flag) adds ONE completion with `max_tokens: 8` to
  the first OpenRouter model that identity may use.
- **Local-dev.** `compose.local.yml` sets `SSL_CERT_FILE` to the step-ca bundle, which replaces the
  system roots, so a local gateway cannot verify `openrouter.ai`'s certificate. The commented
  example in `platform/inventory/local-dev.yml.example` is off for that reason. Production sets no
  `SSL_CERT_FILE`, and the image ships public roots (`/etc/ssl/certs/ca-certificates.crt`).
- **Probes.** The conformance run, the isolation probe and the MCP verify's chat call never pick an
  OpenRouter model (`agw_conformance_model` still names one explicitly). `mcp-self` and
  `legacy-shared` can never be granted OpenRouter models, and a glob character in any model name is
  refused.

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

How the TLS listener treats a client it must refuse (tasks 6.1, 6.4): the Semaphore template
`Probe agentgateway Client TLS (Dev)` (`platform/playbooks/probe-agentgateway-client-tls.yml`) sends
`GET /v1/models` through `tasks/agw-probe.yml` with no client certificate, then presenting a declared
client leaf that is on no allowlist, once with no key and once with an invalid one, and reports
statuses and failure classes only (no body, header or key).

The pre-switch gate (tasks 4.5, 6.4, and the route-switch runbook's open `/health` item): the
Semaphore template `Probe agentgateway Gate (Dev)` (`platform/playbooks/probe-agentgateway-gate.yml`)
presents the verifier leaf, which the SAN rule admits, so the key check is the layer that answers.
Keyless `GET /v1/models` proves a client with no key is refused; `GET /v1/models` with a literal key
that is no credential proves a key the gateway does not hold is refused. Both must be 401 or the run
fails. A keyless `GET /health` is **reported, not asserted**: the gateway config has no `/health`
route and the key policy is strict on the listener, so the probe records what it actually does
(200 means a public `/health` survives gateway mode; any other status means the route switch needs a
`/health` handler). The report prints before the assertion. Read-only, no OpenBao call, no survey.

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

### Streaming pass-through check (tasks 2.3, 2.4; opt-in, not yet run)

The default run proves the gateway adds no measurable delay to a stream that lasts seconds. Spec
scenario "Stream is not buffered" needs one past 130 s, so `Run agentgateway Conformance` (and its
`(Dev)` variant) takes the survey var `agw_conformance_long_stream` (`-e
agw_conformance_long_stream=true` in the playbook). Set, it adds a fifteenth case, `stream-long`, to
the fourteen; unset it runs the fourteen as before.

The case sends one streamed `xhigh` completion with `max_tokens` 8000 and a prompt asking for an
exhaustive answer to the gateway and then straight to vLLM, with the same hard ceiling on each
(`agw_conformance_long_stream_timeout`, 600 s). That is the technique of dgx-spark's
`vllm/public_probe.py` `long` mode, which produced the 256 s public-path stream recorded in the
inference-endpoint-reliability change. dgx-spark sets `--sse-keep-alive-interval 15`, so vLLM sends
an SSE comment line (`: keep-alive`) whenever no chunk has gone out for 15 s: during a queue wait or
a long prefill. A stream that generates steadily may therefore carry few or no comments. The case
still passes on timing, but the run does not count zero against zero as proof of pass-through: when
vLLM sent none, `keepalives_seen_from_vllm` is false, the report and the CUSTOM STATS carry
`unproven: ["keepalive"]`, and the playbook prints a visible (non-failing) task, "Warn: keep-alive
pass-through is unproven on this run": "keep-alive pass-through UNPROVEN: vLLM sent no keep-alive
comments during this stream; do not close gateway task 2.3 on this run." Task 2.3 closes only on a
run without that warning, which needs a stream that sat idle past 15 s (a queue wait or a long
prefill).

**Making the stream queue (`agw_conformance_long_stream_saturate`).** Production run task 4910
passed 15/15 with 0 comments on both sides (`unproven`), because the stream generated steadily.
The survey var (and playbook var) `agw_conformance_long_stream_saturate`, default 0 = off, an
integer 0 to 16, fixes that: before each side's `stream-long` request the script starts that many
filler streams on the SAME side (direct for the direct measurement, the gateway for the gateway
one), waits 2 s so they reach the scheduler first, then sends the measured request, which waits in
vLLM's queue with comments flowing. dgx-spark's `vllm.yml:636` public verify play does the same with
twelve streams at once (`public_probe.py queue --n 12`, `vllm/public_probe.py:46` counts the
comments; the 2026-09-14 run completed all twelve, the four queued ones after 390 s —
`docs/TEAM-ENDPOINT.md:91`). Here the queue is sized to the scheduler: dgx-spark's
`profiles/qwen3.8-flash-next-nvfp4/model.yaml:20` sets `max-num-seqs: 8`, and
`docs/TEAM-ENDPOINT.md:97` says the scheduler runs 8 at once, so **8 fillers fill every slot** and
the measured request is the ninth. Use 8 or more; fewer leave a slot free and the stream does not
queue.

- A filler is the stream-long request shaped differently only in size: the same prompt and
  `xhigh` effort, `max_tokens` `agw_conformance_stream_max_tokens` (2048), and a hard curl time
  limit `agw_conformance_long_stream_saturate_seconds` (45 s; the script refuses more than the long-stream ceiling). Cutting the client makes vLLM abort
  the request and free the slot, so the measured request waits about 45 s on each side: three
  15 s keep-alives. Each filler's key travels exactly as the measured request's does (a pipe,
  never argv), and only its response status line is kept.
- Every filler is killed and waited for as soon as the measured request ends, and again by the
  script's exit trap, so no path out of the script (a failure, a signal) leaves a generation
  running on the upstream.
- The measured request's first line is expected to be a comment about 15 s in, on BOTH sides
  (vLLM's `--sse-keep-alive-interval 15`, dgx-spark `docs/LOAD-TESTING-9-13-26.md:12`), so the
  existing relative checks stay sensible: the gateway's first byte and longest silence must be
  within 2 s of vLLM's, which a gateway that buffers or drops comments still fails (its first
  byte comes only when the first token does). The keep-alive count check (gateway at least vLLM's,
  less one) is the one most exposed to noise: the two sides' waits can differ by a whole 15 s
  interval if another client holds a slot on one side only. A saturated run that fails only on
  that count should be repeated before it is read as a gateway fault.
- A filler whose side did not answer 2xx (a 429 from the gateway's one global request bucket,
  `agw_rate_requests_per_minute_total`, default 120 a minute, or a spent key budget) means the
  measured request may not have queued: the case fails naming the side, `fillers accepted`, rather
  than reporting a missing comment as a gateway fault. "Accepted" means the upstream returned a
  2xx status line for the filler, so a gateway that holds its response headers until the first
  token would be under-counted (unverified against vLLM). A filler generates for at most 45 s, a
  few hundred tokens at the 13 tok/s per stream dgx-spark measured
  (`docs/LOAD-TESTING-9-13-26.md:53`), charged to the identity's hourly token budget.
- **Load on the DGX.** The fillers are real generations on the shared upstream. For about 45 s on
  each side, one side after the other (about 100 s of held slots in all), every scheduler slot is held and any other client of the endpoint queues
  behind them; the endpoint's own guidance is at most 3 streams for latency and 6 for throughput
  (`docs/TEAM-ENDPOINT.md:98-99`). Run it when nobody else is using the Spark.
- The report adds `long_stream.saturate` and, per side, `fillers_accepted`; the result line's
  timing carries `fillers: {requested, accepted, lifetime_s}`. A saturated run with
  `unproven: []` and no warning is what closes task 2.3.

For each side the report gives `duration_s`, `first_byte_s`, `keepalives` (SSE comment lines) and
`gap_max_s` (the longest silence between two received lines, comments included). The case passes
only when every one of these holds; otherwise it fails and `failure` names each that does not:

- the direct stream itself ran past 130 s (`agw_conformance_long_stream_min_seconds`), so a stream
  that ended early cannot pass for lack of evidence;
- both streams ended with `[DONE]` and the gateway's also ran past 130 s;
- the gateway passed at least as many comments as vLLM, less one for a boundary;
- the gateway's first byte came no more than 2 s after vLLM's, and its longest silence no more than
  2 s over vLLM's. A gateway that holds the whole stream delivers it in one burst, which shows as a
  late first byte rather than a long gap between lines, so both are checked.

The two bodies are not compared: generation over thousands of tokens is not bit-identical even at
temperature 0 with a seed, and the usage-chunk check belongs to `stream-xhigh`. No result is
recorded here yet; the first production run's task number and figures go in this section.

Per-identity isolation (task 4.5, scenario "One client cannot exceed its share"): the Semaphore
template `Probe agentgateway Isolation (Dev)` (`platform/playbooks/probe-agentgateway-isolation.yml`)
sends up to five small chat completions as `agw_isolation_burst`, then one as `agw_isolation_control`.
It passes when the burst identity gets HTTP 429 with error code `budget_exceeded` and the control
identity gets 200. A 429 without that code is the global request bucket, not isolation, and fails.
The burst identity must declare `agw_client_policies.<name>.tokens_per_hour` of at most 1000, so
a run can never exhaust a real client's budget or burn meaningful GPU time. The budget window is a
rolling hour, so a burst identity already exhausted answers 429 on its first request, which still
counts. Both keys are read from OpenBao in `no_log` tasks; the report carries statuses and error
codes only. A workable probe budget is below about five times the per-request charge of the chosen
model (local-dev uses 50 against roughly 94 tokens per completion); a budget above what five attempts
can exhaust ends the run "NOT proven", loudly, never as a false pass.

## Known issues

- **`/config_dump` leaves the budget database password unredacted (found by spike 1.8 of `agentgateway-mcp`,
  2026-10-14; hand-off, not fixed there).** The dump on the admin listener redacts `backendAuth` keys and every
  `apiKey` policy, but prints `config.database.url`, expanded from `$AGW_DATABASE_URL`, with the Postgres
  password in clear text. The admin listener is pinned to the container loopback and never published, so
  nothing outside the container reaches it, but that pin is what protects the credential. The condition
  exists in every current deployment, MCP or not. Tracked as its own change (for example a read-only
  database role, or a URL without the password); the MCP change records the finding and does not change it
  (`plan/development/openspec/changes/agentgateway-mcp/design.md` "Findings" 1.8, `tasks.md` 8.6).

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
