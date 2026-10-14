# Design: MCP on the agentgateway, per-admin identities, and harness clients

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

## Context

Three kinds of fact are kept apart below, because only the first kind is a ground to
build on.

**Verified 2026-10-14 by this author** (read from source at the cited location):

- **The schema serves MCP.** At tag v1.5.0, `schema/config.json` has `mcp` and `llm` as
  sibling top-level properties. `mcp` is `LocalSimpleMcpConfig`: `gateways`, `targets`
  (required), `statefulMode` (`stateless` | `stateful`), `prefixMode` (`conditional` |
  `always` | `never`), `failureMode` (default `failClosed`), `dnsRebindingProtection`
  (opt-in, "Host/Origin must be localhost"), `policies`. Its description: "served under
  the attached `gateways` at /mcp and /sse. All MCP servers listed will be served as a
  single virtual MCP server."
- **`gateways` defaults to attaching to `default`.** The schema says: when omitted and a
  gateway named `default` exists, the MCP routes attach to it. This design therefore
  names `gateways: [default]` explicitly (decision G1).
- **Target kinds.** `LocalMcpTarget` is one of `sse`, `mcp` (streamable HTTP; `host`, or
  `host`+`port`+`path`, or `backend`), `stdio` (`cmd`, `args`, `env`) and `openapi`
  (`schema` as file, inline or remote, plus the host forms). A target's own `policies`
  are transport policies only (`SimpleLocalBackendPolicies`, which includes `backendAuth`),
  "not supported on stdio targets"; the MCP-level policies belong on `mcp.policies`.
- **`mcp.policies` is `FilterOrPolicy`**, whose keys include `apiKey`, `jwtAuth`, `oidc`,
  `mcpAuthentication`, `mcpAuthorization`, `mcpGuardrails`, `cors`, `localRateLimit` and
  `authorization`. So an `apiKey` policy of the same shape as the one under `llm.policies`
  is expressible.
- **The gateway has no built-in MCP over itself.** `admin_router` in
  `crates/agentgateway/src/management/admin.rs:185-210` (tag v1.5.0) routes only
  `/debug/pprof/*`, `/memory`, `/quitquitquit`, `/debug/tasks`, `/debug/trace`,
  `/config_dump`, `/logging`, plus the UI router. Nothing in it is MCP.
- **Our rendering.** `config.yaml.j2` defines the `listener_tls()` macro at `:41-49`
  (mutual TLS plus a SAN `authorization.require` limited to the Caddy leaf), the two
  gateways `default` (:4000) and `ui` (:4001) at `:79-91`, the UI OIDC policy gating on
  the `platform-admins` group at `:92-108`, and `llm:` from `:169`, with `policies.apiKey`
  (`mode: strict`, `Bearer` header, one `keyHash: sha256:` entry per `agw_clients` name,
  `metadata.name`, an `hourly-tokens` budget) at `:179-223`, the legacy shared-key block
  at `:224-`, the global request `localRateLimit` at `:254`, and `models:` at `:259`.
- **Identities today.** `agw_clients` feeds the secret declarations
  (`platform/playbooks/vars/secret-declarations/agentgateway.yml:18`, `client_<name>`
  `random`, length 48, minted once and reused) and the name assert
  (`platform/playbooks/deploy-agentgateway.yml:123`, `^[a-z0-9][a-z0-9-]*$`).
  `legacy-shared` is refused as an `agw_clients` entry (`deploy-agentgateway.yml:140`).
- **No shell in the image**, so no stdio target can run
  (`platform/services/agentgateway/deployment/compose.yml:22-23`).
- **The identity gap.** Authentik users and groups are declared as code
  (`platform/services/authentik/deployment/blueprints/platform-users.yaml:1-24`: one
  fixed block per person, identity values from `!Env`, real values from site-config via
  the Authentik `env.j2`); RBAC is `platform-groups.yaml` (the group is defined at `:18`). Read
  2026-10-14 under
  `platform/services/authentik/deployment/blueprints/`, the files that put a user in
  `platform-admins` as a fixed block are `platform-users.yaml` (the admin blocks, `:39` and
  `:74`), `stray-admin.yaml:25` (the operator's admin), `agent-cloud-admin.yaml:35` (the
  day-to-day admin) and `service-account.yaml:22` (a service account, not a person);
  `openbao-oidc.yaml:44` only maps the group, it does not add a user. Each fixed-block
  file must either move into the declared list or be named as an explicit exception (task
  2.1 decides which, Open Question 1). The gateway UI gates on
  `platform-admins` (`config.yaml.j2:105-107`). The API identities are the separate list
  `agw_clients`.
- **Handout today.** `Back Up Credentials to site-config` with
  `credential_service=agentgateway credential_fields=client_<name>`
  (`platform/services/agentgateway/context/agent-practices.md:124`,
  `architecture.md:65`); it writes `secrets/<service>/<field>.txt` on a new site-config
  branch (AGENTS.md, Independent Workflows). A workstation OpenBao login is not an
  established runtime path (`platform/semaphore/README.md:17-21`).
