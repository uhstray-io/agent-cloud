# Automation Composability Plan

**Date:** 2026-04-02
**Status:** PROPOSED
**Context:** The NetBox deployment exposed that our deploy.sh scripts mix infrastructure concerns (credential management, OpenBao, SSH) with container operations (compose, migrations, health checks). This plan decomposes service deployments into reusable Ansible building blocks that Semaphore orchestrates.

---

## Problem

Each service's deploy.sh is a monolith that handles everything from secret generation to container lifecycle to API bootstrapping. This creates:

1. **Duplication** — Every deploy.sh reimplements OpenBao auth, secret generation, health waiting
2. **Tight coupling** — deploy.sh can't run without OpenBao creds, but Ansible has them natively
3. **Fragility** — A 17-step bash script has 17 failure points with limited error recovery
4. **Inconsistency** — Each service handles secrets slightly differently
5. **Testing difficulty** — Can't test individual steps in isolation

## Solution: Composable Ansible Roles

Decompose service deployment into reusable Ansible task files (or roles) that any `deploy-<service>.yml` playbook can compose together.

### Proposed Task Library

```
platform/playbooks/tasks/
  clone-repo.yml           Clone/update the monorepo on target VM
  seed-secrets.yml         Pull user-managed secrets from OpenBao → VM
  run-deploy.yml           Execute service deploy.sh (container operations)
  sync-secrets.yml         Push generated secrets from VM → OpenBao
  verify-health.yml        Health check a service endpoint
  install-docker.yml       Install Docker CE (idempotent)
  install-podman.yml       Install Podman (idempotent)
```

### Task Responsibilities

**`clone-repo.yml`** — Monorepo lifecycle on the target VM
- Clone or update `~/agent-cloud` from HTTPS
- Create convenience symlink `~/<service>` → deployment dir
- No credentials needed (public repo)

**`seed-secrets.yml`** — Pre-deploy: OpenBao → VM
- Authenticate to OpenBao via AppRole (creds from Semaphore environment)
- Fetch `secret/services/<service_name>` from OpenBao
- Write specified secret keys to `secrets/<key>.txt` on the VM
- Accepts a list of secret keys to seed (service-specific)
- Skips missing keys gracefully (not all services have all secrets)

**`run-deploy.yml`** — Container operations
- `cd` to deployment dir, run `bash deploy.sh`
- Passes `CONTAINER_ENGINE`, `OPENBAO_ADDR`, service URL as env vars
- deploy.sh handles: templates, secret generation, image pull/build, compose lifecycle, migrations, superuser, OAuth2, agent start
- deploy.sh does NOT handle OpenBao — that's Ansible's job

**`sync-secrets.yml`** — Post-deploy: VM → OpenBao
- Read all `secrets/*.txt` from the VM
- Build JSON, authenticate to OpenBao, PUT to `secret/services/<service_name>`
- Includes service URL in the stored data
- Reusable across all services (already exists as `sync-secrets-to-openbao.yml`)

**`verify-health.yml`** — Post-deploy validation
- HTTP GET to `service_url + health_path`
- Retries with backoff
- Reports HEALTHY/UNHEALTHY

### Composable Playbook Pattern

Every `deploy-<service>.yml` follows the same structure:

```yaml
# deploy-<service>.yml

# Phase 1: Pre-deploy
- name: "Pre-deploy"
  hosts: <service>_svc
  tasks:
    - include_tasks: tasks/clone-repo.yml
    - include_tasks: tasks/seed-secrets.yml
      vars:
        _user_secrets: [snmp_community, pfsense_api_key]  # service-specific

# Phase 2: Deploy  
- name: "Deploy"
  hosts: <service>_svc
  tasks:
    - include_tasks: tasks/run-deploy.yml

# Phase 3: Post-deploy
- name: "Post-deploy"
  hosts: <service>_svc
  tasks:
    - include_tasks: tasks/sync-secrets.yml
    - include_tasks: tasks/verify-health.yml
```

Services that need Docker add `install-docker.yml` before phase 1. Services that need `become` for specific steps set it per-task.

