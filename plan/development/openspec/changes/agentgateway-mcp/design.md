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
  the Authentik `env.j2`); RBAC is `platform-groups.yaml`; the blueprint files under
  `platform/services/authentik/deployment/blueprints/` that name `platform-admins` are
  `platform-users.yaml` (the admin blocks), `agent-cloud-admin.yaml`, the operator-admin
  blueprint, `service-account.yaml` (a service account, not a person) and
  `openbao-oidc.yaml` (read 2026-10-14 by grep; which of them put a user in the group, as
  opposed to gating on it, is task 2.1). The gateway UI gates on
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
  named `ANTHROPIC_*` are read empty in remote headers, so the key variable is
  `AGW_MCP_KEY`.
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
terminates or recurses is a spike item (3.5); if it recurses, (a) is delivered as the
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
  all read. A name present in both sources, or twice, is refused before any restart.
- Declaring an admin mints `client_<name>` on the next gateway deploy; handout is
  `Back Up Credentials to site-config (Dev)`. The same identities are admitted on `/mcp`.
- Removing an admin from the list drops the hash from the next render, so the key stops
  working; `manage-agentgateway-client-key.yml` validates against the merged list so its
  revoke step still deletes the field.

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
`claude mcp add -s user` (or the equivalent merge) and `~/.pi/agent/mcp.json` by JSON merge
that never rewrites another server. The key is read at connect time by a command reference
(`headersHelper` for Claude Code, `!command` for pi) from the handed-out site-config file
or the macOS Keychain, exported to the harness as `AGW_MCP_KEY`. The installer has a
removal mode and prints names only.

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
  (`handle /mcp*` with a Bearer-shape 401, bare `handle { respond 404 }`), so `/sse` is
  not reachable through the edge even though the gateway answers it.
- **G2. The legacy shared key is not admitted on `/mcp`.** The shared macro takes the
  identity list as a parameter, and the `mcp` call passes the merged identities without
  `legacy-shared`. Whether budgets carry over to MCP traffic is spike item 1.4; if they do
  not, `mcp-self` is the only budgeted path and its budget is the control.
- **G3. Cloudflare: one new skip rule and exactly one new rate-limit rule.** A separate
  `mcp-api-bypass-challenge` skip rule on the MCP host's `/mcp` path (the inference rule's
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
  connect time. A user who exports `AGW_MCP_KEY` into a shell profile has defeated that;
  the documentation says not to.
- **Rate-limit slot.** G3 uses the last free slot on the zone.
- **The internal key and the empty-fields handout.** See Open Question 6.
- **Mutual TLS for the self-call.** If the listener cannot admit the gateway's own call
  without widening the SAN allowlist beyond the Caddy leaf, that widening is a security
  trade-off for the operator to take, not a default (spike item 1.5 reports it).

## Migration Plan

1. Spike (section 1), findings recorded below.
2. Land the admin list and the Authentik render with no change in outcome (section 2.1 to
   2.6); migrate existing identity names (2.7) in one site-config change, so no key is
   re-minted.
3. Land the gateway merge and the `mcp` block flagged off (sections 2 and 3); deploy
   through the `(Dev)` template, dry run first; verify asserts MCP is not served.
4. Add the first target (section 4), then the edge (section 5): Cloudflare plan then
   apply, Caddy managed site dry run then apply.
5. Flip `agw_mcp_enabled`, deploy, verify, hand out keys (section 6).
6. Publish the client templates and installer (section 7).

## Open Questions

1. Does the declared admin list include the bootstrap and break-glass accounts? Those
   blueprint files also put a user in `platform-admins`. Default proposed: only accounts a
   person signs in with; a service or break-glass account is not given an API key.
2. Per-caller attribution for model requests made through MCP: pass the caller's identity
   to the self-call, or accept `mcp-self`? Out of scope here.
3. When `inference-personal-keys` lands, do admins keep the derived identity or move to
   the personal key?
4. Where the helper reads the key first, the site-config file or the Keychain, and whether
   the Keychain item is created by the installer or by hand.
5. The MCP rate-limit bucket figures, after measurement.
6. `Back Up Credentials to site-config` with its fields left empty copies every field at
   the secret path (the template's own survey text: "empty = every field at the path",
   `platform/semaphore/templates.yml`, `Back Up Credentials to site-config`), which would
   include the internal `client_mcp-self` key and the upstream key. Existing behaviour, not
   introduced here; whether to add a field denylist is the operator's decision. Until then
   the documentation names fields explicitly and never shows an empty list.

## Findings

To be written by the local spike (tasks 1.1 to 1.9): one dated paragraph per unverified item,
with the command and the observed output, before section 2 starts and before any
template change depends on an answer.
