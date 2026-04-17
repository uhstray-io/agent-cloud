# WisAI Deployment Plan

**Date:** 2026-04-07 (revised 2026-04-16 after cross-specialist review)
**Status:** PLANNING — revised to align with `AUTOMATION-COMPOSABILITY.md` and `CREDENTIAL-LIFECYCLE-PLAN.md`
**Context:** WisAI is the current self-hosted LLM inference stack (Ollama + Open WebUI) deployed across consumer NVIDIA GPU VMs on Proxmox. This plan integrates WisAI into the agent-cloud Semaphore pipeline using the composable 4-phase deployment pattern and the full credential lifecycle (metadata, TTLs, rotation, audit).

> **Review provenance:** This revision incorporates findings from four specialist reviews (architect, automation, security, LLM/data). Decisions that differed between reviewers were resolved in Round 2 — see "Review Decisions Ledger" at the end of the document for the audit trail.

---

## Problem

WisAI currently exists as upstream infrastructure files (`docker-compose.yml`, `docker-compose.multi.yml`, scripts) with no integration into the agent-cloud Semaphore pipeline. There is no OpenBao-managed secret flow, no Ansible provisioning, no composable path to add GPU nodes. The existing `platform/services/inference/` is a bare placeholder (two empty subdirs).

An earlier draft of this plan placed WisAI inside `platform/services/inference/`. The current monorepo structure replaces that single bare placeholder with three purpose-specific directories — `inference-ollama/` (WisAI workers), `inference-webui/` (WisAI coordinator + agent API surface), and `inference-vllm/` (reserved additively for future 24 GB+ hardware). WisAI itself IS the platform inference backbone; vLLM is a future additive capability, not a replacement.

---

## Architecture

### How WisAI fits into the agent-cloud inference story

agent-cloud eventually wants a pluggable, OpenAI-compatible inference backbone. Hardware reality today is consumer GPUs (8–16 GB VRAM), where Ollama's GGUF-based engine is the pragmatic choice; vLLM becomes attractive only when 24 GB+ cards are available. This plan deploys Ollama as the **current** platform inference backbone, while reserving a directory slot for vLLM to land additively when hardware supports it.

```
platform/services/
  inference-ollama/        ← THIS PLAN (worker node service)
  inference-webui/         ← THIS PLAN (coordinator + user UI service + agent API surface)
  inference-vllm/          ← RESERVED (.gitkeep only; future plan for 24 GB+ hardware)
```

No separate inference gateway is planned. Open WebUI exposes an OpenAI-compatible `/api/chat/completions` endpoint; agents consume that directly. If load-balancing or per-agent key isolation becomes necessary later, the replacement lands under a new directory name — never LiteLLM.

### Two services, not one

WisAI is two deployable services with different failure domains, different topologies, and different security postures:

| Service | Runs on | Scale | Contains secrets | Network | Data |
|---|---|---|---|---|---|
| `inference-ollama` | GPU VM (one per GPU node) | N | No (config only) | Inference VLAN; listener bound to `ansible_host` | Model files (regenerable) |
| `inference-webui` | Dedicated DMZ VM | 1 (coordinator) | Yes (session key, admin creds, DB password) | DMZ; reaches Ollama nodes via allowlist | Conversation history (PII, irreplaceable) |

The two services share no runtime — they only share an upstream reference (the WisAI repo) and a naming prefix.

### Deployment topology

```
                     Operator (browser)
                           │
                 ┌─────────┴─────────┐
                 │  Caddy (future)   │ ← HTTPS, Authentik OIDC
                 └─────────┬─────────┘
                           │
                 ┌─────────▼─────────┐
                 │  inference-webui  │ ← Open WebUI + Postgres
                 │   (DMZ VM)        │
                 └─────────┬─────────┘
           OLLAMA_BASE_URLS (allow-listed outbound to Inference VLAN)
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   ┌────────────┐   ┌────────────┐   ┌────────────┐
   │ inference- │   │ inference- │   │ inference- │
   │  ollama    │   │  ollama    │   │  ollama    │
   │ (GPU VM 1) │   │ (GPU VM 2) │   │ (GPU VM N) │
   │ :11434     │   │ :11434     │   │ :11434     │
   │ bound to   │   │ bound to   │   │ bound to   │
   │ internal IP│   │ internal IP│   │ internal IP│
   └────────────┘   └────────────┘   └────────────┘
   DCGM :9400 (profile-gated, for future Prometheus scrape)

Agents (NemoClaw, NetClaw, WisBot, Cowork):
  → consume WisAI's OpenAI-compatible API via Open WebUI, using a single
    endpoint URL stored at secret/{{ vault_secret_prefix }}/inference/endpoint
    (Open WebUI fans out to Ollama nodes via OLLAMA_BASE_URLS — the
     coordinator is both the user UI and the agent API surface)
```

### Why the WebUI does NOT share a VM with an Ollama node

Two independent objections from Round 1 review made co-location untenable:

1. **Security segmentation:** GPU VMs run privileged containers with NVIDIA Container Toolkit (elevated container-escape blast radius). They must sit on a separate VLAN with outbound-only firewall. A user-facing web app on that VM breaks the segmentation.
2. **Data durability:** Open WebUI's `open-webui-data` volume holds all user conversation history, the most sensitive artifact in a privacy-focused platform. Losing a GPU node (hardware fault or clean-redeploy) must not lose user history.

The WebUI runs on its own DMZ VM, with its own dedicated Postgres (per-service pattern, not the default SQLite — matches the NetBox/NocoDB convention).

