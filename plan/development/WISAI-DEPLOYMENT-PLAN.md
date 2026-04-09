# WisAI Deployment Plan

**Date:** 2026-04-07
**Status:** PLANNING
**Context:** WisAI is a self-hosted local LLM inference stack (Ollama + Open WebUI) designed for consumer NVIDIA GPUs across Proxmox VMs. This plan integrates WisAI into the agent-cloud Semaphore deployment pipeline with multi-node support.

---

## Problem

WisAI currently has infrastructure files (`docker-compose.yml`, `docker-compose.multi.yml`, scripts) but no integration with the agent-cloud deployment pipeline. There is no way to deploy WisAI through Semaphore, manage its secrets via OpenBao, or provision multiple inference nodes automatically. The existing `platform/services/inference/` directory is a placeholder (`.gitkeep` only).

---

## Architecture: WisAI in agent-cloud

WisAI is fundamentally different from other agent-cloud services (NetBox, NocoDB, n8n, etc.) in two ways:

1. **GPU dependency** — requires NVIDIA GPU passthrough and the NVIDIA Container Toolkit
2. **Multi-node by design** — each GPU node runs an independent Ollama instance; a coordinator node runs Open WebUI pointing at all backends

This means **one Semaphore deployment is NOT sufficient**. The deployment must handle:
- N independent Ollama worker nodes (one per GPU VM)
- 1 coordinator node running Open WebUI with load balancing across all workers
- The worker list is dynamic — adding a GPU means adding an Ollama endpoint

### Proposed Topology

```
Semaphore
  ├── "Deploy WisAI Node"     → deploys Ollama on a single GPU VM (run per node)
  ├── "Deploy WisAI WebUI"    → deploys Open WebUI coordinator (run once)
  ├── "Update WisAI Node"     → pulls latest images + re-pulls models on a node
  ├── "Update WisAI WebUI"    → pulls latest Open WebUI image
  └── "Pull WisAI Models"     → pulls models on a specific node by VRAM profile
```

### Why Multiple Templates, Not One

A single "Deploy WisAI" template would need to:
- Know which hosts are Ollama workers vs. the WebUI coordinator
- Handle GPU detection per host
- Manage a dynamic endpoint list

