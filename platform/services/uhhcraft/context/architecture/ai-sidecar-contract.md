# UhhCraft ↔ AI sidecar contract

How UhhCraft consumes the two inference services. Mirrors of this doc live on the sidecar side and **must stay in lockstep**:

- [`../../../inference-comfyui/context/architecture/contract.md`](../../../inference-comfyui/context/architecture/contract.md) — image generation (Flux.1).
- [`../../../inference-hunyuan3d/context/architecture/contract.md`](../../../inference-hunyuan3d/context/architecture/contract.md) — 3D mesh generation.

If you change the contract here, change both sidecar docs (and `app/main.py` in each) in the same PR.

---

## Topology

```text
Browser
  │ HTTPS
  ▼
Central Caddy (uhhcraft.uhstray.io)
  │  /static/uploads/*  ────────────►  UhhCraft MinIO         (catalog assets)
  │  /generated/img/*   ────────────►  inference-comfyui MinIO (Flux.1 outputs)
  │  /generated/3d/*    ────────────►  inference-hunyuan3d MinIO (GLB / STL)
  │  /                  ────────────►  UhhCraft Go app  (port 3000 on uhhcraft VM)
  │                                      │
  │                                      │ internal HTTP (uhhcraft VM → sidecar VMs)
  │                                      ▼
  │                                    inference-comfyui (port 8189)
  │                                    inference-hunyuan3d (port 8001)
  │                                      │
  │                                      ▼ (writes to its own MinIO)
  │                                    sidecar MinIO  (returned URL is Caddy-routed)
  ▼
```

**Three independent MinIO instances** — one per service. This is a deliberate Phase 2 decision recorded in [`../spec/SPEC.md`](../spec/SPEC.md) `## Alignment with agent-cloud conventions`. Per-service isolation outweighs the simplicity of a shared bucket.

## Network boundaries

| Edge | Reachable | Auth |
|------|-----------|------|
| Browser → central Caddy | public (443) | none |
| Central Caddy → UhhCraft Go app | internal (uhhcraft VM:3000) | none (network boundary) |
| Central Caddy → any MinIO API | internal (port 9000) | none (read-only path-style fetches) |
| UhhCraft Go app → sidecar API | internal (sidecar VM:8189/:8001) | none (network boundary) |
| UhhCraft Go app → its own MinIO | internal (loopback or compose network) | MinIO root creds via .env |
| Sidecar API → its own MinIO | internal (compose network) | MinIO root creds via .env |
| Anything → another service's MinIO direct | **never** | the API is Caddy; never connect MinIO→MinIO |

Sidecar MinIOs are **never** exposed publicly. Browsers reach generated assets only via central Caddy's `/generated/*` proxy paths.

## What UhhCraft sends

### To `inference-comfyui`

```http
POST http://<comfyui_vm>:8189/generate
Content-Type: application/json

{
  "generation_id": "<uuid>",
  "prompt": "<text>",
  "width": 1024,
  "height": 1024,
  "steps": 4
}
```

Seeds and negative prompts are not exposed (Flux Schnell runs at cfg=0).

### To `inference-hunyuan3d`

```http
POST http://<hunyuan_vm>:8001/generate
Content-Type: application/json

{
  "generation_id": "<uuid>",
  "prompt": "<text>",
  "steps": 30,
  "guidance": 7.5,
  "octree_resolution": 256
}
```

## What UhhCraft receives + stores

The Go app stores the **URL** the sidecar returns, not the bytes. URLs are relative paths routed by central Caddy. Response from `inference-comfyui`:

```json
{
  "generation_id": "<uuid>",
  "url": "/generated/img/generations/<uuid>/output.png",
  "status": "completed"
}
```

And from `inference-hunyuan3d`:

```json
{
  "generation_id": "<uuid>",
  "glb_url": "/generated/3d/generations/<uuid>/model.glb",
  "stl_url": "/generated/3d/generations/<uuid>/model.stl",
  "status": "completed"
}
```

UhhCraft persists the returned URL(s) on the `generations` row keyed by `generation_id`. The browser, when rendering the canvas page, fetches `https://uhhcraft.uhstray.io/generated/img/...` — central Caddy proxies to the relevant sidecar MinIO.

## Failure handling

| Sidecar response | UhhCraft behaviour |
|------------------|--------------------|
| `200` | Persist URL, return to caller. |
| `400` | Surface validation error to the user (e.g., "prompt too short"). |
| `429` | Rate-limit hit upstream. UhhCraft enforces its own rate limit via `internal/ratelimit/` and should not normally see this. Treat as 503. |
| `503 vram_exhausted` | Queue retry via River with exponential backoff (initial 30s). Cap at 3 retries. Show "AI is busy" to user. |
| `503 weights_not_loaded` | Same as `vram_exhausted` for retries; alert ops if it persists >5min. |
| `503 comfyui_unreachable` / `gpu_unavailable` | Page ops. Mark sidecar `down` in app health. Show "AI is offline" to user; allow catalog browsing. |
| Network error / timeout (>30s for image, >120s for 3D) | Same as 503. |

**Never** render a Python traceback or sidecar error message verbatim to the end user.

## Caching

- **Sidecars do not cache.** Same input → repeated work.
- **UhhCraft** persists each result on its `generations` row keyed by `generation_id`; the canvas re-reads that row rather than re-generating. (A future content-hash dedup is tracked in the integration plan.)
- **Browser caches** generated assets via `Cache-Control: public, max-age=31536000, immutable` (set in `caddy-site.j2`). Keys are UUID-based so cache busting is free.

## Rate limiting

Enforced at UhhCraft's `internal/ratelimit/` (Redis-backed), per session and per IP. Sidecars trust the caller's discipline; they have no auth or quota of their own.

If UhhCraft is bypassed (a future internal tool wants to call sidecars directly), add per-caller quota at the sidecar layer — do not assume "internal" means "trusted forever."

## Concurrency

- `inference-comfyui`: 1 in-flight generation per GPU. Wrapper does not queue.
- `inference-hunyuan3d`: 1 in-flight generation per GPU. Wrapper does not queue.
- **UhhCraft** queues all generation requests through **River** (Postgres-backed job queue) and dispatches at most 1 concurrent request per sidecar.

If we add a second GPU per sidecar in the future, the limit becomes a per-sidecar config — currently `MAX_INFLIGHT_<SIDECAR>=1` in `.env`.

## Versioning

Contract is at **v1** as of 2026-05-25. Breaking changes (new required fields, removed endpoints, changed URL shape) require a coordinated PR:

1. `internal/generation/` in this service.
2. `app/main.py` in both sidecars.
3. This doc + both sidecar `contract.md` mirrors.

Backwards-compatible additions (new optional request fields, new response fields, new error codes) do not bump the version but should still be PR'd to all three locations together.

## Open questions tracked elsewhere

- **MinIO-per-service complexity.** If three buckets prove too much operational overhead, the alternative is "shared MinIO under UhhCraft with scoped users." Revisit at the 3-month mark. (Tracked in `plan/development/WEBSMITH-INTEGRATION-PLAN.md` §4.)
- **GPU host capacity.** If only one GPU host exists for both sidecars, scheduling matters. Tracked in `plan/development/UHHCRAFT-GPU-PASSTHROUGH.md` (added in Phase 6).
- **CSP for worker-src.** The Three.js canvas uses WASM workers. `caddy-site.j2` includes `worker-src 'self' blob:` — verify in Phase 10.