---

## Alignment with existing standards

### From `AUTOMATION-COMPOSABILITY.md`
- ✅ 4-phase pattern: sparse-checkout → manage-secrets + setup-runtime-dir → run-deploy → verify-health
- ✅ Source/runtime separation: `~/agent-cloud/` (read-only) vs. `~/services/<name>/` (mutable env files)
- ✅ Variable contract: `_monorepo_dir`, `_deploy_dir`, `_runtime_dir`, `_sparse_paths`, `_secret_definitions`, `_env_templates`, `_symlinks`
- ✅ `deploy.sh` is container operations ONLY — no secret generation, no OpenBao calls, no GPU pre-checks
- ✅ Each workflow independent; Clean-deploy + revoke-service-credentials pattern
- ✅ All templates declared in `platform/semaphore/templates.yml` and applied via `setup-templates.yml`

### From `CREDENTIAL-LIFECYCLE-PLAN.md`
- ✅ Paths use `secret/{{ vault_secret_prefix }}/<service_name>`
- ✅ Every secret carries KV v2 custom metadata (`created_at`, `creator`, `purpose`, `rotation_schedule`)
- ✅ Create → Verify → Retire for every rotation (session key, admin password, SSH keys)
- ✅ AppRole TTLs (90d `secret_id`, 25 `token_num_uses`) when a per-service AppRole is introduced
- ✅ Weekly audit scope extended to `{{ vault_secret_prefix }}/inference-*`

---

## Implementation Steps

### Step 1 — Service directory structure

Remove the empty `platform/services/inference/` placeholder. Create three new directories:

```
platform/services/inference-ollama/
  deployment/
    CLAUDE.md                  # service-specific guidance
    deploy.sh                  # container ops only (no GPU checks, no OpenBao)
    config/
      profiles.yml             # inference_model_profile → {num_parallel, max_loaded_models, models}
    templates/
      ollama.env.j2
    compose/
      compose.node.yml         # Ollama + optional DCGM sidecar
  context/
    architecture/              # pointers to upstream WisAI docs

platform/services/inference-webui/
  deployment/
    CLAUDE.md
    deploy.sh
    templates/
      webui.env.j2
      postgres.env.j2
    compose/
      compose.webui.yml        # Open WebUI + per-service Postgres
  context/
    architecture/

platform/services/inference-vllm/
  .gitkeep                     # reserved; future plan
```

### Step 2 — Inventory groups

Add to `platform/inventory/production.yml` (public, placeholders only):

```yaml
inference_node_svc:
  hosts:
    "{{ inference_node_1_host }}":
      service_name: inference-ollama
      service_url: "http://{{ inference_node_1_host }}:11434"
      ollama_bind_ip: "{{ inference_node_1_host }}"     # = ansible_host
      health_path: "/api/tags"
      monorepo_deploy_path: platform/services/inference-ollama/deployment
      inference_model_profile: medium                   # small | medium | large
      gpu_count: 1
      # Models directory; override to a large disk if needed
      # models_path: /srv/ollama-models
  # Add more hosts as GPUs are provisioned

inference_webui_svc:
  hosts:
    "{{ inference_webui_host }}":
      service_name: inference-webui
      service_url: "http://{{ inference_webui_host }}:3000"
      health_path: "/health"
      monorepo_deploy_path: platform/services/inference-webui/deployment
```

Real IPs live in **site-config** (private). `ollama_bind_ip` defaults to `ansible_host`; override only when a VM has multiple NICs and you need a specific management interface.

### Step 3 — OpenBao secrets layout

All paths use `vault_secret_prefix` from site-config inventory (current single-site value: `services`).

**`inference-ollama` has NO stored secrets.** The worker has no runtime secrets — all config is inventory-driven. (Its SSH key is stored at `{{ vault_secret_prefix }}/ssh/inference-ollama` per the standard SSH pattern.)

**`inference-webui` secrets:**

| Path | Field | Type | Metadata |
|---|---|---|---|
| `{{ vault_secret_prefix }}/inference-webui` | `webui_secret_key` | random, 64 chars | `rotation_schedule: 180d`, `purpose: "Open WebUI session signing key"` |
| `{{ vault_secret_prefix }}/inference-webui` | `admin_password` | random, 24 chars | `rotation_schedule: 90d`, `purpose: "WebUI first-user admin bootstrap + rotation target"` |
| `{{ vault_secret_prefix }}/inference-webui` | `admin_email` | user-seeded (never generated) | `purpose: "Admin bootstrap email"` |
| `{{ vault_secret_prefix }}/inference-webui` | `admin_name` | user-seeded | `purpose: "Admin display name"` |
| `{{ vault_secret_prefix }}/inference-webui` | `postgres_password` | random, 48 chars | `rotation_schedule: 180d` (candidate for Phase 6 dynamic DB secrets in CREDENTIAL-LIFECYCLE-PLAN) |

Operator seeds `admin_email` and `admin_name` once before first deploy:

```bash
bao kv patch secret/{{ vault_secret_prefix }}/inference-webui \
  admin_email="admin@uhstray.io" admin_name="WisAI Admin"
```

**`ollama_nodes` is NOT a secret.** The coordinator computes it from the `inference_node_svc` inventory group at deploy time:

```yaml
- name: "Compute ollama_nodes from inference_node_svc group"
  ansible.builtin.set_fact:
    ollama_nodes: >-
      {{ groups['inference_node_svc']
         | map('extract', hostvars)
         | map(attribute='service_url')
         | join(';') }}
```

