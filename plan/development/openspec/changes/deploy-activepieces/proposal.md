Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Why

The platform has no workflow-automation service, and n8n's retirement was decided on 2026-11-10. Activepieces is the chosen replacement, and this change deploys it as a composable service behind the platform's identity provider, with the reasoning in `plan/development/26-activepieces.md`.

## What Changes

A new platform service `activepieces` on its own VM, deployed, cleaned, backed up and restored through Semaphore playbooks, with its secrets in OpenBao. Interactive access is limited to platform-administrators through an Authentik forward-auth gate at Caddy. Account creation is closed once the owner exists.

## Capabilities

### New Capabilities

- `platform/activepieces-automation`: the Activepieces service. Its requirements are encryption-key continuity, flows without privileged containers, account creation closed after the owner, access for platform-admins only, the application port reachable from the Caddy host only, exact-path webhook exemptions, outbound traffic limited to a declared allowlist, and database backup and restore.

### Modified Capabilities

None.

## Impact

New service directory, playbooks, templates, an Authentik blueprint and app-catalog entry, and Caddy, DNS and firewall entries. See `tasks.md`.

Open risk: the Community edition may offer no API key. See plan 26, Risk: no API key on Community edition.

## Rollback Plan

Rollback is staged to match the deployment, and it is lossless only while no real data exists.

1. **Before production data exists.** Remove the Caddy route and DNS record through `Manage Caddy Sites` and `Apply Cloudflare Tofu`. Retire the SSO application as `platform/services/authentik/deployment/app-catalog.yml` lines 28 to 34 describe: delete the catalog entry and its blueprint, add `state: absent` entries for its application and provider to `blueprints/retired-apps.yaml`, remove the slug from the site-config `authentik_apps` list, and run `Deploy Authentik`. Then destroy the VM with `Destroy VM`, launched with `confirm_destroy` set to the VM id. Do not use `Clean Deploy Activepieces` for teardown, because it redeploys: `clean-deploy-postiz.yml` imports `deploy-postiz.yml`, and task 1.3 plans the Activepieces clean deploy to import `deploy-activepieces.yml` in the same way.
2. **After data exists.** Run `Back Up Activepieces DB`, which copies the dump off the service VM (task 2.7), and back up the encryption key with `Back Up Credentials to site-config`, all before any step that could remove data. `Destroy VM` removes the data, and any dump still on the VM, with the VM, so it runs only after the off-VM copy exists and the task 2.7 restore has passed its local-dev round trip; reading the production copy itself before the destroy is part of open item O14. Stored connections cannot be decrypted without the encryption key (upstream docs, `docker-compose.mdx#L108`). Restore with `Restore Activepieces DB` on a rebuilt VM that declares `activepieces_restore_pending`, which requires an explicit dump file.
3. **Secrets.** The OpenBao fields stay in place until `Retire OpenBao Fields` retires them, after the change is promoted to `main`.

Decisions and their rationale are in `plan/development/26-activepieces.md`.
