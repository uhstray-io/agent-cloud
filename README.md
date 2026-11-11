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

The four-layer architecture keeps AI proposals upstream of guardrails and
automation. See the [four-layer architecture](ARCHITECTURE.md#four-layer-architecture)
for the full model.

## Getting Started

agent-cloud runs the **same way on your laptop and in production** — the same
Ansible playbooks, the same OpenBao credential flow. The fastest way to adopt it
is to run the whole platform locally first, then promote changes upstream.

### Quick start — run it locally

A local control plane (OpenBao + Semaphore) deploys supported service profiles
behind DNS, TLS and Authentik. Start with the foundation on macOS:

```bash
# prerequisites (one time)
brew bundle                          # toolchain: ansible, podman, podman-compose, jq, gh, ...
podman machine init && podman machine start

# stand up the secure foundation, then wire macOS DNS/TLS
make local-bootstrap
make local-dns-resolver
make local-tls-trust

# show the local SSO login (this prints local credentials):
make local-creds
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
2. Run the corresponding task template in Semaphore (e.g., "Deploy UhhCraft")
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

For current service purposes, status, and owning records, see the
[service architecture catalog](plan/architecture/service-architecture.md#2-service-catalog).
It is the single catalog; verify the selected environment before relying on live
health.
## Repository Structure

The tree below shows representative paths; it is not an exhaustive inventory.

```
agent-cloud/
  platform/
    services/             Per-service: deployment/ + context/ + templates/
      openbao/            Secrets backbone (AppRole, KV v2, policies)
      nocodb/             Retired (replaced by tududi, itself retired 2026-11-09); kept until its decommission
      n8n/                Workflow automation
      semaphore/          Deployment orchestration
      netbox/             Infrastructure modeling + Diode discovery + Orb Agent
      dns/                hickory-dns internal resolution (zones-as-code; production VM provisioned, deploy pending)
      step-ca/            Internal CA (Smallstep; production has no ACME provisioner)
      caddy/              Reverse proxy
      authentik/          Central IdP / SSO (server+worker+Postgres+Redis)
      inference/          README only, no deployment
      inference-comfyui/  UhhCraft image-gen sidecar (Flux.1, GPU)
      inference-hunyuan3d/ UhhCraft 3D-gen sidecar (Hunyuan3D, GPU)
      uhhcraft/           First WebSmith-built site (Go + templ + HTMX)
      tududi/             Retired 2026-11-09 (was the to-do app; Plane replaces it)
      honcho/             Memory API (api + deriver + pgvector + redis)
      o11y/               Live production observability: Grafana + Prometheus + Loki + Alloy
      postiz/             Social publishing (app + Temporal workflow engine)
      agentgateway/       Inference edge gateway (per-client keys, token budgets)
      github-runner/      Self-hosted GitHub Actions runners
      opa/                Policy engine (Rego policy-as-code)
      openhands/          Agent Canvas
      erpnext/            ERP (composable slim local tier)
      wikijs/ nextcloud/ a2a-registry/   Further service directories (see each one's docs)
    playbooks/            Ansible playbooks (see playbooks/README.md)
      tasks/              Composable tasks (manage-secrets, deploy-orb-agent, etc.)
    semaphore/            Semaphore template definitions + setup playbook
    workflows/
      service-onboarding/ Service deployment workflow: step registry, step-result and proposal schemas,
                          NetBox custom fields, the collector's step-result parser
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

All secrets are managed by **OpenBao**. Ansible authenticates with AppRole at
deploy time and renders credential-bearing env/config files for services. These
generated, gitignored files are a runtime bridge, not the source of truth. Only
specific consumers, such as Orb Agent, use scoped runtime OpenBao access:

```
Semaphore environment (AppRole role-id + secret-id only)
  -> playbook starts
  -> community.hashi_vault lookup
  -> OpenBao AppRole auth -> scoped token -> fetch secrets
  -> Ansible manage-secrets.yml templates .env files
  -> deploy.sh starts containers (reads .env, no OpenBao interaction)
```

A secret only an operator holds (a third-party API key) enters OpenBao the other way: a
seed CLI stages it as an encrypted input in that seed template's own isolated Semaphore
environment, runs one task that merges it into OpenBao, then removes the input. It is never
a survey value or launch-time extra var, both of which Semaphore persists. See
[the Semaphore operating guide](platform/semaphore/README.md).

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
| `ensure-service-persistence.yml` | Make a service's containers start at boot (linger + podman's boot unit, the system unit for rootful podman, or a per-service boot unit for legacy containers); restarts nothing |
| `verify-service-persistence.yml` / `verify-service-health.yml` | Read-only proof a service starts at boot and answers its declared health path |
| `inspect-host-containers.yml` | Read-only list of the containers on one host that the connecting user, a declared `linger_user`, root and Docker can see, with engine version, state and restart policy |

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
| `plan/development/` | Numbered roadmap: local-dev, secrets, SSO, guardrails, NetBox discovery, observability, skynet, websmith, ERPNext, migrations, resilience, tududi (retired 2026-11-09) and honcho, RBAC, Cloudflare IaC, Postiz |
| `plan/archive/development/IMPLEMENTATION_PLAN.md` | Original full implementation plan (archived; phases, architecture, decisions) |

For new services, start with `plan/architecture/02-service-onboarding.md`. For new features, create an implementation plan in `plan/development/` before coding begins.

## CI/CD and Testing

Every pull request runs these automated checks. The three below are the gates; a PR also
runs change detection, conditional Go jobs for uhhcraft, a
CodeRabbit review, and — on a `dev` → `main` PR — a promotion-source check:

| Job | Tools | What it catches |
|-----|-------|-----------------|
| **Static Analysis** | Ruff, ShellCheck, ansible-lint, yamllint, hadolint, terraform fmt | Code style, bugs, Ansible best practices, YAML formatting, Dockerfile issues, HCL policy formatting |
| **Security Scan** | TruffleHog, Bandit, IP/credential grep | Leaked secrets, Python security issues, hardcoded IPs and credentials |
| **Unit Tests** | pytest (`pytest --collect-only` for the current count), BATS (`bats -c platform/tests/*.bats` for the current count) | Discovery worker logic, bash helpers, per-service deployment structure |

`.githooks/pre-push` **attempts** the same suites before a push, with the same test paths,
working directory and `PYTHONPATH` as CI. Live via the repo's `core.hooksPath` after
`make git-setup`, no install step. `bats platform/tests/` runs the same files as CI, serially;
CI runs them with `--jobs 4 --no-parallelize-within-files`. Both
suites run on the locked environment when `uv` is installed: the hook runs `uv sync --locked`
and puts `.venv/bin` first on `PATH`, as CI does. Without uv, when the sync fails, or when
`UV_PROJECT_ENVIRONMENT` points outside the checkout's `.venv`, it falls back, with a
warning, to whatever `python3` and tools are on your `PATH`.

**Two different gates, on two different things.** The hook blocks *your push* when a suite
runs and fails; CI blocks *the merge*. A green push means the suites passed on your machine
or were skipped, which is not evidence CI will pass. It skips, with a message, on
`SKIP_TESTS=1`, when `bats` is absent, when the Python suite is not collectable, and on a
branch-deletion push — failing open deliberately, unlike the pre-commit secret gate, which
fails closed because a leaked secret is irreversible and a skipped test is not.

Branch testing via Semaphore allows deploying feature branches to production VMs for validation before merging. See `plan/architecture/03-testing-ci-quality.md`.

`main` is protected by the `protect-main` repository ruleset (config-as-code in `.github/rulesets/`): no direct or force pushes, no deletion, PR required, review conversations resolved, and the three checks above must pass before the merge button unlocks. Merges into `main` allow **merge commits (the default) or squash** — linear history is NOT required, so `dev` → `main` promotions land as merge commits (squash only to scrub accidental sensitive content). (The checked-in ruleset declares `active`; this documentation review did not query remote enforcement.) See `plan/development/03-guardrails-governance.md`.

### Versioning

agent-cloud follows [Semantic Versioning](https://semver.org/) and is in its 0.x line. The pull
request title sets the bump, in Conventional Commits form (`type(scope)!: description`): `feat`
is a minor bump; `fix`, `perf`, `security` and `revert` are a patch; `docs`, `chore`, `test`,
`ci`, `refactor`, `build`, `style`, `plan` and `promote` add none. A `!` marks a breaking change,
which bumps the minor while on 0.x. The `Version` job on every pull request fails a title not in
that form and shows the version the merge would produce. A merge into `dev` is tagged
`vX.Y.Z-dev.N`; a promotion into `main` is tagged `vX.Y.Z` with a GitHub Release. Semaphore's
`Release Version (Dev)` template runs the tagging every 15 minutes, so a merge is tagged within
15 minutes rather than at once. 1.0.0, and every later major version, is cut only when the
operator launches `Release Major Version (Dev)` with the exact major version expected. History before
this was backfilled from the promotions: `main` was `v0.13.0` when versioning began. See
`plan/development/20-semantic-versioning.md`.

For local setup and the full pre-PR checklist, see `plan/architecture/03-testing-ci-quality.md`.

## Technology Stack

```text
INFRASTRUCTURE        Docker, Podman, Proxmox; Kubernetes (k0s) planned
SECRETS & IDENTITY    OpenBao, AppRole auth, per-service SSH keys
DEPLOYMENT & GITOPS   Semaphore, Ansible, OpenTofu (Cloudflare edge as code), ArgoCD (planned)
DATA                  PostgreSQL, SQLite, MinIO, DuckDB
AI AGENTS             NemoClaw, NetClaw, Claude Cowork, WisBot
INFERENCE             agentgateway inference edge (/v1); skynet orchestration API calls through it (deployed in production; Semaphore launch rights are guest-only since 2026-10-31)
AGENT PROTOCOLS       A2A (agent-to-agent), MCP (agent-to-tool)
OBSERVABILITY         Grafana, Prometheus, Loki, Tempo (planned)
```

Production DGX Spark scrape targets are rendered from private inventory by
[`deploy-o11y.yml`](platform/playbooks/deploy-o11y.yml); see the
[`o11y deployment notes`](platform/services/o11y/deployment/README.md).

## Related Repositories

| Repo | Visibility | Purpose |
|------|-----------|---------|
| [uhstray-io/agent-cloud](https://github.com/uhstray-io/agent-cloud) | Public | This repo -- platform monorepo |
| [uhstray-io/WisBot](https://github.com/uhstray-io/WisBot) | Public | Discord bot (C#/.NET) |
| [uhstray-io/skynet](https://github.com/uhstray-io/skynet) | Private | Orchestration API (deployed in production 2026-10-30), which asks OPA for each decision and calls models through the agentgateway inference edge; its Semaphore launch rights are guest-only since 2026-10-31; supersedes WisAI's LLM plane and the NemoClaw/OpenClaw framework |
| [uhstray-io/WisAI](https://github.com/uhstray-io/WisAI) | Public | **Legacy** — Ollama + Open WebUI LLM plane (its compose stack is in that repo's `infrastructure/`), superseded by skynet (production `/v1` is the agentgateway inference edge, in front of vLLM); non-LLM inference sidecars (ComfyUI, Hunyuan3D) are unaffected |

## Contributing

- [Code of Conduct](https://www.uhstray.io/en/code-of-conduct)
- [CONTRIBUTING.md](CONTRIBUTING.md)