Adding a node = add to inventory + rerun `deploy-wisai-webui.yml` (self-healing).

### Step 4 — Jinja2 templates

**`platform/services/inference-ollama/deployment/templates/ollama.env.j2`:**

```jinja2
# Ollama inference node — templated by Ansible (inventory + profile, no secrets)
OLLAMA_BIND_IP={{ hostvars[inventory_hostname].ansible_host }}
OLLAMA_PORT=11434
OLLAMA_HOST={{ hostvars[inventory_hostname].ansible_host }}:11434
OLLAMA_KEEP_ALIVE={{ ollama_keep_alive | default('5m') }}
OLLAMA_NUM_PARALLEL={{ _profile.num_parallel }}
OLLAMA_MAX_LOADED_MODELS={{ _profile.max_loaded_models }}
OLLAMA_FLASH_ATTENTION={{ ollama_flash_attention | default('1') }}
GPU_COUNT={{ gpu_count | default('1') }}
MODELS_PATH={{ models_path | default('') }}
WISAI_OBSERVABILITY_PROFILE={{ wisai_observability_profile | default('') }}
```

Bind is to `ansible_host` (the operator-declared management IP), not `0.0.0.0`. `service_url` uses the same IP — WebUI-reachability invariant preserved.

**`platform/services/inference-webui/deployment/templates/webui.env.j2`:**

```jinja2
# Open WebUI coordinator — templated by Ansible from OpenBao + inventory
WEBUI_PORT=3000
OLLAMA_BASE_URLS={{ ollama_nodes }}

# Session signing
WEBUI_SECRET_KEY={{ secrets.webui_secret_key }}

# Lockdown (verified against upstream docs/getting-started/advanced-topics/hardening.md)
WEBUI_AUTH=true
ENABLE_SIGNUP=false
ENABLE_LOGIN_FORM=true
DEFAULT_USER_ROLE=pending

# First-boot admin bootstrap (no-op after first user exists, per upstream)
WEBUI_ADMIN_EMAIL={{ secrets.admin_email }}
WEBUI_ADMIN_NAME={{ secrets.admin_name | default('WisAI Admin') }}
WEBUI_ADMIN_PASSWORD={{ secrets.admin_password }}

# Dedicated Postgres (not SQLite)
DATABASE_URL=postgresql://webui:{{ secrets.postgres_password }}@postgres:5432/webui
```

**`platform/services/inference-webui/deployment/templates/postgres.env.j2`:**

```jinja2
POSTGRES_USER=webui
POSTGRES_PASSWORD={{ secrets.postgres_password }}
POSTGRES_DB=webui
```

### Step 5 — Compose files

**`platform/services/inference-ollama/deployment/compose/compose.node.yml`:**

```yaml
# Single Ollama inference node + profile-gated DCGM exporter
services:
  ollama:
    image: ollama/ollama:latest
    container_name: ollama
    restart: unless-stopped
    ports:
      - "${OLLAMA_BIND_IP}:${OLLAMA_PORT:-11434}:11434"
    volumes:
      - ${MODELS_PATH:-ollama-data}:/root/.ollama
    environment:
      - OLLAMA_HOST=${OLLAMA_HOST}
      - OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE:-5m}
      - OLLAMA_NUM_PARALLEL=${OLLAMA_NUM_PARALLEL:-2}
      - OLLAMA_MAX_LOADED_MODELS=${OLLAMA_MAX_LOADED_MODELS:-2}
      - OLLAMA_FLASH_ATTENTION=${OLLAMA_FLASH_ATTENTION:-1}
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: ${GPU_COUNT:-1}
              capabilities: [gpu]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:11434/api/tags"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s

  # Profile-gated; default profile is "" (off). To enable:
  # set wisai_observability_profile: observability in inventory.
  dcgm-exporter:
    image: nvcr.io/nvidia/k8s/dcgm-exporter:3.3.5-3.4.0-ubuntu22.04
    profiles: ["observability"]
    restart: unless-stopped
    ports:
      - "${OLLAMA_BIND_IP}:9400:9400"
    cap_add: [SYS_ADMIN]
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

volumes:
  ollama-data:
```

> DCGM scrape wiring (Prometheus target, Grafana dashboard) lands in the o11y service plan. Exposing `:9400` now means zero redeploy later.

**`platform/services/inference-webui/deployment/compose/compose.webui.yml`:**

```yaml
# Open WebUI + dedicated Postgres
services:
  postgres:
    image: postgres:16-alpine
    container_name: webui-postgres
    restart: unless-stopped
    env_file: env/postgres.env
    volumes:
      - webui-postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U webui -d webui"]
      interval: 10s
      timeout: 5s
      retries: 5

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    ports:
      - "${WEBUI_PORT:-3000}:8080"
    volumes:
      - open-webui-data:/app/backend/data
    environment:
      - OLLAMA_BASE_URLS=${OLLAMA_BASE_URLS}
      - WEBUI_SECRET_KEY=${WEBUI_SECRET_KEY}
      - WEBUI_AUTH=${WEBUI_AUTH}
      - ENABLE_SIGNUP=${ENABLE_SIGNUP}
      - ENABLE_LOGIN_FORM=${ENABLE_LOGIN_FORM}
      - DEFAULT_USER_ROLE=${DEFAULT_USER_ROLE}
      - WEBUI_ADMIN_EMAIL=${WEBUI_ADMIN_EMAIL}
      - WEBUI_ADMIN_NAME=${WEBUI_ADMIN_NAME}
      - WEBUI_ADMIN_PASSWORD=${WEBUI_ADMIN_PASSWORD}
      - DATABASE_URL=${DATABASE_URL}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 60s

volumes:
  webui-postgres-data:
  open-webui-data:
```

