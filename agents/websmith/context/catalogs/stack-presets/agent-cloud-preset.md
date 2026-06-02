# Preset — agent-cloud

The **opinionated stack for sites landing inside the agent-cloud monorepo**. Use this preset whenever the site is going to live at `platform/services/<sitename>/` rather than its own standalone repo. It pre-decides every infrastructure question that agent-cloud already has a strong answer for, so the WebSmith session can spend its time on what's specific to *this* site (purpose, template, style, considerations).

This is not an alternative to the language-specific presets (`go-templ-htmx`, `nextjs-typescript`, `rails`, etc.) — it sits **above** them. Pick the agent-cloud preset first, then layer one of those on top for the actual web framework.

---

## When it fits

- **Always**, if the site is going into agent-cloud.
- The site needs to share secrets infrastructure (OpenBao), deployment orchestration (Semaphore), reverse-proxy (central Caddy), or CI (the unified `lint-and-test.yml`) with the rest of the platform.
- You want a new site to be deployable, observable, and rollback-able the same way every other platform service is, with no bespoke ops.
- The site benefits from co-locating with NemoClaw / NetClaw / Cowork — internal admin tools, dashboards over platform data, customer-facing storefronts with AI behind them.

## When it doesn't

- The site needs to ship before the next agent-cloud deploy can happen and ops is currently red. Use a standalone preset and a quick VPS instead; integrate later.
- The site is a marketing one-pager going to a CDN. agent-cloud is over-engineered for static hosting on Cloudflare Pages or Vercel.
- The team building the site has no exposure to Ansible, OpenBao, or Podman and won't be onboarding any time soon. The platform's deployment shape is real cognitive load — don't impose it if it won't be operated.
- Hard regulatory isolation requirements (e.g., dedicated tenant per customer, FedRAMP boundary). agent-cloud is single-tenant by design.

## Composition

| Category | agent-cloud opinion |
|----------|---------------------|
| **Web framework** | Pick a language preset (`go-templ-htmx`, `nextjs-typescript`, `rails`, `django`, etc.). UhhCraft uses `go-templ-htmx`. |
| **Database** | **PostgreSQL 16+**. Used by every stateful service in the platform. |
| **Cache / queue** | **Redis 7+** for cache + rate-limiting. Job queue uses the language-native option (River for Go, Sidekiq for Rails, etc.) backed by Postgres or Redis. |
| **Object storage** | **MinIO** — one instance per service (per-service isolation is the convention). Cross-service assets are served through central Caddy proxy paths. |
| **Container runtime** | **Podman 4.4+ with podman-compose**. Docker is reserved for NetBox; everything else runs Podman. Rootless containers, CDI for GPU. |
| **Reverse proxy** | **Central Caddy** at `platform/services/caddy/`. Each site ships `templates/caddy-site.j2` rendered by Ansible into `sites/<sitename>.caddy`; central Caddy `import sites/*.caddy`. TLS via CloudFlare DNS-01. |
| **Secrets** | **OpenBao** at `secret/services/<sitename>`. Ansible templates `.env` from OpenBao at deploy time; the app reads `.env` at startup. No runtime OpenBao calls from app code in v1. |
| **Deployment** | **Semaphore-orchestrated Ansible**. Composable 5-phase playbook: clone + secrets → containers → post-deploy bootstrap → caddy fragment → verify. Mirrors `deploy-uhhcraft.yml`. |
| **Hosting** | **Dedicated Proxmox VM** per service. Spec'd in `platform/hypervisor/proxmox/vm-specs.example.yml` (real values in site-config). One service, one VM, one SSH key, one OpenBao policy. |
| **SSH** | Per-service ed25519 keypair stored at `secret/services/ssh/<sitename>`. Distributed via `distribute-ssh-keys.yml`, hardened with `harden-ssh.yml`. |
| **Generated code** | **Generate-in-CI only**. Whatever the language's codegen tool produces (templ, sqlc, protoc, …) is gitignored. CI runs the generator before lint + test. |
| **CI/CD** | **Unified `.github/workflows/lint-and-test.yml`** with path filters. New language? Add a path-gated job. Don't ship a per-service workflow. |
| **Email** | Resend (transactional). |
| **Payments** | Stripe via the language SDK; test mode in dev + CI. |
| **Notifications** | Discord webhook for ops alerts; Slack if/when the team migrates. |
| **Logs** | App writes JSON to stdout; container engine collects; future Grafana Loki ingestion (planned). |
| **Metrics** | Prometheus scrape endpoint on the app (planned; Phase-2 future). |
| **Tracing** | OpenTelemetry SDK → Tempo (planned). |

## Hosting

- **Proxmox cluster** at the uhstray.io datacenter. New VMs cloned from a base Ubuntu 24.04 template via `provision-vm.yml`.
- **GPU services** (image / 3D / inference) need PCIe passthrough — see [`plan/development/UHHCRAFT-GPU-PASSTHROUGH.md`](../../../../../plan/development/UHHCRAFT-GPU-PASSTHROUGH.md).
- **Public DNS** via CloudFlare; A record per service pointing at the Caddy VM's public IP. Caddy fronts everything.
- **Internal-only services** (queue workers, internal tools) don't get DNS records or Caddy entries — they bind loopback and are reached from within the platform network.

## CI/CD

The unified workflow at `.github/workflows/lint-and-test.yml` has these jobs already:

