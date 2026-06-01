# UhhCraft — deployment

Self-hosted e-commerce storefront for AI-designed, one-of-a-kind physical goods (stickers + 3D prints). First concrete site built with WebSmith.

**Stack:** Go 1.26 · templ · HTMX · Tailwind CSS · PostgreSQL 16 · Redis 7 · MinIO · Stripe · River · Caddy

**Full spec:** [`../context/spec/SPEC.md`](../context/spec/SPEC.md) — read the `## Alignment with agent-cloud conventions` section at the bottom for everything platform-specific.

---

## How UhhCraft deploys in agent-cloud

Production deploys go through **Semaphore → Ansible → Podman**. Never run `deploy.sh` directly on a VM; the OpenBao credentials it needs are injected by Ansible.

```text
Semaphore "Deploy UhhCraft" template
  └─ platform/playbooks/deploy-uhhcraft.yml (Phase 4 of WEBSMITH-INTEGRATION-PLAN.md)
     ├─ tasks/manage-secrets.yml   # fetch from OpenBao, render templates/env.j2 → .env
     ├─ deploy.sh                  # podman compose pull + up + wait healthy
     ├─ post-deploy.sh             # goose migrate, river migrate, healthcheck
     └─ render templates/caddy-site.j2 into central Caddy sites/ + reload
```

The `deploy.sh` in this directory is **container lifecycle only** — no secret generation, no OpenBao calls, no migrations.

## Local development quickstart

```bash
# Prereqs
go install github.com/a-h/templ/cmd/templ@v0.3.1020
go install github.com/sqlc-dev/sqlc/cmd/sqlc@v1.31.1
go install github.com/pressly/goose/v3/cmd/goose@v3.27.1
go install github.com/air-verse/air@latest
# Tailwind standalone — see Dockerfile for the version pin
brew install tailwindcss   # or download the binary from the tailwindlabs releases

# 1. Env
cp .env.example .env
# Edit .env — fill SESSION_SECRET at minimum:
#   openssl rand -hex 32

# 2. Backing services
docker compose up -d postgres redis minio
# (Podman works the same: `podman compose up -d postgres redis minio`)

# 3. Generate code
make templ     # *_templ.go from *.templ
make sqlc      # typed Go from db/queries/*.sql

# 4. Migrate
source .env && make db-migrate
# River migrations run on app startup or via `./bin/uhhcraft river migrate-up`

# 5. Run
make dev       # air-driven hot reload
```

App at <http://localhost:3000>. MinIO console at <http://localhost:9001>.

## AI sidecars (optional locally)

UhhCraft calls two HTTP services for generation:

- `AI_IMAGE_SERVICE_URL` → `platform/services/inference-comfyui/` (Flux.1 via ComfyUI)
- `AI_3D_SERVICE_URL`    → `platform/services/inference-hunyuan3d/` (Hunyuan3D)

In production each runs on its own GPU VM with its own MinIO. For local dev, point at any mock returning the same JSON shape — see [`../context/architecture/ai-sidecar-contract.md`](../context/architecture/ai-sidecar-contract.md).

## Project structure

```text
deployment/
├── deploy.sh            Container lifecycle only — invoked by Ansible
├── post-deploy.sh       Migrations + healthcheck — invoked by Ansible
├── Dockerfile           Multi-stage Go build (templ + sqlc + Tailwind + go build)
├── compose.yml          Podman/Docker compose for all 4 containers
├── Makefile             Dev helpers (templ, sqlc, db-migrate, run, test, lint)
├── go.mod / go.sum / sqlc.yaml
├── .env.example         Local dev only; production .env is templated by Ansible
├── templates/
│   ├── env.j2           Jinja2 — production .env templated from OpenBao
│   └── caddy-site.j2    Jinja2 — Caddy fragment dropped into central Caddy
├── cmd/server/          main.go, entrypoint
├── internal/            Domain packages (auth, cart, catalog, checkout, …)
├── web/
│   ├── templates/       templ source (*.templ) and generated (_templ.go, gitignored)
│   └── static/          CSS source, JS bundle, fonts
├── db/
│   ├── migrations/      goose SQL migrations
│   └── queries/         sqlc query files
├── config/              Non-secret TOML (materials, fulfillment routing, USPS, Printify)
└── sites/               (reserved)
```

## Common tasks

| Task | Command |
|------|---------|
| Hot reload dev server | `make dev` or `air` |
| Production-shape build | `docker build .` (or `podman build .`) |
| Regenerate templ | `make templ` |
| Regenerate sqlc | `make sqlc` |
| Run migrations | `source .env && make db-migrate` |
| Run tests | `make test` |
| Lint | `make lint` |
| Format | `make fmt` |

## Adding a material

1. Open [`config/materials.toml`](config/materials.toml).
2. Copy an existing `[[sticker.materials]]` or `[[print.materials]]` block.
3. Set a unique `id` and fill in the fields.
4. Commit + push; Semaphore re-renders config on next deploy.

No DB migration, no Go change.

## Production rollback

- `update-uhhcraft.yml --extra-vars "uhhcraft_image=ghcr.io/uhstray-io/uhhcraft:<previous-sha>"` — no data loss.
- `clean-deploy-uhhcraft.yml` — **destructive** (wipes Postgres / Redis / MinIO volumes). Use only on a known-broken stack with a fresh DB backup.

## Outstanding integration items

These are tracked in [`../../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md`](../../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md):

- **Phase 3:** OpenBao policy + AppRole for UhhCraft.
- **Phase 4:** `deploy-uhhcraft.yml` Ansible playbook + composable tasks.
- **Phase 5:** Caddy `sites/*.caddy` import pattern + reload task.
- **Phase 6:** Proxmox VM provisioning.
- **Phase 7:** Semaphore templates.
- **Phase 8:** CI extensions (Go / templ / sqlc / gosec).
- **Two app subcommands** the Dockerfile and `compose.yml` assume but the source may not have yet: `./uhhcraft healthcheck` (used by container `HEALTHCHECK`) and `./uhhcraft river migrate-up` (used by `post-deploy.sh`). Add to `cmd/server/main.go` early in implementation.

## Related

- Service-level Claude guidance: [`../CLAUDE.md`](../CLAUDE.md)
- Signed spec + alignment entries: [`../context/spec/SPEC.md`](../context/spec/SPEC.md)
- AI sidecar contract: [`../context/architecture/ai-sidecar-contract.md`](../context/architecture/ai-sidecar-contract.md)
- WebSmith agent that produced the spec: [`../../../../agents/websmith/`](../../../../agents/websmith/)