### Step 6 — Profile config

**`platform/services/inference-ollama/deployment/config/profiles.yml`** (versioned, PR-reviewed, not a bash case statement):

```yaml
# Inference model profiles — prescriptive mapping from profile name to
# runtime tuning + model list. Profile choice is per-host via
# `inference_model_profile` inventory var. PR-review any change.
profiles:
  small:   # 8–12 GB VRAM
    num_parallel: 2
    max_loaded_models: 1
    models:
      - "llama3.2:3b-instruct-q4_K_M"
      - "phi3.5:3.8b-mini-instruct-q4_K_M"
      - "nomic-embed-text:v1.5"
  medium:  # 16–24 GB VRAM
    num_parallel: 4
    max_loaded_models: 2
    models:
      - "llama3.1:8b-instruct-q4_K_M"
      - "qwen2.5-coder:7b-instruct-q4_K_M"
      - "nomic-embed-text:v1.5"
  large:   # 48 GB+
    num_parallel: 8
    max_loaded_models: 3
    models:
      - "llama3.3:70b-instruct-q4_K_M"
      - "qwen2.5:32b-instruct-q4_K_M"
      - "nomic-embed-text:v1.5"
```

All model tags are explicit versions. Pinning by digest (`model:tag@sha256:…`) is preferred when Ollama's registry supports it for the tag; until then, explicit version tags are the minimum.

### Step 7 — Playbooks (10 Semaphore templates)

All deploy playbooks use the **4-phase pattern** (no legacy `deploy-service.yml` wrapper). Skeleton for nodes:

**`platform/playbooks/deploy-wisai-node.yml`:**

```yaml
---
# Phase 0: GPU + NVIDIA Container Toolkit verification
- name: "Phase 0: Verify GPU runtime"
  hosts: "{{ target_host | default('inference_node_svc') }}"
  gather_facts: false
  tasks:
    - include_tasks: tasks/verify-gpu.yml

# Phase 1: Sparse checkout + profile + secrets + runtime dir
- name: "Phase 1: Source + profile + secrets + runtime"
  hosts: "{{ target_host | default('inference_node_svc') }}"
  gather_facts: false
  vars:
    _monorepo_dir: "/home/{{ ansible_user }}/agent-cloud"
    _deploy_dir:   "{{ _monorepo_dir }}/{{ monorepo_deploy_path }}"
    _runtime_dir:  "/home/{{ ansible_user }}/services/{{ service_name }}"
    _sparse_paths:
      - "platform/services/inference-ollama/deployment"
      - "platform/lib"
    _secret_definitions: []                # worker has no stored secrets
    _env_templates:
      - { src: ollama.env.j2, dest: .env }
    _symlinks:
      - { src: "{{ _deploy_dir }}/compose/compose.node.yml", dest: "docker-compose.yml" }
      - { src: "{{ _monorepo_dir }}/platform/lib",           dest: "lib" }
  tasks:
    - include_tasks: tasks/sparse-checkout.yml
    - name: "Load inference profile settings"
      ansible.builtin.include_vars:
        file: "{{ _deploy_dir }}/config/profiles.yml"
        name: _profiles
    - name: "Select profile for this host"
      ansible.builtin.set_fact:
        _profile: "{{ _profiles.profiles[inference_model_profile | default('medium')] }}"
    - include_tasks: tasks/manage-secrets.yml
    - include_tasks: tasks/setup-runtime-dir.yml

# Phase 2: Container operations
- name: "Phase 2: Start Ollama"
  hosts: "{{ target_host | default('inference_node_svc') }}"
  gather_facts: false
  tasks:
    - include_tasks: tasks/run-deploy.yml

# Phase 3: Verify
- name: "Phase 3: Verify node health"
  hosts: "{{ target_host | default('inference_node_svc') }}"
  gather_facts: false
  tasks:
    - include_tasks: tasks/verify-health.yml
      vars:
        _health_url: "{{ service_url }}{{ health_path }}"
```

**`platform/playbooks/deploy-wisai-webui.yml`:**

