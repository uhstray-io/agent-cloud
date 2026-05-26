# Playbooks

Ansible playbooks for deploying, updating, validating, and hardening agent-cloud services via Semaphore.

## Conventions

### Thin Wrappers

There are two deployment patterns in use:

**Legacy (thin wrapper):** Services that have not yet migrated to the composable pattern use a thin wrapper that imports `deploy-service.yml` with `target_service` set. This is required because the Semaphore version in use does not support `extra_cli_arguments`.

```yaml
# deploy-nocodb.yml (legacy pattern)
- name: "Deploy NocoDB"
  import_playbook: deploy-service.yml
  vars:
    target_service: nocodb_svc
```

**Composable (multi-phase):** Services migrated to the composable pattern have their own multi-phase playbook that uses the composable task library directly. NetBox is the reference implementation.

```yaml
# deploy-netbox.yml (composable pattern — abbreviated)
# Phase 1: Clone repo, manage-secrets.yml (OpenBao fetch/generate, Jinja2 templates)
# Phase 2: Run deploy.sh (container lifecycle only)
# Phase 3: Application bootstrap + Diode credential sync
# Phase 4: Health verification
```

See `plan/architecture/AUTOMATION-COMPOSABILITY.md` for the full composable pattern specification.

### Variable Sources

| Variable | Source | Notes |
|----------|--------|-------|
| `ansible_user` | Inventory (private) | No defaults in playbooks — must be set in inventory |
| `service_name` | Inventory per-host | e.g., `nocodb`, `openbao` |
| `monorepo_deploy_path` | Inventory per-host | Path within monorepo to deploy.sh |
| `monorepo_repo` | Inventory global | Git SSH URL |
| `openbao_addr` | Environment | OpenBao API URL |
| `bao_role_id` / `bao_secret_id` | Environment | AppRole credentials |
| `target_service` | Wrapper playbook vars | Inventory group name (e.g., `netbox_svc`) |

### Become (sudo)

`become` is **not** set in the inventory. Each playbook declares its own:
- `distribute-ssh-keys.yml` — `become: false` (writes to user-owned `~/.ssh/`)
- `harden-ssh.yml` — `become: true` (modifies `/etc/ssh/sshd_config`)
- `deploy-service.yml` — `become: false` (runs deploy.sh as the service user)
- `provision-vm.yml` — runs against Proxmox API, no SSH become

When a playbook needs become, pass `ansible_become_password` via a Semaphore environment if NOPASSWD sudo is not configured.

### Delegate Tasks

Tasks that run on the Semaphore runner (e.g., fetching keys from OpenBao, writing temp files) use `delegate_to: localhost` with explicit `become: false` — the runner container does not have sudo.

### Secrets

**No credentials, IPs, or usernames in playbooks.** All sensitive values come from:
- **Inventory** (private repo) — IPs, usernames, host vars
- **OpenBao** — SSH keys, API tokens, passwords (fetched at runtime via `community.hashi_vault`)
- **Semaphore environment** — AppRole credentials for OpenBao access

### SSH Keys

SSH keys are fetched from OpenBao at runtime and written to temp files that are cleaned up in `always` blocks. The pattern:

```yaml
- name: "Fetch key"
  set_fact:
    _key: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:private_key', ...) }}"

- name: "Write to temp file"
  tempfile: { state: file }
  register: _key_file
  delegate_to: localhost

- name: "Set contents"
  copy: { content: "{{ _key }}\n", dest: "{{ _key_file.path }}", mode: "0600" }
  delegate_to: localhost
  no_log: true

# ... use _key_file.path ...

- name: "Cleanup"  # in always block
  file: { path: "{{ _key_file.path }}", state: absent }
  delegate_to: localhost
```

## Playbook Reference

