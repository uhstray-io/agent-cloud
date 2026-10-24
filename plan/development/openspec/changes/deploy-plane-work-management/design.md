Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Context

tududi was the team's work tracker. Joe retired it on 2026-10-23, and Plane replaces it as an agent-cloud platform service. Plane has to connect four things that today are not linked: GitHub issues and pull requests, each repository's OpenSpec changes, the work cards the team uses, and the harnesses and bots that act on them.

The approved plan is `plan/development/16-plane-work-management.md`. It fixes nine decisions, D1 to D9, and eight verify-first spikes. Three further checks, spikes 0.14 to 0.16, were added on 2026-10-24 for the migration identity, the webhook digest encoding and delivery identity, and the GitHub webhook signature. This design records the decisions with their rejected alternatives, the risks, the Phase 0 findings as they arrive, and the facts that are still unverified.

The composable-service shape comes from the Postiz change set. Postiz is the closest multi-container service on the platform: `platform/playbooks/deploy-postiz.yml` and the `platform/services/postiz/` directory are the models for Plane's deploy playbook and service layout.

**Base ref.** All work starts from `origin/dev` at commit `391a4f7c`. The change itself is authored in a worktree cut from that commit.

The existing tududi-to-GitHub sync is built from n8n workflows and a dedicated GitHub App. Its inventory of repository pairs is `platform/services/tududi/sync/github-mapping.yml`, which declares nine pairs. The sync is bidirectional. A Plane sync that runs at the same time would fight it on the same items, so the order of the phases matters, and it is recorded below.

## Goals and Non-Goals

**Goals**

- One tracker links GitHub issues and pull requests, each repository's OpenSpec changes, and the work cards.
- Human access goes through Authentik, and Plane roles follow Authentik groups.
- Each edit made by a harness is attributed to the real user who signed in.
- Deploy, backup and restore run as Semaphore playbooks, so the next operator can reproduce them.

**Non-Goals**

- Plane does not become the source of intent. The repository stays the source, and Plane holds status.
- Discord threads are not synchronised with Plane items.
- The Community edition is not used.
- agentgateway does not carry the MCP traffic.

## Decisions

### D1. Commercial edition on the Pro plan in production

**Chosen.** Run the Commercial edition on the Pro plan on the production VM.

**Rejected: Community edition.** The approved plan cites three reasons from the Plane documentation. OIDC is listed under `Plan: Pro, Business` in the developer docs at `self-hosting/govern/oidc-sso.mdx`. The GitHub integration is a Pro feature. OAuth app registration, which Plane Apps and MCP OAuth need, is not part of Community (`mcp-server-self-host`).

**Evidence status.** The approved plan cites a price of $6 per user per month on the annual plan, from `plane.so/pricing`. Neither the tier listing nor the price was fetched again while this change was written. Both are listed under Unverified Going In.

### D2. Local-dev runs the Commercial Free tier

**Chosen.** Local-dev runs the Free tier. Local validation proves the deploy, Caddy, backup and restore, Compose, MCP in personal-access-token mode, and the Discord app. The OIDC and GitHub-sync gates move to production.

**Rejected: a second Pro key for local-dev.** The approved plan states the binding rule: a license key binds to one workspace and one machine. It treats the Free tier as enough for the local proof.

**Rejected: validating OIDC and GitHub sync locally.** Both depend on the Pro plan, so they are proven in production. The plan follows the Postiz precedent, where the connect leg was proven in production. That precedent is recorded in Hindsight and was not re-checked for this change.

### D3. Role mapping from Authentik groups

**Chosen.** `platform-admins` maps to Owner. `platform-developers` and `platform-business` map to Member. `platform-user` is denied. A reconcile playbook applies the mapping, because the Plane OIDC documentation, as cited in the plan, describes no group-to-role mapping. Sequencing follows from that. Self sign-up is turned off first, so that only invited members can join. The reconcile playbook then invites and sets roles, and only after its apply run succeeds are the OIDC settings applied and the reconcile schedule registered (tasks 2.3 to 2.7).

**Rejected: `platform-business` as Guest.** The approved plan rejects Guest for business users. It records no separate rationale, and it does not describe what a Guest can do in Plane. This change therefore does not compare the two roles beyond the decision itself.

