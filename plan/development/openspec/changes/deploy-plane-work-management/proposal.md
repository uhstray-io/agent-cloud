Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Why

Joe retired tududi on 2026-10-23. The team has no single place where GitHub issues, the per-repository plans and the work cards are linked together. tududi was the tracker, and its GitHub sync is built from n8n workflows and a dedicated GitHub App. Two bidirectional syncs running at once would fight over the same items, so the existing sync has to be switched off before the replacement is switched on.

Plane replaces tududi as the work tracker. It gives the team one place where a GitHub issue, a pull request, a repository's OpenSpec change and a work card are linked, and it lets harnesses and the Forge Discord bot work against the same data. The plan is recorded in `plan/development/16-plane-work-management.md`, and the decisions D1 to D9 in that plan are the basis for this change.

## What Changes

- **New platform service `plane`.** It is a composable Compose stack on its own VM in the `plane_svc` group, deployed, cleaned, backed up and restored through Semaphore playbooks. Its secrets come from OpenBao. Production runs the Commercial edition on the Pro plan, and local-dev runs the Commercial Free tier.
- **Single sign-on and roles.** Users sign in through the Authentik OIDC client. A reconcile playbook maps Authentik groups to Plane roles: `platform-admins` to Owner, `platform-developers` and `platform-business` to Member, and `platform-user` is denied.
- **Native GitHub integration.** A GitHub App named "Plane" carries issues, pull requests and the webhook. The trigger labels are `plane` for GitHub-to-Plane and `github` for Plane-to-GitHub. Pull requests that name a work item key link to it.
- **Projects as code.** A generator reads each repository's OpenSpec `tasks.md` files and writes Plane modules and work items. It never writes status, so Plane owns status. A scheduled sync pushes the generated state.
- **Plane MCP server for harnesses.** The MCP server runs in OAuth mode behind Caddy, so each edit is attributed to the signed-in user. weft uses a service identity, a personal access token held in OpenBao.
- **Forge Discord app.** A second composable service on the Plane VM posts notifications, answers slash commands and unfurls links. It uses the platform bot token, read from the shared store and never copied.
- **tududi teardown.** The teardown is the final phase. It runs behind an explicit confirmation value, after a final backup, and it removes the tududi containers, route, DNS record, identity client, GitHub App and inventory entries.
- **BREAKING for tududi.** tududi stops being the tracker. Open tasks migrate into Plane under the `migrated-from-tududi` label.
- **Documentation.** `AGENTS.md` loses the tududi lines and gains the Plane entries in Phase 7.

## Capabilities

### New Capabilities

- `platform/plane-work-management`: the Plane service as a platform capability. It covers single sign-on through the central identity provider, role reconciliation from groups, the GitHub integration, projects-as-code, the MCP server for harnesses and weft, secrets and backups, the edge and firewall, and the tududi retirement order.
- `platform/plane-discord-app`: the Forge Discord app as a platform capability. It covers the signature-verified webhook receiver, slash commands, link previews, the shared bot-token boundary, and the deployment of the app as its own composable service.

Each capability is grouped under the `platform/` domain, with a kebab-case leaf name, following the convention the Postiz change set.

### Modified Capabilities

None. The existing specs in this store are `platform/inference-edge` and `platform/n8n-automation`. Neither one mentions tududi or GitHub, so this change does not alter their requirements.

## Impact

- **New files.** A service directory `platform/services/plane/` with Compose files, `deploy.sh`, templates, the projects declaration, the generator and its test, and the Compose schema. A second service directory `platform/services/plane-discord/`. Playbooks for deploy, clean-deploy, backup, restore, seeding, member reconciliation, projects sync, tududi migration and the Discord app deploy. A BATS suite `platform/tests/test_service_plane.bats`. A secret-free MCP client script `scripts/plane-mcp-client.py`. An Authentik client blueprint.
- **Modified files.** The Authentik deploy playbook, to inject the Plane client secret. The Cloudflare OpenTofu root, `platform/infra/cloudflare/dns.tf` and `platform/infra/cloudflare/waf.tf`, to add the two hostnames and a challenge skip for the GitHub webhook path only. `platform/semaphore/templates.yml`. The OPA catalog in `platform/services/opa/deployment/policies/agentcloud/data.json`. The inventory placeholder files. `AGENTS.md`. The tududi sync templates and workflows, removed in Phase 7.
- **Secret store.** A new path `secret/services/plane`, holding generated values, a seeded license key, the GitHub App fields and the service personal access token. The shared read of `secret/services/discord:bot_token`. In Phase 7, the tududi sync key fields are deleted from `secret/services/github`.
- **New dependencies, approved by Joseph A. Wisneski IV on 2026-10-23.** The `plane-compose` CLI for the controller (Phase 4), and `discord.py` and `plane-sdk` for the Forge app image (Phase 6). Each version is pinned at implementation time.
- **Out of repository.** A companion pull request in the weft repository adds the MCP entry for weft's builder. Each UI-only step, including the GitHub App and OAuth app registrations, is resolved in Phase 0 by a code path or an explicit FORCED playbook, and never left as a runbook click.
- **Not affected.** The n8n stack keeps running. Its tududi-sync workflows are removed only in Phase 7, through the provisioning playbook.

## Rollback Plan

Rollback is staged to match the deployment, and it is lossless only while no real data exists.

1. **Before production data exists.** Remove the Plane site block from the managed Caddy sites and re-run the route playbook. Removing the route touches only this hostname. Then run the Plane clean-deploy playbook. That playbook is destructive, but at this point the only data it removes is test data.
2. **After data exists.** Take a VM snapshot with `snapshot-vm.yml` and a database backup with `backup-plane-db.yml` before any step that could remove data. Restore with `restore-plane-db.yml`, which is modelled on the n8n restore playbook. That playbook requires an explicit dump file. Rolling back after data exists is a restore or a teardown, not a revert.
3. **Identity provider client.** Delete the Plane blueprint and re-run `deploy-authentik.yml`. Other clients are not affected, because each client is its own blueprint.
4. **Discord app.** Stop and remove the Forge app service. The platform bot token is only read, never written, so it is not affected.
5. **MCP and harnesses.** Remove the Plane MCP route. The harness entries contain no secrets, so leaving them behind exposes nothing, and the removal method is decided with the client script in Phase 5.
6. **GitHub sync.** Uninstall the Plane GitHub App installation from the repositories to stop the sync.
7. **tududi teardown (Phase 7) is one-way.** Once the tududi VM is destroyed, rollback means restoring tududi from the final backup taken to site-config. The approved plan does not record a restore procedure for tududi, so this is an open item recorded in `design.md`.

Phase 3 disables tududi's sync before the Plane sync is enabled. Undoing that step is a deliberate action recorded in the tasks, not a free revert.
