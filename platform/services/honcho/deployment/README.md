# honcho — deployment

Plastic Labs memory API (`ghcr.io/uhstray-io/honcho`, CI-built from a pinned upstream ref) — a four-container rootless-podman stack with its own pgvector Postgres and redis. Reached at `memory.uhstray.io` (local: `memory.agent-cloud.test`) behind central Caddy. Backs evolve's team memory (workspace/peer/session tenancy).

```text
Browser/agents ──> Caddy (TLS) ──memory.uhstray.io──┬─ /v3      ──> honcho-api :8000 (JWT-authenticated)
                                                    └─ /docs*   ──> forward_auth (Authentik) ──> honcho-api
                                                                       honcho-api ──> honcho-db (pgvector, internal)
                                                                       honcho-deriver ──> honcho-redis + honcho-db + Gemini (interim LLM)
```

## How this deploys

Composable pattern (same as every platform service):

```text
Semaphore "Deploy honcho"
  └─ platform/playbooks/deploy-honcho.yml
     ├─ tasks/place-monorepo.yml     # clone/copy the monorepo, ensure podman + compose
     ├─ tasks/manage-secrets.yml     # OpenBao → templates/env.j2 → .env (JWT secret + DB
     │                               #   password; Gemini key shared-read from nemoclaw)
     ├─ deploy.sh                    # podman compose pull + up -d --force-recreate + wait healthy
     └─ verify                       # GET /health → 200 (inside the api container)
```

`deploy.sh` is container lifecycle only — it never generates secrets (Critical Deployment Rule #2). The image is prebuilt by `.github/workflows/build-honcho.yml` (dispatch-only; pinned `plastic-labs/honcho` ref → GHCR) — nothing builds on the VM. Clean rebuild: `platform/playbooks/clean-deploy-honcho.yml` (destroys the Postgres + redis volumes).

## Auth

- **`/v3` API — JWT** (`AUTH_USE_AUTH=true`). Workspace/peer-scoped tokens signed with the OpenBao-held secret; per-member tokens are minted post-deploy (`POST /v3/keys`) and stored at `secret/services/honcho/tokens/<member>` (the phase-4 evolve step).
- **`/docs`, `/redoc`, `/openapi.json` — Authentik forward_auth at Caddy**, via the `honcho-docs` proxy provider (`../../authentik/deployment/blueprints/honcho-docs-forward-auth.yaml`, applied by Deploy Authentik). honcho makes no server-side IdP calls, so no CA-root distribution is needed.

## LLM

The deriver and dialectic require a **tool-calling** model. Interim: Gemini (`honcho_llm_model`, default `gemini-2.5-flash`; key shared-read from nemoclaw) across every text-gen feature. Embeddings are off (`EMBED_MESSAGES=false`) until skynet serves an embedding model. Later swap to skynet is config-only — flip `*_MODEL_CONFIG__TRANSPORT` to `openai` and add `*_MODEL_CONFIG__OVERRIDES__BASE_URL=<skynet /v1>` (see `templates/env.j2`).

## Persistence

Named volumes (survive redeploys):

| Volume | Container path | Contents |
|--------|----------------|----------|
| `honcho-pg-data` | `/var/lib/postgresql/data` | pgvector Postgres — workspaces, peers, sessions, messages, representations |
| `honcho-redis-data` | `/data` | redis RDB snapshots — deriver queue + cache |

## File layout

```text
deployment/
├── deploy.sh              Container lifecycle only (pull, up, wait healthy)
├── compose.yml            api + deriver (shared image) + pgvector db + redis
├── compose.local.yml      local_mode overlay (mem caps, label=disable, local-dev net on api)
└── templates/env.j2       Jinja2 — .env rendered from OpenBao + inventory
```

## Related

- Playbooks: [`../../../playbooks/deploy-honcho.yml`](../../../playbooks/deploy-honcho.yml), [`../../../playbooks/clean-deploy-honcho.yml`](../../../playbooks/clean-deploy-honcho.yml)
- Authentik blueprint: [`../../authentik/deployment/blueprints/honcho-docs-forward-auth.yaml`](../../authentik/deployment/blueprints/honcho-docs-forward-auth.yaml)
- Image build workflow: [`../../../../.github/workflows/build-honcho.yml`](../../../../.github/workflows/build-honcho.yml)
- Plan: [`../../../../plan/development/11-tududi-honcho-deployment.md`](../../../../plan/development/11-tududi-honcho-deployment.md) (Service B — honcho)
- Root conventions: [`../../../../CLAUDE.md`](../../../../CLAUDE.md)