- **Edge today.** The inference route is an allowlist (`/v1/*` with a Bearer-shape 401,
  `/health`, else 404; `Caddyfile.local.j2`, same shape as the prod block in site-config
  `caddy_managed_sites`), with an optional `upstream_tls` that proxies to the gateway over
  mutual TLS presenting a named client leaf. Cloudflare: a challenge skip for the
  inference host's `/v1/*` and `/health` (`platform/infra/cloudflare/waf.tf:124-128`) and
  one `http_ratelimit` rule over `/v1/*` for the inference and admin-UI hosts
  (`ratelimit.tf:38-64`). The zone is on the Pro tier, 2 rate-limit rules at most
  (AGENTS.md, "Inference edge"), so one slot is free. The 125 s Cloudflare read timeout is
  an accepted decision (`plan/architecture/05-platform-infra.md`, "Cloudflare-proxied
  hostnames").
- **`mcp.<zone>` needs no nested-host certificate SAN.** The local wildcard covers one
  label; the nested `admin.inference.<zone>` name needed its own SAN and relies on the
  zone's Total TLS for its edge certificate (`dns.tf`, the `admin.inference` comment), which
  a single-label MCP host does not.

**Research, cited and not re-fetched by this author** (dated 2026-10-14; each is
re-verified by task 7.1 before a template depends on it):

- agentgateway docs, MCP: `https://agentgateway.dev/docs/standalone/latest/documentation/mcp/`
  (the `latest` path tracks `main`, not v1.5.0, so the schema above governs); examples at
  tag v1.5.0: `examples/mcp-basic`, `mcp-multiplex`, `mcp-openapi`, `mcp-authentication`,
  `mcp-authorization`.
- Claude Code client: `https://agentgateway.dev/docs/standalone/latest/integrations/mcp/
  clients/claude/` and `https://code.claude.com/docs/en/mcp`. Reported: `claude mcp add
  --transport http <name> <url> --header "Authorization: Bearer ..."` with scope
  `-s user|project|local`; `.mcp.json` expands `${VAR}` in `url` and headers; a
  `headersHelper` command prints JSON headers on every connect (10 s limit); a project
  `.mcp.json` is auto-loaded in non-interactive and SDK sessions; environment variables
  named `ANTHROPIC_*` are read empty in remote headers, so the fallback variable (D4) is
  not named that.
- pi client (`pi` from badlogic/pi-mono; this workstation runs the `pi-mcp-adapter` npm
  package at 5.1.0 in place of the built-in MCP): streamable HTTP supported, SSE rejected;
  `headers`/bearer values accept `!command` (10 s) or `${VAR}`; the user-level
  `~/.pi/agent/mcp.json` already holds other servers, so installing must merge.

**Unverified, and settled by the local spike (section 1) before any template is written:**

1. An `llm:` block and an `mcp:` block in one file at runtime. The schema allows both;
   runtime behaviour is not read.
2. Whether `apiKey.name` is available in an `mcpAuthorization` CEL rule, and whether
   `budgets` under `mcp.policies.apiKey` are charged at all.
3. The self-call: a `openapi` target whose host is the gateway's own `/v1` listener, which
   requires a client certificate in the mutual-TLS rendering; whether a target pointing
   back at the same listener terminates; what the `backendAuth` of the target looks like
   with an environment-referenced key.
4. `statefulMode` against the 125 s Cloudflare read timeout (a long-lived server-sent
   stream on `/mcp`).
5. Whether `/config_dump` redacts environment-expanded secrets. Unknown, so it stays
   unexposed (D6); the spike only reads the answer for the record.
6. The Keychain lookup command and the headers helper behaviour of both harnesses.

## Goals / Non-Goals

**Goals**
- A key-gated `/mcp` endpoint reachable from a workstation only through its own hostname.
- Agents and operators can send a model request through MCP, charged to a dedicated
  internal identity with its own budget.
- Every declared platform admin holds a gateway identity without a separate step, from one
  declared list.
- A new workstation gets working Claude Code and pi MCP configuration from one idempotent
  command, with no credential in the repository.

**Non-Goals**
- stdio targets, Grafana or metrics MCP, exposing the admin API or `/config_dump` (D2, D6).
- An SSE endpoint for clients (pi rejects SSE; Claude Code speaks streamable HTTP).
- Per-person budgets for model requests made through MCP.
- OIDC or JWT authentication on `/mcp`.
- Changing what the inference hostname serves.

## Decisions

All six were made by the operator on 2026-10-14 and are not reopened here. The rejected
alternatives are recorded so a later reader sees what was weighed.

### D1. MCP on its own hostname, key-gated at the gateway

`agw_mcp_hostname` (inventory; default placeholder `mcp.<zone>`) is a separate Caddy
managed site proxying `/mcp` to the gateway over the same `upstream_tls` mutual-TLS
transport the operator-UI host uses. The gateway's key check is the authentication.
Cloudflare declares the record, a challenge skip and a rate-limit rule for that hostname
in `platform/infra/cloudflare`, applied by `apply-cloudflare-tofu.yml`. The inference
allowlist does not change.

Rejected:
- **`/mcp` on the inference host.** It widens an allowlist that exists because the
  upstream leaves control routes unauthenticated, and it couples MCP traffic to the
  inference rate limit and challenge skip.
- **LAN-only.** The operator's premise is workstation harnesses reaching the gateway from
  outside the LAN, as inference clients already do through the public edge.
- **Serving MCP under the admin-UI host.** That host sits under Cloudflare's managed
  challenge, which the inference skip rule does not extend to
  (`platform/infra/cloudflare/dns.tf`, the `admin.inference` comment), and it exists for a
  browser login a machine client cannot complete.

### D2. Targets first: the endpoint itself, and an OpenAPI target over `/v1`

(a) The gateway's own `/mcp` endpoint is the surface clients connect to. Because the
gateway has no MCP tools of its own (Context), "the endpoint as a target" cannot mean tools
supplied by the gateway; it means the virtual MCP server that multiplexes whatever targets
are declared. Whether declaring a target that points back at the same `/mcp` listener
terminates or recurses is a spike item (1.5); if it recurses, (a) is delivered as the
served endpoint plus target (b), and nothing is declared that points at `/mcp`.

(b) An `openapi` target over the gateway's own OpenAI-compatible `/v1`, from a schema file
shipped in the repository (proposed: chat completions and model listing only; `responses`
deferred), so an agent sends a model request as an MCP tool call. The target
authenticates with `backendAuth` carrying a dedicated internal identity, `mcp-self`, which
is a service identity in `agw_clients` with its own `agw_client_policies` budget, whose
key is read from OpenBao into the environment (`$AGW_MCP_SELF_KEY`, the pattern
`VLLM_API_KEY` uses in `env.j2`) and is never handed out. The self-loop through the
listener, including how a mutual-TLS listener admits the gateway's own call, is a spike
item (1.5).

Rejected:
- **Stdio targets.** The image has no shell (`compose.yml:22-23`).
- **Grafana MCP first.** It needs a second service's credential and an MCP-capable
  Grafana; the self-call proves the whole path with no new dependency. Grafana and metrics
  are listed for later.
- **The self-call straight to the vLLM upstream.** It would skip the gateway's identity,
  budget and telemetry, which is the reason for the gateway.

### D3. One declared admin list feeds both Authentik and the gateway

Personal admin identities reuse each person's own key (the operator's existing identity
keeps its key). Every member of `platform-admins` gets a personal gateway identity
automatically. The process is the fix: one list, declared in site-config, feeds the
Authentik group membership and the gateway's identities.