### Deployment
| Playbook | Pattern | Purpose |
|----------|---------|---------|
| `deploy-service.yml` | Legacy | Generic deploy: clone monorepo, run deploy.sh, health check |
| `deploy-all.yml` | Mixed | Deploy all services in dependency order (4 phases) |
| `deploy-openbao.yml` | Legacy | Deploy OpenBao (self-bootstrapping, special case) |
| `deploy-nocodb.yml` | Legacy | Deploy NocoDB (migration to composable planned) |
| `deploy-n8n.yml` | Legacy | Deploy n8n (migration to composable planned) |
| `deploy-semaphore.yml` | Legacy | Deploy Semaphore (new VM only) |
| `deploy-netbox.yml` | Composable | Deploy NetBox (5-phase: secrets, containers, bootstrap, Diode creds, verify) |
| `deploy-nemoclaw.yml` | Legacy | Deploy NemoClaw |
| `deploy-orb-agent.yml` | Composable | Deploy Orb Agent (standalone: Diode creds + agent.yaml + start) |
| `clean-deploy-netbox.yml` | Composable | Destructive: wipe volumes + fresh NetBox deploy |

### Updates
| Playbook | Purpose |
|----------|---------|
| `update-service.yml` | Generic update: pull images, restart compose, health check |
| `update-nocodb.yml` | Update NocoDB |
| `update-n8n.yml` | Update n8n |
| `update-semaphore.yml` | Update Semaphore |
| `update-netbox.yml` | Update NetBox |

### SSH & Security
| Playbook | Purpose |
|----------|---------|
| `distribute-ssh-keys.yml` | Deploy SSH keys from OpenBao, verify key auth (no sudo) |
| `harden-ssh.yml` | NOPASSWD sudo + sshd lockdown + post-lockdown verification (requires sudo) |

### Secrets & Policies
| Playbook | Purpose |
|----------|---------|
| `check-secrets.yml` | Read-only secret inventory from OpenBao (present/missing/empty) |
| `validate-secrets.yml` | Active credential testing (DB, Redis, HTTP auth) |
| `seed-discovery-credentials.yml` | Copy/migrate discovery credentials to new vault paths |
| `sync-secrets-to-openbao.yml` | Push VM-local secrets to OpenBao (recovery/migration) |
| `sync-netbox-secrets.yml` | Sync NetBox-specific secrets to OpenBao |
| `update-proxmox-token.yml` | Update Proxmox API token in OpenBao |
| `apply-openbao-policies.yml` | Apply all OpenBao policies from .hcl files |
| `apply-policy-orb-agent.yml` | Apply orb-agent policy |
| `apply-policy-semaphore.yml` | Apply Semaphore policy |
| `apply-policy-nemoclaw.yml` | Apply NemoClaw policy |
| `apply-policy-uhhcraft.yml` | Apply UhhCraft policy (reserved) |
| `apply-policy-inference-comfyui.yml` | Apply ComfyUI sidecar policy (reserved) |
| `apply-policy-inference-hunyuan3d.yml` | Apply Hunyuan3D sidecar policy (reserved) |

### Validation & Provisioning
| Playbook | Purpose |
|----------|---------|
| `validate-all.yml` | Health check all services (HTTP only, no SSH commands) |
| `check-discovery.yml` | Validate NetBox Diode discovery pipeline health |
| `cleanup-netbox.yml` | Clean up orphaned NetBox objects |
| `provision-vm.yml` | Clone Proxmox template, configure cloud-init, provision VM |
| `provision-template.yml` | Create Proxmox VM template with cloud-init |
| `proxmox-validate.yml` | Validate Proxmox cluster readiness |

### Infrastructure
| Playbook | Purpose |
|----------|---------|
| `install-docker.yml` | Install Docker CE from official repo (idempotent) |

### Composable Task Library

These reusable tasks are the building blocks for all playbooks. See `plan/architecture/AUTOMATION-COMPOSABILITY.md` for the full architecture.

