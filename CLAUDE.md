# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

**agent-cloud** is the unified platform monorepo for the uhstray-io privacy-focused AI platform. It consolidates service deployments, AI agent configurations, Ansible playbooks, Kubernetes manifests, and shared libraries into a single public repository.

Private configuration (real IPs, credentials, production inventory) lives in the separate **site-config** repository. This repo contains only templates, placeholders, and code.

## Architecture

### Four-Layer Guardrails Model

```
AI Layer         NemoClaw (headless), NetClaw (network), WisBot (Discord), Claude Cowork (interactive)
                 Backed by: vLLM + llama.cpp (local inference, OpenAI-compatible API)

Guardrail Layer  OpenBao (secrets), Kyverno (k8s), OPA (policy), AppRole scoping
                 AI proposes -> guardrails validate -> automation executes

Automation Layer Ansible playbooks, Bash deploy scripts, Semaphore orchestration, n8n workflows
                 Deterministic, idempotent, auditable

Platform Layer   Docker/Podman (dev), Kubernetes/k0s (prod), Proxmox VMs
```

AI agents manage context and workloads. They do NOT execute infrastructure changes directly — all changes flow through the automation layer behind guardrails.

### Repository Structure

```
platform/
  services/<name>/
    deployment/              How to run it (compose, deploy.sh, config, secrets/)
    context/                 How AI agents interact with it (skills, use-cases, prompts)
  playbooks/                 Ansible orchestration (see playbooks/README.md)
  lib/                       Shared libraries (common.sh, bao-client.sh)
  inventory/                 Inventory templates (placeholders, no real IPs)
  hypervisor/proxmox/        VM provisioning and cloud-init
  k8s/                       Kubernetes manifests (Kustomize overlays)
  scripts/                   Setup and utility scripts

agents/<name>/
  deployment/                Agent-specific deploy (same 5-step pattern)
  context/                   Agent skills, MCP server configs, architecture docs

data/                        Data warehouse, lake, analytics (planned)
plan/                        Architecture and implementation plans
```

### Deployment / Context Split

