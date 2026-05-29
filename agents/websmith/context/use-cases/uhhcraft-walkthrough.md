# Use case: UhhCraft

UhhCraft is the **first concrete site** built with WebSmith and integrated into agent-cloud. This walkthrough shows how each of its phase artifacts translated into a deployable service tree.

> If you're building a new site and want a worked example to copy from, this is it. The shape of `platform/services/uhhcraft/` is the shape every future WebSmith output should land in.

## What UhhCraft is

An e-commerce storefront for AI-designed, one-of-a-kind physical goods (primarily stickers and 3D-printed items). Every item is previewed in an interactive 3D canvas before checkout. Stack: Go + templ + HTMX + Postgres + Redis + MinIO + Stripe + River + Caddy.

## Spec → service map

The signed spec lives at [`platform/services/uhhcraft/context/spec/SPEC.md`](../../../../platform/services/uhhcraft/context/spec/SPEC.md). Each phase artifact mapped as follows:

| Phase | Artifact | Mapped to |
|-------|----------|-----------|
| 0 — Intake | [`intake.md`](../../../../platform/services/uhhcraft/context/spec/intake.md) | Frozen as-of-build context; not load-bearing for code |
| 1 — Purpose | [`purpose.md`](../../../../platform/services/uhhcraft/context/spec/purpose.md) | Drives archetype = "e-commerce + custom configurator" → page inventory in `web/templates/pages/` |
| 2 — Template | [`template.md`](../../../../platform/services/uhhcraft/context/spec/template.md) | Pages + components → `web/templates/{layouts,components,pages}/` (templ source) |
| 3 — Tooling | [`tooling.md`](../../../../platform/services/uhhcraft/context/spec/tooling.md) | Go + templ + HTMX + Postgres + Redis + MinIO + Stripe + River + Caddy → `compose.yml`, `Dockerfile`, `go.mod`, `internal/` packages |
| 4 — Style | [`style.md`](../../../../platform/services/uhhcraft/context/spec/style.md) | Nunito + warm orange / soft blue + warm-neutral backgrounds → `web/static/css/input.css` (Tailwind config) |
| 5 — Considerations | [`considerations.md`](../../../../platform/services/uhhcraft/context/spec/considerations.md) | PCI DSS SAQ A, COPPA 13+, CCPA — implemented in `internal/checkout/`, `internal/auth/`, the site footer template, and the Caddy CSP |

## Agent-cloud-specific changes

The original spec made assumptions that don't survive contact with agent-cloud's conventions. The deltas are recorded in the spec's `## Alignment with agent-cloud conventions` section (read it before authoring a new site):

1. **Native binary → Podman containers.** UhhCraft assumed Kamal + native systemd unit; agent-cloud is Podman-only.
2. **`/etc/uhhcraft/env` → OpenBao + templated `.env`.** Secrets come from `secret/services/uhhcraft/*`.
3. **Kamal → Semaphore + Ansible.** Deployment goes through `platform/playbooks/deploy-uhhcraft.yml`.
4. **Per-service Caddy → central Caddy fragment.** UhhCraft ships `templates/caddy-site.j2`, dropped into `platform/services/caddy/sites/`.
5. **One MinIO shared with AI services → three independent MinIOs.** Per-service isolation. Cross-service assets are served through central Caddy.
6. **`ai/image/` + `ai/model3d/` inside UhhCraft → separate platform services.** `platform/services/inference-comfyui/` and `platform/services/inference-hunyuan3d/`.
7. **Service-local `ci.yml` → unified `lint-and-test.yml` with path filters.** One CI surface across the monorepo.
8. **Committed `_templ.go` → generate-in-CI.** Generated code is gitignored.

Every future WebSmith site should expect at least items 1–4 and 7–8 to apply.

## Service tree

