Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Why

The team has no single place where GitHub issues, the per-repository plans and the work cards are linked together.

Plane is the work tracker. It gives the team one place where a GitHub issue, a pull request, a repository's OpenSpec change and a work card are linked, and it lets harnesses and the Forge Discord bot work against the same data. The plan is recorded in `plan/development/16-plane-work-management.md`, and the decisions D1 to D9 in that plan are the basis for this change.

## What Changes

- **New platform service `plane`.** It is a composable Compose stack on its own VM in the `plane_svc` group, deployed, cleaned, backed up and restored through Semaphore playbooks. Its secrets come from OpenBao. Production runs the Commercial edition on the Pro plan, and local-dev runs the Commercial Free tier.
- **Single sign-on and roles.** Users sign in through the Authentik OIDC client. A reconcile playbook maps Authentik groups to Plane roles: `platform-admins` to Admin (the top role; Plane has no Owner role), `platform-developers` and `platform-business` to Member, and `platform-user` is denied.
- **Native GitHub integration.** A GitHub App named "Plane" carries issues, pull requests and the webhook. The trigger labels are `plane` for GitHub-to-Plane and `github` for Plane-to-GitHub. Pull requests that name a work item key link to it.
- **Projects as code.** A generator reads each repository's OpenSpec `tasks.md` files and writes Plane modules and work items. It never writes status, so Plane owns status. Plane Compose configures each project's schema, and a scheduled sync creates the modules and work items through the Plane API, create-only and keyed on an external identifier, so nothing set in Plane is overwritten.
- **Plane MCP server for harnesses.** The MCP server runs in OAuth mode behind Caddy, so each edit is attributed to the signed-in user. weft uses a service identity, a personal access token held in OpenBao.
- **Forge Discord app.** A second composable service on the Plane VM posts notifications, answers slash commands and unfurls links. It uses the platform bot token, read from the shared store and never copied.
- **Documentation.** `AGENTS.md` gains the Plane entries.

## Capabilities

### New Capabilities

- `platform/plane-work-management`: the Plane service as a platform capability. It covers single sign-on through the central identity provider, role reconciliation from groups, the GitHub integration, projects-as-code, the MCP server for harnesses and weft, secrets and backups, the edge and firewall.
- `platform/plane-discord-app`: the Forge Discord app as a platform capability. It covers the signature-verified webhook receiver, slash commands, link previews, the shared bot-token boundary, and the deployment of the app as its own composable service.

Each capability is grouped under the `platform/` domain, with a kebab-case leaf name, following the convention the Postiz change set.

### Modified Capabilities

None. The existing spec in this store is `platform/inference-edge`. It does not mention GitHub or Plane, so this change does not alter its requirements.

## Impact

- **New files.** A service directory `platform/services/plane/` with Compose files, `deploy.sh`, templates, the projects declaration, the generator and its test, and the Compose schema. A second service directory `platform/services/plane-discord/`. Playbooks for deploy, clean-deploy, backup, restore, seeding, member reconciliation, projects sync and the Discord app deploy. A BATS suite `platform/tests/test_service_plane.bats`. A secret-free MCP client script `scripts/plane-mcp-client.py`. An Authentik client blueprint.
- **Modified files.** The Authentik deploy playbook, to inject the Plane client secret. The Cloudflare OpenTofu root, `platform/infra/cloudflare/dns.tf` and `platform/infra/cloudflare/waf.tf`, to add the two hostnames and, only if spike 0.16 passes, a challenge skip for the GitHub webhook path only. `platform/semaphore/templates.yml`. The OPA catalog in `platform/services/opa/deployment/policies/agentcloud/data.json`. The inventory placeholder files. `AGENTS.md`.
- **Secret store.** A new path `secret/services/plane`, holding generated values, a seeded license key, the GitHub App fields and the service personal access token. The shared read of `secret/services/discord:bot_token`.
- **New dependencies, approved by Joseph A. Wisneski IV on 2026-10-23.** The `plane-compose` CLI for the controller (Phase 4), and `discord.py` and `plane-sdk` for the Forge app image (Phase 6). Each version is pinned at implementation time.
- **Out of repository.** A companion pull request in the weft repository adds the MCP entry for weft's builder. Each UI-only step, including the GitHub App and OAuth app registrations, is resolved in Phase 0 by a code path or an explicit FORCED playbook, and never left as a runbook click.

## Rollback Plan

Rollback is staged to match the deployment, and it is lossless only while no real data exists.

1. **Before production data exists.** Remove the Plane site block from the managed Caddy sites and re-run the route playbook. Removing the route touches only this hostname. Then run the Plane clean-deploy playbook. That playbook is destructive, but at this point the only data it removes is test data.
2. **After data exists.** Take a VM snapshot with `snapshot-vm.yml` and a database backup with `backup-plane-db.yml` before any step that could remove data. Restore with `restore-plane-db.yml`. That playbook requires an explicit dump file. Rolling back after data exists is a restore or a teardown, not a revert.
3. **Identity provider client.** Delete the Plane blueprint and re-run `deploy-authentik.yml`. Other clients are not affected, because each client is its own blueprint.
4. **Discord app.** Stop and remove the Forge app service. The platform bot token is only read, never written, so it is not affected.
5. **MCP and harnesses.** Remove the Plane MCP route. The harness entries contain no secrets, so leaving them behind exposes nothing, and the removal method is decided with the client script in Phase 5.
6. **GitHub sync.** Uninstall the Plane GitHub App installation from the repositories to stop the sync.