This violates the agent-cloud principle that **each workflow is independent** (CLAUDE.md rule #3). Instead:
- **Worker nodes** are deployed independently — each gets Ollama + NVIDIA runtime
- **Coordinator** is deployed after workers — it reads the list of Ollama endpoints from inventory/OpenBao
- Adding a new GPU node = add it to inventory + run "Deploy WisAI Node" — no change to the coordinator playbook

---

## Implementation Steps

### Step 1: Create the Service Directory Structure

Populate the existing `platform/services/inference/` placeholder:

```
platform/services/inference/
  deployment/
    CLAUDE.md               ← service-specific guidance
    deploy-node.sh          ← container lifecycle for a single Ollama node
    deploy-webui.sh         ← container lifecycle for the Open WebUI coordinator
    pull-models.sh          ← model pull script (adapted from WisAI)
    templates/
      ollama.env.j2         ← Jinja2 template for Ollama node .env
      webui.env.j2          ← Jinja2 template for Open WebUI .env
    compose/
      compose.node.yml      ← single-node Ollama compose (from WisAI docker-compose.yml)
      compose.webui.yml     ← Open WebUI coordinator compose (from WisAI docker-compose.multi.yml)
  context/
    architecture/           ← WisAI architecture docs (reference)
```

**Key decisions for the compose files:**
- `compose.node.yml` runs Ollama only (no Open WebUI) — each GPU VM is a headless inference backend
- `compose.webui.yml` runs Open WebUI only — it connects to all Ollama nodes via `OLLAMA_BASE_URLS`
- GPU access uses the Docker `deploy.resources.reservations.devices` block (NVIDIA Container Toolkit)
- The `devices: nvidia.com/gpu=all` line from WisAI's compose (Podman CDI) should be removed — production targets Docker on Proxmox VMs

### Step 2: Define Secrets in OpenBao

WisAI has minimal secrets compared to database-backed services, but we still need:

| Secret Path | Contents | Purpose |
|---|---|---|
| `secret/services/inference` | `webui_secret_key` (random), `ollama_nodes` (semicolon-separated endpoints) | Open WebUI session key, dynamic node list |

**Note:** Ollama itself has no authentication — it binds to `0.0.0.0:11434` and relies on network-level access control (firewall rules). This is a known limitation. If API auth is needed in the future, a reverse proxy (Caddy) with basic auth is the path forward. The existing `platform/services/caddy/` service could be extended for this.

The `ollama_nodes` value in OpenBao serves as the **source of truth for which nodes exist**. When a new node is added, the operator updates this value and re-runs the WebUI deploy. This is simpler than having Ansible dynamically discover nodes.

### Step 3: Add Inventory Groups

Add to `platform/inventory/production.yml`:

```yaml
    inference_node_svc:
      hosts:
        "{{ inference_node_1_host }}":
          service_name: inference-node
          service_url: "http://{{ inference_node_1_host }}:11434"
          health_path: "/"
          monorepo_deploy_path: platform/services/inference/deployment
          gpu_vram_profile: "12gb"  # controls which models pull-models.sh downloads
        # Add more nodes as GPUs are provisioned:
        # "{{ inference_node_2_host }}":
        #   ...

    inference_webui_svc:
      hosts:
        "{{ inference_webui_host }}":
          service_name: inference-webui
          service_url: "http://{{ inference_webui_host }}:3000"
          health_path: "/"
          monorepo_deploy_path: platform/services/inference/deployment
```

**Open question:** Should the WebUI coordinator run on its own VM or co-locate on one of the GPU nodes? Open WebUI is lightweight (no GPU needed) — it could share a VM. But the agent-cloud convention is one service per VM. **Recommendation:** co-locate on the first inference node initially (saves a VM), split out if it becomes a bottleneck. The inventory group separation makes this a one-line change later.

### Step 4: Create Ansible Playbooks

#### `deploy-wisai-node.yml`

```yaml
---
- name: "Deploy WisAI Node"
  import_playbook: deploy-service.yml
  vars:
    target_service: inference_node_svc
```

But `deploy-service.yml` uses `clone-and-deploy.yml` which calls a generic `deploy.sh`. We need `deploy-node.sh` to:
1. Verify NVIDIA GPU is accessible (`nvidia-smi`)
2. Verify NVIDIA Container Toolkit is installed
3. Start Ollama via `compose.node.yml`
4. Wait for health (`curl http://localhost:11434/`)
5. Optionally run `pull-models.sh` based on `gpu_vram_profile`

**Pre-requisite playbook:** The target VM must have Docker + NVIDIA Container Toolkit installed. This is NOT part of the standard `install-docker.yml` playbook. We need either:
- A new `install-nvidia-toolkit.yml` playbook, OR
- An extension to `install-docker.yml` that detects GPU and installs the toolkit

**Recommendation:** Create `install-nvidia-toolkit.yml` as an independent playbook (follows the "each workflow is independent" rule). Run it once per GPU VM before deploying WisAI.

#### `deploy-wisai-webui.yml`

```yaml
---
- name: "Deploy WisAI WebUI"
  import_playbook: deploy-service.yml
  vars:
    target_service: inference_webui_svc
```

The `deploy-webui.sh` script:
1. Fetch `ollama_nodes` from the templated `.env` (sourced from OpenBao)
2. Start Open WebUI via `compose.webui.yml` with `OLLAMA_BASE_URLS` set
3. Wait for health

#### `pull-wisai-models.yml`

A standalone playbook that runs `pull-models.sh` on inference nodes. This is separate from deploy because model pulls are slow (minutes to hours) and should be retryable without redeploying the service.

#### `update-wisai-node.yml` / `update-wisai-webui.yml`

Standard update pattern: pull latest images, restart compose, health check. Same as `update-n8n.yml` etc.

### Step 5: Create deploy.sh Scripts

#### `deploy-node.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../../lib/common.sh"

log "=== Deploy WisAI Inference Node ==="

# Verify GPU
if ! command -v nvidia-smi &>/dev/null; then
  die "nvidia-smi not found — GPU passthrough or driver not configured"
fi
log "GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader)"

# Verify NVIDIA Container Toolkit
if ! docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi &>/dev/null; then
  die "NVIDIA Container Toolkit not working — run install-nvidia-toolkit.yml first"
fi

# Start Ollama
compose_up compose/compose.node.yml

# Health check
wait_for_health "http://localhost:11434/" 60

log "=== WisAI Node deployed ==="
```

#### `deploy-webui.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../../lib/common.sh"

log "=== Deploy WisAI Open WebUI ==="

# Verify .env has OLLAMA_NODES
if ! grep -q 'OLLAMA_NODES=' .env 2>/dev/null; then
  die ".env missing OLLAMA_NODES — run Ansible to template env files first"
fi

# Start Open WebUI
compose_up compose/compose.webui.yml

# Health check
wait_for_health "http://localhost:3000/" 60

log "=== WisAI WebUI deployed ==="
```

### Step 6: Create Jinja2 Templates

#### `templates/ollama.env.j2`

```jinja2
# Ollama inference node — templated by Ansible from OpenBao
OLLAMA_PORT=11434
OLLAMA_KEEP_ALIVE={{ ollama_keep_alive | default('5m') }}
OLLAMA_NUM_PARALLEL={{ ollama_num_parallel | default('1') }}
GPU_COUNT={{ gpu_count | default('1') }}
MODELS_PATH={{ models_path | default('') }}
```

#### `templates/webui.env.j2`

```jinja2
# Open WebUI coordinator — templated by Ansible from OpenBao
WEBUI_PORT=3000
OLLAMA_NODES={{ ollama_nodes }}
WEBUI_SECRET_KEY={{ webui_secret_key }}
```

### Step 7: Add Semaphore Templates

Add to `platform/semaphore/templates.yml`:

```yaml
  - name: Deploy WisAI Node
    playbook: platform/playbooks/deploy-wisai-node.yml

  - name: Deploy WisAI WebUI
    playbook: platform/playbooks/deploy-wisai-webui.yml

  - name: Update WisAI Node
    playbook: platform/playbooks/update-wisai-node.yml

  - name: Update WisAI WebUI
    playbook: platform/playbooks/update-wisai-webui.yml

  - name: Pull WisAI Models
    playbook: platform/playbooks/pull-wisai-models.yml

  - name: Install NVIDIA Toolkit
    playbook: platform/playbooks/install-nvidia-toolkit.yml
```

Then run `setup-templates.yml` to apply.

### Step 8: SSH Keys and Access

Generate an SSH key pair for inference nodes, store in OpenBao at `secret/services/ssh/inference`, and distribute with `distribute-ssh-keys.yml`. This follows the existing pattern — no special handling needed.

### Step 9: Optional — AppRole for Inference

WisAI's secret footprint is minimal (just `webui_secret_key` and `ollama_nodes`). An AppRole is only needed if the inference nodes need to fetch secrets at runtime (they don't — Ansible templates everything). **Skip AppRole provisioning for now.** Semaphore's AppRole handles the deploy-time OpenBao access.

---

## Compose Files (Adapted from WisAI)

### `compose/compose.node.yml`

```yaml
# Single Ollama inference node — deployed per GPU VM
services:
  ollama:
    image: ollama/ollama:latest
    container_name: ollama
    restart: unless-stopped
    ports:
      - "${OLLAMA_PORT:-11434}:11434"
    volumes:
      - ${MODELS_PATH:-ollama-data}:/root/.ollama
    environment:
      - OLLAMA_HOST=0.0.0.0
      - OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE:-5m}
      - OLLAMA_NUM_PARALLEL=${OLLAMA_NUM_PARALLEL:-1}
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: ${GPU_COUNT:-1}
              capabilities: [gpu]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:11434/"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s

