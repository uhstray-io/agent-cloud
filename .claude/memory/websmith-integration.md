---
name: websmith-integration
description: WebSmith (website_framework) + UhhCraft integration into agent-cloud — locked decisions, plan location, and merged-phase status.
metadata:
  node_type: memory
  type: project
---

The external `website_framework` repo is being integrated into agent-cloud across 11 phases. The framework becomes the **WebSmith** agent (`agents/websmith/`); its concrete output — **UhhCraft**, a Go + templ + HTMX storefront with Python AI sidecars — becomes the first platform service built that way. Full plan: `plan/development/WEBSMITH-INTEGRATION-PLAN.md`.

## Phase status (verified against `main`, 2026-05-31)

- **Phase 1 — MERGED** (PR #18, squash `7ebb178`): `agents/websmith/` agent move.
- **Phase 2 — MERGED** (PR #19, squash `5a513b5`): full UhhCraft service at `platform/services/uhhcraft/` + `inference-comfyui` / `inference-hunyuan3d` sidecars. Cleared a large CodeRabbit pass (125 findings + 19 on re-review, worked in 10 thematic clusters) with all CI gates green before merge.
- **Phases 3–11 — NOT STARTED.**

**Workflow used:** one PR per phase, stacked; wait for *all* CI checks + CodeRabbit to complete and pass before merging; squash-merge. See [[coderabbit-rate-limits]] for the review-cadence constraint.

## Locked decisions (do not re-litigate)

1. Framework lives at `agents/websmith/` as a first-class agent (not under cowork, not `frameworks/`).
2. AI sidecars are **separate platform services**: `inference-comfyui/` + `inference-hunyuan3d/`.
3. UhhCraft is fronted by the **central Caddy** via a route-fragment template — no per-service Caddy.
4. UhhCraft runs on **Podman** in production (agent-cloud convention; NetBox stays the only Docker exception). Its SPEC carries an `## Alignment with agent-cloud conventions` section — framed as alignment, not deviation.
5. **Separate MinIO instance per service** (UhhCraft + each sidecar own their bucket). Caddy proxies cross-service asset paths (`/generated/img/*`, `/generated/3d/*`).
6. Hosting: dedicated Proxmox VMs — `uhhcraft_svc` (CPU) + two GPU VMs (PCIe passthrough) for the sidecars.
7. CI extends the unified `.github/workflows/lint-and-test.yml` with path-filtered Go + templ + sqlc jobs.
8. **Generated code is generate-in-CI only** — `*_templ.go` and sqlc output are gitignored; contributors run `make templ` / `make sqlc` after clone.
9. **`.env`-at-boot, no runtime OpenBao/AppRole** for UhhCraft + sidecars in v1. Ansible `tasks/manage-secrets.yml` templates `.env` from OpenBao; apps read it once at startup. AppRole HCL is reserved but no AppRoles created. No scheduled rotation in v1.

## Sidecar HTTP contract (settled in Phase 2)

Both sidecars expose a single normalized `POST /generate` (NOT `/generate/image` or `/generate/3d`). comfyui returns `{generation_id, url, status}`; hunyuan3d returns `{generation_id, glb_url, stl_url, status}` — always Caddy-routed public URLs, never raw bucket keys. Failures normalize to `503 {error, detail}`. hunyuan3d serializes generations on an `asyncio.Semaphore(1)` and runs inference off the event loop. UhhCraft env var names must match `internal/config/config.go` exactly (it boots via `requireEnv` and panics on a mismatch) — keep `templates/env.j2` and `.env.example` in lockstep with it.

## Open questions for later phases

GPU host / PCIe passthrough readiness (Phase 6); container registry GHCR vs Harbor (Phase 8); Stripe live-vs-test in smoke (Phase 10, test default); CSP for Three.js WASM workers (Phase 10).
