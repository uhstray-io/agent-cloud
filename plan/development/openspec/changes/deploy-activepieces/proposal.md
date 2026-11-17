Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Why

The platform needs a workflow-automation service. Scheduled and event-triggered flows that call other services have no shared runtime today. Activepieces was chosen to fill that role after n8n was retired on 2026-11-12, and Temporal was dropped the same day as the wrong tool for low-code flows. The plan is recorded in `plan/development/26-activepieces.md`, and the decisions D1 to D13 in that plan are the basis for this change.

Two upstream properties shape the change. Stored connections are encrypted with a key that must never be regenerated, and the first account to sign up on a fresh instance becomes its platform administrator. The change closes the first-account window and keeps the encryption key stable across redeploys.

## What Changes

- **New platform service `activepieces`.** It is a composable compose project on its own VM in the `activepieces_svc` group: an app container, a worker container, Postgres and Redis, under rootless podman. It is deployed, cleaned, backed up and restored through Semaphore playbooks. Its secrets come from OpenBao. The edition is Community, with `AP_EDITION=ce` set explicitly.
- **Identity and edge.** Interactive access goes through an Authentik forward-auth gate bound to the `platform-admins` group, applied at Caddy. Webhook paths are exempted by exact path only, and only for flows Joseph approves.
- **Account creation closed after the owner.** The deploy creates the owner account before the application port is reachable from any other host. Sign-up for uninvited addresses is refused, and `AP_ALLOW_OPEN_SIGN_UP` is never set.
- **Sandboxed execution.** Flows run under `SANDBOX_CODE_ONLY`, so no container is privileged.
- **Database backup and restore.** A dump playbook and a staging-restore playbook follow the Plane pattern.
- **Shared automation.** The shared `manage-secrets` task gains a hexadecimal secret type, so the encryption key has the format the upstream service expects.

## Capabilities

### New Capabilities

- `platform/activepieces-automation`: the Activepieces workflow-automation service as a platform capability. It covers deployment through Semaphore with secrets from OpenBao, flow execution without privileged containers, account creation closed after the owner, the forward-auth gate for platform administrators, the application port reachable from the Caddy host only, exact-path webhook exemptions, and database backup and restore.

### Modified Capabilities

None. The existing specs in this store do not describe workflow automation, so this change does not alter their requirements.

## Impact

- **New files.** A service directory `platform/services/activepieces/deployment/` with the compose files, `deploy.sh`, the env template and operator notes. Playbooks for deploy, clean-deploy, backup and restore. An Authentik blueprint `activepieces-forward-auth.yaml`. One or two test files.
- **Modified files.** `platform/semaphore/templates.yml`, the OPA catalog entry in `platform/services/opa/deployment/policies/agentcloud/data.json`, `platform/playbooks/validate-all.yml`, the inventory placeholder files, `platform/playbooks/bootstrap-local-dev.yml`, `platform/services/authentik/deployment/app-catalog.yml`, `platform/playbooks/tasks/manage-secrets.yml` (the hexadecimal type), `platform/infra/cloudflare/dns.tf`, `AGENTS.md`, `README.md`, the architecture maps and `platform/playbooks/README.md`.
- **Secret store.** A new path `secret/services/activepieces`, holding `encryption_key`, `jwt_secret`, `db_password` and `owner_password`. A `worker_token` is added only if spike O2 requires one.
- **Open risk.** The Community edition may offer no API key (upstream docs, `overview.mdx#L7`). Early spike O8, in task 1.5, decides it. A refuted result returns the edition decision to Joseph.
- **Out of scope.** The inference-gateway client certificate and identity (plan 27), the content flows (plan 28), S3 storage, SAML and paid SSO, and any data migration.

## Rollback Plan

Rollback is staged to match the deployment, and it is lossless only while no real data exists.

1. **Before production data exists.** Remove the Activepieces Caddy route and DNS record through `Manage Caddy Sites` and `Apply Cloudflare Tofu`. Then remove the Authentik blueprint through `app-catalog.yml` and re-run `Deploy Authentik`. Then run `Clean Deploy Activepieces`, which removes the containers and volumes. The VM can then be removed with `Destroy VM`.
2. **After data exists.** Run `Back Up Activepieces DB` and back up the encryption key with `Back Up Credentials to site-config` before any step that could remove data. Clean Deploy removes the stored connections along with the database. A stored connection cannot be decrypted without the encryption key (upstream docs, `docker-compose.mdx#L108`). Restore with `Restore Activepieces DB`, which requires an explicit dump file.
3. **Secrets.** The OpenBao fields stay in place until they are retired with `Retire OpenBao Fields`. Retiring them is a separate, deliberate step.

Decisions and their rationale are in `plan/development/26-activepieces.md`.
