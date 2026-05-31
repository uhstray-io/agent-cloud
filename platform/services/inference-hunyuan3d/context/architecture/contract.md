# inference-hunyuan3d — HTTP contract

How UhhCraft talks to this service. Mirror on the consumer side at [`../../../uhhcraft/context/architecture/ai-sidecar-contract.md`](../../../uhhcraft/context/architecture/ai-sidecar-contract.md).

## Endpoints

### `GET /health`

Liveness — confirms the wrapper process is up. No inference, no disk or GPU probe.

**Response 200**

```json
{ "status": "ok" }
```

### `GET /health/weights`

Verifies the model weights are **present on disk** (`MODEL_PATH`). Cheap — it
does NOT load the model into VRAM, because loading is lazy on the first
generation. `loaded` reflects whether a generation has already warmed the
pipeline this process lifetime.

**Response 200**

```json
{ "model_path": "/weights/hunyuan3d-2-mini", "present": true, "loaded": false }
```

**Response 503** with `{ "present": false, "error": "weights_missing" }` when the
weights directory is absent.

### `GET /health/gpu`

CUDA visibility + free VRAM. Cheap.

**Response 200**

```json
{ "cuda_available": true, "device_count": 1, "device_name": "NVIDIA RTX 5070", "vram_free_mb": 4096 }
```

**Response 503** if CUDA is not visible — host PCIe passthrough or NVIDIA toolkit issue.

### `POST /generate`

Generate one 3D mesh from a text prompt. Returns both GLB (browser preview) and STL (manufacturing).

**Request**

```json
{
  "generation_id": "string (required, UhhCraft generation UUID)",
  "prompt": "string (required)",
  "steps": "int (optional; 10-60; default 30)",
  "guidance": "float (optional; 1.0-15.0; default 7.5)",
  "octree_resolution": "int (optional; 128-512; default 256)"
}
```

**Response 200**

```json
{
  "generation_id": "<uuid>",
  "glb_url": "/generated/3d/generations/<uuid>/model.glb",
  "stl_url": "/generated/3d/generations/<uuid>/model.stl",
  "status": "completed"
}
```

`glb_url` / `stl_url` are relative and served through central Caddy. The caller
stores these URLs, never the raw bucket keys.

**Response 422** — invalid input (pydantic validation).
**Response 503** — error contract below: a generation already in progress, VRAM
exhausted, weights not loaded, or GPU lost. Body is `{ "error": "...", "detail": "..." }`.

## Error codes

| `error` | Meaning |
|---------|---------|
| `vram_exhausted` | Generation aborted; retry later, not immediately. |
| `weights_not_loaded` | Cold start in progress, or initial weight download incomplete. |
| `gpu_unavailable` | CUDA visibility lost; ops issue. |
| `prompt_rejected` | Moderation failure. Don't retry. |
| `prompt_too_long` | Caller bug. |
| `internal_error` | Anything else. |

## Non-functional

- **Latency:** P50 ~25s, P99 ~60s at resolution=256 on RTX 5070 (Hunyuan3D-2-mini).
- **Concurrency:** 1 generation in-flight, enforced by an `asyncio.Semaphore(1)`. The wrapper does not queue — a request that arrives while another is running is rejected immediately with `503 {error: "vram_exhausted"}`. The Go caller serializes via River.
- **Cold start:** ~60-90s to load weights on first request after container start. `compose.yml` has `start_period: 120s`.
- **Idempotency:** Not guaranteed (seed is not exposed); the caller keys on `generation_id` and does not retry-for-dedup.
- **Auth:** None at this layer.

## Versioning

v1 as of 2026-05-25. Breaking changes coordinated across:
1. `app/main.py` here.
2. UhhCraft's `internal/generation/` package.
3. Both `architecture/` docs (this one and the UhhCraft mirror).