```yaml
---
# Phase 0: Verify all worker nodes are reachable
- name: "Phase 0: Verify upstream Ollama backends"
  hosts: inference_webui_svc
  gather_facts: false
  tasks:
    - name: "Each inference node must respond on /api/tags"
      ansible.builtin.uri:
        url: "{{ item }}/api/tags"
        status_code: [200]
      loop: "{{ groups['inference_node_svc'] | map('extract', hostvars, 'service_url') | list }}"

# Phase 1: Compute ollama_nodes + sparse + secrets + runtime
- name: "Phase 1: Source + secrets + runtime"
  hosts: inference_webui_svc
  gather_facts: false
  vars:
    _monorepo_dir: "/home/{{ ansible_user }}/agent-cloud"
    _deploy_dir:   "{{ _monorepo_dir }}/{{ monorepo_deploy_path }}"
    _runtime_dir:  "/home/{{ ansible_user }}/services/{{ service_name }}"
    _sparse_paths:
      - "platform/services/inference-webui/deployment"
      - "platform/lib"
    _secret_definitions:
      - { name: webui_secret_key,   type: random, length: 64 }
      - { name: admin_password,     type: random, length: 24 }
      - { name: admin_email,        type: user }
      - { name: admin_name,         type: user }
      - { name: postgres_password,  type: random, length: 48 }
    _secret_metadata:
      purpose: "Open WebUI coordinator (session key, admin bootstrap, DB password)"
      rotation_schedule: "mixed"            # per-field in CLP audit
    _env_templates:
      - { src: webui.env.j2,    dest: .env }
      - { src: postgres.env.j2, dest: env/postgres.env }
    _symlinks:
      - { src: "{{ _deploy_dir }}/compose/compose.webui.yml", dest: "docker-compose.yml" }
      - { src: "{{ _monorepo_dir }}/platform/lib",            dest: "lib" }
  tasks:
    - include_tasks: tasks/sparse-checkout.yml
    - name: "Compute ollama_nodes from inventory"
      ansible.builtin.set_fact:
        ollama_nodes: >-
          {{ groups['inference_node_svc']
             | map('extract', hostvars)
             | map(attribute='service_url')
             | join(';') }}
    - include_tasks: tasks/manage-secrets.yml
    - include_tasks: tasks/setup-runtime-dir.yml

# Phase 2: Container operations
- name: "Phase 2: Start Open WebUI + Postgres"
  hosts: inference_webui_svc
  gather_facts: false
  tasks:
    - include_tasks: tasks/run-deploy.yml

# Phase 3: Verify
- name: "Phase 3: Verify WebUI"
  hosts: inference_webui_svc
  gather_facts: false
  tasks:
    - include_tasks: tasks/verify-health.yml
      vars:
        _health_url: "{{ service_url }}{{ health_path }}"
```

No patch to `manage-secrets.yml` is required. Pre-setting `ollama_nodes` and `_profile` via `set_fact` puts them in the Jinja2 scope that the template task already sees. (The initially-proposed `_extra_template_vars` hook was retracted in Round 2 as over-engineered.)

**The 10 Semaphore templates:**

| # | Template | Playbook | Purpose |
|---|---|---|---|
| 1 | Install NVIDIA Toolkit | `install-nvidia-toolkit.yml` | One-time per GPU VM; installs driver + toolkit + configures Docker runtime. |
| 2 | Deploy WisAI Node | `deploy-wisai-node.yml` | Per-node worker deploy (Semaphore survey var targets a single host). |
| 3 | Deploy WisAI WebUI | `deploy-wisai-webui.yml` | Coordinator + Postgres; self-heals on new nodes. |
| 4 | Pull WisAI Models | `pull-wisai-models.yml` | Reads profile; idempotent (skips existing); `async: 7200, poll: 30` per model. |
| 5 | Update WisAI Node | `update-wisai-node.yml` | Thin wrapper over generic `update-service.yml`. |
| 6 | Update WisAI WebUI | `update-wisai-webui.yml` | Same pattern. |
| 7 | Clean Deploy WisAI Node | `clean-deploy-wisai-node.yml` | Revoke → clean → deploy. `wipe_models=false` by default. |
| 8 | Clean Deploy WisAI WebUI | `clean-deploy-wisai-webui.yml` | Revoke → clean → deploy. **Warns operator** that conversation history is destroyed. |
| 9 | Rotate WebUI Credentials | `rotate-webui-credentials.yml` | Create→Verify→Retire for `webui_secret_key` (180d), `admin_password` (90d), `postgres_password` (180d). |
| 10 | Validate WisAI | `validate-wisai.yml` | E2E connectivity: each node responds, WebUI answers, WebUI→node reachability matches inventory. |

### Step 8 — Semaphore templates

Append the 10 entries above to `platform/semaphore/templates.yml`, then run `setup-templates.yml`. No ad-hoc API calls.

### Step 9 — SSH keys

Standard pattern. Two key pairs:
- `secret/{{ vault_secret_prefix }}/ssh/inference-ollama` (shared across all GPU nodes)
- `secret/{{ vault_secret_prefix }}/ssh/inference-webui`

Distribute with `distribute-ssh-keys.yml`, then `harden-ssh.yml`. Metadata: `rotation_schedule: 365d` (per CREDENTIAL-LIFECYCLE-PLAN annual rotation).

### Step 10 — AppRole provisioning (deferred, tracked)

`inference-ollama` and `inference-webui` do **not** fetch secrets at runtime — Ansible templates everything at deploy time. Semaphore's orchestrator AppRole is sufficient for deploy-time writes.

**Per-service AppRoles become required when any of these triggers occur** (add to CLAUDE.md deployment status tracker; review on every Phase bump):

1. WebUI configures OAuth/SSO providers (Authentik integration)
2. External model API keys added to WebUI (Anthropic, OpenAI)
3. WebUI reads per-user API keys from vault at runtime
4. Agents consume per-agent virtual keys from an inference gateway (scheduled by `INFERENCE-GATEWAY-PLAN.md`)
5. Dynamic Postgres secrets migration (Phase 6 of CREDENTIAL-LIFECYCLE-PLAN)

When a trigger fires, create the AppRole via `tasks/manage-approle.yml` with the minimal HCL policy scoped to that service's paths (`secret/data/{{ vault_secret_prefix }}/inference-webui/*` + needed dynamic engine paths). Default TTLs apply (`90d` `secret_id`, `25` `token_num_uses`).

### Step 11 — NVIDIA toolkit pre-requisite playbook

`platform/playbooks/install-nvidia-toolkit.yml` — standalone, idempotent:

