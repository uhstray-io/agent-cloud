# Agent Cloud

Dual-agent AI infrastructure: NemoClaw (headless, background tasks) + Claude Cowork (interactive, GUI). OpenBao is the secrets backbone; NocoDB, n8n, and Semaphore are the shared service layer. Each service runs on its own Proxmox VM. Semaphore orchestrates deployments via Ansible playbooks pulled from GitHub.

## Current Status

| Phase | Status | Summary |
|-------|--------|---------|
| **0** Foundation | DONE | Monolithic compose, OpenBao initialized, all services running locally |
| **0.5** Per-VM Deployment | DONE | Deploy scripts, shared libs, Semaphore orchestration, Proxmox provisioning |
| **0.5** OpenBao Production | DONE | VM 210 provisioned, OpenBao v2.5.2 deployed via Semaphore |
| **0.75** Automation Infra | IN PROGRESS | Git-driven deploys, OpenBao-backed credentials; NetBox inventory pending |
| **1** NemoClaw Tasks | PLANNED | NocoDB CRUD, GitHub issues, Discord, Proxmox monitoring |


## Production Topology

See `../semaphore/config/inventory.yml` for the canonical host inventory. Key agent-cloud VMs:

| VM | Services | Port | VMID | Node |
|----|----------|------|------|------|
| `openbao` | OpenBao v2.5.2 | 8200 | 210 | alphacentauri |
| `nocodb` | NocoDB + Postgres | 8181 | 205 | alphacentauri |
| `n8n` | n8n + Worker + Postgres + Redis | 5678 | 208 | apollo |
| `semaphore` | Semaphore + Postgres | 3000 | 203 | apollo |
| `nemoclaw` | NemoClaw + OpenShell | — | 209 | andromeda |
| `netbox` | NetBox + Diode Pipeline | 8000 | 202 | mercier77 |

**Proxmox:** 11 nodes, primary host `aurora`, storage `vm-lvms` (931GB), template VMID 9000.

## Deployment Architecture

```
GitHub (uhstray-io/infra-automation)     ← public, no secrets
  ↓ Semaphore pulls playbooks
Semaphore (semaphore VM)
  ↓ playbook authenticates via AppRole
OpenBao (openbao VM)                     ← secrets backbone
  ↓ returns scoped credentials
Ansible playbook
  ↓ clones service repo (e.g., uhstray-io/openbao)
Target VM (rootless podman, ~/service_name)
```

**Credentials:** All secrets stored in OpenBao. Semaphore environments contain only AppRole role-id + secret-id. Playbooks fetch credentials at runtime via `community.hashi_vault` lookup. No secrets in the public infra-automation repo.

## OpenBao Secrets Layout

| Path | Contents | Source |
|------|----------|--------|
| `secret/services/proxmox` | PVE API token, URL, token ID | Stored manually |
| `secret/services/ssh` | SSH public key | Stored manually |
| `secret/services/semaphore-vm` | Semaphore VM SSH creds | Stored manually |
| `secret/services/nocodb` | NocoDB API token, URL | Programmatic (deploy.sh) |
| `secret/services/n8n` | n8n API key, URL | Programmatic (deploy.sh) |
| `secret/services/semaphore` | Semaphore API token, URL | Programmatic (deploy.sh) |
| `secret/services/github` | GitHub PAT | Manual |
| `secret/services/discord` | Discord bot token | Manual |

**AppRoles:** nemoclaw (read all), nocodb/n8n/semaphore (write own path), semaphore (read all via semaphore-read policy).

## Local Dev

```bash
./orchestrate.sh --local                         # Deploy all locally
./orchestrate.sh --local --skip netbox nemoclaw  # Skip unavailable services
cd vms/nocodb && ./deploy.sh                     # Deploy single service
```

## References

- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — Phase definitions, checklists, ADRs
- [SYSTEM-DESIGN-VM-DEPLOYMENT.md](SYSTEM-DESIGN-VM-DEPLOYMENT.md) — Per-VM architecture, bootstrap sequence
- [PHASE0-REPORT.md](PHASE0-REPORT.md) — Phase 0 validation report
- [uhstray-io/infra-automation](https://github.com/uhstray-io/infra-automation) — Ansible playbooks (public)
- [uhstray-io/openbao](https://github.com/uhstray-io/openbao) — OpenBao deployment (private)
