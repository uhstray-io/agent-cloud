Author: Joseph A. Wisneski IV <stray@uhstray.io>

## 0. Verify-first spikes

Each spike writes a dated finding into the Phase 0 table in `design.md`. No answer carries forward until it is recorded there. Every task in this phase is a read-only check, or a check that writes only to the change's own files.

- [ ] 0.1 Compose licensing (spike 1). Fetch the version-pinned zip from `prime.plane.so/api/v2/setup/?version=…&platform=amd64`. Read its LICENSE or EULA and the plane.so terms. Record the dated finding in `design.md`. Until the terms say the zip is redistributable, do not commit it to the public repository, and pin `plane_version` and the zip sha256 in inventory.
- [ ] 0.2 Commercial service list and environment (spike 2). Diff the zip's Compose file against the community Compose file, which the plan cites as 13 services. Record the extra services (`silo` and others), their images, ports and environment names, and whether the built-in proxy can run on an internal port behind Caddy. Record the dated finding in `design.md`.
- [ ] 0.3 UI-only configuration: workspace creation (spike 3). Find an API, a Django management command or an instance-config variable for this step. If none exists, classify the step FORCED under `plan/architecture/01-automation-model.md` and encode it as an idempotent post-deploy playbook. Record the dated finding in `design.md`.
- [ ] 0.4 UI-only configuration: license activation (spike 3). Same procedure as 0.3. Record the dated finding in `design.md`.
- [ ] 0.5 UI-only configuration: OIDC settings, the God Mode page `/god-mode/authentication/oidc` (spike 3). Same procedure as 0.3. Record the dated finding in `design.md`.
- [ ] 0.6 UI-only configuration: GitHub integration connect (spike 3). Same procedure as 0.3. Record the dated finding in `design.md`.
- [ ] 0.7 UI-only configuration: webhook creation (spike 3). Same procedure as 0.3. Record the dated finding in `design.md`.
- [ ] 0.8 UI-only configuration: OAuth app registration for the Discord app and for MCP (spike 3). Same procedure as 0.3. Record the dated finding in `design.md`.
- [ ] 0.9 Member-role API (spike 4). Confirm whether an endpoint exists that invites a workspace member and updates that member's role. If none exists, record in `design.md` that decision D3 takes its fallback: invite as Member, and promote Owners through a management command.
- [ ] 0.10 Plane Compose idempotency (spike 5). Answer three questions and record each answer in `design.md`. Does `plane push` duplicate items if `.plane/state.json` is lost? Does `plane pull` rebuild that state? Does a changed item overwrite a `state` set in Plane? The answers decide where state persists in Phase 4.
- [ ] 0.11 OpenTelemetry on Compose (spike 6). Test whether the `OTEL_*` environment variables work in the Commercial Compose images. The documentation shows Helm only. Fallback if the test fails: logs only, through the existing Alloy docker discovery. Record the dated finding in `design.md`.
- [ ] 0.12 Hardware sizing (spike 7). Derive the VM size from the self-hosting guide. Record the entry in the private `site-config/proxmox/vm-specs.yml` file, and record the dated finding in `design.md`.
- [ ] 0.13 Self-host OAuth endpoints (spike 8). Confirm the authorize, token and installation URLs on the platform's own domain. The documentation shows `api.plane.so`. Record the dated finding in `design.md`.
- [ ] 0.14 Validation gate: scenario "Spike findings are recorded before dependent phases start" holds. Every row of the Phase 0 table in `design.md` carries a dated finding, and none is still marked open.

## 1. Plane service

The service follows the Postiz change as its model: `platform/playbooks/deploy-postiz.yml` and the `platform/services/postiz/` directory. Every deploy step is idempotent and re-runnable.

