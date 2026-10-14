# MCP on the agentgateway, per-admin identities, and harness clients

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Explored 2026-10-14; the six decisions
below (D1 to D6) were made by the operator on 2026-10-14 and are recorded as decided in
`design.md`.

Companion changes: `inference-gateway-agentgateway` (the gateway this change extends; its
listener mutual TLS, `llm` shortcut, API-key rendering and Caddy routes are the base),
`inference-personal-keys` (mints `user-<username>` identities from an Authentik group;
overlap recorded in `design.md`, "Interaction with inference-personal-keys"), and
`agentgateway-observability` (the gateway's signals; MCP traffic is labelled by the same
`identity`).

## Why

The gateway speaks MCP natively, and the platform's agents and the operators' coding
harnesses (Claude Code, pi) are the consumers the inference-gateway change named as
"on the roadmap". Four things stand between that and a working setup:

- **No MCP surface is configured.** `config.yaml.j2` renders the `llm:` shortcut and the
  two gateways and nothing else
  (`platform/services/agentgateway/deployment/templates/config.yaml.j2:79-108,169-303`).
  The gateway has no MCP tools of its own: its admin router serves only profiling,
  memory, shutdown, debug, `config_dump` and logging routes
  (`crates/agentgateway/src/management/admin.rs:185-210` at tag v1.5.0), so MCP exists
  only as a proxy in front of targets the platform declares.
- **The public edge is inference-shaped.** The inference hostname is an allowlist of
  `/v1/*` and `/health` at Caddy, with a matching Cloudflare challenge skip and rate
  limit (`platform/infra/cloudflare/waf.tf:124-128`, `ratelimit.tf:38-64`). An MCP path
  added there would widen an allowlist whose whole point is that it is narrow.
- **Admin access is two hand-kept lists.** Authentik users and their group membership
  are declared as code, one fixed block per person with `!Env` identity values
  (`platform/services/authentik/deployment/blueprints/platform-users.yaml:1-24`), and the
  gateway UI admits whoever is in `platform-admins` (`config.yaml.j2:105-107`). The API
  identities are a separate list, `agw_clients`
  (`platform/playbooks/vars/secret-declarations/agentgateway.yml:18`). A second admin who
  signs in to the UI therefore holds no API key, and nothing says so.
- **Clients have nothing to install.** Handing a key out works
  (`platform/services/agentgateway/context/agent-practices.md:124`), but each person
  would otherwise hand-edit their harness's MCP configuration, and the easy way to share
  that setup is to commit a repo-root `.mcp.json`, which Claude Code auto-loads in
  non-interactive and SDK sessions.

## What Changes

- **A key-gated `/mcp` surface on the gateway (D5, D6).** An `mcp:` block on the
  `default` gateway only (never the UI listener), its `policies.apiKey` built from a
  shared Jinja macro that also renders the `llm` key loop, so the two lists cannot drift.
  Off by default behind an inventory flag; neither the legacy shared key nor `mcp-self`
  is admitted on it.
  The configuration dump and admin API are not exposed as MCP tools.
- **Two first targets (D2).** The gateway's own `/mcp` endpoint is what clients connect to
  (the gateway has no tools of its own); the first target is an OpenAPI target over the
  gateway's own OpenAI-compatible `/v1`, from a schema file shipped in the repo, so an
  agent can send a model request through MCP. The self-call authenticates with a
  dedicated internal identity (`mcp-self`) that has its own budget and is never handed
  out. Grafana MCP and metrics targets are listed for later and not built here.
- **Its own hostname and edge rules (D1).** `agw_mcp_hostname` (placeholder
  `mcp.<zone>`) is a separate Caddy managed site proxying `/mcp` over the same upstream
  mutual-TLS transport the operator UI host uses, key-gated at the gateway, with a
  Cloudflare record, a challenge skip and one rate-limit rule for that hostname declared
  in `platform/infra/cloudflare`. The inference allowlist is unchanged.
- **One declared admin list (D3).** A single list in site-config drives both the Authentik
  group membership of platform admins and the gateway's personal identities. The Authentik
  admin entries are rendered from it; the gateway deploy derives one identity per entry and
  merges them with the service identities in `agw_clients`, refusing a duplicate within the
  list and reporting an overlap with `agw_clients` by name; a drift guard fails the Authentik
  and gateway deploys when `platform-admins` and the list disagree. Declaring
  an admin therefore mints their `client_<name>` on the next gateway deploy, an existing
  identity keeps its key, and handout stays `Back Up Credentials to site-config (Dev)`.
  The same identities are admitted on `/mcp`.
