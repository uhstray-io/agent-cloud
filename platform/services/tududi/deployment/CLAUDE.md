# CLAUDE.md — platform/services/tududi/deployment

tududi — self-hosted to-do app. A single rootless-podman container (`docker.io/chrisvel/tududi:1.1.1`, port 3002) backed by SQLite, SSO'd to Authentik via native OIDC. Reached at `todo.uhstray.io` behind central Caddy.

## What this service is

A single stateful container:
1. Serves the to-do UI + REST API on `:3002` (plain HTTP; Caddy terminates TLS).
2. Authenticates users via **native OIDC** against Authentik (SSO-only).
3. Persists everything in SQLite + uploads on two named volumes (`tududi-db`, `tududi-uploads`).

It also becomes the migration sink for NocoDB work data — weft writes via `POST /api/v1/...` with a personal API key. That is a post-deploy step, not part of this deploy.

## Conventions specific to this service

### deploy.sh is lifecycle only
No secret generation, no OpenBao calls (Critical Deployment Rule #2). `deploy-tududi.yml` renders `.env` from OpenBao via `manage-secrets`; `deploy.sh` only pulls, `up -d --force-recreate`, and waits healthy. `--force-recreate` is deliberate — an `env_file` content change is not a compose-spec change, so plain `up -d` would run with a stale `.env`.

### SSO-only with a break-glass admin
`PASSWORD_AUTH_ENABLED=false` — login is OIDC-only. The break-glass local admin (`TUDUDI_USER_EMAIL` / `TUDUDI_USER_PASSWORD`, password from OpenBao) is the recovery path if the IdP is down. `TUDUDI_TRUST_PROXY=true` is mandatory behind Caddy or SSO sessions 401.

### Slug consistency (tududi ↔ Authentik)
The Authentik application slug, tududi's `OIDC_PROVIDER_SLUG`, and the OIDC callback path must all be `tududi`. The Authentik signing key must be **RS256** (its default) — tududi rejects ES256 and does not use PKCE. Callback: `{BASE_URL}/api/oidc/callback/tududi`. The provider + application live in `../../authentik/deployment/blueprints/tududi-oidc.yaml` and are applied by Deploy Authentik (the IdP owns `tududi_oidc_client_secret`; tududi reads it via a manage-secrets `_shared_reads`).

### Env-file path
`.env` at the deploy-dir root, matching Authentik — compose auto-loads a project-root `.env` for `${TUDUDI_BIND}` / `${TUDUDI_PORT}` interpolation, so the local-vs-prod host bind is inventory-driven. Gitignored via the root `.gitignore` (`platform/services/*/deployment/.env`).

### Ports live in the base compose, not the overlay
compose merges `ports` lists append-only, so an overlay can't remove a base publish. The loopback-vs-host bind is env-driven in `compose.yml` via `TUDUDI_BIND` / `TUDUDI_PORT` (set in `.env`); `compose.local.yml` only adds the SELinux opt, a mem cap, and the `local-dev` network.

## What not to do

- Don't generate or read secrets in `deploy.sh` — Ansible + OpenBao own credentials.
- Don't enable password auth in prod — SSO-only is intentional; the break-glass admin is the fallback.
- Don't put the port publish in `compose.local.yml` — it's env-parameterized in the base.
- Don't commit `.env` (it holds the session / OIDC / admin secrets; it's gitignored).
- Don't change the OIDC signing key to ES256 or add PKCE — tududi does not support them.

## Related

- Deploy playbooks: [`../../../playbooks/deploy-tududi.yml`](../../../playbooks/deploy-tududi.yml), [`../../../playbooks/clean-deploy-tududi.yml`](../../../playbooks/clean-deploy-tududi.yml)
- **GitHub issue sync** — the automation contract, and the only place the
  per-user visibility rules are written down:
  [`../context/github-sync-contract.md`](../context/github-sync-contract.md).
  Read it before changing anything about tasks, tags or projects: tududi
  scopes every list per user, a project GRANTEE sees every task in that
  project while the project OWNER sees only tasks they created themselves,
  and a task's `user_id` is fixed to its creator. The service account remains
  configured independently of project ownership; task 6.0 must prove both the
  operator and sync can see new work before production enablement.
  Store Token's `tududi_token_validate_only=true` proves existing access without
  minting or writing OpenBao; failed proof never rotates access. Provisioning
  preserves objects and deactivates obsolete workflows with read-back.
  Declaration:
  [`../sync/github-mapping.yml`](../sync/github-mapping.yml); engine:
  [`../sync/lib/sync-core.js`](../sync/lib/sync-core.js)
- Authentik OIDC blueprint: [`../../authentik/deployment/blueprints/tududi-oidc.yaml`](../../authentik/deployment/blueprints/tududi-oidc.yaml)
- Plan: [`../../../../plan/development/11-tududi-honcho-deployment.md`](../../../../plan/development/11-tududi-honcho-deployment.md)
- Root conventions: [`../../../../CLAUDE.md`](../../../../CLAUDE.md)
