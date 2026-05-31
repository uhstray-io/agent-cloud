# CLAUDE.md — platform/services/inference-comfyui

FastAPI wrapper around ComfyUI (Flux.1 Schnell) for image generation. Sidecar to UhhCraft; reusable by future generative sites.

## What this service is

A small Python service that:
1. Receives `POST /generate` from UhhCraft over the internal network.
2. Talks to ComfyUI (running on the host) over its HTTP/WebSocket API.
3. Uploads the resulting image to **its own** MinIO instance.
4. Returns a `{generation_id, url, status}` payload whose `url` points at central Caddy.

ComfyUI itself is not part of this compose stack — it lives on the host with direct GPU access. This wrapper is a redeployable, stateless HTTP frontend.

## Conventions specific to this service

### Independent MinIO

Per the Phase 2 decision (see [`../uhhcraft/context/spec/SPEC.md`](../uhhcraft/context/spec/SPEC.md) `## Alignment with agent-cloud conventions`), every sidecar has its own MinIO. Do not share buckets with UhhCraft or the other sidecar.

Generated assets are served to the browser via central Caddy at `/generated/img/*` (defined in `../uhhcraft/deployment/templates/caddy-site.j2`).

### GPU access

Production hosts run with the NVIDIA Container Toolkit configured for Podman (`platform/playbooks/tasks/install-nvidia-toolkit.yml`). The compose.yml uses CDI handoff (`devices: nvidia.com/gpu=all`). For Docker dev, swap in `deploy.resources.reservations.devices`.

ComfyUI must not be containerized for performance reasons (large model loads, weight caching) — keep it on the host under a systemd unit.

### Failure modes

The wrapper must be resilient to:
- ComfyUI unreachable → return 503 with a structured error; never crash.
- MinIO unreachable → same; the caller is responsible for retry.
- Out-of-VRAM during generation → return 503 with `{error: "vram_exhausted"}`.

UhhCraft handles these as a graceful degradation in the UI; never propagate a Python traceback.

### Stateless wrapper, stateful weights

The FastAPI process is stateless. All persistent state lives in:
- ComfyUI's model weights on the host (`/srv/comfyui/models/...`).
- This service's MinIO (generated assets).

The wrapper container can be rebuilt and redeployed at any time without data loss.

### Health endpoints

`app/main.py` implements `GET /health` (cheap liveness), `GET /health/comfyui`
(wrapper → ComfyUI reachability) and `GET /health/models` (Flux.1 weights
present). `post-deploy.sh` depends on the latter two. `/generate` normalizes all
upstream/MinIO failures to `503 {error, detail}` and returns a Caddy-routed
`url` — never the raw bucket key.

## What not to do

- Don't bundle ComfyUI into this image. It's a deployment-time host responsibility.
- Don't share MinIO with UhhCraft. The decision is intentional (per-service isolation).
- Don't write directly to UhhCraft's database. Communication is HTTP only.
- Don't store anything in the wrapper container's filesystem — it's ephemeral.
- Don't bypass central Caddy when surfacing assets to the browser; always return Caddy-routed URLs.

## Related

- Sibling sidecar: [`../inference-hunyuan3d/CLAUDE.md`](../inference-hunyuan3d/CLAUDE.md)
- Consumer (UhhCraft): [`../uhhcraft/CLAUDE.md`](../uhhcraft/CLAUDE.md)
- HTTP contract: [`context/architecture/contract.md`](context/architecture/contract.md)
- Integration plan: [`../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md`](../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md)
- Root conventions: [`../../../CLAUDE.md`](../../../CLAUDE.md)