- **lint** — ruff, shellcheck, ansible-lint, yamllint, hadolint, terraform fmt.
- **security** — trufflehog (verified + all), RFC1918 IP audit, credential pattern audit, Jinja2 hardcoded-value audit, bandit (Python).
- **test** — pytest (Python), BATS (Bash).
- **Path-gated language jobs** — Go lint + Go test + Go build (golangci-lint, gosec, templ generate, sqlc generate, race-mode test, Buildx image build).

When adding a new site:

1. If your language already has jobs (Go), no CI changes needed — path filter picks them up.
2. If you bring a new language, add a path-gated job triggered when `platform/services/<sitename>/deployment/**` changes. Use the Go jobs as the shape to mirror.

Branch testing via Semaphore (see `plan/architecture/BRANCH-TESTING-WORKFLOW.md`) deploys feature branches to real VMs for end-to-end validation before merge.

## Cost profile

| Item | Approx |
|------|--------|
| Proxmox VM (CPU, 4 vCPU / 8GB / 60GB) | Run on existing cluster; marginal cost ≈ electricity + disk |
| Proxmox VM (GPU, RTX 5070 passthrough) | One-time hardware cost; ongoing electricity |
| CloudFlare DNS-01 + CDN | Free tier for typical sites |
| Postgres / Redis / MinIO | Self-hosted on the service VM; no SaaS fees |
| Stripe | 2.9% + 30¢ per US card txn |
| Resend | Free tier covers most sites; ~$20/mo at scale |
| Discord webhook | Free |
| Sentry (errors) | Optional; free tier covers most sites |

The expensive thing is **human ops time**, not infra. The agent-cloud preset is optimised to minimise that — every site lands at the same shape, troubleshooting one site teaches you to troubleshoot all of them.

## Watch-outs

- **Secrets discipline.** No literal credentials in code, compose files, or templates. Everything is `{{ }}` references resolved by Ansible. CI's credential pattern audit will fail builds that violate this.
- **No `version:` key in `compose.yml`.** Podman-compose < 1.3.0 dislikes it. Compose-spec deprecated it anyway.
- **Fully-qualified image names.** Podman requires `docker.io/library/postgres:16-alpine`, not bare `postgres:16-alpine`. See [`PODMAN-VS-DOCKER-COMPOSE.md`](../../../../../plan/architecture/PODMAN-VS-DOCKER-COMPOSE.md).
- **`depends_on: condition: service_healthy`** requires podman-compose ≥ 1.3.0. Stage container start manually in deploy.sh if you can't guarantee that.
- **`deploy.sh` is lifecycle only.** No secret generation, no migrations, no API calls. Anything that's not "podman compose up + wait healthy" goes in `post-deploy.sh`.
- **Caddy fragment, not main Caddyfile edits.** Each site lives at `sites/<sitename>.caddy`, distributed by `tasks/distribute-caddy-site.yml`. The main Caddyfile gets one `import` line and otherwise stays out of your way.
- **GPU services need a §1 decision before provisioning.** See `plan/development/UHHCRAFT-GPU-PASSTHROUGH.md` §1 — the path through passthrough setup is different depending on whether the host is already in the Proxmox cluster.
- **SPEC deviations get recorded.** Any departure from this preset must be captured in `platform/services/<sitename>/context/spec/SPEC.md` under `## Alignment with agent-cloud conventions` or `## Tracking future deviations`. No silent drift.

## Customization points

The agent-cloud preset is opinionated about *infra*, agnostic about *product*. You can freely customize:

- **Web framework / language** — pick from the language presets.
- **CSS / design system** — Phase 4 (Style) is yours to drive.
- **Page inventory + routing** — Phase 2 (Template) decides this; no platform constraint.
- **Auth model** — sessions, JWT, OAuth, Hydra-mediated SSO. agent-cloud has Hydra deployed if you want federated identity.
- **Background workers** — language-native is fine (River, Sidekiq, Celery, …).
- **Browser-side stack** — HTMX + Alpine.js, React, Svelte, vanilla. Pick what matches your team and your Phase 4 style.

What you **don't** customize without a recorded deviation:

- Database engine (Postgres).
- Container runtime (Podman; NetBox is the only Docker exception).
- Secrets backbone (OpenBao + Ansible-templated `.env`).
- Deploy orchestrator (Semaphore + composable Ansible).
- Reverse proxy (central Caddy).
- Generated code policy (gitignored, CI-only).

## Pair with

- **Language presets** — overlay one of `go-templ-htmx.md`, `nextjs-typescript.md`, `rails.md`, `django.md`, `sveltekit.md`. The agent-cloud preset says *where* and *how*; the language preset says *what*.
- **Domain presets** — `domains/ecommerce.md`, `domains/saas.md`, `domains/documentation.md`, `domains/marketing-landing.md` for archetype-specific add-ons.

## Reference implementation

[`platform/services/uhhcraft/`](../../../../../platform/services/uhhcraft/) is the canonical agent-cloud-preset site. Read its `deployment/`, `context/spec/SPEC.md` (including `## Alignment with agent-cloud conventions`), and the corresponding `platform/playbooks/deploy-uhhcraft.yml` for the full shape every future site should mirror.

The second-site recipe (what to copy verbatim, what to adjust) is at [`agents/websmith/context/architecture/integration-with-agent-cloud.md`](../../architecture/integration-with-agent-cloud.md).