- Each entry carries the Authentik identity values the blueprint takes today and an
  optional `agw_identity` that overrides the derived gateway name. The derived name is the
  Authentik username; the override exists because an Authentik username need not match the
  gateway charset (`^[a-z0-9][a-z0-9-]*$`, `deploy-agentgateway.yml:123`) and because an
  existing identity must keep its name so manage-secrets reuses `client_<name>` instead of
  minting a new key.
- The Authentik admin entries are rendered from the list: today they are fixed blocks with
  `!Env` values, so "one list feeds both" means a templated blueprint plus a looped secret
  definition per entry (task 2.3). The render is proven behaviour-identical to the fixed
  blocks before the switch (task 2.6).
- `deploy-agentgateway.yml` derives the personal identities from the list and merges them
  with the service identities in `agw_clients` into one `agw_identities` list that the
  secret-declaration loop, the name assert, the `llm` key loop and the new `mcp` key loop
  all read. A name twice in the list, a list name equal to a reserved internal identity
  (`legacy-shared`, `mcp-self`), or a name outside the charset is refused before any
  restart. A name present in BOTH the list and `agw_clients` is the same identity, so it is
  merged once and reported by name as a migration overlap; this is what lets the code that
  derives identities be deployed before site-config removes the derived names (Migration
  Plan).
- **The Authentik verify is the one drift guard.** The Authentik deploy's read-back verify
  (`verify-users.py.j2`, run inside the server container) additionally asserts the members
  of `platform-admins` equal the declared admin list plus an explicit declared exceptions
  list (break-glass and service accounts, names only, in site-config). A member added by
  hand in the Authentik UI, or a blueprint block added outside the list, fails the verify
  instead of silently holding an admin tier with no gateway identity (task 2.9). The
  gateway side adds no second guard: it derives its identities from the same list, so
  "derived equals declared" is true by construction. What the gateway deploy does carry is
  a consistency check of its own render (the name set and the `keyHash` count it renders
  equal the merged identity list), which catches a templating fault, not a membership
  drift.
- Declaring an admin mints `client_<name>` on the next gateway deploy; handout is
  `Back Up Credentials to site-config (Dev)`. The same identities are admitted on `/mcp`.
- Removing an admin from the list drops the hash from the next render, so the key stops
  working; `manage-agentgateway-client-key.yml` validates against the merged list so its
  revoke step still deletes the field.

Dated 2026-10-14, the operator's answer to Open Question 1: every `platform-admins` member moves
into the declared list, so the declared exceptions list is empty by default. The consequence is that
the break-glass account and the service account each get a stored `client_<name>` secret and a
handed-out personal gateway key like any person (the operator confirms this under the Open Question 1
follow-up).

Rejected:
- **A shared key held by the operator.** One leak rotates for everyone and no identity or
  budget is per person.
- **Keeping two lists and documenting the sync.** It is the current state and it already
  produced a second admin with a UI login and no API key.
- **Wiring `/mcp` to the UI's OIDC.** v1.5.0 budgets attach only to API keys
  (`inference-personal-keys`, Why), and a machine client cannot complete a browser login.

### D4. Secret-free client templates and an idempotent installer

Templates live in `platform/services/agentgateway/context/clients/` (`claude-code.json`,
`pi-mcp.json`). One `make` target (proposed `agw-mcp-client`) runs an installer that merges
the agentgateway entry into the user-level configuration: `~/.claude.json` through
`claude mcp add -s user` (the harness's own CLI wherever it exists) and
`~/.pi/agent/mcp.json` by JSON merge that never rewrites another server. The templates
carry a placeholder URL; the installer takes the MCP hostname as an argument, because the
repository is public and a workstation has no inventory. The key mechanism is one: a
command the harness runs at connect time (`headersHelper` for Claude Code, `!command` for
pi) reads the key from the macOS Keychain or the handed-out site-config file and prints the
`Authorization` header as the harness expects. Nothing is exported to the environment and
nothing is written to a configuration file. `${AGW_MCP_KEY}` is a documented fallback only
where `headersHelper` is unavailable, flagged as weaker because an exported variable is
visible to every process the user starts. The installer has a removal mode and prints
names only.

Rejected:
- **A committed repo-root `.mcp.json`.** Claude Code auto-loads a project `.mcp.json` in
  non-interactive and SDK sessions (research above), so a live one would connect every
  automated session in this repository to the gateway. Templates ship; nothing live does,
  and a BATS check asserts no repo-root `.mcp.json` exists.
- **A literal key in the template or the user file.** The repository is public, and a key
  in `~/.claude.json` is readable by every process the user runs.
- **Project scope for the credentialed server.** pi and Claude both keep credentialed
  servers at user level in this design so a repository checkout never carries one.

### D5. Sequence: local spike, then template and verify, then production, then clients

1. Local-dev spike (section 1): the six unverified items above, answered against a scratch
   gateway on the local stack and written into "Findings" below.
2. Template and verify (sections 2 to 4): the deploy asserts an unauthenticated `/mcp` is
   401 and an authenticated `initialize` succeeds.
3. Edge (section 5), then production through the `(Dev)` Semaphore templates, dry run first
   (section 6).
4. Clients (section 7) only after the endpoint is live, because a client template for an
   endpoint that does not exist is unverifiable.

Rejected: **Production first with the spike in prod.** A misrendered `mcp:` block in the
shared file can take `/v1` down (Risks), and production is not where to learn that.

### D6. Not exposed: `/config_dump` and the admin API

Neither is offered as an MCP tool until `/config_dump`'s handling of environment-expanded
secrets is read and proven. The admin listener stays pinned to the container loopback as
the gateway change set it, and no `mcp` target addresses it.

Rejected: **An admin-API MCP target for convenience.** The configuration dump is
unauthenticated at the admin listener (the reason the UI got its own listener), and
whether it redacts what `$VAR` expansion produced is exactly the unverified fact.

## Decisions added by this author (small gaps in the brief)

These are not operator decisions; they close gaps the brief left open and are listed so
the orchestrator can overrule any of them.

- **G1. Attach to `default` only, serve `/mcp` only at the edge.** `mcp.gateways:
  [default]` explicitly, so `/mcp` is not also served on the UI listener behind
  `admin.inference`. The schema also serves `/sse`; the MCP Caddy site is an allowlist
  (exact `handle /mcp` with a Bearer-shape 401, bare `handle { respond 404 }`; if the
  streamable-HTTP transport needs any sub-path, spike item 1.6 decides and the spec
  changes with it), so `/sse` is
  not reachable through the edge even though the gateway answers it.
- **G2. The legacy shared key and `mcp-self` are not admitted on `/mcp`.** The shared macro
  takes the identity list as a parameter, and the `mcp` call passes the merged identities
  without `legacy-shared` and without `mcp-self`, which exists only as the target's
  `backendAuth` identity for the self-call and must never authenticate a client. Whether budgets
  carry over to MCP traffic is spike item 1.4; if they do
  not, `mcp-self` is the only budgeted path and its budget is the control.
- **G3. Cloudflare: one new skip rule and exactly one new rate-limit rule.** A separate
  `mcp-api-bypass-challenge` skip rule on the MCP host's exact `/mcp` path (the inference rule's
  expression is not widened), and one new rate-limit rule with its own bucket, figures set
  after the spike measures a real session's handshake burst (initialize, initialized
  notification, tool listing). That uses the zone's last free rate-limit slot; the
  fallback if the slot is wanted for something else is to add the MCP host to the
  existing rule's host set at the cost of sharing its 10 requests per 10 seconds bucket.