**Fallback.** Phase 0 spike 4 confirms whether a member-role API exists. If it does not, D3 becomes: invite each member as Member, and promote Owners through a management command. The approved plan records this fallback.

### D4. OpenSpec changes become modules, and tasks become work items

**Chosen.** Each repository's `changes/*/tasks.md` is read by a generator. The generator writes `work/modules.yaml` and `work/workitems.yaml`. Each work item links back to its plan file. The generator omits `state`, so Plane owns status.

**Rejected: Plane as the source of intent.** The approved plan states that the repository stays the source of intent and Plane holds status.

### D5. Plane MCP server in OAuth mode, behind Caddy

**Chosen.** The `makeplane/plane-mcp-server` container runs in OAuth mode. Its port 8211 sits behind Caddy at `plane-mcp.<zone>`. OAuth attributes each edit to the signed-in user.

**Rejected: the agentgateway MCP.** The gateway's template at `platform/services/agentgateway/deployment/templates/config.yaml.j2:402-440` shows three limits that the plan relies on.

- Targets come only from `agw_mcp_openapi_targets`, so the gateway federates OpenAPI targets only.
- The gateway admits only `_agw_admin_identities`, which are admin-derived personal identities.
- `backendAuth` sets the caller's authorization to the gateway's own `mcp-self` key by environment reference, so the gateway replaces the caller's credential.

**Open reading.** D5 says "on its own host". Phase 5 adds the MCP container to the Plane Compose stack on the Plane VM, and gives it its own public hostname. **Resolved 2026-10-23:** "own host" means its own public hostname; the question Joe answered read "at its own hostname behind Caddy". The container runs in the Plane stack on the Plane VM.

### D6. weft uses a service identity

**Chosen.** A Plane bot user named `weft` holds a personal access token. The token is stored at `secret/services/plane:weft_mcp_pat`. weft's builder Claude Code gets an MCP entry for `/http/api-key/mcp` with the `x-workspace-slug` header, and the token is injected from weft's sealed store.

**Rejected: browser OAuth for weft.** The approved plan states that weft's builder is a headless container and cannot complete a browser OAuth flow.

### D7. Forge is the platform bot

**Chosen.** The Plane Discord app runs under the platform bot token, `secret/services/discord:bot_token`, which is shared-read and never copied. The app connects through Discord's gateway websocket. That connection is outbound only, so it adds no public route. Features are notifications, the `/plane create|assign|status` slash commands, and link previews. Slash commands are received over the Gateway and answered through Discord's HTTP interaction callback, never through the Gateway. The mutating commands `/plane create` and `/plane assign` are limited to members who hold a Discord role declared in site-config and who are mapped to a Plane member. The link previews need the privileged `MESSAGE_CONTENT` intent. The requirements are in `specs/platform/plane-discord-app/spec.md`.

**Rejected: Discord thread synchronisation.** The approved plan does not include it, and Joe did not want it.

**Recorded cost.** The app becomes one more long-lived holder of the platform bot token. Phase 6 records that blast radius in the service's `CLAUDE.md`.

### D8. tududi teardown is the final phase

**Chosen.** The teardown is Phase 7, the last phase of this plan. Its order follows the sync traps: the kill switch and refresher come first, then the Plane sync, then the GitHub App, then the teardown.

**Rejected alternatives.** The approved plan records none. The order is the rationale.

### D9. Hostnames and own VM

**Chosen.** Production uses `plane.uhstray.io` and `plane-mcp.uhstray.io`. Local-dev uses `plane.agent-cloud.test` and `plane-mcp.agent-cloud.test`. Plane runs on its own VM in the `plane_svc` group.

**Rejected alternatives.** The approved plan records none. It calls the hostnames a placeholder that Joe can change. The own-VM rule follows `plan/architecture/02-service-onboarding.md:38-41`, where the tier table places the full-integration services on a dedicated VM.

## Phase 0 Findings

Each spike writes a dated finding into this table. No answer carries forward until it is recorded here. Every row is open as of 2026-10-23, and rows 0.14 to 0.16 were added on 2026-10-24.

