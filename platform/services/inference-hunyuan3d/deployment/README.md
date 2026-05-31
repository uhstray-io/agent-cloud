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
  └─ platform/playbooks/deploy-inference-hunyuan3d.yml (Phase 4)
     ├─ tasks/install-nvidia-toolkit.yml
     ├─ tasks/ensure-weights.yml             # one-time: download Hunyuan3D-2-mini to /srv/hunyuan3d/weights
     ├─ tasks/manage-secrets.yml             # OpenBao → templates/env.j2 → .env
     ├─ deploy.sh                            # podman compose up
     └─ post-deploy.sh                       # /health, weights loaded, GPU visible
```

**Model weights are host state.** They are large (~5GB for the -mini variant) and slow to download. The deploy expects them already on disk at `HUNYUAN3D_WEIGHTS_DIR` (default `/srv/hunyuan3d/weights`); the compose mounts them read-only. A separate playbook task handles initial download.

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
| `GET` | `/health/weights` | Verifies model is loaded into GPU memory |
| `GET` | `/health/gpu` | Verifies CUDA visibility + free VRAM |
| `POST` | `/generate` | `{ prompt, seed?, resolution? }` → `{ glb_url, stl_url, seed }` |

Responses include URLs routed through central Caddy at `/generated/3d/*`. The caller stores the URLs, not the bytes.

## File layout

```text
deployment/
├── deploy.sh                Container lifecycle (+ verifies weights mount)
├── post-deploy.sh           Health + GPU + weight-load checks
├── Dockerfile               nvidia/cuda:12.4.1-cudnn + Python 3.11 + torch + Hunyuan3D from source
├── compose.yml              FastAPI wrapper + independent MinIO + weights host mount
├── templates/env.j2         Jinja2 — production .env templated from OpenBao
└── app/
    ├── main.py              FastAPI service (moved from website_framework/output/ai/model3d/)
    └── requirements.txt
```

## Outstanding integration items

- **Phase 3:** OpenBao policy + AppRole for `inference-hunyuan3d`.
- **Phase 4:** `platform/playbooks/deploy-inference-hunyuan3d.yml` + `tasks/ensure-weights.yml`.
- **Phase 6:** GPU VM provisioning (see `plan/development/UHHCRAFT-GPU-PASSTHROUGH.md`).
- **Phase 7:** Semaphore template.
- **Phase 8:** CI extensions (Python lint + import-time check; do **not** run the model in CI).
- **Wrapper additions** the deploy scripts assume but may need to be added to `main.py`: `GET /health/weights`, `GET /health/gpu` endpoints; both should be cheap (don't run the model).

## Related

- Sibling sidecar: [`../../inference-comfyui/`](../../inference-comfyui/)
- UhhCraft (consumer): [`../../uhhcraft/`](../../uhhcraft/)
- Full contract: [`../context/architecture/contract.md`](../context/architecture/contract.md)
- UhhCraft-side view: [`../../uhhcraft/context/architecture/ai-sidecar-contract.md`](../../uhhcraft/context/architecture/ai-sidecar-contract.md)
