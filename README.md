# agent-cloud

Privacy-focused, open-source AI platform for startups and small business. Customizable, scalable, extensible, and performant.

**agent-cloud** is the unified platform monorepo for [uhstray-io](https://github.com/uhstray-io) -- the single source of truth for service deployments, AI agent configurations, Ansible playbooks, and shared libraries.

## What is agent-cloud?

agent-cloud is an AI infrastructure platform that runs on the uhstray.io datacenter Proxmox cluster. It deploys and manages a set of interconnected services that enable AI agents to automate infrastructure operations, monitor networks, and interact with users -- all behind policy-enforced guardrails.

```
AI Layer         NemoClaw (headless), NetClaw (network), WisBot (Discord), Claude Cowork (interactive)
                 Backed by: WisAI -- Ollama worker nodes + Open WebUI coordinator
Guardrail Layer  OpenBao (secrets), Kyverno (k8s), OPA (policy), AppRole scoping
                 AI proposes -> guardrails validate -> automation runs
Automation Layer Ansible playbooks, Bash deploy scripts, Semaphore orchestration
                 Deterministic, idempotent, auditable
Platform Layer   Docker/Podman (dev), Kubernetes/k0s (prod), Proxmox VMs
```

## Getting Started

### Prerequisites

- A Proxmox cluster (or any Linux VMs with Docker/Podman)
- OpenBao deployed and initialized (see `platform/services/openbao/deployment/`)
- Ansible installed locally (for development) or Semaphore deployed (for production)
- A private `site-config` repository with your real IPs, inventory, and credentials

### Deploy a Service

Every service follows the same pattern. From the target VM:

```bash
git clone https://github.com/uhstray-io/agent-cloud.git ~/agent-cloud
cd ~/agent-cloud/platform/services/nocodb/deployment
bash deploy.sh
```

Or via Semaphore (production):
1. Push changes to this repo
2. Run the corresponding task template in Semaphore (e.g., "Deploy NocoDB")
3. Semaphore clones the repo, SSHes to the target VM, runs `deploy.sh`

### Composable Deploy Pattern

Deployments are orchestrated by Ansible via Semaphore. Each service follows the composable pattern defined in `plan/architecture/AUTOMATION-COMPOSABILITY.md`:

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
| **WisAI** | Local LLM inference backbone -- Ollama workers + Open WebUI coordinator (OpenAI-compatible API) |
| **UhhCraft** | First WebSmith-built site -- AI-designed sticker + 3D-print storefront (Go + templ + HTMX) |
| **inference-comfyui** | Image-generation sidecar -- Flux.1 Schnell behind a FastAPI wrapper, for UhhCraft and future generative sites |
| **inference-hunyuan3d** | 3D mesh-generation sidecar -- Hunyuan3D-2-mini behind a FastAPI wrapper |

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
      caddy/              Reverse proxy
      inference-ollama/   WisAI worker nodes (GPU, Ollama)
      inference-webui/    WisAI coordinator (Open WebUI + Postgres)
      inference-vllm/     Reserved (future 24 GB+ hardware)
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

See `platform/playbooks/README.md` and `plan/architecture/AUTOMATION-COMPOSABILITY.md` for architecture details.

## Architecture Documentation

Detailed architecture and planning documents live in `plan/`:

| Document | Purpose |
|----------|---------|
| `plan/architecture/AUTOMATION-COMPOSABILITY.md` | Composable deployment architecture and task library |
| `plan/architecture/SERVICE-INTEGRATION-PLAN.md` | Service onboarding checklist and integration touchpoints |
| `plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md` | Secret generation, storage, rotation, and retirement |
| `plan/architecture/TESTING-AND-LINTING-PLAN.md` | CI/CD testing strategy and implementation status |
| `plan/architecture/BRANCH-TESTING-WORKFLOW.md` | Branch deploy and validation workflow |
| `plan/architecture/CADDY-REVERSE-PROXY.md` | Caddy reverse proxy -- TLS/DNS-01, traffic flow, routing patterns, automation gaps |
| `plan/architecture/skills-recommendation.md` | Claude Code skills for development workflows |
| `plan/development/IMPLEMENTATION_PLAN.md` | Full implementation plan (phases, architecture, decisions) |
| `plan/development/NETBOX-DISCOVERY-EXPANSION.md` | Discovery pipeline architecture (Proxmox, pfSense, SNMP, LLDP) |

For new services, start with `plan/architecture/SERVICE-INTEGRATION-PLAN.md`. For new features, create an implementation plan in `plan/development/` before coding begins.

## CI/CD and Testing

Every pull request to main runs three automated checks:

| Job | Tools | What it catches |
|-----|-------|-----------------|
| **Static Analysis** | Ruff, ShellCheck, ansible-lint, yamllint, hadolint, terraform fmt | Code style, bugs, Ansible best practices, YAML formatting, Dockerfile issues, HCL policy formatting |
| **Security Scan** | TruffleHog, Bandit, IP/credential grep | Leaked secrets, Python security issues, hardcoded IPs and credentials |
| **Unit Tests** | pytest (79 tests), BATS (36 tests) | Discovery worker logic, bash helper functions |

Branch testing via Semaphore allows deploying feature branches to production VMs for validation before merging. See `plan/architecture/BRANCH-TESTING-WORKFLOW.md`.

For local setup and the full pre-PR checklist, see `docs/LINTING-AND-TESTING.md`.

## Technology Stack

```
INFRASTRUCTURE        Docker, Podman, Kubernetes (k0s), Proxmox
SECRETS & IDENTITY    OpenBao, AppRole auth, per-service SSH keys
DEPLOYMENT & GITOPS   Semaphore, Ansible, ArgoCD (planned)
DATA                  PostgreSQL, MinIO, DuckDB, NocoDB
AI AGENTS             NemoClaw, NetClaw, Claude Cowork, WisBot
INFERENCE             WisAI (Ollama workers + Open WebUI, OpenAI-compat)
AGENT PROTOCOLS       A2A (agent-to-agent), MCP (agent-to-tool)
OBSERVABILITY         Grafana, Prometheus, Loki, Tempo (planned)
```

## Related Repositories

| Repo | Visibility | Purpose |
|------|-----------|---------|
| [uhstray-io/agent-cloud](https://github.com/uhstray-io/agent-cloud) | Public | This repo -- platform monorepo |
| [uhstray-io/WisBot](https://github.com/uhstray-io/WisBot) | Public | Discord bot (C#/.NET) |
| [uhstray-io/WisAI](https://github.com/uhstray-io/WisAI) | Public | Upstream inference stack (Ollama + Open WebUI) — integrated as the platform inference backbone via `platform/services/inference-ollama/` + `platform/services/inference-webui/` |

## Contributing

- [Code of Conduct](https://www.uhstray.io/en/code-of-conduct)
- [CONTRIBUTING.md](CONTRIBUTING.md)