| Spike | Informs | Finding (dated) |
|---|---|---|
| 0.1 Compose licensing | Phase 1 layout | Open. Default, from the plan: do not commit the Compose zip to the public repository. Pin `plane_version` and the zip sha256 in inventory, and fetch and verify at deploy time. Fallback: vendor the zip in site-config. |
| 0.2 Commercial service list and env | Phase 1 | Open. The plan expects extra services beyond the 13 in the community Compose file, named as `silo` and others. Unverified. |
| 0.3 to 0.8 UI-only configuration (spike 3) | Phases 2, 3 and 5 | Open. Six items: workspace creation, license activation, OIDC settings, GitHub integration connect, webhook creation, and OAuth app registration for the Discord app and MCP. Each one needs an API, a management command or an instance-config variable, or it is classified FORCED. |
| 0.9 Member-role API (spike 4) | Phase 2 | Open. Confirms or triggers the D3 fallback. |
| 0.10 Plane Compose idempotency (spike 5) | Phase 4 | Open. Three questions: whether `plane push` duplicates items when `.plane/state.json` is lost, whether `plane pull` rebuilds that state, and whether a changed item overwrites a state set in Plane. The answers decide where state persists. |
| 0.11 OpenTelemetry on Compose (spike 6) | Phase 1 telemetry | Open. The documentation shows Helm only. Fallback: logs only, through the existing Alloy docker discovery. |
| 0.12 Hardware sizing (spike 7) | Phase 1 | Open. The entry goes into the private `site-config/proxmox/vm-specs.yml`. |
| 0.13 Self-host OAuth endpoints (spike 8) | Phases 5 and 6 | Open. The documentation shows `api.plane.so`. The authorize, token and installation URLs on the platform's own domain must be confirmed. |
| 0.14 External identifier support (added 2026-10-24) | Phase 7 migration identity (task 7.1) | Open. Whether Plane's work-item API offers an external identifier field that can hold a tududi task id. If it does, the migration uses it. If not, the migration uses a work-item link to the tududi task URL. |
| 0.15 Webhook digest encoding and delivery id (added 2026-10-24) | Phase 6 webhook receiver (tasks 6.3 and 6.5) | Open. Two findings from one signed local delivery: the encoding of the `X-Plane-Signature` digest (hex or base64), and whether each delivery carries an `X-Plane-Delivery` header. |
| 0.16 GitHub webhook signature (added 2026-10-24) | Phase 1 edge boundary (task 1.11) and risk 7 | Open. Whether Plane verifies `X-Hub-Signature-256` against the configured webhook secret, shown by a forged unsigned local delivery being rejected. If this does not pass, no challenge skip is added, and an authenticated boundary is recorded in its place before any Phase 1 edge task. |

## Risks and Trade-offs

1. **Licence redistributability.** The Compose zip may not be redistributable. Until spike 0.1 answers that, the zip stays out of the public repository, and its version and checksum are pinned in inventory.
2. **Machine-bound licence.** A license key binds to one workspace and one machine (D2). Moving the production stack to another host would need a licence decision. The approved plan records no such decision, so this stays open.
3. **UI-only configuration.** Any step that exists only in the Plane UI is classified FORCED under `plan/architecture/01-automation-model.md`, section 5. FORCED means imperative by a real constraint, to be encoded as an idempotent post-deploy playbook. It is never left as a runbook click. Each of the six UI-only items in spike 0.3 to 0.8 is classified this way if no API exists.
4. **Compose state.** The sync persists `state.json` somewhere, and the choice depends on spike 0.10. Until that answer is recorded, the generator omits `state`, so Plane keeps ownership of status. A lost state file could duplicate items, and that is what spike 0.10 tests.
5. **Sync ordering.** The tududi sync and the Plane sync are both bidirectional on `dev-test`. Phase 3 switches the tududi sync off first with `provision-tududi-github-sync.yml -e sync_enabled=false`. Then it removes the 45-minute refresher schedule from both refresher templates: the production template at `platform/semaphore/templates.yml:691-692`, and the local template at `platform/semaphore/templates-local.yml:69-70`. Both are `cron: "*/45 * * * *"`. Both templates stay until Phase 7. The sync GitHub App is uninstalled only in Phase 7, after the refresher is gone, because the approved plan records that the refresher's mint task refuses on any scope drift.
6. **Bot-token blast radius.** The Forge app is one more holder of the platform bot token (D7). Phase 6 records that in the service's `CLAUDE.md`.
7. **Public webhook path.** Phase 1 may add a Cloudflare challenge skip for `/silo/api/github/github-webhook` only, shaped like the `inference-api-bypass-challenge` rule at `platform/infra/cloudflare/waf.tf:124`. The skip is added only if spike 0.16 shows that Plane verifies the GitHub webhook signature. The approved plan does not record that verification, so the spike decides it. If the spike does not pass, no skip is added, and an authenticated boundary is recorded in the design before any Phase 1 edge task. Examples are Caddy verifying the signature, or an allowlist of GitHub hook sources taken from GitHub's meta API. That decision is never deferred to Phase 3.
8. **Tier gating.** Webhooks and personal access tokens may need a tier above Free. If they do, local validation of Phases 5 and 6 is limited. The approved plan lists this as unverified.
9. **Step-result emit conflict.** Phase 1 has the deploy playbook emit a step result. `platform/playbooks/deploy-authentik.yml` is the only `deploy-*.yml` that includes `tasks/emit-step-result.yml`, and it records only its `oidc-config` step. The entry for the emit task in `AGENTS.md` says that per-service deploy playbooks do not include it, and that the `service-deploy` step records none. **Resolved 2026-10-23:** the collector records a synthetic `fail` for a failed deploy task that emitted nothing and records nothing for a successful one (`platform/workflows/service-onboarding/lib/step_results.py:239-249`). Every per-service deploy follows that convention, so the Plane deploy does too. A missing `pass` for `service-deploy` is a platform-wide gap; it is fixed once in the shared mechanism by a separate change, never special-cased here.
10. **tududi restore path.** The final backup goes to site-config, but the approved plan does not record a restore procedure for tududi. Phase 7 is one-way until one exists.