- **G4. The `mcp` block sits behind `agw_mcp_enabled`, default false.** With the flag
  false nothing about the rendered file changes for an existing deployment, and the
  deploy verify asserts `/mcp` is not served.
- **G5. A pre-restart validation.** The MCP-enabled rendering is checked with the existing
  `platform/playbooks/tasks/agw-validate-config.yml` (it runs the pinned image's
  `--validate-only` against the rendered file before any container is recreated, per its
  header) because the file is shared with `llm`. That the validator accepts and rejects
  the `mcp:` block correctly is spike item 1.1.
- **G6. An MCP request bucket is a spike item.** `mcp.policies.localRateLimit` (a
  `FilterOrPolicy` key) as the MCP counterpart of the global `llm` bucket is measured in
  the spike and rendered only if it behaves.

## Interaction with inference-personal-keys

The unarchived `inference-personal-keys` change mints `user-<username>` identities from an
Authentik group (`inference-users`) with OpenBao delivery and monthly rotation, and has a
scenario that fails an `agw_clients` entry named `user-x`. This change is a companion:

- Derived names here are the Authentik username or the explicit `agw_identity`, with no
  `user-` prefix, so the two namespaces cannot collide.
- This change is the interim path for admins, who need a key now. If
  `inference-personal-keys` lands, admins are also eligible for its personal key; at that
  point the admin-derived identity is retired by removing the `agw_identity` derivation, a
  decision that change's owner takes, not this one. Recorded as Open Question 3.
- Both changes edit the same `apiKey` rendering. The shared macro here is written so the
  per-user entries that change renders can be passed in as another identity source.

## Risks / Trade-offs

- **One file, two surfaces.** A bad `mcp:` render can break `/v1`. Mitigated by the flag
  default (G4), the pre-restart validation (G5), the local spike, and a dry run first in
  production.
- **The self-call is charged to `mcp-self`.** Every MCP caller shares one budget for model
  requests, so one heavy caller can exhaust it for the rest, and telemetry attributes the
  model call to `mcp-self`, with the caller visible only on the `/mcp` request. Accepted
  for the first target; per-caller attribution is Open Question 2.
- **The 125 s Cloudflare read timeout.** A long-lived stream on `/mcp` is cut at 524 after
  125 s idle. Mitigation to measure: `statefulMode: stateless` so no session depends on a
  held stream, and client reconnect. If stateless is not enough, the risk stands as the
  same accepted decision the inference route recorded.
- **Rendering Authentik from a list.** A wrong render could rename or deactivate an admin.
  Mitigation: task 2.6 renders both forms for the same inputs and diffs them, and the
  Authentik deploy's read-back verify (every blueprint `successful`, every declared account
  present, active and in a group) fails the deploy on a miss.
- **A key on a workstation.** The installer never writes the key; the helper reads it at
  connect time. A user who falls back to `${AGW_MCP_KEY}` (D4) and exports it into a shell profile
  has
  chosen the weaker path; the documentation says so.
