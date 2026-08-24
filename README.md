# agent-cloud

Privacy-focused, open-source AI platform for startups and small business. Customizable, scalable, extensible, and performant.

**agent-cloud** is the unified platform monorepo for [uhstray-io](https://github.com/uhstray-io) -- the single source of truth for service deployments, AI agent configurations, Ansible playbooks, and shared libraries.

## Conventions

- **[AGENTS.md](AGENTS.md)** — canonical operating instructions for AI agents and
  human contributors (`CLAUDE.md` is a symlink to it; there is exactly one source
  of truth). Includes the memory/spec store routing.
- **[kickstart.md](kickstart.md)** — set up, run, and develop here.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — current-state architecture summary.
- **[plan/](plan/)** — architecture documents (`plan/architecture/`),
  implementation plans and the OpenSpec store (`plan/development/`), and
  product-inception artifacts (`plan/product/`).

## What is agent-cloud?

agent-cloud is an AI infrastructure platform that runs on the uhstray.io datacenter Proxmox cluster. It deploys and manages a set of interconnected services that enable AI agents to automate infrastructure operations, monitor networks, and interact with users -- all behind policy-enforced guardrails.

```
AI Layer         NemoClaw (headless), NetClaw (network), WisBot (Discord), Claude Cowork (interactive)
                 Backed by: skynet -- OpenAI-compatible /v1 gateway (placement scheduling + policy gates)
Guardrail Layer  OpenBao (secrets), Kyverno (k8s), OPA (policy), AppRole scoping
                 AI proposes -> guardrails validate -> automation runs
Automation Layer Ansible playbooks, Bash deploy scripts, Semaphore orchestration
                 Deterministic, idempotent, auditable
Platform Layer   Docker/Podman (dev), Kubernetes/k0s (prod), Proxmox VMs
```

## Getting Started

agent-cloud runs the **same way on your laptop and in production** — the same
Ansible playbooks, the same OpenBao credential flow. The fastest way to adopt it
is to run the whole platform locally first, then promote changes upstream.

### Quick start — run it locally

A local control plane (OpenBao + Semaphore) deploys every service with the same
playbooks as prod, behind real DNS + TLS and Authentik SSO. On macOS:

```bash
# prerequisites (one time)
brew bundle                          # toolchain: ansible, podman, podman-compose, jq, gh, ...
podman machine init && podman machine start

# stand up the whole stack + macOS DNS/TLS wiring (idempotent; asks for sudo once)
make local-all

# `make local-all` prints your SSO login at the end (re-show with `make local-creds`):
#   agent-cloud-admin  ->  full access to every app
# then open any app in the browser, e.g.:
#   https://semaphore.agent-cloud.test:8443

# deploy a single service through local Semaphore, exactly like prod:
make local-deploy-<name>             # e.g. make local-deploy-uhhcraft
make local-validate                  # health-check everything deployed
```

> **Full local guide → [LOCAL-DEV-README.md](LOCAL-DEV-README.md).** How to adopt
> and work with agent-cloud locally: the architecture, SSO logins and per-app
> access, clean port-free `:443` URLs, why a few steps need `sudo`, what runs
> locally today, and the **local-dev → production promotion pipeline**. Operate/
> triage in [`docs/LOCAL-DEV.md`](docs/LOCAL-DEV.md); full design in
> [`plan/development/00-foundation-local-dev.md`](plan/development/00-foundation-local-dev.md).

### Deploy to production

**Prerequisites:**

- A Proxmox cluster (or any Linux VMs with Docker/Podman)
- OpenBao deployed and initialized (see `platform/services/openbao/deployment/`)
- Semaphore deployed (production deploys go through Semaphore, never SSH-and-run)
- A private `site-config` repository with your real IPs, inventory, and credentials

**Every service deploys the same way — through Semaphore:**

1. Push changes to this repo
2. Run the corresponding task template in Semaphore (e.g., "Deploy NocoDB")
3. Semaphore injects OpenBao credentials, SSHes to the target VM, and runs the composable playbook (`manage-secrets` → `deploy.sh` → verify)

Production deploys always go through Semaphore so OpenBao credentials are injected and the run is auditable — never SSH into a VM and run `deploy.sh` directly.

### Composable Deploy Pattern

Deployments are orchestrated by Ansible via Semaphore. Each service follows the composable pattern defined in `plan/architecture/01-automation-model.md`:

1. **Manage secrets** -- Ansible fetches/generates credentials from OpenBao, templates `.env` files
2. **Start containers** -- deploy.sh handles Docker Compose lifecycle (pull, build, start)
3. **Configure application** -- post-deploy.sh runs migrations, creates users, registers OAuth2 clients
4. **Sync credentials** -- Ansible pushes any runtime-created credentials back to OpenBao
5. **Verify** -- Health check confirms the service is running

deploy.sh does NOT generate secrets or interact with OpenBao. All credential management is Ansible-driven.

## AI Agents

| Agent | Type | Role |
|-------|------|------|
| **NemoClaw** | Headless engineer | Background automation, API integrations, CI/CD, health monitoring |
| **NetClaw** | Network engineer | Network monitoring, topology discovery, config backup, security auditing |
| **Claude Cowork** | Interactive architect | Research, architecture decisions, document generation |
| **WebSmith** | Website builder | Prompt-only agent — walks users through a 5-phase workflow to produce a signed `SPEC.md` for a new website service |
| **WisBot** | Community interface | Discord voice/chat bot with LLM-powered interactions |

## Platform Services

| Service | Purpose |
|---------|---------|
| **OpenBao** | Secrets management -- KV v2, AppRole auth, database engine |
| **NocoDB** | Shared data layer -- structured tables, REST API, task queue |
| **n8n** | Workflow automation -- event-driven scheduling, webhooks, LLM nodes |
| **Semaphore** | Deployment orchestration -- Ansible playbook execution |
| **NetBox** | Infrastructure modeling -- IPAM/DCIM with Diode auto-discovery |
| **Caddy** | Reverse proxy -- automatic TLS, CloudFlare DNS integration |
| **DNS** | Internal name resolution -- hickory-dns, zones-as-code, authoritative + forward (local-dev live; prod planned) |
| **step-ca** | Internal CA -- stable root, issues the `*.agent-cloud.test` wildcard Caddy serves (local-dev live; prod via ACME) |
| **Authentik** | Central identity / SSO -- one login for every app: OIDC (Semaphore/Grafana/ERPNext) + Caddy forward_auth (NetBox/OpenBao/n8n), with `platform-admins`/`developers`/`user` RBAC tiers (local-dev live) |
| **skynet** | Local-first, policy-gated LLM inference backbone -- OpenAI-compatible `/v1` gateway with multi-backend placement scheduling + policy gates (supersedes WisAI's Ollama + Open WebUI LLM plane) |
| **UhhCraft** | First WebSmith-built site -- AI-designed sticker + 3D-print storefront (Go + templ + HTMX) |
| **inference-comfyui** | Image-generation sidecar -- Flux.1 Schnell behind a FastAPI wrapper, for UhhCraft and future generative sites |
| **inference-hunyuan3d** | 3D mesh-generation sidecar -- Hunyuan3D-2-mini behind a FastAPI wrapper |
| **tududi** | Self-hosted to-do app -- single rootless container (SQLite), native Authentik OIDC, `todo.uhstray.io`; the migration sink for NocoDB work data via weft (local-dev live) |
| **honcho** | Memory API for agents (Plastic Labs) -- api + deriver + pgvector + redis, JWT `/v3`, Authentik-gated `/docs`, `memory.uhstray.io`; evolve's team-memory backend (local-dev live) |
| **Postiz** | Social-media scheduling and publishing -- app + its Postgres/Redis + a Temporal workflow engine that executes scheduled posts, native Authentik OIDC, `postiz.uhstray.io`; driven by n8n over an API-key endpoint deliberately left ungated at the edge (code-complete, first bring-up pending) |
| **github-runner** | Self-hosted GitHub Actions runners -- two hosts forming one interchangeable pool, org-scoped to the five PRIVATE repos (`agent-cloud` excluded: it is public, and a fork can propose workflow code onto hosts inside the perimeter). For workflows that must originate from inside the network or its stable address (both live, serving jobs) |

## Repository Structure

```
agent-cloud/
  platform/
    services/             Per-service: deployment/ + context/ + templates/
      openbao/            Secrets backbone (AppRole, KV v2, policies)
      nocodb/             Data layer
      n8n/                Workflow automation
      semaphore/          Deployment orchestration
      netbox/             Infrastructure modeling + Diode discovery + Orb Agent
      dns/                hickory-dns internal resolution (zones-as-code)
      step-ca/            Internal CA (Smallstep; stable root, *.agent-cloud.test)
      caddy/              Reverse proxy
      authentik/          Central IdP / SSO (server+worker+Postgres+Redis)
      inference-ollama/   Legacy WisAI workers (GPU, Ollama) -- superseded by skynet /v1
      inference-webui/    Legacy WisAI coordinator (Open WebUI + Postgres) -- superseded by skynet /v1
      inference-vllm/     Reserved (future 24 GB+ hardware; candidate skynet backend)
      inference-comfyui/  UhhCraft image-gen sidecar (Flux.1, GPU)
      inference-hunyuan3d/ UhhCraft 3D-gen sidecar (Hunyuan3D, GPU)
      uhhcraft/           First WebSmith-built site (Go + templ + HTMX)
    playbooks/            Ansible playbooks (see playbooks/README.md)
      tasks/              Composable tasks (manage-secrets, deploy-orb-agent, etc.)
    semaphore/            Semaphore template definitions + setup playbook
    lib/                  Shared bash libraries (common.sh, bao-client.sh)
    inventory/            Inventory templates (placeholders, no real IPs)
    hypervisor/proxmox/   VM provisioning and cloud-init
    k8s/                  Kubernetes manifests (Kustomize overlays)
  agents/
    nemoclaw/             Headless workflow agent
    netclaw/              Network engineering agent
    cowork/               Interactive architect agent
    websmith/             Website-building agent (prompt-only; produces SPEC.md)
  plan/
    architecture/         Architecture plans (automation, testing, service integration)
    development/          Development plans (discovery, deployment, migration)
  docs/                   Developer guides (linting, testing, onboarding)
  .github/
    workflows/            CI pipeline (lint, security, test)
    dependabot.yml        Dependency scanning config
```

Each service directory uses the **deployment/ + context/** split:
- **deployment/** -- compose files, deploy.sh, config, Dockerfile (how to run it)
- **context/** -- skills, use-cases, prompts, architecture docs (how AI agents interact with it)

## Credential Flow

All secrets are managed by **OpenBao**. Services authenticate via AppRole at runtime -- no credentials are stored in environment files or committed to this repository:

```
Semaphore environment (AppRole role-id + secret-id only)
  -> playbook starts
  -> community.hashi_vault lookup
  -> OpenBao AppRole auth -> scoped token -> fetch secrets
  -> Ansible manage-secrets.yml templates .env files
  -> deploy.sh starts containers (reads .env, no OpenBao interaction)
```

Private configuration (real IPs, production inventory, credential backups) lives in the separate **site-config** repository.

## Automation

Deployments are orchestrated by **Semaphore** running composable Ansible playbooks. Each concern is an independent workflow:

| Workflow | Purpose |
|----------|---------|
| `deploy-<service>.yml` | Full deploy: secrets → containers → app config → verify |
| `deploy-orb-agent.yml` | Standalone: Diode credentials + orb-agent for NetBox |
| `clean-deploy-<service>.yml` | Destructive rebuild: wipe volumes + fresh deploy |
| `distribute-ssh-keys.yml` | Deploy SSH keys from OpenBao to VMs |
| `harden-ssh.yml` | Lock down sshd (after key verification) |
| `check-secrets.yml` | Read-only secret inventory from OpenBao |

Playbooks use composable tasks from `platform/playbooks/tasks/` (manage-secrets, manage-diode-credentials, manage-approle, etc.). Semaphore templates are managed as code in `platform/semaphore/templates.yml`.

See `platform/playbooks/README.md` and `plan/architecture/01-automation-model.md` for architecture details.

## Architecture Documentation

Detailed architecture and planning documents live in `plan/`:

Start with [`ARCHITECTURE.md`](ARCHITECTURE.md) (the 5-minute map) and [`PRINCIPLES.md`](PRINCIPLES.md) (the durable constitution). The per-area depth lives under `plan/architecture/` (the numbered `00–07` HOW) and `plan/development/` (the service-by-service roadmap):

| Document | Purpose |
|----------|---------|
| `plan/architecture/00-foundation-standards.md` | Doc standards, status values, and the master architecture index |
| `plan/architecture/01-automation-model.md` | Composable deployment architecture and task library |
| `plan/architecture/02-service-onboarding.md` | Service onboarding checklist and integration touchpoints |
| `plan/architecture/03-testing-ci-quality.md` | CI/CD testing strategy, security gates, and branch-deploy validation workflow |
| `plan/architecture/04-credentials-access.md` | Secret generation, storage, rotation, retirement; Semaphore vs SSH access |
| `plan/architecture/05-platform-infra.md` | Caddy reverse proxy (TLS/DNS-01, routing), container runtime, platform infra |
| `plan/architecture/06-observability-instrumentation.md` | Observability-by-declaration model (metrics/logs/traces) |
| `plan/architecture/skills-recommendation.md` | Claude Code skills for development workflows |
| `plan/development/` | Numbered `00–13` roadmap: local-dev, secrets, SSO, guardrails, NetBox discovery, observability, skynet, websmith, ERPNext, migrations, resilience, tududi/honcho, RBAC, Cloudflare IaC |
| `plan/archive/development/IMPLEMENTATION_PLAN.md` | Original full implementation plan (archived; phases, architecture, decisions) |

For new services, start with `plan/architecture/02-service-onboarding.md`. For new features, create an implementation plan in `plan/development/` before coding begins.

## CI/CD and Testing

Every pull request runs these automated checks. The three below are the gates; a PR also
runs change detection, CodeQL analysis per language, conditional Go jobs for uhhcraft, a
CodeRabbit review, and — on a `dev` → `main` PR — a promotion-source check:

| Job | Tools | What it catches |
|-----|-------|-----------------|
| **Static Analysis** | Ruff, ShellCheck, ansible-lint, yamllint, hadolint, terraform fmt | Code style, bugs, Ansible best practices, YAML formatting, Dockerfile issues, HCL policy formatting |
| **Security Scan** | TruffleHog, Bandit, IP/credential grep | Leaked secrets, Python security issues, hardcoded IPs and credentials |
| **Unit Tests** | pytest (79 tests), BATS (452 tests) | Discovery worker logic, bash helpers, per-service deployment structure |

`.githooks/pre-push` **attempts** the same suites before a push, invoking them exactly as
CI does, and refuses the push if one runs and fails. Live via the repo's `core.hooksPath`
after `make git-setup`, no install step.

It is a convenience, not a guarantee — CI remains the authority. It skips, with a message,
when `SKIP_TESTS=1` is set, when `bats` is not installed, when the Python suite is not
collectable (pytest or a test dependency missing), and on a branch-deletion push. That
fail-open behaviour is deliberate and the opposite of the pre-commit secret gate, which
fails closed: a leaked secret is irreversible, a skipped test is not.

Branch testing via Semaphore allows deploying feature branches to production VMs for validation before merging. See `plan/architecture/03-testing-ci-quality.md`.

`main` is protected by the `protect-main` repository ruleset (config-as-code in `.github/rulesets/`): no direct or force pushes, no deletion, PR required, review conversations resolved, and the three checks above must pass before the merge button unlocks. Merges into `main` allow **merge commits (the default) or squash** — linear history is NOT required, so `dev` → `main` promotions land as merge commits (squash only to scrub accidental sensitive content). (The ruleset currently runs in `evaluate`/dry-run — logging, not yet blocking — and flips to `active` after verification.) See `plan/development/03-guardrails-governance.md`.

For local setup and the full pre-PR checklist, see `plan/architecture/03-testing-ci-quality.md`.

## Technology Stack

```
INFRASTRUCTURE        Docker, Podman, Kubernetes (k0s), Proxmox
SECRETS & IDENTITY    OpenBao, AppRole auth, per-service SSH keys
DEPLOYMENT & GITOPS   Semaphore, Ansible, OpenTofu (Cloudflare edge as code), ArgoCD (planned)
DATA                  PostgreSQL, MinIO, DuckDB, NocoDB
AI AGENTS             NemoClaw, NetClaw, Claude Cowork, WisBot
INFERENCE             skynet (OpenAI-compatible /v1; placement + policy gates)
AGENT PROTOCOLS       A2A (agent-to-agent), MCP (agent-to-tool)
OBSERVABILITY         Grafana, Prometheus, Loki, Tempo (planned)
```

## Related Repositories

| Repo | Visibility | Purpose |
|------|-----------|---------|
| [uhstray-io/agent-cloud](https://github.com/uhstray-io/agent-cloud) | Public | This repo -- platform monorepo |
| [uhstray-io/WisBot](https://github.com/uhstray-io/WisBot) | Public | Discord bot (C#/.NET) |
| [uhstray-io/skynet](https://github.com/uhstray-io/skynet) | Private | Inference backbone + agent framework — OpenAI-compatible `/v1` gateway (placement + policy gates); supersedes WisAI's LLM plane and the NemoClaw/OpenClaw framework |
| [uhstray-io/WisAI](https://github.com/uhstray-io/WisAI) | Public | **Legacy** — Ollama + Open WebUI LLM plane (dirs `inference-ollama/` + `inference-webui/`), superseded by skynet `/v1`; non-LLM inference sidecars (ComfyUI, Hunyuan3D) are unaffected |

## Contributing

- [Code of Conduct](https://www.uhstray.io/en/code-of-conduct)
- [CONTRIBUTING.md](CONTRIBUTING.md)