volumes:
  ollama-data:
```

**Changes from WisAI's `docker-compose.yml`:**
- Removed Open WebUI (runs separately on coordinator)
- Removed `devices: nvidia.com/gpu=all` (Podman CDI — not used in production)
- Removed Open WebUI volume

### `compose/compose.webui.yml`

```yaml
# Open WebUI coordinator — connects to all Ollama nodes
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    restart: unless-stopped
    ports:
      - "${WEBUI_PORT:-3000}:8080"
    volumes:
      - open-webui-data:/app/backend/data
    environment:
      - OLLAMA_BASE_URLS=${OLLAMA_NODES}
      - WEBUI_SECRET_KEY=${WEBUI_SECRET_KEY:-}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s

volumes:
  open-webui-data:
```

**Changes from WisAI's `docker-compose.multi.yml`:**
- Added `WEBUI_SECRET_KEY` for session management
- Added healthcheck (needed for deploy script health wait)
- Uses `OLLAMA_NODES` from `.env` (templated by Ansible from OpenBao)

---

## Things That Need Updating in WisAI or agent-cloud

### WisAI Repo
No changes needed to the WisAI repo itself. WisAI remains the **development/documentation repo** — its compose files, scripts, and docs are the reference. agent-cloud's `platform/services/inference/` is the **production deployment** adapted from WisAI for the Semaphore pipeline.

### agent-cloud Changes Required

| File/Directory | Action | Description |
|---|---|---|
| `platform/services/inference/deployment/` | Populate | deploy scripts, compose files, templates, pull-models.sh |
| `platform/services/inference/deployment/CLAUDE.md` | Create | Service-specific guidance for AI agents |
| `platform/services/inference/context/` | Populate | Link to WisAI architecture docs |
| `platform/inventory/production.yml` | Edit | Add `inference_node_svc` and `inference_webui_svc` groups |
| `platform/playbooks/deploy-wisai-node.yml` | Create | Thin wrapper for deploy-service.yml |
| `platform/playbooks/deploy-wisai-webui.yml` | Create | Thin wrapper for deploy-service.yml |
| `platform/playbooks/update-wisai-node.yml` | Create | Image update + restart |
| `platform/playbooks/update-wisai-webui.yml` | Create | Image update + restart |
| `platform/playbooks/pull-wisai-models.yml` | Create | Model pull by VRAM profile |
| `platform/playbooks/install-nvidia-toolkit.yml` | Create | NVIDIA Container Toolkit setup |
| `platform/semaphore/templates.yml` | Edit | Add 6 new templates |
| `CLAUDE.md` (root) | Edit | Add inference to deployment status |

### site-config Changes Required (Private Repo)

| File | Action | Description |
|---|---|---|
| `inventory/production.yml` | Edit | Add real IPs for inference nodes |
| OpenBao | Seed | Create `secret/services/inference` with initial values |

---

## Scaling: Adding a New GPU Node

Once the infrastructure is in place, adding a new inference node is:

1. Provision a new Proxmox VM with GPU passthrough (use `provision-vm.yml`)
2. Run `install-docker.yml` + `install-nvidia-toolkit.yml` on it
3. Add the host to `inference_node_svc` in site-config inventory
4. Update `ollama_nodes` in OpenBao to include the new endpoint
5. Run "Deploy WisAI Node" in Semaphore targeting the new host
6. Run "Deploy WisAI WebUI" to pick up the new endpoint
7. Run "Pull WisAI Models" on the new node

No code changes needed — just inventory and OpenBao updates.

---

## Open Questions

1. **Container runtime:** WisAI's CLAUDE.md says Docker is the target for Proxmox. But agent-cloud uses Podman for most services and Docker only for NetBox/NemoClaw. Should inference nodes use Docker (simpler NVIDIA toolkit integration) or Podman (consistency with other services)? **Recommendation:** Docker — NVIDIA Container Toolkit has first-class Docker support; Podman GPU support requires CDI configuration which is less mature on Linux servers.

2. **Model storage:** Should models live on the VM's local disk or a shared NFS mount? Local is simpler and faster (no network I/O during inference). NFS saves disk space if multiple nodes run the same models. **Recommendation:** Local disk initially — use `MODELS_PATH` env var to point at a dedicated disk/partition if needed. NFS can be added later without architecture changes.

3. **Caddy integration:** Should Open WebUI be fronted by the existing Caddy reverse proxy for HTTPS/auth? **Recommendation:** Yes, eventually — but out of scope for initial deployment. Add a Caddyfile block when Caddy is fully deployed.

4. **Monitoring:** Should inference nodes report to the o11y (observability) stack? Ollama exposes limited metrics. Open WebUI has usage logs. **Recommendation:** Defer until the o11y service (`platform/services/o11y/`) is deployed.