```text
platform/services/uhhcraft/
├── CLAUDE.md                      Service-specific Claude guidance
├── deployment/
│   ├── README.md                  Dev quickstart + deploy story
│   ├── deploy.sh                  Container lifecycle only
│   ├── post-deploy.sh             Migrations + River bootstrap + healthcheck
│   ├── Dockerfile                 Multi-stage Go build (templ + sqlc + Tailwind)
│   ├── compose.yml                Podman-compatible (Postgres + Redis + MinIO + app)
│   ├── Makefile                   Dev helpers
│   ├── go.mod / go.sum / sqlc.yaml
│   ├── .env.example               Local dev only — prod .env is templated by Ansible
│   ├── templates/
│   │   ├── env.j2                 Jinja2 — prod .env from OpenBao
│   │   └── caddy-site.j2          Jinja2 — central Caddy site fragment
│   ├── cmd/server/                main.go
│   ├── internal/                  Domain packages (auth, cart, catalog, …, ratelimit)
│   ├── web/templates/             templ source (.templ); generated *_templ.go gitignored
│   ├── web/static/                CSS source, JS bundle, fonts
│   ├── db/migrations/             goose SQL migrations
│   ├── db/queries/                sqlc query files
│   └── config/                    Non-secret TOML (materials, fulfillment, USPS, …)
└── context/
    ├── spec/                      Signed phase artifacts + SPEC.md + alignment entries
    ├── architecture/
    │   └── ai-sidecar-contract.md UhhCraft-side view of the HTTP contract
    ├── prompts/
    ├── skills/
    └── use-cases/

platform/services/inference-comfyui/   (sister service — Flux.1 image generation)
platform/services/inference-hunyuan3d/ (sister service — Hunyuan3D 3D mesh generation)
```

## Patterns to copy in a future site

If you've just finished a WebSmith session and need to land the output in agent-cloud, follow this checklist:

1. **Service skeleton.** Mirror `platform/services/uhhcraft/` — `deployment/ + context/` split.
2. **Spec home.** Drop all six phase artifacts + `SPEC.md` into `context/spec/`. Append an `## Alignment with agent-cloud conventions` section the moment any platform constraint applies.
3. **Container Dockerfile.** Multi-stage, distroless runtime, build args for any generators (templ, sqlc, esbuild, Tailwind, etc.).
4. **`compose.yml`.** Podman-friendly (no `version:` key, `nvidia.com/gpu=all` for GPU services), bind app ports to loopback only.
5. **`deploy.sh`.** Lifecycle only. Source `platform/lib/common.sh`. No secret generation, no migrations, no API calls.
6. **`post-deploy.sh`.** Migrations, schema bootstrap, healthcheck. Idempotent.
7. **`templates/env.j2`.** Every env var referenced in `compose.yml` and the app, fed by OpenBao via `tasks/manage-secrets.yml`.
8. **`templates/caddy-site.j2`.** Central Caddy fragment with TLS, CSP, compression, route handlers. Dropped into `platform/services/caddy/sites/` by the deploy playbook.
9. **Service `CLAUDE.md`.** Anything site-specific that future contributors need (generated code policy, healthcheck subcommand, migration ordering, deviations from defaults).
10. **`context/architecture/`.** Diagrams + HTTP contracts with sibling services.

## What did *not* come from the spec

Some things UhhCraft needs that the spec did not enumerate (these are the catch-all-question items):

- `./uhhcraft healthcheck` subcommand for container HEALTHCHECK — added during integration; not in spec.
- `./uhhcraft river migrate-up` subcommand for River — added during integration.
- Separate MinIO instances — emerged from the per-service isolation review.
- CSP `worker-src 'self' blob:` for Three.js WASM — emerged from canvas integration.

These additions are tracked in the spec's `## Tracking future deviations` register (currently empty in UhhCraft's case because alignment changes were captured in the alignment section, not the deviation register).

The lesson for future sites: the spec is the contract, but integration always surfaces a small number of additions. Capture them in `## Alignment` or `## Tracking future deviations` — don't leave them undocumented.
