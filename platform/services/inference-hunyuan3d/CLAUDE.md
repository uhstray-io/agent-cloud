# CLAUDE.md — platform/services/inference-hunyuan3d

FastAPI wrapper around Hunyuan3D-2-mini for 3D mesh generation. Sidecar to UhhCraft; produces GLB (browser preview) and STL (manufacturing).

## What this service is

In-process diffusers pipeline behind a FastAPI HTTP API. Receives `POST /generate`, runs the Hunyuan3D model on the GPU, uploads results to its own MinIO, returns Caddy-routed URLs.

## Conventions specific to this service

### Weights live on the host

Model weights are ~5GB and slow to download. The container mounts them **read-only** from `HUNYUAN3D_WEIGHTS_DIR` (default `/srv/hunyuan3d/weights`). Initial download is a one-time playbook task (`tasks/ensure-weights.yml`); never bake weights into the image.

### In-process model, not a separate daemon

Unlike `inference-comfyui` (where ComfyUI is a separate host process), Hunyuan3D runs inside this container. The container therefore needs the full torch + CUDA stack.

This has two implications:
- Cold start is slow (~60-90s to load weights into VRAM). `compose.yml` healthcheck uses a 120s `start_period`.
- The wrapper is **not stateless** — a restart reloads weights. Don't restart casually.

### Two outputs per generation

`POST /generate` produces both:
- A **GLB** for the Three.js canvas in the browser.
- An **STL** for the 3D printer / Shapeways.

Both upload to the same MinIO bucket with different keys. The response contains both URLs.

If a future caller only needs one format, add a `formats: ["glb"]` request parameter rather than running the model twice.

### Independent MinIO

Per Phase 2 decision — own MinIO instance, served through central Caddy at `/generated/3d/*`. See [`../uhhcraft/context/spec/SPEC.md`](../uhhcraft/context/spec/SPEC.md) `## Alignment` section.

### Failure modes

- Weights not mounted → `deploy.sh` aborts before starting the container.
- GPU not visible → `GET /health/gpu` returns 503; the playbook fails.
- OOM during generation → return `{error: "vram_exhausted"}`; do not crash the wrapper.

### Health endpoints

`app/main.py` implements three cheap probes (none run inference):
- `GET /health` — liveness only.
- `GET /health/weights` — checks the weights are **present on disk** at `MODEL_PATH` (loading is lazy on first generation, so this does not require VRAM load).
- `GET /health/gpu` — `torch.cuda.is_available()` + `torch.cuda.mem_get_info()`.

`/generate` runs the GPU-bound pipeline off the event loop via `asyncio.to_thread`, serializes on an `asyncio.Semaphore(1)` (concurrent calls get `503 vram_exhausted`), and returns Caddy-routed `glb_url`/`stl_url` — never raw bucket keys.

## What not to do

- Don't bake model weights into the image — they're host state, mounted at runtime.
- Don't share MinIO with another service. Per Phase 2 isolation.
- Don't add a queue inside this service. Queuing is UhhCraft's job (via River).
- Don't reduce the `start_period` below 120s — first-token latency includes weight load.
- Don't restart the container in a hot path; treat it as expensive.

## Related

- Sibling sidecar: [`../inference-comfyui/CLAUDE.md`](../inference-comfyui/CLAUDE.md)
- Consumer: [`../uhhcraft/CLAUDE.md`](../uhhcraft/CLAUDE.md)
- HTTP contract: [`context/architecture/contract.md`](context/architecture/contract.md)
- Integration plan: [`../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md`](../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md)
- Root conventions: [`../../../CLAUDE.md`](../../../CLAUDE.md)