Every service and agent has two subdirectories:
- **deployment/** — Runtime executable code. Compose files, deploy.sh, configuration, gitignored secrets. Developers and automation read this.
- **context/** — AI agent guidance. Skills, use-cases, prompts, architecture docs. AI agents read this, not deployment/.

### Sub-directory Documentation

These directories have their own detailed guidance:
- `platform/services/netbox/deployment/CLAUDE.md` — NetBox + Diode discovery pipeline (12 containers, OAuth2 stack)
- `agents/nemoclaw/deployment/CLAUDE.md` — NemoClaw agent deployment modes and configuration
- `platform/playbooks/README.md` — Playbook conventions, variable sources, wrapper pattern, new service checklist

Defer to those files when working within those directories.

## Critical Deployment Rules

1. **All deployments go through Semaphore.** Never SSH into a VM and run `deploy.sh` directly. Semaphore injects `OPENBAO_ADDR`, `BAO_ROLE_ID`, and `BAO_SECRET_ID` via its environment — without these, secrets are generated but never stored in OpenBao, creating unmanaged credentials.
2. **deploy.sh MUST fail if OpenBao is unreachable.** No service should run with credentials that aren't tracked in OpenBao. The `error()` function halts the deploy — it does not skip silently.
3. **If debugging requires a manual run**, pass OpenBao credentials explicitly: `OPENBAO_ADDR=http://... BAO_ROLE_ID=... BAO_SECRET_ID=... bash deploy.sh`

## Secrets Management

### OpenBao as Source of Truth

**OpenBao** manages all credentials at runtime. No secrets are stored in environment files or committed to this repository. Deploy scripts generate secrets locally, store them in OpenBao, and fetch them at runtime via AppRole.

### Three-Tier Variable Architecture

```
Tier 1: DEFAULTS (in repo, committed)
  .env.example, compose.yml defaults
  Example: NOCODB_PORT=8181

Tier 2: SITE CONFIG (private repo, per-environment)
  Inventory files, environment overrides, network definitions
  Example: NOCODB_HOST={{ nocodb_host }}

Tier 3: SECRETS (OpenBao, never on disk in steady state)
  Passwords, tokens, API keys
  Example: POSTGRES_PASSWORD, API_TOKEN
```

Resolution order: Tier 3 > Tier 2 > Tier 1 (secrets override everything).

### Credential Flow (Composable Pattern)

All new service deployments follow the composable pattern defined in `plan/AUTOMATION-COMPOSABILITY-PLAN.md`:

```
OpenBao (source of truth)
  ↑ generate + store (first deploy)
  ↓ fetch (subsequent deploys)
Ansible manage-secrets.yml
  ↓ Jinja2 template
env/*.env, .env, config files (on VM, compose-ready)
  ↓ read
deploy.sh (container operations only)
```

deploy.sh does NOT generate secrets or interact with OpenBao.
Ansible handles the full credential lifecycle:
- `manage-secrets.yml` — fetch/generate/store/template
- `check-secrets.yml` — read-only inventory of secrets in OpenBao
- `validate-secrets.yml` — active credential testing (DB, Redis, HTTP)
- `clean-service.yml` — destroy containers, volumes, clone for full rebuild

Legacy credential flow (older services not yet migrated):

```
Semaphore environment (AppRole role-id + secret-id only)
  -> Ansible playbook -> community.hashi_vault lookup
  -> OpenBao AppRole auth -> scoped token -> fetch secrets
  -> deploy.sh generates runtime .env from fetched secrets
  -> docker/podman compose up -d
```

### OpenBao Secrets Layout

| Path | Contents |
|------|----------|
| `secret/services/ssh` | Management SSH key (private + public) |
| `secret/services/ssh/<service>` | Per-service SSH key pairs |
| `secret/services/proxmox` | Proxmox API token, URL |
| `secret/services/nocodb` | NocoDB API token, URL, DB credentials |
| `secret/services/n8n` | n8n API key, URL |
| `secret/services/semaphore` | Semaphore API token, URL |
| `secret/services/netbox` | Superuser password, DB creds, URL |
| `secret/services/github` | GitHub PAT |
| `secret/services/discord` | Discord bot token |
| `secret/services/nemoclaw` | Agent API keys |

### AppRole Policies (Least Privilege)

| Role | Policy | Access |
|------|--------|--------|
| nemoclaw | nemoclaw-read | Read-only: `secret/services/*` |
| nocodb | nocodb-write | Read/write: `secret/services/nocodb` only |
| n8n | n8n-write | Read/write: `secret/services/n8n` only |
| semaphore | semaphore-write | Read/write: `secret/services/semaphore` only |
| semaphore (ansible) | semaphore-read | Read-only: `secret/services/*` (for playbooks) |

Policy files: `platform/services/openbao/deployment/config/policies/`

## Service Deployment

### 5-Step Idempotent Deploy Pattern

Every `deploy.sh` follows the same structure:
1. **Generate secrets** from `secrets/` directory (creates if missing, reuses if existing)
2. **Start containers** via Docker/Podman Compose
3. **Bootstrap credentials** programmatically (REST API token creation, no manual UI login)
4. **Store in OpenBao** (tokens, API keys, URLs)
5. **Validate** via health check endpoint

All steps are idempotent — safe to re-run at any time.

### Container Runtime

Runtime is per-service, set via `container_engine` in the site-config inventory:
- **Docker**: NetBox (complex Diode pipeline), NemoClaw (OpenShell requirement)
- **Podman**: All other services (rootless, security-focused)

The `lib/common.sh` auto-detects the available runtime. Set `CONTAINER_ENGINE=docker` environment variable to override.

### Semaphore Orchestration

Semaphore runs Ansible playbooks from this repo to deploy services:

1. Clones this repo via HTTPS (public, no credentials needed)
2. Runs wrapper playbook (e.g., `deploy-nocodb.yml`)
3. Playbook SSHes to target VM, clones monorepo to `~/agent-cloud`
4. Creates convenience symlink `~/<service>` -> deployment directory
5. Runs `deploy.sh` with `OPENBAO_ADDR` set

**Wrapper playbooks are required** because this Semaphore version doesn't support `extra_cli_arguments`. Each service has `deploy-<service>.yml` and `update-<service>.yml` that import the generic playbook with `target_service` set.

## SSH Key Architecture

- **Management key**: Used by Semaphore for all VM access (in Semaphore key store + OpenBao)
- **Per-service keys**: One ed25519 key pair per service in OpenBao at `secret/services/ssh/<service>`
- Both keys in `authorized_keys` on each VM
- Password auth disabled, root login disabled, NOPASSWD sudo configured
- Keys distributed via `distribute-ssh-keys.yml`, lockdown via `harden-ssh.yml`

## Playbook Conventions

See `platform/playbooks/README.md` for full details. Key rules:
- **No default usernames** in playbooks — `ansible_user` comes from private inventory
- **No credentials, IPs, or sensitive data** in any committed file
- **become** is declared per-playbook, NOT in inventory
- **delegate_to: localhost** tasks always set `become: false` (runner has no sudo)
- **SSH keys** fetched from OpenBao, written to temp files, cleaned in `always` blocks

## AI Agent Integration

### Agent Capabilities (what agents CAN do)
- Read all service APIs (NocoDB, n8n, NetBox, Proxmox)
- Create tasks and data entries
- Trigger workflows and playbook runs
- Open GitHub issues/PRs
- Query observability data
- Run local LLM inference
- Monitor network devices (read-only)

### Agent Limitations (what agents CANNOT do)
- Write OpenBao policies or unseal OpenBao
- Modify network configurations
- Run Ansible directly (must go through Semaphore)
- Access Proxmox API for destructive operations
- Delete persistent data
- Bypass AppRole scope
- Send data to external AI providers

### Task Routing
Task types in NocoDB determine which agent handles them:
- `workflow:*` -> NemoClaw (GitHub, n8n, Semaphore)
- `network:*` -> NetClaw (device health, config, topology)
- `infra:*` -> Either (Proxmox -> NemoClaw, network -> NetClaw)

### Agent Protocols
- **A2A** — Agent discovery and task delegation (horizontal, on NATS JetStream)
- **MCP** — Tool/data access (vertical, agent <-> database/API/tool)
- **OpenTelemetry** — Observability (traces, metrics, logs -> Grafana)

## Deployment Status

### Completed
- **Phase 0**: Foundation — OpenBao initialized, core services deployed, AppRole auth configured
- **Phase 0.5**: Per-VM deployment — deploy scripts, shared libraries, Semaphore orchestration
- **Monorepo consolidation** — all code in agent-cloud, deprecated infra-automation and openbao repos
- **SSH hardening** — per-service keys, sshd locked down, NOPASSWD sudo on all VMs
- **Semaphore pipeline** — 20 task templates, SSH key auth, wrapper playbooks

### In Progress
- **NetBox deployment** — Docker installed, deploy.sh running (Compose startup staging fix applied)
- **Service rollout** — NocoDB, n8n deploy via Semaphore next

### Planned
- **Phase 1**: NemoClaw task automation — NocoDB CRUD, GitHub/Discord integration, Proxmox monitoring, n8n/Semaphore orchestration, scheduled tasks
- **Phase 2**: Claude Cowork workflows — browser research, document generation, visual verification
- **Phase 3**: Cross-agent coordination — NocoDB task queue, handoff workflows, audit logging
- **NetClaw**: Network device monitoring with 101+ skills, 46 MCP backends
- **Kubernetes**: k0s clusters, Kustomize overlays, ArgoCD GitOps, Harbor registry

See `plan/IMPLEMENTATION_PLAN.md` and `plan/UNIFICATION-PLAN.md` for detailed roadmaps.

## Known Issues & Production Hardening

### Addressed
- OpenBao AppRole per-service isolation
- Secrets via stdin (not process args)
- SSH key auth everywhere, password auth disabled
- Per-service container runtime selection
- Staged Docker Compose startup for DNS race conditions

### Pending
- OpenBao TLS (`tls_disable=1`) — enable before production
- Auto-unseal (currently 1-of-1 Shamir) — use transit or 3-of-5
- AppRole secret_id TTL = 0 (never expires) — set rotation schedule
- Root token rotation — rotate after setup, use recovery tokens
- Proxmox API token scope restriction

## Git Conventions

- **No AI attribution** in commits — no `Co-Authored-By` lines referencing AI tools
- **No credentials, IPs, or usernames** in committed files — use `{{ }}` template variables
- IPs and real credentials belong exclusively in site-config (private)

### Mandatory Pre-Push Audit

**Before EVERY `git push`, run this audit.** No exceptions:

```bash
# 1. List staged/committed files
git diff HEAD~1 --name-only

# 2. Scan for secrets, IPs, credentials
git diff HEAD~1 | grep -iE '^\+.*192\.168\.|^\+.*password\s*[:=]\s*[A-Za-z0-9]{8}|^\+.*api_token[:=]\s*[A-Za-z0-9]{20}|^\+.*secret_id[:=]\s*[a-f0-9-]{30}'

# 3. Scan new files specifically
git diff HEAD~1 --diff-filter=A --name-only | xargs grep -liE '192\.168\.|password|api_key|secret'
```

If any matches are found, fix before pushing. The grep excludes template variables (`{{ }}`), `no_log`, and example/default patterns.

## Adding a New Service

Follow the composable pattern from `plan/AUTOMATION-COMPOSABILITY-PLAN.md`:

1. Create `platform/services/<name>/deployment/deploy.sh` — container operations only (no secret generation)
2. Create `platform/services/<name>/deployment/templates/*.j2` — Jinja2 templates for env files
3. Add host to site-config inventory under `agent_cloud` with `service_name`, `monorepo_deploy_path`, `service_url`
4. Create `deploy-<name>.yml` using composable tasks: `manage-secrets.yml` → deploy.sh → health check
5. Define `_secret_definitions` (what secrets the service needs) and `_env_templates` (what files to render)
6. Create `clean-deploy-<name>.yml` using `tasks/clean-service.yml` + `deploy-<name>.yml`
7. Create Semaphore task templates pointing at the playbooks
8. Generate SSH key pair, store in OpenBao at `secret/services/ssh/<name>`
9. Run `distribute-ssh-keys.yml` to deploy the key to the VM

## Dependencies

Ansible collections (auto-installed by Semaphore from `collections/requirements.yml`):
- `community.hashi_vault` — OpenBao/Vault lookups
- `ansible.posix` — `authorized_key` module

Shared bash libraries:
- `platform/lib/common.sh` — logging, secret generation, container runtime detection, compose wrapper, health checks
- `platform/lib/bao-client.sh` — HTTP-based OpenBao API client (curl + jq, no binary needed)
