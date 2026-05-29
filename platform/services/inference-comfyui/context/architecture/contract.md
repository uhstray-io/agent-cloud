# inference-comfyui — HTTP contract

How UhhCraft (or any other future caller) talks to this service.

This contract is mirrored on the consumer side at [`../../../uhhcraft/context/architecture/ai-sidecar-contract.md`](../../../uhhcraft/context/architecture/ai-sidecar-contract.md). If you change one, change the other in the same PR.

## Endpoints

### `GET /health`

Liveness probe. No upstream calls.

**Response 200**

```json
{ "status": "ok" }
```

### `GET /health/comfyui`

Verifies the wrapper can reach the ComfyUI HTTP API. Used by `post-deploy.sh`.

**Response 200**

```json
{ "comfyui_url": "http://host.containers.internal:8188", "reachable": true }
```

**Response 503** on failure with `{ "reachable": false, "error": "..." }`.

### `GET /health/models`

Verifies Flux.1 Schnell weights are present in ComfyUI's models directory. Used by `post-deploy.sh`.

**Response 200**

```json
{ "models": ["flux1-schnell-fp8.safetensors"], "missing": [] }
```

**Response 503** with `{ "missing": ["..."] }` if any required model is absent.

### `POST /generate`

Generate one image from a text prompt.

**Request**

```json
{
  "generation_id": "string (required, UhhCraft generation UUID)",
  "prompt": "string (required)",
  "width": "int (optional; 512..2048; default 1024)",
  "height": "int (optional; 512..2048; default 1024)",
  "steps": "int (optional; 1..20; default 4 for Schnell)",
  "guidance": "float (optional; default 0.0 — Flux Schnell runs at cfg=0)"
}
```

Flux.1 Schnell runs at cfg=0 and ignores negative prompts, so this API
intentionally does not expose `negative_prompt`. Seeds are not exposed.

**Response 200**

```json
{
  "generation_id": "<uuid>",
  "url": "/generated/img/generations/<uuid>/output.png",
  "status": "completed"
}
```

The `url` is **relative** and **served through central Caddy**. The caller stores the URL, not the bytes. Direct MinIO endpoints are not exposed to browsers.

**Response 422** — invalid input (pydantic validation: prompt missing, bad dimensions/steps).

**Response 504** — generation timed out waiting on ComfyUI.

**Response 503** — ComfyUI unreachable, OOM, MinIO upload failed, or no image returned. Body: `{ "error": "<machine-readable>", "detail": "<human-readable>" }`.

## Error codes

| `error` | Meaning |
|---------|---------|
| `comfyui_unreachable` | Wrapper cannot reach ComfyUI. |
| `vram_exhausted` | Generation aborted; GPU is full. Caller should retry later, not immediately. |
| `weights_missing` | Required model file not on disk. Ops issue, not retryable. |
| `prompt_rejected` | Failed moderation. Do not retry. |
| `prompt_too_long` | Caller bug. |
| `internal_error` | Anything else. Log + alert; safe to retry once. |

## Non-functional

- **Latency:** P50 ~3.5s, P99 ~9s for Flux.1 Schnell at 1024×1024, 4 steps, RTX 5070.
- **Concurrency:** 1 generation in-flight per GPU. The wrapper does not queue — callers do (UhhCraft uses River).
- **Idempotency:** Not guaranteed — seed is not exposed and ComfyUI seeds randomly. The caller keys on `generation_id`; the wrapper does not cache.
- **Auth:** None at this layer. Network-level isolation (only UhhCraft's VM can reach this port) is the security boundary.

## Versioning

This contract is at v1 as of 2026-05-25. Breaking changes require a coordinated PR touching:
1. `app/main.py` here.
2. UhhCraft's `internal/generation/` package.
3. Both `architecture/` docs.

Add a `## v2` section to both docs before changing routes or schemas.
