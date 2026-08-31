# Complete the n8n composable deployment

## Why

n8n's composable deployment machinery landed on `main` months ago (`deploy-n8n.yml`,
`seed-n8n-secrets.yml`, `clean-deploy-n8n.yml`, `n8n.env.j2`), but the production
instance still runs the legacy secret-generating path — the cutover has been
**execution HELD** since 2026-06-02 (`plan/development/09-service-migrations-tooling.md`)
pending live OpenBao access, which Semaphore now provides. Meanwhile Postiz shipped
with an explicit automation contract for n8n
(`platform/services/postiz/context/use-cases.md`) that nothing consumes yet: the
API key sits in OpenBao waiting for n8n workflows that cannot be built until the
Postiz community node and credential exist in n8n. PR #15 — the original migration
attempt — is 395 commits behind `main` and superseded by newer implementations of
everything it contained; it needs a recorded disposition.

## What Changes

- **Execute the HELD prod cutover** for n8n: pre-seed the live stateful secrets
  (`N8N_ENCRYPTION_KEY`, both Postgres passwords) into OpenBao via
  `seed-n8n-secrets.yml`, verify they resolve as *pre-existing*, then run the
  composable `deploy-n8n.yml` — preserving every stored workflow credential.
- **Add the missing Semaphore templates**: `Seed n8n Secrets`, `Clean Deploy n8n`,
  and the new `Store n8n API Key` (only `Deploy n8n` and `Update n8n` exist today
  in `platform/semaphore/templates.yml`).
- **Install `n8n-nodes-postiz` as code**: pin the community node
  (name + version + checksum) via `N8N_COMMUNITY_PACKAGES_MANAGED_BY_ENV` /
  `N8N_COMMUNITY_PACKAGES` in the n8n env template, so the node install is
  reconciled at container start and UI installs stay disabled.
- **Capture the n8n API key into OpenBao** (`secret/services/n8n:n8n_api_key`)
  via a new `store-n8n-api-key.yml`, following the `store-postiz-api-key.yml`
  pattern — key never printed, key-bearing steps `no_log`.
- **Provision the Postiz credential inside n8n as code**: a playbook shared-reads
  `secret/services/postiz:postiz_api_key` (never a copy under
  `secret/services/n8n`) and creates the `postizApi` credential with the
  self-hosted `Host` pointing at the public Postiz URL, per the automation
  contract.
- **Cleanup**: remove the dead `generate_n8n_env()` from `platform/lib/common.sh`
  after the cutover is validated (NocoDB's generator stays — that migration is
  paused); update `09-service-migrations-tooling.md` to lift the n8n HOLD; record
  PR #15 as superseded and close it (user-gated action).
- **NOT in scope**: NocoDB migration (explicitly paused), building actual n8n
  workflows against Postiz (this change delivers the substrate: node, credential,
  key), n8n SSO (community n8n has none — Caddy forward_auth remains the gate).

## Capabilities

### New Capabilities

- `platform/n8n-automation`: the n8n workflow-automation service as a composable,
  OpenBao-sourced deployment — stateful-secret preservation across the legacy→
  composable cutover, code-managed community-node installation, API-key custody
  in OpenBao, and code-provisioned service credentials (Postiz first).

### Modified Capabilities

<!-- none — postiz-publishing's contract (rate ceiling, key ownership, public-host
     path) is unchanged; this change consumes it exactly as written. -->

## Impact

- **Playbooks**: new `store-n8n-api-key.yml`, new credential-provisioning playbook;
  `platform/semaphore/templates.yml` gains three templates.
- **Templates/env**: `platform/services/n8n/deployment/templates/n8n.env.j2` and/or
  `compose.yml` gain the community-package variables.
- **Shared lib**: `platform/lib/common.sh` loses `generate_n8n_env()` (post-validation).
- **Live state**: the production n8n VM is cut over in place — the one irreversible
  risk is the encryption key; the entire sequencing exists to protect it.
- **Secrets layout**: `secret/services/n8n` gains `n8n_api_key`; no new copies of
  the Postiz key.
- **Docs**: `CLAUDE.md` workflow table, `09-service-migrations-tooling.md` HOLD
  status, `platform/services/n8n` deployment docs.
- **PR #15**: closed as superseded (with the NocoDB half explicitly deferred, not
  discarded).

## Rollback Plan

- **Before the cutover deploy**: nothing has touched the running service —
  `seed-n8n-secrets.yml` only reads the host env file and writes OpenBao; abort
  freely.
- **Cutover deploy fails**: the legacy path remains intact until the cleanup phase
  (`generate_n8n_env()` is only removed after validation); re-run the legacy
  `deploy.sh` flow with the on-VM `config/n8n.env`, which the seed playbook never
  modifies. The compose image tag is pinned, so no silent version jump compounds
  a rollback.
- **Community node misbehaves**: remove the package entry from
  `N8N_COMMUNITY_PACKAGES` and redeploy — `MANAGED_BY_ENV` reconciliation
  uninstalls it; credentials created in n8n are inert without the node.
- **API key capture**: additive KV-v2 merge; deleting the key from OpenBao and
  revoking it in n8n reverts fully.