- **Secret-free client templates and an idempotent installer (D4).** Templates under
  `platform/services/agentgateway/context/clients/` for Claude Code and pi, and one `make`
  target that merges the agentgateway server entry into the user-level configuration of
  each harness without touching existing servers. The key is read at connect time from the
  handed-out site-config file or the macOS Keychain by command reference; no key and no
  live repo-root `.mcp.json` is ever committed.
- **A local spike first (D5).** The facts this design cannot read from a schema (an `llm`
  and an `mcp` block in one file at runtime, `apiKey.name` in an MCP authorization rule,
  the self-call through the mutual-TLS listener) are proven locally before any template or
  production change.

## Capabilities

### New Capabilities
- `platform/agentgateway-mcp`: the authenticated MCP endpoint, the declared-admin identity
  derivation, its own edge hostname, and the secret-free harness client configuration.

### Modified Capabilities
- None. `platform/inference-gateway` (companion change) is not yet in the main specs, so
  nothing is modified there; "the inference allowlist is unchanged" is an added requirement
  of this capability.

## Impact

- Files (proposed; each is confirmed or corrected by the spike, section 1):
  `platform/services/agentgateway/deployment/templates/config.yaml.j2` (shared key macro,
  `mcp:` block), `templates/env.j2` (the self-call key), a new OpenAPI schema file under
  `platform/services/agentgateway/deployment/`, `platform/playbooks/deploy-agentgateway.yml`
  (identity merge, MCP verify), `platform/playbooks/vars/secret-declarations/agentgateway.yml`
  (merged identity loop), `manage-agentgateway-client-key.yml` (merged list),
  `platform/playbooks/tasks/agw-probe.yml` (MCP probe), Authentik admin blueprint rendering
  and its `env.j2`/secret definitions, `Caddyfile.local.j2` (an MCP allowlist route shape),
  `platform/inventory/local-dev.yml.example`, `platform/infra/cloudflare/{dns,waf,ratelimit}.tf`,
  `platform/services/agentgateway/context/{architecture,agent-practices}.md` and
  `context/clients/`, a client installer script and a `Makefile` target,
  `platform/tests/` BATS for each.
- site-config (separate branch, via its own PR): the declared admin list, `agw_mcp_*`
  variables, the MCP Caddy managed site in `caddy_managed_sites`, and the removal of any
  `agw_clients` entry now derived from the admin list.
- OpenBao: `secret/services/agentgateway` gains `client_mcp-self` and one `client_<name>`
  per newly declared admin (existing fields are reused, never re-minted).
- Live: one Authentik redeploy (blueprint render, no user change expected), one gateway
  redeploy (identities, then the MCP block), one Caddy managed-site apply, one Cloudflare
  apply (record, skip rule, rate-limit rule), key handouts. Nothing is deployed by this
  change's authoring; production runs use the `(Dev)` templates with a dry run first.
- Out of scope, recorded: stdio targets (the gateway image has no shell,
  `platform/services/agentgateway/deployment/compose.yml:22-23`); Grafana and metrics MCP
  targets; exposing `/config_dump` or the admin API over MCP; an SSE endpoint for clients;
  per-person budgets on model requests made through MCP (the self-call is charged to
  `mcp-self`); the OIDC/JWT path for `/mcp`; the committed live repo-root `.mcp.json`.

## Rollback Plan

- Disable MCP: set the inventory flag `agw_mcp_enabled` to false and run the gateway
  deploy. The `mcp:` block is no longer rendered and `/mcp` answers as an unknown path;
  the deploy's verify asserts exactly that state when the flag is false.
- Remove the edge: delete the MCP managed site from site-config's `caddy_managed_sites`
  and run `Manage Caddy Sites (Dev)`; remove the record and the two rules from
  `platform/infra/cloudflare` and run `apply-cloudflare-tofu.yml` (plan first, zero
  unexpected diff). The inference hostname is untouched throughout, so no inference
  client is affected by any rollback step.
- Identities: the derived identities are ordinary `client_<name>` fields. Revoke one with
  `Manage agentgateway Client Key` after removing the name from the admin list; reverting
  the list to the previous per-person Authentik blocks is a single revert commit because
  the rendered Authentik entries are behaviour-identical to the fixed ones (task 2.6 proves
  it before the switch). Removing the derivation leaves the keys in OpenBao, still valid
  only while the name is in `agw_clients`.
- Clients: the installer's removal mode deletes exactly the agentgateway entry from each
  harness configuration and leaves every other server alone.
- State lost by a full rollback: nothing durable. No budget state is created by this
  change beyond the existing hourly windows.
