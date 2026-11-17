Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Why

The platform has no workflow-automation service, and n8n was retired on 2026-11-12. Activepieces is the chosen replacement, and this change deploys it as a composable service behind the platform's identity provider, with the reasoning in `plan/development/26-activepieces.md`.

## What Changes

A new platform service `activepieces` on its own VM, deployed, cleaned, backed up and restored through Semaphore playbooks, with its secrets in OpenBao. Interactive access is limited to platform-administrators through an Authentik forward-auth gate at Caddy. Account creation is closed once the owner exists.

## Capabilities

### New Capabilities

- `platform/activepieces-automation`: the Activepieces service. Its requirements are encryption-key continuity, flows without privileged containers, account creation closed after the owner, access for platform-admins only, the application port reachable from the Caddy host only, exact-path webhook exemptions, and database backup and restore.

### Modified Capabilities

None.

## Impact

New service directory, playbooks, templates, an Authentik blueprint and app-catalog entry, and Caddy, DNS and firewall entries. See `tasks.md`.

Open risk: the Community edition may offer no API key. See plan 26, Risk: no API key on Community edition.

## Rollback Plan

Rollback is staged to match the deployment, and it is lossless only while no real data exists.

1. **Before production data exists.** Remove the Caddy route and DNS record through `Manage Caddy Sites` and `Apply Cloudflare Tofu`. Remove the Authentik blueprint through `app-catalog.yml` and re-run `Deploy Authentik`. Run `Clean Deploy Activepieces`, which removes the containers and volumes. The VM can then be removed with `Destroy VM`.
2. **After data exists.** Run `Back Up Activepieces DB` and back up the encryption key with `Back Up Credentials to site-config` before any step that could remove data. Clean Deploy removes the stored connections with the database, and they cannot be decrypted without the encryption key (upstream docs, `docker-compose.mdx#L108`). Restore with `Restore Activepieces DB`, which requires an explicit dump file.
3. **Secrets.** The OpenBao fields stay in place until `Retire OpenBao Fields` retires them, as a separate and deliberate step.