## Open Items

- Spike 0.16 decides whether the Cloudflare challenge skip for the GitHub webhook path is added (risk 7).
- A restore procedure for tududi is written or explicitly accepted as absent before Phase 7 runs.

## Migration Plan

The phases run in order: 0, then 1 to 7. Phase 0 blocks each phase that names one of its spikes. Phase 3 switches the tududi sync off before Phase 4 switches on the projects sync, because of the sync ordering in risk 5. Phase 7 runs last and only after the gates before it pass. The task list in `tasks.md` carries the detail for each phase.

## Unverified Going In

Each item below is unverified as of 2026-10-23. Each one is owned by a Phase 0 spike or by a named later task.

- Commercial Compose contents and redistributability (spike 0.1 and 0.2).
- Commercial image names.
- Non-UI configuration paths for the six UI-only items (spike 0.3 to 0.8).
- The member-role API (spike 0.9).
- Compose state semantics (spike 0.10).
- OpenTelemetry on Compose (spike 0.11).
- The self-host OAuth URLs (spike 0.13).
- Hardware sizing (spike 0.12).
- Whether webhooks and personal access tokens need a tier above Free (risk 8).
- The Commercial tier gating and the $6 per user per month price cited for D1. The approved plan cites them. Neither was fetched again for this change.
- The `plane-sdk` and `discord.py` versions. Both packages were approved by Joseph A. Wisneski IV on 2026-10-23, and each version is pinned at implementation time. Neither package was checked against a package index while this change was written.
- The version of `makeplane/plane-mcp-server`. No version has been chosen.
- The `prime.plane.so` setup URL pattern, `prime.plane.so/api/v2/setup/?version=…&platform=amd64`, cited in the plan. It was not fetched.
- Whether the GitHub webhook verifies its own signature (spike 0.16, risk 7).
- The external identifier field in Plane's work-item API (spike 0.14).
- The Plane webhook digest encoding and the `X-Plane-Delivery` header (spike 0.15).
- The Discord rules this change relies on: the 3-second window for answering an interaction, the HTTP interaction callback, the privileged `MESSAGE_CONTENT` intent, and close code 4014 for an intent that is not enabled. They are cited from `https://docs.discord.com/developers/interactions/receiving-and-responding` and `https://docs.discord.com/developers/events/gateway`. Both pages were fetched on 2026-10-24 and state these rules; the live bot behaviour is still proven in Phase 6.
- A restore procedure for tududi (risk 10).
- The Forge image workflow. The approved plan says it is built to GHCR by a workflow like `.github/workflows/build-honcho.yml`. That file exists, but its contents were not reviewed for this change.
