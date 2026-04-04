# Automation Composability Plan

**Date:** 2026-04-02
**Status:** ACCEPTED — Implementing with NetBox as first service
**Context:** The NetBox deployment exposed that deploy.sh scripts mix infrastructure concerns (credential management, OpenBao) with container operations (compose, migrations). This plan decomposes service deployments into reusable Ansible building blocks that Semaphore orchestrates.

---

## Problem

Each service's deploy.sh is a monolith that handles everything from secret generation to container lifecycle to API bootstrapping. This creates:

1. **Secret drift** — deploy.sh generates new secrets on each run if intermediary files are missing, causing password mismatches with existing databases
2. **Duplication** — Every deploy.sh reimplements OpenBao auth, secret generation, health waiting
3. **Tight coupling** — deploy.sh needs OpenBao credentials but Ansible already has them natively
4. **No validation** — Secrets are generated and used without verifying they work
5. **Intermediary files** — secrets/ directory on VM is a redundant copy of what's in OpenBao

## Solution: OpenBao-Driven Secret Lifecycle

**OpenBao is the single source of truth.** No `secrets/` directory on VMs. No local secret generation. Ansible fetches from OpenBao via `community.hashi_vault`, templates compose-ready config files, and deploy.sh only runs container operations.

### Architecture

```
OpenBao (source of truth — all credentials live here)
  ↑ generate + store (first deploy only)
  ↓ fetch (every deploy)
Ansible manage-secrets.yml (in-memory, never writes secrets to disk files)
  ↓ Jinja2 template
env/*.env, .env, config.yaml (on VM — compose-readable, gitignored)
  ↓ read at container start
Docker Compose (variable substitution from .env, env_file from env/*.env)
  ↓
deploy.sh (container lifecycle only: pull, build, start, wait, migrate)
  ↓ runtime credentials created (e.g., orb-agent OAuth2)
post-deploy.sh (writes new creds to .env → Ansible Phase 4 syncs to OpenBao)
```

### Why Env Files on Disk?

Docker Compose does not integrate with OpenBao natively. The `.env` and `env/*.env` files are the **minimal required bridge** between Ansible-managed secrets and compose-consumed configuration. These files:
- Are gitignored (never committed)
- Are written by Ansible on every deploy (no drift)
- Contain resolved secrets but no generation logic
- Are the ONLY local secret storage — no `secrets/` directory

This cannot be eliminated without switching to Docker Swarm secrets, Kubernetes + ESO, or a compose-external injection mechanism. For the Docker Compose pattern, env files on disk are the standard approach.

### What Ansible/Semaphore Manages Natively

| Layer | Mechanism | Disk? |
|-------|-----------|-------|
| Semaphore environment JSON | `bao_role_id`, `bao_secret_id`, `openbao_addr` | No — Semaphore injects as env vars |
| `community.hashi_vault` lookup | Reads secrets from OpenBao at runtime | No — Ansible memory only |
| `ansible.builtin.template` | Renders `.env.j2` → `.env` on VM | Yes — compose needs files |
| `ansible.builtin.uri` | Patches OpenBao with new creds (Phase 4) | No — API call |

### Secret Lifecycle

**First deploy (no secrets in OpenBao):**
1. `manage-secrets.yml` checks OpenBao — empty
2. Generates random secrets via Ansible `password` lookup (in memory)
3. Stores all secrets in OpenBao (single API call)
4. Templates env files on VM from generated values (Jinja2)
5. deploy.sh starts containers using the env files
6. post-deploy.sh creates runtime credentials (orb-agent OAuth2)
7. Phase 4 reads new creds from `.env`, patches OpenBao

**Subsequent deploys (secrets exist in OpenBao):**
1. `manage-secrets.yml` checks OpenBao — has values
2. Reuses ALL existing secrets (no regeneration, no drift)
3. Templates env files on VM from fetched values
4. deploy.sh starts containers — passwords match existing database volumes
5. post-deploy.sh finds existing credentials, skips creation
6. Phase 4 confirms creds in OpenBao (no-op patch)

**Secret validation (check-secrets.yml / validate-secrets.yml):**
1. `check-secrets.yml` — Reads OpenBao, reports present/missing/empty (read-only)
2. `validate-secrets.yml` — Tests each credential against live services (DB, Redis, HTTP)
3. Neither modifies anything — pure verification

### No Local Secret Generation

deploy.sh and post-deploy.sh do NOT:
- Call `generate-secrets.sh`
- Write to a `secrets/` directory
- Generate random passwords
- Interact with OpenBao directly