- [ ] 1.1 Write the service files under `platform/services/plane/deployment/`: `compose.yml` (the source is set by 0.1), `compose.local.yml` (step-ca trust and the `local-dev` network), `deploy.sh` (container lifecycle only), `templates/env.j2`, `templates/plane.env.j2`, `CLAUDE.md` and `README.md`.
- [ ] 1.2 Apply the Compose rules. Every container uses `restart: always`. The one-shot `migrator` container carries the label `agent-cloud.one-shot: "true"`, with the precedent at `platform/services/erpnext/deployment/compose.yml:81`. Only the proxy publishes a port.
- [ ] 1.3 Write `platform/playbooks/deploy-plane.yml` as preflight, secrets, `deploy.sh`, then verify. The verify step probes the API path rather than `/`. Like every other per-service deploy it does not include `tasks/emit-step-result.yml` (resolved 2026-10-23, `design.md` risk 9).
- [ ] 1.4 Store the secrets at `secret/services/plane`. Generate once, through `manage-secrets`: `secret_key`, `live_server_secret_key`, and the Postgres, RabbitMQ and MinIO passwords. Seed `license_key` through `seed-openbao-key.yml` in the isolated seed environment.
- [ ] 1.5 Write `platform/playbooks/clean-deploy-plane.yml` on top of `tasks/clean-service.yml`.
- [ ] 1.6 Write `platform/playbooks/backup-plane-db.yml` and `platform/playbooks/restore-plane-db.yml`, modelled on `backup-n8n-db.yml` and `restore-n8n-db.yml`.
- [ ] 1.7 Cover the uploads volume with `snapshot-vm.yml` before any upgrade. This stays in force until the `pbs-vm-and-restore-gate` change lands.
- [ ] 1.8 Add the Semaphore templates to `platform/semaphore/templates.yml`: Deploy, Clean Deploy, Back Up and Restore, with a `dev_variant` for each. Publish them through `setup-templates.yml`.
- [ ] 1.9 Add the exact-string entry to `service_deploy_templates` in `platform/services/opa/deployment/policies/agentcloud/data.json`.
- [ ] 1.10 Add a `validate-all.yml` play for Plane, the `plane` group in `local-dev.yml.example`, and the static local Semaphore inventory in `platform/playbooks/bootstrap-local-dev.yml`.
- [ ] 1.11 Add the edge entries. Locally, add a `caddy_routes` entry. In production, add the `caddy_managed_sites` entry in site-config. Add `plane` and `plane-mcp` to `platform_subdomains` in `platform/infra/cloudflare/dns.tf`. Add a challenge skip to `platform/infra/cloudflare/waf.tf` for `/silo/api/github/github-webhook` only, shaped like the `inference-api-bypass-challenge` rule.
- [ ] 1.12 Apply the host firewall. Deny inbound by default, and allow inbound only from the reverse proxy. Declare the egress to `prime.plane.so:443`, the GitHub API, and the Discord gateway.
- [ ] 1.13 Set up telemetry. Standard output reaches Alloy automatically. Run `verify-o11y-service.yml`. Send OTLP to the o11y VM on ports 4317 and 4318 only if spike 0.11 passes.
- [ ] 1.14 Write `platform/tests/test_service_plane.bats`, modelled on `platform/tests/test_service_postiz.bats`. It covers `no_log` scoped to credential tasks, generate-once secrets, the OIDC shared read, the restart policy, and a health path that is not `/`.
- [ ] 1.15 Run the local deploy through local Semaphore, and check that the workflow collector shows the `service-deploy` result as pass. Then run `backup-plane-db.yml`, then `restore-plane-db.yml`, and confirm the data is intact after the restore.
- [ ] 1.16 Validation gate: scenarios "Local deployment is reachable over TLS" and "Backup restores with data intact" hold. `https://plane.agent-cloud.test` loads, and the restored data matches the backup taken before the restore.

## 2. SSO and roles

- [ ] 2.1 Write `platform/services/authentik/deployment/blueprints/plane-oidc.yaml`, modelled on `postiz-oidc.yaml`. The redirect is strict, at `https://<host>/auth/oidc/callback/`. The scopes are openid, email and profile.
- [ ] 2.2 Add the `plane` entry to `app-catalog.yml` at tier member. Add `plane_oidc_client_secret` to `_secret_definitions` in `deploy-authentik.yml`. Plane reads that secret through `_shared_reads`.
- [ ] 2.3 Apply the OIDC settings through the code path that 0.5 identified, inside `deploy-plane.yml`. The step is idempotent.
- [ ] 2.4 Write `platform/playbooks/reconcile-plane-members.yml`. It reads Authentik group members through the API, using a token from OpenBao. It invites members and sets roles under D3, using the member-role API from 0.9 or its fallback. It removes Plane members who are no longer in a member group. It is read-only unless `-e apply=true` is passed.
- [ ] 2.5 Register `reconcile-plane-members.yml` on a Semaphore schedule, after its check-mode run is verified.
- [ ] 2.6 Production sign-in check. Sign in once as a user from each group. After `reconcile-plane-members.yml` has run with `apply=true`, confirm the roles match D3. Scenarios "Administrator receives Owner", "Developer and business user receive Member", "User group is denied", and "Reconcile is read-only without apply" are checked as part of this step.
- [ ] 2.7 Validation gate: scenario "Member signs in through Authentik" holds. A signed-in member reaches Plane, and the session is established through the redirect registered in 2.1.

