# CLAUDE.md — platform/services/honcho/deployment

honcho — Plastic Labs memory API for agents. A four-container rootless-podman stack: `honcho-api` (:8000) + `honcho-deriver` on the CI-built `ghcr.io/uhstray-io/honcho` image, backed by honcho's OWN pgvector Postgres and redis (both internal-only). Reached at `memory.uhstray.io` behind central Caddy.

## What this service is

1. `honcho-api` serves the `/v3` REST API on `:8000` (plain HTTP; Caddy terminates TLS). Its entrypoint runs the alembic migrations (incl. `CREATE EXTENSION vector`) before fastapi starts.
2. `honcho-deriver` (same image, `python -m src.deriver`) consumes the redis-backed queue and derives representations from stored messages — it requires a **tool-calling** LLM.
3. Tenancy: one **workspace** for the team, each member a **peer** (stable id), a **session** per thread — shared-yet-scoped memory. This is the evolve backend (phase 4).

## Auth — two planes

- **`/v3` = JWT** (`AUTH_USE_AUTH=true`): every call carries a workspace/peer-scoped JWT signed with `AUTH_JWT_SECRET` (OpenBao). Member tokens are minted post-deploy (`POST /v3/keys`) into `secret/services/honcho/tokens/<member>` — phase 4, not this deploy.
- **`/docs` (+`/redoc`, `/openapi.json`) = Authentik forward_auth at Caddy** (`../../authentik/deployment/blueprints/honcho-docs-forward-auth.yaml`, slug `honcho-docs`). honcho itself never calls the IdP — hence **no distribute-ca-root** in the playbook and no CA mount in the overlay.

## LLM — Gemini interim, skynet later

Every text-gen feature (deriver, summary, all dialectic levels, dream) is pointed at Gemini via `*_MODEL_CONFIG__TRANSPORT=gemini` + `honcho_llm_model` (default `gemini-2.5-flash`); the key is a `_shared_reads` from nemoclaw. Embeddings are OFF (`EMBED_MESSAGES=false`; upstream default transport is openai and no OpenAI key exists here). The skynet swap is config-only: flip transports to `openai` + `*_MODEL_CONFIG__OVERRIDES__BASE_URL=<skynet /v1>` — see `templates/env.j2`.

## Conventions specific to this service

- **deploy.sh is lifecycle only** (Critical Deployment Rule #2). `deploy-honcho.yml` renders `.env` from OpenBao via `manage-secrets`; `deploy.sh` pulls, `up -d --force-recreate` (env_file changes are not compose-spec changes), waits for `honcho-api` healthy.
- **Prebuilt GHCR image** (wisbot pattern): `.github/workflows/build-honcho.yml` builds `plastic-labs/honcho` at a pinned ref → `ghcr.io/uhstray-io/honcho:<ref>`. Never built on the VM; bump the pin deliberately.
- **Only the api publishes a port** — env-driven in the base compose via `HONCHO_BIND`/`HONCHO_PORT` (`.env`); db/redis/deriver have no host ports (no firewall rules needed for them). The overlay only adds mem caps, `label=disable`, and the `local-dev` network on the api.
- **Stable secrets**: the JWT secret and DB password are generated once into OpenBao and reused — rotating the JWT secret invalidates every minted token; rotating the DB password locks the app out of the existing volume.

## What not to do

- Don't generate or read secrets in `deploy.sh` — Ansible + OpenBao own credentials.
- Don't publish ports for db/redis or move the api publish into `compose.local.yml` — it's env-parameterized in the base.
- Don't point the deriver at a non-tool-calling model — derivation silently degrades to stored-but-underived messages.
- Don't use `:latest-built` in deploys — it only marks the newest CI build; pin `<ref>`.
- Don't commit `.env` (JWT secret, DB password, Gemini key; gitignored).

## Related

- Deploy playbooks: [`../../../playbooks/deploy-honcho.yml`](../../../playbooks/deploy-honcho.yml), [`../../../playbooks/clean-deploy-honcho.yml`](../../../playbooks/clean-deploy-honcho.yml)
- Authentik forward_auth blueprint: [`../../authentik/deployment/blueprints/honcho-docs-forward-auth.yaml`](../../authentik/deployment/blueprints/honcho-docs-forward-auth.yaml)
- Image build workflow: [`../../../../.github/workflows/build-honcho.yml`](../../../../.github/workflows/build-honcho.yml)
- Plan: [`../../../../plan/development/11-tududi-honcho-deployment.md`](../../../../plan/development/11-tududi-honcho-deployment.md) (Service B — honcho)
- Root conventions: [`../../../../CLAUDE.md`](../../../../CLAUDE.md)
