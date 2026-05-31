# inference-comfyui — deployment

FastAPI wrapper around **ComfyUI** running **Flux.1 Schnell** for sticker / image generation. One of UhhCraft's two AI sidecars.

```text
UhhCraft (Go) ──HTTP──> inference-comfyui FastAPI (this service)
                          │
                          ▼
                       ComfyUI ── GPU (Nvidia RTX 5070+, host-installed)
                          │
                          ▼
                       MinIO (this service's own instance)
                          │
                          ▼ (served via central Caddy at /generated/img/*)
                       Browser
```

## How this deploys

Same composable pattern as UhhCraft:

```text
Semaphore "Deploy inference-comfyui"
  └─ platform/playbooks/deploy-inference-comfyui.yml (Phase 4)
     ├─ tasks/install-nvidia-toolkit.yml   # GPU host prereq
     ├─ tasks/manage-secrets.yml           # OpenBao → templates/env.j2 → .env
     ├─ deploy.sh                          # podman compose up
     └─ post-deploy.sh                     # /health, ComfyUI reachability, model weights
```

**ComfyUI itself is not in this compose.** It runs on the host (or in a separate container with direct GPU + model-weight volume mounts) so that this FastAPI wrapper can be redeployed independently. The wrapper talks to ComfyUI over `COMFYUI_URL`.

## Local development

This wrapper expects ComfyUI to be reachable at `COMFYUI_URL` (default `http://localhost:8188`). You need:

1. ComfyUI installed and running locally: <https://github.com/comfyanonymous/ComfyUI>
2. Flux.1 Schnell weights downloaded to `ComfyUI/models/unet/flux1-schnell-fp8.safetensors`

```bash
cd app/
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8189 --workers 1
```

## API contract

See [`../context/architecture/contract.md`](../context/architecture/contract.md) for the full HTTP schema. Quick reference:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Wrapper liveness (200 OK + JSON) |
| `GET` | `/health/comfyui` | Verifies ComfyUI is reachable |
| `GET` | `/health/models` | Verifies Flux.1 weights are loaded |
| `POST` | `/generate` | `{ prompt, seed?, width?, height? }` → `{ url, key, seed }` |

Responses include a URL routed through central Caddy under `/generated/img/*`. UhhCraft stores the URL, not the bytes.

## File layout

```text
deployment/
├── deploy.sh                Container lifecycle only
├── post-deploy.sh           Health + ComfyUI reachability + model weight checks
├── Dockerfile               nvidia/cuda:12.4.1-runtime-ubuntu22.04 + Python 3.11 + FastAPI
├── compose.yml              FastAPI wrapper + independent MinIO
├── templates/env.j2         Jinja2 — production .env templated from OpenBao
└── app/
    ├── main.py              FastAPI service (moved from website_framework/output/ai/image/)
    └── requirements.txt
```

## Outstanding integration items

- **Phase 3:** OpenBao policy + AppRole for `inference-comfyui`.
- **Phase 4:** `platform/playbooks/deploy-inference-comfyui.yml` + `tasks/install-nvidia-toolkit.yml`.
- **Phase 6:** GPU VM with PCIe passthrough on Proxmox (see `plan/development/UHHCRAFT-GPU-PASSTHROUGH.md`).
- **Phase 7:** Semaphore template.
- **Phase 8:** CI extensions (Python lint + import-time checks on `main.py`).
- **Wrapper additions** the deploy scripts assume but may need to be added to `main.py`: `GET /health/comfyui` and `GET /health/models` endpoints.

## Related

- Sibling sidecar: [`../../inference-hunyuan3d/`](../../inference-hunyuan3d/)
- UhhCraft (consumer): [`../../uhhcraft/`](../../uhhcraft/)
- Full contract: [`../context/architecture/contract.md`](../context/architecture/contract.md)
- UhhCraft-side contract view: [`../../uhhcraft/context/architecture/ai-sidecar-contract.md`](../../uhhcraft/context/architecture/ai-sidecar-contract.md)