| Task | Status | Purpose |
|------|--------|---------|
| `tasks/manage-secrets.yml` | Implemented | Fetch/generate secrets from OpenBao, template env files via Jinja2 |
| `tasks/manage-approle.yml` | Implemented | Create/update AppRole + HCL policy, store credentials in OpenBao |
| `tasks/manage-diode-credentials.yml` | Implemented | Create fresh Diode orb-agent OAuth2 credentials via NetBox plugin API |
| `tasks/deploy-orb-agent.yml` | Implemented | Start privileged orb-agent with vault-integrated agent.yaml config |
| `tasks/clean-service.yml` | Implemented | Destroy containers, volumes, runtime dir, and clone for full rebuild |
| `tasks/clone-and-deploy.yml` | Legacy | Clone monorepo, symlink, run deploy.sh, health check (used by legacy services) |
| `tasks/apply-openbao-policy.yml` | Implemented | Apply a single OpenBao policy from an .hcl file |
| `tasks/seed-discovery-credential.yml` | Implemented | Copy/update one credential set at a discovery/* vault path |
| `tasks/update-vault-field.yml` | Implemented | Read a vault secret, update a specific field, write back |

Planned tasks (not yet implemented):

| Task | Purpose |
|------|---------|
| `tasks/sparse-checkout.yml` | Sparse-clone monorepo for specific service paths |
| `tasks/setup-runtime-dir.yml` | Create ~/services/<name>/, symlinks to clone |
| `tasks/run-deploy.yml` | Execute deploy.sh from runtime dir (passes CLONE_DIR) |
| `tasks/verify-health.yml` | Health check a service endpoint with retry/backoff |
| `tasks/write-secret-metadata.yml` | Write KV v2 custom metadata after secret store |
| `tasks/rotate-credential.yml` | Generic Create-Verify-Retire rotation wrapper |
| `tasks/revoke-service-credentials.yml` | Revoke AppRole secret_id + delete Hydra clients |

## Adding a New Service

For the complete onboarding checklist (7 phases, all tiers), see `plan/architecture/SERVICE-INTEGRATION-PLAN.md`.

**Quick reference for the composable pattern (preferred for all new services):**

1. Create `platform/services/<name>/deployment/` with `deploy.sh` (container-lifecycle only), `compose.yml`, and `templates/*.j2`
2. Define `_secret_definitions` and `_env_templates` for the service
3. Create `platform/playbooks/deploy-<name>.yml` using composable tasks: `manage-secrets.yml` -> deploy.sh -> verify
4. Create `platform/playbooks/clean-deploy-<name>.yml` using `tasks/clean-service.yml`
5. Add host to site-config inventory with `service_name`, `monorepo_deploy_path`, `service_url`
6. Add Semaphore templates to `platform/semaphore/templates.yml`, run `setup-templates.yml`
7. Generate SSH key pair, store in OpenBao, run `distribute-ssh-keys.yml`
8. Optionally provision a dedicated AppRole via `tasks/manage-approle.yml`

**Legacy pattern (for services not yet migrated):**

1. Add the service to the inventory (private repo) under `agent_cloud` with `service_name` and `monorepo_deploy_path`
2. Create `platform/services/<service>/deployment/deploy.sh` (idempotent, sources `../../lib/common.sh`)
3. Create `deploy-<service>.yml` wrapper (import `deploy-service.yml` with `target_service: <service>_svc`)
4. Create `update-<service>.yml` wrapper (import `update-service.yml`)
5. Create Semaphore task templates pointing at the wrapper playbooks
6. Generate an SSH key pair, store in OpenBao at `secret/services/ssh/<service>`
7. Run `distribute-ssh-keys.yml` to deploy the key to the VM

Note: The legacy pattern is maintained for backward compatibility. All new services should use the composable pattern.

## Dependencies

Declared in `collections/requirements.yml` (auto-installed by Semaphore):
- `community.hashi_vault` — OpenBao/Vault lookups
- `ansible.posix` — `authorized_key` module