- **Rate-limit slot.** G3 uses the last free slot on the zone.
- **The internal key and the empty-fields handout.** See Open Question 6.
- **Who may call `/mcp` (2026-10-14, security review).** MCP admits personal admin identities only; a
  service client cannot reach the chat tool, so `allowedModels` and per-client budgets are not escapable
  through `mcp-self` (the self-call runs as `mcp-self`, which carries neither the caller's model allow-list
  nor the caller's budget). An admin's MCP model spend is bounded by `mcp-self`'s budget, not their own
  (accepted). Enforced by the deploy: `/mcp` is rendered from the admin-derived identity list, never from
  `agw_clients`; the deploy refuses MCP with no admin identities, with an `allowed_models` on an admin
  identity, or without `agw_client_policies.mcp-self.tokens_per_hour`. This amends G2: the `mcp` call no
  longer passes the merged identities.
- **Mutual TLS for the self-call.** If the listener cannot admit the gateway's own call
  without widening the SAN allowlist beyond the Caddy leaf, that widening is a security
  trade-off for the operator to take, not a default (spike item 1.5 reports it).

## Migration Plan

1. Spike (section 1), findings recorded below.
2. Land the admin list and the Authentik render with no change in outcome (section 2.1 to
   2.6, drift guard 2.9). The code that derives identities merges overlapping names (D3),
   so it is merged AND deployed before any site-config change removes a derived name from
   `agw_clients`.
3. Land the gateway merge and the `mcp` block flagged off (sections 2 and 3); deploy
   through the `(Dev)` template, dry run first; verify asserts MCP is not served. The
   rendered key-hash count is read from the rendered config on the host (or the deploy's
   own report) before and after the real deploy, never from a check-mode diff: whether
   `--check` output shows the rendered `keyHash` lines is unverified. Only then does a
   second site-config change remove the derived names from `agw_clients` (task 6.8), held
   until the deployed branch carries the derivation, followed by an inventory sync and a
   real deploy, because the render changes only on deploy.
4. Add the first target (section 4), then the edge (section 5): Cloudflare plan then
   apply, Caddy managed site dry run then apply.
5. Flip `agw_mcp_enabled`, deploy, verify, hand out keys (section 6).
6. Publish the client templates and installer (section 7).

## Open Questions

1. Does the declared admin list include the bootstrap and break-glass accounts? Those
   blueprint files also put a user in `platform-admins` (`stray-admin.yaml:25`,
   `agent-cloud-admin.yaml:35`, `service-account.yaml:22`). Each must move into the list or
   be named in the declared exceptions list the drift guard reads (D3). Default proposed:
   only accounts a person signs in with are in the list; a service or break-glass account
   is an exception and is not given an API key.
   - Answer 2026-10-14 (operator): every `platform-admins` member moves INTO the declared admin list,
     including the break-glass account and the service account; each gets a personal gateway identity.
     This replaces the proposed default above; no account is named in the exceptions list for now.
   - Follow-up (open, for the operator): the break-glass account and the service account then each get a
     stored `client_<name>` secret in OpenBao and a handed-out gateway key (see D3, dated line); confirm
     that is intended.
2. Per-caller attribution for model requests made through MCP: pass the caller's identity
   to the self-call, or accept `mcp-self`? Out of scope here.
3. When `inference-personal-keys` lands, do admins keep the derived identity or move to
   the personal key?
   - Answer 2026-10-14 (operator): admins first. `inference-personal-keys` later generalises the list
     mechanism; admins keep their key.
4. Where the helper reads the key first, the site-config file or the Keychain, and whether
   the Keychain item is created by the installer or by hand.
5. The MCP rate-limit bucket figures, after measurement.
6. `Back Up Credentials to site-config` with its fields left empty copies every field at
   the secret path (the template's own survey text: "empty = every field at the path",
   `platform/semaphore/templates.yml`, `Back Up Credentials to site-config`), which would
   include the internal `client_mcp-self` key and the upstream key. Existing behaviour, not
   introduced here; whether to add a field denylist is the operator's decision. Until then
   the documentation names fields explicitly and never shows an empty list.
   - Answer 2026-10-14 (operator): no field denylist. Runbooks always name the fields explicitly.
7. D2(a) is read as "the served `/mcp` endpoint", not "a target that points at it", because
   the gateway has no MCP tools of its own and a self-target may recurse. The operator
   confirms the reading (task 0.4); spike item 1.5 settles whether the literal form is even
   possible.
8. How the gateway's own call is admitted when `agw_listener_tls` is on (spike 1.5): a second client
   leaf plus a widening of `agw_client_cert_allowlist`, or a plain listener the gateway calls itself.
   - Answer 2026-10-14 (operator): the loopback listener; no widening of the allowlist. Measured in the
     spike (1.5, loopback paragraph); v1.5.0 has no gateway bind address, so the listener is restricted
     by a `source.address` rule instead of a bind, which the operator should confirm is acceptable.

## Findings (spike 2026-10-14)

Local spike, tasks 1.1 to 1.8 and the local gate 1.10. Image: `cr.agentgateway.dev/agentgateway:v1.5.0`
(the pin in `compose.yml`). Everything ran on the local container engine in throwaway containers
named `agw-spike-*` (the gateway, its budget Postgres and a one-shot probe) on a private network, with a
stub OpenAI-compatible and REST upstream run from the scratchpad. The scratch configuration was written
by hand in the shape `config.yaml.j2` renders (`config:` with `database.url: $AGW_DATABASE_URL`,
`gateways.default` on 4000, `llm:` with `keyHash: sha256:` identities and an hourly token budget each,
one `custom` model, plus the `mcp:` block under test); it was not rendered from the template, and it
has no UI listener or access-log policy. Keys are synthetic strings generated for the run, enrolled as
hashes; no real secret, credential or host was used, and nothing was committed but this text. No
finding contradicts a decision D1 to D6 or G1 to G6; the ones that change what a later task must do are
marked **Impact**.

**1.1 `llm:` and `mcp:` in one file (2026-10-14).** `--validate-only` accepts a file with both blocks
(`Configuration is valid!`, exit 0), and so does `tasks/agw-validate-config.yml` when included from a
throwaway play (`_deploy_dir` set to a scratch directory, `local_mode: false`, `_tls: false`, the OpenAPI
schema inline so no schema mount is needed): exit 0, success assertion. Started, the gateway serves both:
`GET /v1/models` without a key is 401 and with an enrolled key is 200, a chat completion through `/v1` is
200 with the stub's `usage` (30 tokens), and `initialize` on `/mcp` is 200. Deliberately wrong `mcp:`
blocks, each fed to the validator (exit code and the first line of its output):
`mcp.bogusField` exit 1 `unknown field`; an `openapi` target with no `host` exit 1 `host or backend is
required for MCP target backend configuration`; a syntactically broken `mcpAuthorization` CEL rule
(`mcp.tool.name == `) exit 1 with the CEL parse error; `mcp.gateways: [nonexistent]` exit 1 `unknown
gateway reference`; an `openapi.schema.file` that is not mounted exit 1 `read resource file`. Through the
repository task the same blocks are refused with the allowlisted categories `config-schema-error` (the
unknown field) and `validator-nonzero-exit-1` (the missing host and the broken CEL rule); the task's
`no_log` and safe-category handling held. **The validator does not catch a semantic CEL error**: a rule
naming an unknown field (`apiKey.nosuchfield == "x"`) is accepted (exit 0), and at runtime it silently
matches nothing (see 1.3). **Impact (G5):** the pre-restart validation is real for structure and for CEL
syntax and refuses before any restart, but a wrong identifier in an `mcpAuthorization` rule passes it, so
the deploy verify must prove an authorised tool is listed, not only that `/mcp` answers. A second
finding bears on 1.1: with no `mcp.policies.apiKey`, `/mcp` is open: `initialize`, `tools/list` and
`tools/call` (the `ping` tool returned its result) all succeeded with no key, and `/sse` answered 200.
**Impact (G4, G2):** the `mcp` block must never be rendered without the key policy; the render check in
tasks 3.2 and 3.6 and the verify assert the policy is present whenever `agw_mcp_enabled`.

**1.2 Authentication on `/mcp` (2026-10-14).** With `mcp.policies.apiKey` of the same shape as the `llm`
one (`mode: strict`, `Bearer` header, a `keyHash` list of two identities, a third enrolled in `llm` only),
a scripted client (JSON-RPC over HTTP, `Accept: application/json, text/event-stream`) got: no key 401
`api key authentication failure: no API Key found`; an unknown key 401; the identity enrolled in `llm` but
left out of the `mcp` list 401 on `initialize`, `notifications/initialized` and `tools/list`; an enrolled
identity 200 on `initialize` (session header set), 202 on the notification, 200 on `tools/list`. A bare
`GET /mcp` and `GET /sse` without a key are 401 as well. The same third identity still works on `/v1`
(200), so the two key lists are independent, as G2 needs. The 401 requests are logged with
`protocol=http` and never reach the MCP layer; the stub upstream's request log did not grow during the
unauthenticated attempts (19 lines before and after). `initialize` and `tools/list` do **not** need a
healthy target for an `openapi` target: with the stub upstream stopped, under both `failureMode:
failClosed` (the default) and `failOpen`, `initialize` and `tools/list` returned 200 and the full tool
list (the tool list comes from the schema); only `tools/call` failed, 503 `upstream call failed: Connect:
Connection refused`. **Impact:** task 3.4's probe cannot use `initialize` or `tools/list` as a target
health signal; it proves the key check and the schema only. A tool call is the only target-health proof.
`failureMode` therefore changes nothing for an openapi target at handshake time; it was not exercised
for `mcp` or `sse` targets.

**1.3 `apiKey.name` in an `mcpAuthorization` CEL rule (2026-10-14).** Rules `allow: 'mcp.tool.name ==
"ping"'` and `allow: 'mcp.tool.name == "echo" && apiKey.name == "alice"'` under `mcp.policies`: the
identity `alice` listed both tools and called both (200 with the result); the identity `bob` listed only
`ping`, called `ping` (200), and calling `echo` returned 400 JSON-RPC `-32602 Unknown tool: echo`. So the
identity is evaluated, and a denied tool is hidden from the listing, not just refused at the call. A rule
with a wrong field (`apiKey.nosuchfield`) denied the tool for everyone with the same `Unknown tool`
answer and nothing in the gateway log beyond the request line. **Impact:** per-identity tool grants are
available as designed; a typo fails closed and silently (see 1.1 for why the validator cannot catch it).

**1.4 Budgets under `mcp.policies.apiKey` (2026-10-14).** `budgets:` entries on the MCP key entries are
accepted by the validator and by the running gateway, and are **not charged and not enforced**. With an
MCP budget named `mcp-tiny` of one token on both identities, three tool calls returned 200 each; the
gateway's budget table (`budget_usage` in its Postgres) held only the `llm` row (30 tokens from an earlier
chat) and no `mcp-tiny` row, and no MCP request appears in `request_logs` (one row in total, the chat).
Budgets are charged from the LLM usage in a model response (`llm:` and the self-call below); an MCP
request has no such usage. The budget identifier is the key hash plus the budget name, so an `llm` and
an `mcp` budget with the same name on the same key would share one counter. **Impact (G2 holds):** the
MCP key entries carry no budget; `mcp-self`'s `llm` budget is the only budgeted path, exactly the
fallback G2 states. Rendering budgets under the MCP policy would be a silent no-op that reads as a
control, so the template must not render them.