They DO:
- Verify env files exist (fail if missing — means Ansible didn't run)
- Read credentials from `.env` (for compose exec commands that need passwords)
- Write runtime-created credentials to `.env` (orb-agent OAuth2 — synced to OpenBao by Ansible Phase 4)

### deploy.sh Split: Infrastructure + Application

deploy.sh is split into two scripts for independent retry and clear separation:

**deploy.sh (Infrastructure — steps 1-10):**
```
1.  Clone upstream dependency repos (netbox-docker)
2.  Copy .example templates (non-secret config)
3.  Verify env files present (fail if Ansible didn't run)
4.  Pull latest images
5.  Build custom image with plugins
6.  Stop stack gracefully
7.  Sync DB passwords to existing volumes
8.  Start services (staged: backing → Hydra → application)
10. Wait for NetBox healthy (up to 10 min for first-boot migrations)
```

**post-deploy.sh (Application — steps 11-16):**
```
11. Run database migrations
12. Create admin superuser (idempotent)
13. Register OAuth2 clients (Hydra)
14. Create/reuse orb-agent credentials (writes to .env)
15. Restart discovery services
16. Start Orb Agent (privileged, host networking)
```

The split enables:
- Retry post-deploy independently (if OAuth2 registration fails, don't rebuild containers)
- Different timeout profiles (infrastructure needs 10+ min, post-deploy is fast)
- Clear failure isolation (container startup vs application config)

---

## Composable Task Library

```
platform/playbooks/tasks/
  manage-secrets.yml       Fetch/generate secrets via OpenBao, template env files  [IMPLEMENTED]
  clone-and-deploy.yml     Clone monorepo, run deploy.sh, health check             [IMPLEMENTED]
  clean-service.yml        Destroy containers, volumes, clone (full wipe)           [IMPLEMENTED]
  clone-repo.yml           Clone/update monorepo on target VM                      [PLANNED]
  run-deploy.yml           Execute deploy.sh (container operations only)            [PLANNED]
  verify-health.yml        Health check a service endpoint                          [PLANNED]

platform/playbooks/
  deploy-<service>.yml     Composable: clone + secrets + deploy + verify            [NETBOX DONE]
  clean-deploy-<service>.yml  Wipe + fresh deploy                                  [NETBOX DONE]
  check-secrets.yml        Read-only secret inventory from OpenBao                  [IMPLEMENTED]
  validate-secrets.yml     Active credential testing (DB, Redis, HTTP)              [IMPLEMENTED]
  distribute-ssh-keys.yml  Deploy SSH keys from OpenBao                             [IMPLEMENTED]
  harden-ssh.yml           NOPASSWD sudo + sshd lockdown                            [IMPLEMENTED]
  install-docker.yml       Install Docker CE (standalone)                            [IMPLEMENTED]
  sync-secrets-to-openbao.yml  Push VM secrets → OpenBao (recovery/migration)       [IMPLEMENTED]
```

### Task Responsibilities

**`clone-repo.yml`**
- Clone or update `~/agent-cloud` via HTTPS (public repo, no creds)
- Create convenience symlink `~/<service>`
- No credentials needed

**`manage-secrets.yml`**
- Authenticate to OpenBao via AppRole
- Fetch existing secrets from `secret/services/<service_name>`
- Generate missing secrets (random or Django-style, per `_secret_definitions`)
- Store all secrets (existing + generated) back to OpenBao
- Template service-specific env files (`env/*.env`, `.env`, config files)
- Accepts `_secret_definitions` list: `[{name, type, length}]`
  - `type: random` — generated if missing (passwords, tokens)
  - `type: django` — Django secret key format if missing
  - `type: user` — user-managed, never auto-generated (SNMP, API keys)
- Accepts `_env_templates` list of Jinja2 templates to render

**`run-deploy.yml`**
- `cd` to deployment dir, run `bash deploy.sh`
- Passes `CONTAINER_ENGINE` as env var
- deploy.sh verifies env files exist (but does NOT generate secrets)
- deploy.sh handles: upstream repos, image pull/build, compose lifecycle, migrations, superuser, OAuth2, agent start

**`verify-health.yml`**
- HTTP GET to `service_url + health_path`
- Retries with backoff
- Reports HEALTHY/UNHEALTHY

**`clean-service.yml`**
- Finds and stops compose stack (detects docker-compose.yml or compose.yml)
- Destroys all volumes (`compose down -v`)
- Removes any leftover containers with service name prefix
- Removes the agent-cloud clone and convenience symlink
- Requires `become: true` for killing stale port processes
- Used by `clean-deploy-<service>.yml` before a fresh deploy

### Validation Playbooks

**`check-secrets.yml`** — Read-only secret inventory
- Lists all secrets in OpenBao for a service
- Reports which are present, which are missing, which are empty
- Does NOT generate or modify anything
- Usage: pre-deploy check, audit, troubleshooting

**`validate-secrets.yml`** — Active credential testing
- Fetches secrets from OpenBao
- Tests each against its service:
  - DB passwords: `psql` connection test
  - API tokens: HTTP request with auth header
  - Redis passwords: `redis-cli ping` with auth
- Reports: valid, invalid, unreachable
- Does NOT modify anything — read-only verification
- Usage: post-deploy verification, scheduled health checks

---

## Composable Playbook Pattern

Every `deploy-<service>.yml` follows this structure:

```yaml
# Phase 1: Clone + Secrets
- name: "Clone and manage secrets"
  hosts: <service>_svc
  tasks:
    - include_tasks: tasks/clone-repo.yml
    - include_tasks: tasks/manage-secrets.yml
      vars:
        _secret_definitions: [...]   # service-specific
        _env_templates: [...]        # service-specific

# Phase 2: Container Operations
- name: "Deploy containers"
  hosts: <service>_svc
  tasks:
    - include_tasks: tasks/run-deploy.yml

# Phase 3: Verify
- name: "Verify deployment"
  hosts: <service>_svc
  tasks:
    - include_tasks: tasks/verify-health.yml
```

Services that need Docker add `install-docker.yml` as a pre-phase. Services that need `become` for specific steps set it per-task.

---

## What deploy.sh Keeps vs What Moves to Ansible

| Concern | deploy.sh | Ansible |
|---------|-----------|---------|
| Clone upstream repos (e.g., netbox-docker) | Yes | No |
| Copy .example templates | Yes | No |
| **Generate secrets** | **No** | **Yes (manage-secrets.yml)** |
| **Write env files from secrets** | **No** | **Yes (Jinja2 templates)** |
| **OpenBao read/write** | **No** | **Yes (native hashi_vault)** |
| Pull/build container images | Yes | No |
| Start/stop compose services | Yes | No |
| Wait for container health | Yes | No |
| Run DB migrations | Yes | No |
| Create admin users | Yes | No |
| Register OAuth2 clients | Yes | No |
| Start privileged agents | Yes (sudo) | No |
| **Clone monorepo** | **No** | **Yes (clone-repo.yml)** |
| **Health check verification** | **No** | **Yes (verify-health.yml)** |
| **Docker/Podman installation** | **No** | **Yes (standalone playbook)** |
| **Secret validation** | **No** | **Yes (validate-secrets.yml)** |

### deploy.sh Becomes Pure Container Operations

```bash
#!/usr/bin/env bash
# deploy.sh — Container operations only.
# Secrets and env files managed by Ansible. Monorepo cloned by Ansible.

# Verify env files exist (Ansible must run first)
[ -f ".env" ] || error ".env missing. Deploy via Semaphore."
[ -f "env/netbox.env" ] || error "env/netbox.env missing."

step 1: clone upstream dependency repos
step 2: copy .example templates (non-secret config only)
step 3: verify env files present (fail if missing)
step 4: pull images
step 5: build custom images
step 6: stop stack
step 7: sync DB passwords (existing volumes)
step 8: start stack (staged)
step 9+: wait, migrate, create superuser, OAuth2, agent
```

No `generate-secrets.sh` call. No OpenBao code. No `BAO_ROLE_ID`. Pure container lifecycle.

---

## Env File Templates (Jinja2)

Each service provides Jinja2 templates that `manage-secrets.yml` renders:

```
platform/services/netbox/deployment/
  templates/
    netbox.env.j2
    postgres.env.j2
    discovery.env.j2
    dot-env.j2
    hydra.yaml.j2
```

These replace `generate-secrets.sh`'s env file writing logic. Variables come from the `_resolved_secrets` dict populated by `manage-secrets.yml`.

---

## Configuration-as-Code for Deployments and Templates

All deployment configurations, Semaphore templates, and service settings are managed as code — not through manual UI actions or ad-hoc API calls.

### Principle: Templates Define the Deployment Surface

Semaphore task templates are the interface between operators and the automation. Each template maps a human-readable name to a composable playbook. Templates are defined in `platform/semaphore/templates.yml` (public repo, no secrets) and applied via `platform/semaphore/setup-templates.yml`.

**Implementation:**
- `platform/semaphore/templates.yml` — Declarative list of all task templates (name → playbook mapping)
- `platform/semaphore/setup-templates.yml` — Ansible playbook that creates/updates templates via Semaphore API
- Idempotent: existing templates are updated, new ones are created
- No secrets in templates — playbook paths only; credentials come from Semaphore environments

**Adding a new template:**
1. Add entry to `platform/semaphore/templates.yml`
2. Run `ansible-playbook semaphore/setup-templates.yml`
3. Verify in Semaphore UI

**Why not API calls:** Ad-hoc API calls are not tracked, not repeatable, and drift from the codebase. The config file is the source of truth for what templates should exist.

### Principle: Env Files as Jinja2 Templates

Service configuration files (`.env`, `env/*.env`, config YAML) are rendered from Jinja2 templates by Ansible's `manage-secrets.yml` task. This replaces bash `generate-secrets.sh` scripts.

**Template design rules:**
- Templates live in `platform/services/<name>/deployment/templates/`
- Variables come from the `_resolved` dict (OpenBao secrets merged with generated values)
- Secret-containing files get mode `0600` (default)
- Config files that containers read (e.g., `hydra.yaml`) get mode `0644` via the `mode` field in `_env_templates`
- Templates are Jinja2, not bash heredocs — supports conditionals, defaults, loops
- Each template maps to one compose-consumed file

**Template flow:**
```
templates/*.j2 (in monorepo, committed)
  + secrets (from OpenBao, in Ansible memory)
  = env files (on VM, gitignored, compose-readable)
```

### Principle: Clean Deploy for State Reset

When secrets or database schemas change incompatibly, a clean deploy destroys volumes and starts fresh. This is a deliberate, tracked operation — not a manual `docker compose down -v`.

**Implementation:**
- `tasks/clean-service.yml` — composable task that destroys containers, volumes, clone
- `clean-deploy-<service>.yml` — clean + deploy wrapper
- Semaphore template "Clean Deploy <Service>" exposes this as a UI action
- Destructive: requires explicit operator action, not triggered automatically

---

## Migration Path

1. **NetBox (current):** First service to implement full composable pattern
2. **Extract reusable tasks:** `clone-repo.yml`, `manage-secrets.yml`, `run-deploy.yml`, `verify-health.yml` from deploy-netbox.yml
3. **NocoDB + n8n:** Apply same pattern — define `_secret_definitions`, create env templates, simplify deploy.sh
4. **OpenBao:** Special case — bootstraps itself, but Ansible still manages post-deploy secret sync
5. **All future services:** Follow the composable pattern from day one

---

## Validation Criteria

| Check | Pass Condition |
|-------|---------------|
| No `secrets/` directory on VM | `ls secrets/` fails or is empty |
| No `generate-secrets.sh` call in deploy.sh | `grep generate-secrets deploy.sh` returns nothing |
| No `get_secret`/`put_secret` in deploy scripts | Functions read from `.env` only |
| deploy.sh fails without env files | Remove `.env` → deploy.sh errors immediately |
| OpenBao is authoritative | Redeploying reuses existing secrets (no new passwords) |
| First deploy works | Empty OpenBao → generate in memory → store → template → deploy |
| Subsequent deploy works | Existing OpenBao → fetch → template → deploy (DB passwords match) |
| Post-deploy creds sync | orb-agent creds written to `.env` → Phase 4 patches OpenBao |
| check-secrets reports accurately | Lists all secrets, flags missing |
| validate-secrets tests credentials | DB/API/Redis auth verified against live services |
| Task reuse works | Same manage-secrets.yml for netbox, nocodb, n8n |
| Idempotent end-to-end | Running deploy twice = same state, same passwords |
| deploy.sh split works | deploy.sh completes independently; post-deploy.sh retryable |

## Security Considerations

- **No `secrets/` directory:** Eliminated entirely. No `.txt` secret files on disk.
- **Minimal disk footprint:** Only `.env` and `env/*.env` files exist on VM (required by Docker Compose). These are gitignored and overwritten on every deploy.
- **Secrets never in bash variables long-term:** Ansible holds secrets in memory during template rendering, then discards. deploy.sh reads from `.env` only when needed (compose exec).
- **Ansible `no_log: true`** on all secret-handling tasks — prevents credential leakage in Semaphore logs.
- **AppRole least privilege:** Semaphore's AppRole can read/write all service paths (orchestrator role). Per-service AppRoles are more restrictive.
- **Validation catches drift:** `validate-secrets.yml` detects when a password in OpenBao no longer matches the database.
- **deploy.sh has no credential access:** Cannot authenticate to OpenBao, cannot generate secrets, cannot write to `secrets/`. Reduces blast radius if a deploy script is compromised.
- **Runtime credentials (orb-agent):** Created by post-deploy.sh, written to `.env`, synced to OpenBao by Ansible Phase 4. The `.env` is the transient holding area, not the source of truth.

---

## AppRole Management (Composable)

### Principle: Self-Service AppRole Provisioning

Services should not depend on OpenBao's `deploy.sh` to create their AppRole. Instead, any service playbook can provision its own AppRole via `tasks/manage-approle.yml`. This decouples identity management from the secrets backbone deployment.

**Implementation:** `tasks/manage-approle.yml`
- Creates/updates an HCL policy with the exact paths the service needs
- Creates/updates the AppRole with the policy attached
- Returns `role_id` and `secret_id`
- Stores credentials at `secret/services/approles/<name>` in OpenBao

**Semaphore's policy** (`semaphore-read.hcl`) includes `sys/policies/acl/*` and `auth/approle/role/*` capabilities so it can manage AppRoles for any service without root access.

**Example — provisioning an orb-agent AppRole:**
```yaml
- include_tasks: tasks/manage-approle.yml
  vars:
    _approle_name: "orb-agent"
    _approle_policy: |
      path "secret/data/services/netbox/orb_agent_*" {
        capabilities = ["read"]
      }
      path "secret/data/services/netbox/snmp_community" {
        capabilities = ["read"]
      }
```

### Principle: Least-Privilege by Default

Each AppRole gets ONLY the paths it needs. The `manage-approle.yml` task enforces this by requiring the caller to specify the exact HCL policy. No blanket `secret/data/services/*` access unless explicitly requested.

---

## Workflow Decoupling

### Principle: Independent Workflows Over Monolithic Playbooks

Each deployment concern should be its own playbook that can run independently. Don't embed optional components (like the orb-agent) into the service deploy — create a separate workflow.

**Before (brittle):**
```
deploy-netbox.yml → 6 phases including orb-agent
  If orb-agent fails, entire deploy fails.
  Can't redeploy orb-agent without redeploying NetBox.
```

**After (decoupled):**
```
deploy-netbox.yml → 5 phases (NetBox only)
deploy-orb-agent.yml → independent (Diode creds + agent start)
run-pfsense-sync.yml → independent (scheduled every 15 min)
```

**Benefits:**
- Retry individual workflows without re-running the whole stack
- Schedule workflows independently (orb-agent after every NetBox deploy, pfsense-sync every 15 min)
- Different failure domains — orb-agent failure doesn't block NetBox availability
- Clear ownership — each playbook has one responsibility

### Principle: Semaphore Templates as Workflow Triggers

Each independent workflow gets its own Semaphore task template. Operators run "Deploy NetBox", then "Deploy Orb Agent", then schedule "Run pfSense Sync" — each independently observable and retryable in the Semaphore UI.

### Implemented Workflows

| Workflow | Playbook | Trigger | Depends On |
|----------|----------|---------|------------|
| Deploy NetBox | `deploy-netbox.yml` | Manual / CI | OpenBao unsealed |
| Deploy Orb Agent | `deploy-orb-agent.yml` | After NetBox deploy | NetBox healthy + Diode auth |
| Clean Deploy NetBox | `clean-deploy-netbox.yml` | Manual (destructive) | OpenBao unsealed |
| pfSense Sync | `run-pfsense-sync.yml` (planned) | Every 15 min | NetBox + Diode healthy |
| Distribute SSH Keys | `distribute-ssh-keys.yml` | After VM provision | OpenBao has SSH keys |
| Harden SSH | `harden-ssh.yml` | After key distribution | Keys verified working |

---

## Anti-Patterns to Avoid

### Brittle: Monolithic deploy scripts that handle everything
deploy.sh should NOT: generate secrets, authenticate to OpenBao, manage AppRoles, start auxiliary services, or handle credential rotation. Each concern has its own Ansible task.

### Brittle: Ad-hoc API calls for Semaphore/OpenBao management
All templates, policies, and AppRoles should be managed as code (`.yml`/`.hcl` files) and applied via playbooks. No `curl` one-liners.

### Brittle: Reusing stale credentials from OpenBao
Always verify credentials against the live service (e.g., `list_clients` from the Diode plugin), not just OpenBao. A clean deploy wipes the Hydra database but OpenBao retains old credentials.

### Brittle: Sed-based credential injection
Don't resolve `${VARIABLE}` references via sed in config files. Use either:
- Ansible Jinja2 templates (for values known at deploy time)
- Service-native secret managers (e.g., orb-agent's vault integration)

### Brittle: Shared AppRoles across unrelated services
Each service/component should have its own AppRole with least-privilege policy. The `semaphore-read` AppRole is the exception — it's the orchestrator.