1. Install NVIDIA driver (if not present; version pinned per GPU generation)
2. Add NVIDIA container-toolkit APT repo
3. Install `nvidia-container-toolkit` (pinned version)
4. Run `nvidia-ctk runtime configure --runtime=docker`
5. Restart Docker daemon
6. Smoke test: `docker run --rm --gpus all nvidia/cuda:<pinned> nvidia-smi`
7. Pin the CUDA image used for smoke test to the same version site-wide

Runs once per GPU VM, before first `deploy-wisai-node.yml`. Smoke test lives **here** (not in deploy.sh) so redeploys do not re-pull multi-GB CUDA images.

### Step 12 — `tasks/verify-gpu.yml`

Lightweight Phase-0 check (not the heavy smoke test):

1. Assert `nvidia-smi` present on PATH
2. Assert docker daemon reports `nvidia` runtime (`docker info --format '{{.Runtimes}}' | grep nvidia`)
3. Fail fast with message pointing to `install-nvidia-toolkit.yml` if either fails

Runs on every `deploy-wisai-node.yml` invocation. Does not pull any images.

---

## Network hardening

### Ollama listener posture

- `OLLAMA_HOST` and `OLLAMA_BIND_IP` set to the node's `ansible_host` — **not** `0.0.0.0`.
- Host port publish syntax `${OLLAMA_BIND_IP}:${OLLAMA_PORT}:11434` (compose supports `HOST_IP:PORT:CONTAINER_PORT`).
- pfSense rules (managed in site-config or synced from NetBox via `run-pfsense-sync.yml`):
  - Allow TCP/11434 from `inference_webui_host` → each node
  - Allow TCP/9400 from future Prometheus host → each node (when o11y lands)
  - Default deny on 11434/9400 from every other source
- `validate-wisai.yml` must include a negative test: from a non-allowlisted VM, `curl http://node:11434/` MUST fail.

### VLAN segmentation

- GPU nodes on their own VLAN (`inference-vlan`)
- WebUI on DMZ VLAN
- Outbound-only firewall on GPU VLAN — GPU nodes never initiate connections to other service VMs
- Management path to GPU nodes via bastion (Semaphore VM) only; no direct ingress from operator workstations

### Recommended mid-term (within 30 days of first production deploy)

- Caddy reverse proxy in front of each Ollama instance with shared-secret bearer auth (token in OpenBao, 180d rotation); Open WebUI passes the header via `OLLAMA_API_CONFIGS`
- WireGuard or Tailscale mesh between WebUI and nodes; Ollama binds to the wg interface only
- OpenBao audit backend enabled with alerts on `{{ vault_secret_prefix }}/inference-*` reads outside deploy windows

---

## Credential lifecycle

### Rotation playbook: `rotate-webui-credentials.yml`

Create→Verify→Retire per `tasks/rotate-credential.yml` wrapper:

| Secret | Schedule | Verify step | Retire step |
|---|---|---|---|
| `webui_secret_key` | 180d | New key accepted → test login | Old key invalidated by restart (all sessions logged out — expected) |
| `admin_password` | 90d | Test login with new password | Old admin password no longer works |
| `postgres_password` | 180d | Test `psql` connect with new password | Rotated in-place via `ALTER USER` |
| SSH key (`inference-ollama`) | 365d | New key SSH succeeds | Remove old key from authorized_keys |
| SSH key (`inference-webui`) | 365d | Same | Same |

Rotating `webui_secret_key` kicks off all active users — document in operator-facing CLAUDE.md in `platform/services/inference-webui/deployment/`.

### Audit

`audit-credentials.yml` runs weekly with scope extended to `{{ vault_secret_prefix }}/inference-*`. Flags:
- Missing `created_at`/`creator`/`purpose`/`rotation_schedule`
- Credentials past `rotation_schedule`
- Orphaned (no matching service in inventory)

### Dynamic DB secrets (future)

`postgres_password` is a candidate for migration to OpenBao's database secret engine (Phase 6 of CREDENTIAL-LIFECYCLE-PLAN — 1-hour TTL leases). When that phase ships, WebUI's `DATABASE_URL` is rendered at container startup from a vault lookup rather than templated at deploy time.

---

## Model lifecycle

### Pull — `pull-wisai-models.yml`

- Reads `_profile.models` from `profiles.yml`
- For each model: `ollama list | grep -q "<name>"` → skip if present
- Otherwise `ollama pull <name:tag>` as an Ansible task with `async: 7200, poll: 30`
- Per-model failure does not abort the batch (use `ignore_errors` + collect results + final summary task)
- Safe to re-run any time; advancing the profile just adds new models to pull on next run

### Pin and version

- Profile manifest uses explicit tags (e.g., `llama3.1:8b-instruct-q4_K_M`), not `latest`
- Digest pinning (`model:tag@sha256:…`) is preferred when available; document the digest in the manifest comment

### Prune — `prune-wisai-models.yml`

Deferred to first operator complaint about disk, then scheduled monthly:
- Diff `ollama list` against `profiles.yml` on each node
- `ollama rm <model>` for entries not in the manifest (with a `dry_run: true` default and explicit `confirm_delete: true` var to actually prune)

### Disaster recovery

Models are not backed up — they're regenerable from manifest + network. **Back up the manifest, not the models.** Conversation data (Postgres volume) is the DR target — nightly snapshot, encrypted at rest.

---

## Observability (minimal, day-one)

- DCGM exporter sidecar in `compose.node.yml` under the `observability` compose profile
- Default: profile unset (sidecar not started) — operator opts in by setting `wisai_observability_profile: observability` in inventory
- Exposed port `:9400` bound to the node's internal IP, allow-listed to the future Prometheus VM
- Full Prometheus/Grafana wiring is in the o11y service plan; this plan only ensures zero-redeploy adoption later