**1.5 The self-call (2026-10-14).** Target: `openapi` over a two-operation schema of the gateway's own
`/v1` (`list_models`, `chat_completions`) with `host: 127.0.0.1:4000`, `policies.backendAuth.key.value:
$AGW_MCP_SELF_KEY` and `location.header` `authorization` with prefix `Bearer ` (the variable set in the
container environment, as `VLLM_API_KEY` is), the `mcp-self` hash enrolled in `llm` and **not** in the
`mcp` key list (G2). Plain listener: `tools/call list_models` and `chat_completions` as `alice` returned
the upstream's bodies; the budget table gained a row for the `mcp-self` key hash (30 tokens), and
`request_logs` recorded the model call with `agentgateway_user = mcp-self`, so the charge and the
attribution are as D2(b) and the Risks section say, with the caller visible only on the `/mcp` request.
The environment reference expanded and authenticated: the same call with the `backendAuth` removed came back as a tool result carrying `{"code":401,"message":"api key authentication failure: no API Key found"}`.

*Mutual TLS.* Listener TLS was exercised against a scratch CA made with `openssl` (no step-ca leaf was
minted and the local stack was not touched): server leaf, one client leaf with SAN `caddy-spike.test` in
the allowlist (the role of the Caddy leaf), one with SAN `gw-self.test` outside it, and the exact
`tls` + `authorization.require` block `listener_tls()` renders. From outside: no client certificate fails
the handshake (`certificate required`); the non-allowlisted leaf with a valid key is 403 `authorization
failed`; the allowlisted leaf without a key is 401, with a key 200, matching the 2026-10-02 `/v1`
measurement in `config.yaml.j2`. The gateway's own call then needs, in order: (a) an `https` target host
with `backendTLS.root` set (a plain-HTTP target against the TLS listener returns 503 `invalid HTTP
version parsed`); (b) a client certificate and key presented by the gateway (`backendTLS.cert`/`key`;
with the root only, 503 `CertificateRequired`); (c) that certificate's SAN **inside the allowlist**: with
a client leaf outside it, the self-call is refused 403 by the gateway's own listener rule even with the
valid `mcp-self` key. With the SAN added to the allowlist it succeeds and returns the model list.
**Impact (reported to the operator, not decided):** under `agw_listener_tls` the self-call needs a
second client-profile leaf for the gateway (cert and key mounted into its container, a declared
`internal_leaves` entry) AND its SAN added to `agw_client_cert_allowlist`, which today is the Caddy leaf
alone: a **widening of the allowlist**, now admitting any holder of that leaf. A different shape would
avoid the widening, a second plain-HTTP listener attached to `llm` only; the operator took it, and it is
built and tested in the next paragraph. Two further measured behaviours bear on the
verify: a 403 (or the 401 above) from the self-call is returned to the MCP client as an HTTP 200 whose tool
result is the text `{"code":403,"message":"authorization failed"}`, so a probe must inspect the tool
result, not the status; and the loop below.