### What deploy.sh Keeps vs What Moves to Ansible

| Concern | deploy.sh | Ansible |
|---------|-----------|---------|
| Clone upstream repos (e.g., netbox-docker) | Yes | No |
| Copy .example templates | Yes | No |
| Generate random secrets | Yes | No |
| Pull/build container images | Yes | No |
| Start/stop compose services | Yes | No |
| Wait for container health | Yes | No |
| Run DB migrations | Yes | No |
| Create admin users | Yes | No |
| Register OAuth2 clients | Yes | No |
| Start privileged agents | Yes (sudo) | Could move here |
| **OpenBao authentication** | **No** | **Yes** |
| **Pull secrets from OpenBao** | **No** | **Yes (pre-deploy)** |
| **Push secrets to OpenBao** | **No** | **Yes (post-deploy)** |
| **Clone monorepo** | **No** | **Yes** |
| **Health check verification** | **No** | **Yes** |
| **Docker/Podman installation** | **No** | **Yes (separate playbook)** |

### deploy.sh Simplification

With Ansible handling credentials, deploy.sh becomes a pure container operations script:

```bash
#!/usr/bin/env bash
# deploy.sh — Container operations only. Credentials managed by Ansible.
set -euo pipefail

# Expect: secrets/ directory already populated by Ansible pre-deploy
# Expect: monorepo already cloned by Ansible
# Produces: additional secrets in secrets/ (pushed to OpenBao by Ansible post-deploy)

source lib/common.sh

step 1: clone upstream dependency repos
step 2: copy .example templates (if missing)
step 3: generate secrets (reads existing from secrets/, generates missing)
step 4: pull images
step 5: build custom images
step 6: stop stack
step 7: sync DB passwords (existing volumes)
step 8: start stack (staged)
step 9: wait for health
step 10: run migrations
step 11: create superuser
step 12+: service-specific operations (OAuth2, agent start, etc.)
```

No OpenBao code. No monorepo cloning. No credential management. Pure container lifecycle.

### Migration Path

1. **Immediate (current session):** NetBox already uses the 3-phase pattern (deploy-netbox.yml)
2. **Next:** Extract `seed-secrets.yml` and `sync-secrets.yml` as reusable task files from deploy-netbox.yml
3. **Then:** Apply the same pattern to NocoDB and n8n deploy playbooks
4. **Future:** Refactor all deploy.sh scripts to remove OpenBao code, rely on Ansible pre/post-deploy

### Validation Criteria

| Check | Pass Condition |
|-------|---------------|
| Task files are reusable | Same `seed-secrets.yml` works for netbox, nocodb, n8n |
| deploy.sh works without OpenBao | `deploy.sh` completes when `OPENBAO_ADDR` is not set |
| Ansible handles all credential lifecycle | Pre-deploy seeds, post-deploy syncs, no gaps |
| Idempotent end-to-end | Running `deploy-<service>.yml` twice = same state |
| No credentials in deploy.sh | `grep -r 'BAO_ROLE_ID\|BAO_SECRET_ID' deploy.sh` returns nothing |

### Security Considerations

- **Separation of concerns:** deploy.sh never authenticates to OpenBao — reduces attack surface if a deploy script is compromised
- **Ansible has AppRole natively** via `community.hashi_vault` — no need to shell out to curl
- **Secrets on VM are ephemeral:** generated by deploy.sh, synced to OpenBao by Ansible, VM copy is the working set (not source of truth)
- **User-managed secrets** (SNMP community, API keys) flow: user → site-config → OpenBao → Ansible pre-deploy → VM secrets/
- **Generated secrets** flow: deploy.sh → VM secrets/ → Ansible post-deploy → OpenBao

### Architectural Considerations

- This pattern scales to any service — NocoDB, n8n, Semaphore, NemoClaw can all use it
- The task library grows incrementally — extract from working playbooks, not designed upfront
- Semaphore orchestrates the playbook; the playbook composes the tasks; the tasks call deploy.sh
- Three layers of idempotency: Semaphore (re-run templates), Ansible (declarative tasks), deploy.sh (check-before-act)
