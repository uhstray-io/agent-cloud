# agent-cloud

Privacy-focused, open-source AI platform for startups and small business. Customizable, scalable, extensible, and performant.

**agent-cloud** is the unified platform monorepo for [uhstray-io](https://github.com/uhstray-io) -- the single source of truth for service deployments, AI agent configurations, Ansible playbooks, and shared libraries.

## What is agent-cloud?

agent-cloud is an AI infrastructure platform that runs on a homelab Proxmox cluster. It deploys and manages a set of interconnected services that enable AI agents to automate infrastructure operations, monitor networks, and interact with users -- all behind policy-enforced guardrails.

```
AI Layer         NemoClaw (headless), NetClaw (network), WisBot (Discord), Claude Cowork (interactive)
                 Backed by: vLLM + llama.cpp (local LLM inference)
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

### Deploy Pattern

All services are idempotent -- safe to re-run:

1. **Generate secrets** from `secrets/` directory (creates if missing, reuses if existing)
2. **Start containers** via Docker/Podman Compose
3. **Bootstrap credentials** (programmatic API token creation)
4. **Store in OpenBao** (secrets backbone for cross-service access)
5. **Validate** via health check

## AI Agents

| Agent | Type | Role |
|-------|------|------|
| **NemoClaw** | Headless engineer | Background automation, API integrations, CI/CD, health monitoring |
| **NetClaw** | Network engineer | Network monitoring, topology discovery, config backup, security auditing |
| **Claude Cowork** | Interactive architect | Research, architecture decisions, document generation |
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
| **vLLM + llama.cpp** | Local LLM inference -- GPU-heavy and lightweight engines |

## Repository Structure

```
agent-cloud/
  platform/
    services/             Per-service: deployment/ + context/
      openbao/            Secrets backbone
      nocodb/             Data layer
      n8n/                Workflow automation
      semaphore/          Deployment orchestration
      netbox/             Infrastructure modeling
      caddy/              Reverse proxy
      inference/          LLM inference (planned)
    playbooks/            Ansible playbooks (see playbooks/README.md)
    lib/                  Shared bash libraries (common.sh, bao-client.sh)
    inventory/            Inventory templates (placeholders, no real IPs)
    hypervisor/proxmox/   VM provisioning and cloud-init
    k8s/                  Kubernetes manifests (Kustomize overlays)
  agents/
    nemoclaw/             Headless workflow agent
    netclaw/              Network engineering agent
    cowork/               Interactive architect agent
  plan/                   Architecture and implementation plans
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
  -> deploy.sh generates runtime env from OpenBao
  -> compose up -d
```

Private configuration (real IPs, production inventory, credential backups) lives in the separate **site-config** repository.

## Automation

Deployments are orchestrated by **Semaphore** running Ansible playbooks from this repo:

- **Deploy playbooks**: `deploy-<service>.yml` -- clone repo, run deploy.sh, health check
- **Update playbooks**: `update-<service>.yml` -- pull images, restart, health check
- **SSH hardening**: `distribute-ssh-keys.yml` + `harden-ssh.yml` -- key distribution and sshd lockdown
- **Provisioning**: `provision-vm.yml` -- clone Proxmox template, configure cloud-init

See `platform/playbooks/README.md` for conventions and full playbook reference.

## Technology Stack

```
INFRASTRUCTURE        Docker, Podman, Kubernetes (k0s), Proxmox
SECRETS & IDENTITY    OpenBao, AppRole auth, per-service SSH keys
DEPLOYMENT & GITOPS   Semaphore, Ansible, ArgoCD (planned)
DATA                  PostgreSQL, MinIO, DuckDB, NocoDB
AI AGENTS             NemoClaw, NetClaw, Claude Cowork, WisBot
INFERENCE             vLLM (GPU), llama.cpp (lightweight)
AGENT PROTOCOLS       A2A (agent-to-agent), MCP (agent-to-tool)
OBSERVABILITY         Grafana, Prometheus, Loki, Tempo (planned)
```

## Related Repositories

| Repo | Visibility | Purpose |
|------|-----------|---------|
| [uhstray-io/agent-cloud](https://github.com/uhstray-io/agent-cloud) | Public | This repo -- platform monorepo |
| [uhstray-io/WisBot](https://github.com/uhstray-io/WisBot) | Public | Discord bot (C#/.NET) |
| [uhstray-io/WisAI](https://github.com/uhstray-io/WisAI) | Public | Personal LLM stack (Ollama + Open WebUI) |

## Contributing

- [Code of Conduct](https://www.uhstray.io/en/code-of-conduct)
- [CONTRIBUTING.md](CONTRIBUTING.md)