*A target pointing back at `/mcp`.* It **recurses without a bound that protects the gateway.** An `mcp`
target (`host` 127.0.0.1, port 4000, path `/mcp`) on an open `/mcp`: one `initialize` produced 17,350
nested `/mcp` requests in about 15 s, took the gateway to 65% CPU and 3.9 GB, and ended 500 `Cannot assign
requested address` (local port exhaustion); memory fell back over the next seconds and `/v1` readiness
stayed 200 afterwards. On a key-gated `/mcp` the same target without `backendAuth` ends at once: the
nested call is refused 401 (the client's `Authorization` header is not forwarded to a target) and that
401 is returned to the client as the answer to `initialize`. With `backendAuth` carrying a key admitted
on `/mcp` (here `mcp-self`, admitted for this test only) and `mcp.policies.timeout.requestTimeout: 3s`,
`initialize` returned 504 `request timeout` at 3.0 s after about 9,850 nested requests and 870 MB, which
settled to 420 MB; the timeout bounds the client's wait, not the loop's cost. The validator accepts all of
these. **Impact (D2(a) as written):** the "if it recurses" branch applies: (a) is delivered as the served
endpoint plus target (b), and no target may point at `/mcp`. Because the validator does not refuse it,
the deploy needs its own refusal, before any restart, of any `mcp`/`sse` target whose host and port are
the gateway's own listener (a task for section 3, not a decision change).

**1.5, the loopback self-call (2026-10-14, after the operator's decision to take it instead of
widening the allowlist).** Same scratch set-up as above (scratch CA, the `listener_tls()` block on the
external gateway `default` on 4000 with the allowlist left at the one Caddy-role SAN) plus a second
gateway, `loopback`, plain HTTP on 4002, attached to `llm` only (`llm.gateways: [default, loopback]`,
`mcp.gateways: [default]`, no UI). The `openapi` self-target's host is `127.0.0.1:4002`, its
`backendAuth` is the `mcp-self` key from an environment reference, and no client certificate is
involved. **A deviation from the decision as phrased, forced by the schema:** v1.5.0 gateways take a
`port` and no bind address (`LocalGateway` has `port`, `protocol`, `listeners`, `tls` and policies only;
the schema offers an address only for the admin, stats and readiness servers), so the listener binds
every container interface, "bound to the container loopback" is not expressible, and an unpublished port
alone is not loopback. Measured with no extra rule: a neighbour container on the same compose-style
network reached `loopback:4002` (no key 401, a valid key 200 with the model list). The restriction is
therefore a gateway-level rule on the loopback gateway, `authorization.rules: - require:
'source.address == "127.0.0.1"'`, and with it: the same neighbour is 403 `authorization failed` with or
without a key (the rule runs before key authentication), while the gateway's own call (peer
`127.0.0.1`) and an in-container client still pass. Results:
(a) through the external mTLS listener with the Caddy-role leaf, `tools/call list_models` and
`chat_completions` as `alice` returned the upstream's real bodies (not an embedded error); the budget
table gained one row for the `mcp-self` key hash (30 tokens) and `request_logs` recorded the model call
with `agentgateway_user = mcp-self`: charged and logged as `mcp-self`.
(b) no port 4002 is published (`curl` from the host to 4002 failed to connect, exit 7); inside the
container namespace a request to the loopback listener with no key is 401 `no API Key found` and
with a valid key 200.
(c) MCP is **not served** on the loopback listener, but it is not a clean 404 either: the `llm` route
there accepts any path, so an MCP `initialize` posted to `/mcp` is answered 400 `LLM request body is
missing string field 'model'`, `GET /sse` 400, no key 401; an LLM-shaped body posted to `/mcp` is
**forwarded to the model upstream** as `/v1/mcp` and returns the upstream's own 404. The same
pass-through exists on the external listener today (the reason Caddy is an allowlist); on loopback it is
reachable only by the gateway's own calls (rule above) and those use only the two schema operations.
(d) the external listener is unchanged and is not widened: no client certificate fails the handshake;
the gateway-role leaf, whose SAN is not in the allowlist, is 403 even with a valid key; the Caddy-role
leaf without a key is 401 and with a key 200; the allowlist expression names the Caddy-role SAN only.
(e) a target aimed at the loopback `/mcp` (`mcp` kind, `127.0.0.1:4002/mcp`, `backendAuth` `mcp-self`)
**does not recurse**: `initialize` returned 400 `missing_model` from the LLM handler at once and
the gateway logged three MCP requests in total (the client's), CPU and memory idle afterwards.
(f) a target aimed at the **external** listener's `/mcp` (`mcp` kind, port 4000, `backendTLS` with the
gateway-role leaf, `backendAuth` `mcp-self`, which is not admitted on `/mcp`) is refused 403 by the
external listener's SAN rule and terminates (one MCP request logged, no loop); with no client certificate
it fails at the handshake (1.5 above) and on the plain listener with no key it is 401.
**Impact (section 3, deploy-time refusal):** every self-reaching shape measured above terminates once the
listener is TLS and `mcp-self` is not admitted on `/mcp`, but the validator accepts all of them, and a
plain external listener with a key admitted on `/mcp` is the earlier unbounded recursion, so the deploy
refuses, before any restart, by allowlist and not by denylist: (1) the only
target kind rendered is `openapi` over the shipped schema; an `mcp` or `sse` target is refused; (2) an
`openapi` target's host must equal the declared loopback listener address and port (`127.0.0.1` and the
loopback port); any other host is refused, since a string check cannot enumerate every alias of the
external listener (`localhost`, the container name, a bridge address) and a denylist of port 4000 would
miss them; (3) the loopback listener carries the `source.address` rule, is attached to `llm` only, and its
port is never in `compose.yml`'s published list (a BATS check on the rendered config and the compose
file, mutation-checked); (4) `mcp-self` stays out of the `/mcp` key list (G2). The `source.address`
comparison uses the IPv4 loopback literal; an IPv6-only path was not exercised.