## 3. GitHub integration

The tududi sync and the Plane sync are both bidirectional. The tududi sync is switched off before the Plane sync is enabled. Do not run the two at the same time on `dev-test`.

- [ ] 3.1 Switch the tududi sync off first. Run `provision-tududi-github-sync.yml -e sync_enabled=false`. The kill switch takes effect before any credential or mapping is read.
- [ ] 3.2 Remove the 45-minute refresh schedule. Delete the `schedule:` block of the "Refresh tududi-sync GitHub Token" template at `platform/semaphore/templates.yml:691`, which is `cron: "*/45 * * * *"`. The template itself stays until Phase 7.
- [ ] 3.3 Create the GitHub App "Plane" from a committed GitHub App manifest, so the setup is reproducible. Repository permissions: Issues read and write, Pull requests read and write, Metadata read. Account permissions: Email read, Profile read and write. Webhook: `/silo/api/github/github-webhook`.
- [ ] 3.4 Store `GITHUB_APP_ID`, `GITHUB_APP_NAME` (the slug), `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` and `GITHUB_PRIVATE_KEY` (base64) at `secret/services/plane`. Scope `no_log` to the credential tasks only.
- [ ] 3.5 Connect each project's repository through the code path that 0.6 identified. The trigger label `plane` moves an issue from GitHub to Plane. The label `github` moves it from Plane to GitHub. A pull request that names a work item key, as `[KEY-123]`, links to that work item.
- [ ] 3.6 Production check on `dev-test`. Apply a `plane`-labelled issue and confirm it appears in Plane. Edit the card and confirm the edit flows back to GitHub. Open a pull request with `[KEY-n]` and confirm it links to its work item.
- [ ] 3.7 Validation gate: scenario "Labelled issue appears in Plane" holds, and so do "Edit flows back to GitHub" and "Pull request key links to its work item".

## 4. Projects as code

- [ ] 4.1 Write `platform/services/plane/projects.yml`. It maps each repository to its Plane project key and name, and it is seeded from the nine pairs in `platform/services/tududi/sync/github-mapping.yml`. Only public repository names go in this public repository. Private names go in site-config.
- [ ] 4.2 Write the generator `platform/services/plane/lib/openspec_to_compose.py` using the standard library only. It reads each repository's `changes/*/tasks.md` and writes `work/modules.yaml` and `work/workitems.yaml`. Each work item links to its plan file on GitHub. The generator omits `state`. Add a `test_*.py` file beside it.
- [ ] 4.3 Write the shared schema under `platform/services/plane/compose-schema/`. It defines the states, labels and types.
- [ ] 4.4 Write `sync-plane-projects.yml` on a Semaphore schedule. It mints a read-only installation token on the controller for a dedicated "plane-compose" GitHub App with Contents read, reusing `platform/lib/github_app_token.py`. It clones each declared repository and runs the generator. It runs `plane push --force`, and it runs `plane push --dry-run` under `--check`. Where `state.json` persists follows the answer to 0.10.
- [ ] 4.5 Pin the `plane-compose` CLI in `platform/requirements-controller.txt`. Joseph A. Wisneski IV approved this new controller dependency on 2026-10-23. The version is pinned at implementation time.
- [ ] 4.6 Verify on `dev-test`. Run `sync-plane-projects`, and confirm it creates modules and work items. Run it again, and confirm the second run makes zero changes. Set a state in the Plane UI, re-push, and confirm the state survives.
- [ ] 4.7 Validation gate: scenario "Second sync run makes zero changes" holds, and so do "Generator never writes status" and "Status set in Plane survives a re-push".

## 5. MCP server

- [ ] 5.1 Add `makeplane/plane-mcp-server` to the Plane Compose stack, with a version pinned at implementation time. Its port 8211 sits behind Caddy at `plane-mcp.<zone>`.
- [ ] 5.2 Enable OAuth mode. Set `PLANE_OAUTH_PROVIDER_*` from an OAuth app registered by code, using the path that 0.8 identified. Set `PLANE_OAUTH_ALLOWED_REDIRECT_URIS`. Token storage uses Plane's Valkey, through `REDIS_URL`.
- [ ] 5.3 Write `scripts/plane-mcp-client.py`, modelled on `scripts/agw-mcp-client.py`. It installs one secret-free HTTP MCP entry into Claude Code and pi. The user signs in through Plane OAuth, which goes through Authentik.
- [ ] 5.4 Set up weft's service identity. Create a Plane bot user named `weft`. Store its personal access token at `secret/services/plane:weft_mcp_pat`. weft's builder Claude Code gets an MCP entry for `/http/api-key/mcp` with the `x-workspace-slug` header, and the token is injected from weft's sealed store. This step is a companion pull request in the weft repository, which is outside this repository.
- [ ] 5.5 Verify locally. The MCP personal-access-token endpoint lists tools from Claude Code. This step depends on risk 8 in `design.md`, because tier gating may limit local validation.
- [ ] 5.6 Verify in production. An MCP OAuth login from Claude Code updates a work item, and the change is attributed to that user. weft's personal-access-token path works.
- [ ] 5.7 Validation gate: scenario "Harness edit is attributed to the signed-in user" holds, and so do "Harness lists Plane tools" and "weft uses its service identity".