---

## Scaling: adding a new GPU node

1. Provision a new Proxmox VM with GPU passthrough (`provision-vm.yml`)
2. Put it on the inference VLAN
3. Run `install-docker.yml` + `install-nvidia-toolkit.yml`
4. Add the host to `inference_node_svc` in **site-config** inventory (with `ollama_bind_ip`, `inference_model_profile`)
5. Distribute SSH key: `distribute-ssh-keys.yml` scoped to the new host
6. Run "Deploy WisAI Node" in Semaphore targeting the new host
7. Run "Pull WisAI Models" for the new host (uses the host's profile)
8. Run "Deploy WisAI WebUI" — **automatically picks up the new node** (`ollama_nodes` recomputed from inventory)
9. Update pfSense allow rule to include the new node IP (or let `run-pfsense-sync.yml` sync it from NetBox tags)

No OpenBao writes required to add a node. No code changes.

---

## Required changes (scope of this plan vs. cross-repo ripple)

### This plan creates/edits in agent-cloud (PR-reviewable)

| File / Directory | Action |
|---|---|
| `platform/services/inference/` | **Remove** (bare placeholder; no content to migrate) |
| `platform/services/inference-ollama/` | **Create** (deployment/, context/, config/profiles.yml, templates/, compose/) |
| `platform/services/inference-webui/` | **Create** (deployment/, context/, templates/, compose/) |
| `platform/services/inference-vllm/` | **Create** (.gitkeep only — reserved) |
| `platform/inventory/production.yml` | **Edit** — add `inference_node_svc` and `inference_webui_svc` groups |
| `platform/playbooks/deploy-wisai-node.yml` | **Create** (4-phase) |
| `platform/playbooks/deploy-wisai-webui.yml` | **Create** (4-phase, inventory-driven `ollama_nodes`) |
| `platform/playbooks/update-wisai-node.yml` | **Create** (thin wrapper over `update-service.yml`) |
| `platform/playbooks/update-wisai-webui.yml` | **Create** (thin wrapper) |
| `platform/playbooks/pull-wisai-models.yml` | **Create** — reads `profiles.yml`, idempotent, async |
| `platform/playbooks/clean-deploy-wisai-node.yml` | **Create** — revoke → clean → deploy |
| `platform/playbooks/clean-deploy-wisai-webui.yml` | **Create** — revoke → clean → deploy; warns on data destruction |
| `platform/playbooks/rotate-webui-credentials.yml` | **Create** — Create→Verify→Retire for all three WebUI secrets |
| `platform/playbooks/validate-wisai.yml` | **Create** — E2E + negative network tests |
| `platform/playbooks/install-nvidia-toolkit.yml` | **Create** — standalone pre-requisite |
| `platform/playbooks/tasks/verify-gpu.yml` | **Create** — lightweight Phase-0 check |
| `platform/semaphore/templates.yml` | **Edit** — add the 10 templates listed above |
| `CLAUDE.md` (root) | **Edit** — add `inference-ollama` / `inference-webui` to deployment status; document vLLM slot as reserved |

### Ripple changes in other agent-cloud docs (same PR, avoid drift)

| File | Edit |
|---|---|
| `CLAUDE.md` (root, AI Layer description around line 17) | Change to `Ollama (current) + vLLM/llama.cpp (reserved for 24GB+ nodes), OpenAI-compatible HTTP` |
| `plan/development/IMPLEMENTATION_PLAN.md` §Inference Backbone (~L209–224) | Split into two rows: `inference-ollama` (current shipping), `inference-vllm` (reserved); update monorepo locations |
| `plan/development/IMPLEMENTATION_PLAN.md` agent table (~L148–151) | Replace `vLLM` cells with `OpenAI-compatible (Ollama today, vLLM when available)` — agents are engine-agnostic |
| `plan/development/IMPLEMENTATION_PLAN.md` WisAI carve-out (~L1846) | Revise to: `WisAI — upstream compose reference; integrated as platform/services/inference-ollama/; IS the current platform backbone` |
| `plan/architecture/AUTOMATION-COMPOSABILITY.md` implemented workflows table | Add WisAI rows after NetBox proves out |

### Required changes in site-config (private repo)

| File | Edit |
|---|---|
| `inventory/production.yml` | Add real IPs for inference nodes + webui host; set `vault_secret_prefix: "services"` if not already |
| OpenBao seeding | `bao kv patch secret/services/inference-webui admin_email=... admin_name=...` (once, before first deploy) |
| pfSense rules | Allow `inference_webui_host` → `inference_node_svc:11434`; allow Prometheus → nodes:9400 (when o11y lands); default-deny |

### Out of scope (separate plans)

| Concern | Plan |
|---|---|
| vLLM deployment (when 24GB+ hardware arrives) | `plan/development/INFERENCE-VLLM-PLAN.md` (future, additive to WisAI) |
| Caddy + Authentik OIDC in front of WebUI | Caddy service plan + Authentik integration plan |
| Prometheus scraping + Grafana dashboards for DCGM/Ollama | o11y service plan |

---

## Open questions

1. **Caddy + Authentik for WebUI.** Architect flagged: once WebUI has persistent conversation history, Caddy with OIDC should land in the *same* deployment, not "eventually." Needs a discrete Caddy-integration plan; if that plan isn't ready, document the interim posture (VPN-only access to WebUI, no public exposure).
2. **GPU VLAN management path.** Outbound-only firewall on GPU VMs means Semaphore cannot reach them directly unless routed through a bastion. Confirm Semaphore → bastion → GPU-node SSH works with the key distribution in `distribute-ssh-keys.yml`.
3. **DCGM image pinning.** `nvcr.io/nvidia/k8s/dcgm-exporter` requires NGC. Confirm air-gap posture; may need to mirror to Harbor when Harbor lands.
4. **`OLLAMA_FLASH_ATTENTION=1` compatibility.** Safe on Ampere+ (RTX 30xx and newer). Pascal/Turing may need per-node opt-out via inventory var — document and provide override.
5. **Postgres dynamic credentials migration.** When CREDENTIAL-LIFECYCLE-PLAN Phase 6 ships, `postgres_password` moves from static KV to OpenBao DB engine with 1h lease. WebUI entrypoint must be able to re-read credentials periodically or reconnect on auth failure. Track as follow-up after Phase 6 scaffolding.

---

## Validation criteria

| Check | Pass condition |
|---|---|
| Runtime dir isolation | `~/services/inference-ollama/` and `~/services/inference-webui/` exist; no generated files in `~/agent-cloud/` |
| Source/runtime split honored | `git -C ~/agent-cloud status` clean after deploy |
| Listener binds internal IP | `ss -tlnp | grep 11434` shows `<ansible_host>:11434`, not `0.0.0.0:11434` |
| `ollama_nodes` inventory-driven | Adding a node + rerunning WebUI deploy updates `OLLAMA_BASE_URLS` with zero OpenBao writes |
| Secrets path uses prefix | `bao kv get secret/{{ vault_secret_prefix }}/inference-webui` returns all five fields |
| Metadata present | `bao kv metadata get secret/{{ vault_secret_prefix }}/inference-webui` has `created_at`, `creator`, `purpose`, `rotation_schedule` |
| Admin bootstrap works on first deploy | First visit to WebUI goes straight to login (no signup screen) |
| Admin bootstrap idempotent | Redeploy does NOT reset admin password or create duplicate admin |
| Signup disabled | `ENABLE_SIGNUP=false` in `.env`; signup form not rendered |
| Network negative test | From non-allowlisted VM, `curl http://node:11434/` fails (connection refused) |
| Health endpoints correct | `/api/tags` on Ollama, `/health` on WebUI — both return 200 |
| Clean deploy preserves models | `clean-deploy-wisai-node.yml` with default vars leaves ollama-data volume intact |
| Clean deploy warns on WebUI | `clean-deploy-wisai-webui.yml` prompts operator about conversation history loss |
| DCGM exporter opt-in | Default: no DCGM container running. With `wisai_observability_profile: observability`, DCGM runs and `:9400/metrics` returns Prometheus text |
| Rotation is C-V-R | Rotating `webui_secret_key` leaves old key active until new key verified working |
| Validate playbook E2E | `validate-wisai.yml` reports all nodes healthy, WebUI healthy, and WebUI→node reachability matches inventory |

---

## Review Decisions Ledger (audit trail)

This plan was reconciled across four specialist reviewers. Points of disagreement and their resolutions:

| # | Issue | R1 positions | R2 resolution | Owner |
|---|---|---|---|---|
| 1 | Directory slot for WisAI | LLM/Data flagged tri-way contradiction with CLAUDE.md + IMPLEMENTATION_PLAN L1846; others missed it | Option C: `inference-ollama/`, `inference-webui/`, reserve `inference-vllm/`; update CLAUDE.md + IMPLEMENTATION_PLAN ripple edits in same PR | Architect |
| 2 | LiteLLM as agent gateway | LLM/Data proposed; Automation + Security + Architect silent | **Rejected from the design.** WisAI is the platform inference backbone; Open WebUI's OpenAI-compatible API is the agent surface. Agents consume a single OpenBao-stored endpoint URL. No LiteLLM, no separate gateway service. | Architect |
| 3 | WebUI co-location on first inference node | WISAI plan recommended it; Architect accepted in R1; Security + LLM/Data rejected | **Reject co-location**; WebUI on own DMZ VM with dedicated Postgres | Architect (revised) |
| 4 | DCGM observability sidecar scope | LLM/Data argued day-one; Automation deferred to o11y plan | **In scope, minimal**: sidecar in compose template under `observability` profile (off by default); wiring in o11y plan | Architect |
| 5 | `ollama_nodes` classification | Unanimous: config, not secret | Computed from `groups['inference_node_svc']` via `set_fact` pre-hook | All |
| 6 | `_extra_template_vars` patch to `manage-secrets.yml` | Automation R1 proposed it | **Retracted** by Automation in R2: use `set_fact` instead; no infra patch needed | Automation |
| 7 | `gpu_vram_profile` naming | LLM/Data: prescriptive var wrongly named descriptive | Rename to `inference_model_profile`; profile→settings mapping in `config/profiles.yml` | Automation |
| 8 | Ollama binding 0.0.0.0 | Security: HIGH must-fix | `OLLAMA_BIND_IP=ansible_host`; compose `${IP}:port:port` syntax; pfSense allow-list | Security + Automation |
| 9 | WebUI admin bootstrap race | Security CR-5 | Verified upstream supports `WEBUI_ADMIN_*` env vars; `ENABLE_SIGNUP=false`; seeded `admin_email` / `admin_name`, generated `admin_password` | Automation |
| 10 | Per-service AppRole | Security: defensible to defer, but must list triggers | Deferred with 5 explicit triggers documented in Step 10 | Security |