**1.6 `statefulMode` (2026-10-14).** Headers: `initialize` needs `Content-Type: application/json`
(missing: 415) and an `Accept` naming **both** `application/json` and `text/event-stream` (JSON only, event
stream only, `*/*` and none: 406 `client must accept both application/json and text/event-stream`). The
response to `initialize`, `tools/list` and `tools/call` is always `Content-Type: text/event-stream`
carrying one `event: message` / `data: {json-rpc}` frame, closed by the server, never a bare JSON body;
errors raised by the gateway itself (unknown tool, rate limit) come as a JSON body with the JSON-RPC
error and HTTP 200 or 400. **Impact (task 3.4):** the probe sends both `Accept` values and reads the
`data:` line. *Default (stateful):* `initialize` returns an `Mcp-Session-Id`; a request without it is 400
`session header is required for non-initialize requests`. The id is a self-describing token (base64 of
a small JSON naming the target set and a random id): the gateway holds no session, so `DELETE /mcp` with
it returns 202 and the same id keeps working afterwards, and an id issued to one identity worked with
another identity's key (authorisation is still decided per request from the key). `GET /mcp` with a
session opens a server stream (200, `text/event-stream`); with no data and no keep-alive it stayed
open for the whole 155 s I held it and was not closed by the server, so behind Cloudflare an idle stream
would be cut at the 125 s timeout (production measure stays 6.6); `GET` without a session is 400.
*`statefulMode: stateless`:* `initialize` returns **no** session header; `tools/list` and `tools/call`
succeed with no prior `initialize` and no session header; `GET /mcp` is 405, so no server-initiated
stream is ever opened. **Impact (Risks, 125 s):** stateless is confirmed to remove the held stream, so
the only request that can approach 125 s is one long `tools/call` (a model call through the self-call);
it was not measured. *Paths:* `POST` to `/mcp`, `/mcp/`, `/mcp?x=1` and `/mcp/sub` all reach the
transport; `/sse` is 422, `/message` and `/` are 400. Nothing beyond the exact `/mcp` is needed, so the
exact-match Caddy block of G1 is sufficient (a client configured with a trailing slash would get the edge
404). `dnsRebindingProtection` was not enabled (opt-in, not exercised).

**1.7 `mcp.policies.localRateLimit` (2026-10-14).** The block renders and limits: with `[{type: requests,
maxTokens: 4, tokensPerFill: 4, fillInterval: 60s}]` the validator accepts, and `/v1` is unaffected (six
`/v1/models` calls all 200). One authenticated session consumed the bucket per HTTP request: `initialize`,
`notifications/initialized`, `tools/list` and the first `tools/call` succeeded (four tokens), the second
and third `tools/call` were refused. **A refusal is HTTP 200 with a JSON-RPC error** (`-32003 rate limit
exceeded`, data with `limit`, `remaining`, `retryAfter`), not 429, so Cloudflare and a status-only probe
see success. The bucket is one for the whole `/mcp` surface: a second identity (`bob`) was refused while
`alice` had drained it, and the 401 for an unauthenticated request is answered before the bucket (key
check first). **Impact (G6, task 5.4):** a handshake costs three requests (initialize, initialized,
tools/list) before any tool call, so a bucket of N per minute admits at most N/3 new sessions a minute;
whether the optional `GET` stream in stateful mode is a fourth request was not measured. The figure is a
deploy choice, and the one shared bucket means one heavy caller starves the rest, as with the `llm`
bucket. A real
Claude Code or pi handshake was not run (client work is section 7); the count above is from a scripted
client, so it is the protocol minimum.

**1.8 `/config_dump` redaction (2026-10-14).** With an environment variable holding a canary
(`CANARY-...-NOT-A-SECRET`) referenced as the target `backendAuth.key.value`, the dump was read from a
one-shot probe container sharing the gateway's network namespace (`GET /config_dump` on the admin
loopback): neither the canary value nor the variable name appears; the dump shows `'value':
'<redacted>'` for `backendAuth.key`, and `'users': '<redacted>'` for every `apiKey` policy (no hash
either). **But the dump does not redact `config.database.url`**: the synthetic Postgres password the
gateway expanded from `$AGW_DATABASE_URL` appears in clear text in the dump (confirmed by a substring
test, not printed). **Impact:** D6 holds, and is strengthened: the dump stays unexposed, and the admin
listener is not only unauthenticated but holds the budget database credential in plain form; the
loopback pin in `config.yaml.j2` is therefore load-bearing for that credential too. The OIDC client
secret and cookie seed (also env-expanded) were not tested because the scratch config had no UI listener.

**1.10 Local gate (2026-10-14), scratch config with `mcp.policies.apiKey` and two enrolled identities.**
Scenario "Unauthenticated MCP request is refused": `initialize` with no key 401, a following `tools/call`
with no key 401, both `api key authentication failure: no API Key found`, logged as `protocol=http`, and
the stub upstream's log unchanged (19 lines before and after): refused and reached no target. Scenario
"An enrolled identity lists tools": with an enrolled key `initialize` 200, `notifications/initialized`
202, `tools/list` 200 returning the target's two tools (`ping`, `echo`); the gateway log shows the three
MCP requests (`mcp.method.name` initialize, notifications/initialized, tools/list). Both scenarios passed
locally.

**Not exercised.** The 125 s Cloudflare timeout itself (production, 6.6); real Claude Code and pi
handshakes (section 7); `mcp` and `sse` targets other than the loopback one, and `failureMode` for them;
`/config_dump` handling of
the UI's env-expanded OIDC values (1.8). The mutual-TLS cases used a scratch `openssl` CA, not the
platform's internal CA, so the allowlist and handshake behaviour are measured but a step-ca leaf with its
real SAN profile was not. Also not exercised: an IPv6-only path to the loopback listener (1.5).
