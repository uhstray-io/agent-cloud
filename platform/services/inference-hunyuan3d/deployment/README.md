# inference-hunyuan3d — deployment

FastAPI wrapper around **Hunyuan3D-2-mini** for 3D mesh generation. Sidecar to UhhCraft. Produces both GLB (for browser canvas preview) and STL (for manufacturing).

```text
UhhCraft (Go) ──HTTP──> inference-hunyuan3d FastAPI ── GPU ── Hunyuan3D pipeline
                                                                  │
                                                                  ▼
                                                              MinIO (own instance)
                                                                  │
                                                                  ▼ (Caddy /generated/3d/*)
                                                              Browser
```

Unlike `inference-comfyui` (which proxies to a separate ComfyUI process), Hunyuan3D runs **in-process** in this container via the diffusers/transformers pipeline. The container therefore needs the full torch + CUDA stack.

## How this deploys

```text
Semaphore "Deploy inference-hunyuan3d"
  └─ platform/playbooks/deploy-inference-hunyuan3d.yml
     ├─ tasks/install-nvidia-toolkit.yml
     ├─ weights directory check             # fail if pre-provisioned host weights are absent
     ├─ tasks/manage-secrets.yml             # OpenBao → templates/env.j2 → .env
     ├─ deploy.sh                            # podman compose up
     └─ post-deploy.sh                       # /health, weights present, GPU visible
```

**Model weights are host state.** They are large (~5GB for the -mini variant) and slow to download. The deploy expects them already on disk at `HUNYUAN3D_WEIGHTS_DIR` (default `/srv/hunyuan3d/weights`); the compose mounts them read-only. No initial-download task is implemented. Provisioning weights requires a reviewed Semaphore mechanism before this prerequisite can be satisfied through platform automation.

## Local development

```bash
cd app/
python3.11 -m venv .venv && source .venv/bin/activate

# torch first (CUDA 12.4):
pip install --index-url https://download.pytorch.org/whl/cu124 torch==2.4.1

pip install -r requirements.txt

# Hunyuan3D-2 from source:
git clone https://github.com/Tencent/Hunyuan3D-2.git ../Hunyuan3D-2
pip install -e ../Hunyuan3D-2

# Download model weights — see ../Hunyuan3D-2/README.md for HuggingFace links.

uvicorn main:app --host 0.0.0.0 --port 8001 --workers 1
```

CPU-only is supported but glacial; use a CUDA GPU.

## API contract

See [`../context/architecture/contract.md`](../context/architecture/contract.md). Quick reference:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Wrapper liveness |
| `GET` | `/health/weights` | Checks MODEL_PATH exists and reports lazy-load state; does not load the model |
| `GET` | `/health/gpu` | Verifies CUDA visibility + free VRAM |
| `POST` | `/generate` | `{ generation_id, prompt, steps?, guidance?, octree_resolution? }` → `{ generation_id, glb_url, stl_url, status }` |

Responses include URLs routed through central Caddy at `/generated/3d/*`. The caller stores the URLs, not the bytes.

## File layout

```text
deployment/
├── deploy.sh                Container lifecycle (+ verifies weights mount)
├── post-deploy.sh           Health + GPU + weight-presence checks
├── Dockerfile               nvidia/cuda:12.4.1-cudnn + Python 3.11 + torch + Hunyuan3D from source
├── compose.yml              FastAPI wrapper + independent MinIO + weights host mount
├── templates/env.j2         Jinja2 — production .env templated from OpenBao
└── app/
    ├── main.py              FastAPI service (moved from website_framework/output/ai/model3d/)
    └── requirements.txt
```

## Integration status

The deploy playbook, GPU prerequisite task, OpenBao policy/AppRole wiring,
Semaphore template, and wrapper health endpoints are checked in. Their presence
is not proof of a successful GPU deployment or end-to-end generation. Production
GPU readiness and generated-artifact delivery require separate runtime validation.

## Related

- Sibling sidecar: [`../../inference-comfyui/`](../../inference-comfyui/)
- UhhCraft (consumer): [`../../uhhcraft/`](../../uhhcraft/)
- Full contract: [`../context/architecture/contract.md`](../context/architecture/contract.md)
- UhhCraft-side view: [`../../uhhcraft/context/architecture/ai-sidecar-contract.md`](../../uhhcraft/context/architecture/ai-sidecar-contract.md)