## 6. Forge Discord Plane app

- [ ] 6.1 Create the service `platform/services/plane-discord/`, a Python container. Its image is built to GHCR by a workflow modelled on `.github/workflows/build-honcho.yml`. The libraries `discord.py` and `plane-sdk` are approved dependencies. Joseph A. Wisneski IV approved them on 2026-10-23, and their versions are pinned at implementation time.
- [ ] 6.2 Implement the Plane-app bot-token flow with client credentials, using the OAuth endpoints confirmed in 0.13.
- [ ] 6.3 Implement the webhook receiver. It verifies the `X-Plane-Signature` header with HMAC-SHA256. It is reachable only on the internal network from Plane. It posts to the per-project channels that site-config declares.
- [ ] 6.4 Implement the slash commands `/plane create`, `/plane assign` and `/plane status` through the Discord gateway websocket.
- [ ] 6.5 Implement link previews for `KEY-123` and for work-item URLs.
- [ ] 6.6 Read the bot token from `secret/services/discord:bot_token` through shared read. Do not copy it. Record the blast radius in the service's `CLAUDE.md`: the app is one more long-lived holder of the platform bot token.
- [ ] 6.7 Write `deploy-plane-discord.yml` and its template. The app deploys as its own composable service on the Plane VM.
- [ ] 6.8 Verify locally, subject to risk 8. A signed webhook event posts to a test channel, `/plane status` answers, and a work-item link unfurls.
- [ ] 6.9 Validation gate: scenario "Signed event posts to the project channel" holds. Its companion scenarios "Unsigned event is refused", "Slash command answers through the gateway", and "Discord thread messages are not mirrored into Plane" are also checked in 6.8.

## 7. tududi retirement

This phase is the last phase of the plan, and it runs only after the gates in Phases 0 to 6 pass. Its order follows the sync traps in `design.md`, risk 5.

- [ ] 7.1 Write `migrate-tududi-to-plane.yml`. It is a one-time playbook, and it is idempotent. It reads tududi's database inside its container, creates work items for the open tasks with the label `migrated-from-tududi`, and skips any item that is already present.
- [ ] 7.2 Remove the tududi-to-GitHub n8n workflows. Run the provisioning playbook with all pairs disabled. Then delete the workflows it owns, and confirm no owned workflow remains.
- [ ] 7.3 Remove the `verify-`, `refresh-` and `provision-tududi-github-sync` templates from `platform/semaphore/templates.yml`.
- [ ] 7.4 Uninstall the "tududi sync" GitHub App. Do this only after the refresher is gone from 7.3, because the refresher's mint step refuses on scope drift. Then delete the sync key fields from `secret/services/github`.
- [ ] 7.5 Run the destructive tududi teardown, gated on the confirmation value that `destroy-vm.yml` requires, `confirm_destroy`, set to the VM id. First take a final backup to site-config. Then run the clean-deploy without redeploying, and then destroy the VM with `destroy-vm.yml`. The scenario "Teardown refuses without confirmation" applies to this step.
- [ ] 7.6 Remove the Caddy route with `caddy_retire_sites`, the DNS record in `platform/infra/cloudflare/`, and the Authentik `tududi` blueprint along with its tombstone.
- [ ] 7.7 Remove the OPA entry, the inventory group and the docs rows that describe tududi.
- [ ] 7.8 Update `AGENTS.md`. Update the workflows, the secrets layout and the deployment status tables, and add the Plane sub-directory docs line. Remove the tududi lines. Scenario "AGENTS.md lists Plane and drops tududi" holds after this step.
- [ ] 7.9 Verify the teardown. No tududi containers, route, DNS record, OIDC application or GitHub App remain. The open tasks exist in Plane under the label `migrated-from-tududi`.
- [ ] 7.10 Validation gate: scenario "Retirement leaves no tududi surface" holds, and so do "Open tasks migrate under a marker label" and "Sync app is uninstalled after the refresher is gone".
