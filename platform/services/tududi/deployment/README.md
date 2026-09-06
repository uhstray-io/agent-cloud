# tududi — deployment

Self-hosted to-do app (`docker.io/chrisvel/tududi:1.1.1`) — a single rootless-podman container backed by SQLite. Reached at `todo.uhstray.io` (local: `todo.agent-cloud.test`) behind central Caddy, with native OIDC login against Authentik. Becomes the migration sink for NocoDB work data (driven by weft).

```text
Browser ──> Caddy (TLS) ──todo.uhstray.io──> tududi :3002 (podman)
                                               │  native OIDC
                                               ▼
                                            Authentik (IdP)
```

## How this deploys

Composable pattern:

```text
Semaphore "Deploy tududi"
  └─ platform/playbooks/deploy-tududi.yml
     ├─ tasks/place-monorepo.yml     # clone/copy the monorepo, ensure podman + compose
     ├─ tasks/manage-secrets.yml     # OpenBao → templates/env.j2 → .env (session secret,
     │                               #   break-glass admin; OIDC secret shared-read from authentik)
     ├─ deploy.sh                    # podman compose pull + up -d --force-recreate + wait healthy
     └─ verify                       # GET /api/health → 200 (inside the container)
```

`deploy.sh` is container lifecycle only — it never generates secrets (Critical Deployment Rule #2). Ansible owns the full credential lifecycle via OpenBao. Clean rebuild: `platform/playbooks/clean-deploy-tududi.yml`.

## Auth

- **SSO-only** (`PASSWORD_AUTH_ENABLED=false`) via native OIDC against Authentik (`OIDC_PROVIDER_SLUG=tududi`).
- The Authentik side is `blueprints/tududi-oidc.yaml` (OAuth2 provider + application, slug `tududi`) under the Authentik service; it is applied by Deploy Authentik, not this playbook.
- Callback (registered as a Strict redirect URI in Authentik): `{BASE_URL}/api/oidc/callback/tududi`.
- A **break-glass local admin** (`TUDUDI_USER_EMAIL` / `TUDUDI_USER_PASSWORD`, password from OpenBao) is created once as the recovery path if OIDC is unavailable.
- `TUDUDI_TRUST_PROXY=true` is **required** behind Caddy or SSO sessions 401.

## Persistence

Two named volumes (survive redeploys):

| Volume | Container path | Contents |
|--------|----------------|----------|
| `tududi-db` | `/app/backend/db` | SQLite database + auto-backups |
| `tududi-uploads` | `/app/backend/uploads` | User-uploaded files |

## File layout

```text
deployment/
├── deploy.sh              Container lifecycle only (pull, up, wait healthy)
├── compose.yml            Single tududi service + named volumes + healthcheck
├── compose.local.yml      local_mode overlay (SELinux label=disable, local-dev network)
└── templates/env.j2       Jinja2 — .env rendered from OpenBao + inventory
```

## Related

- Playbooks: [`../../../playbooks/deploy-tududi.yml`](../../../playbooks/deploy-tududi.yml), [`../../../playbooks/clean-deploy-tududi.yml`](../../../playbooks/clean-deploy-tududi.yml)
- Authentik OIDC blueprint: [`../../authentik/deployment/blueprints/tududi-oidc.yaml`](../../authentik/deployment/blueprints/tududi-oidc.yaml)
- Plan: [`../../../../plan/development/11-tududi-honcho-deployment.md`](../../../../plan/development/11-tududi-honcho-deployment.md)
- Root conventions: [`../../../../CLAUDE.md`](../../../../CLAUDE.md)
