---
name: websmith-integration
description: WebSmith (website_framework) + UhhCraft integration into agent-cloud — locked decisions, plan location, and merged-phase status.
metadata:
  node_type: memory
  type: project
---

The external `website_framework` repo is being integrated into agent-cloud across 11 phases. The framework becomes the **WebSmith** agent (`agents/websmith/`); its concrete output — **UhhCraft**, a Go + templ + HTMX storefront with Python AI sidecars — becomes the first platform service built that way. Full plan: `plan/development/WEBSMITH-INTEGRATION-PLAN.md`.

## Phase status (verified against `main`, 2026-06-01)

- **Phases 1–9 — MERGED**, one PR each: P1 #18 (`agents/websmith/` agent move), P2 #19 (full UhhCraft service + `inference-comfyui` / `inference-hunyuan3d` sidecars; cleared a 125+19-finding CodeRabbit pass), P3 #27 (reserved OpenBao policies), P4 #28 (composable Ansible playbooks), P5 #30 (central-Caddy per-site fragment), P6 #32 (inventory + Proxmox VM specs + GPU sub-plan), P7 #34 (Semaphore templates), P8 #35 (CI: Go/templ/sqlc/gosec), P9 #40 (architecture docs + cross-links).
- **Phase 10 — PENDING, hardware-gated.** Acceptance needs branch-deploy to the real uhhcraft/comfyui/hunyuan3d/caddy VMs + happy-path smoke + GPU passthrough + rollback drill — can't be completed from a dev box.
- **Phase 11 — PENDING (doable without hardware):** codify the second-site recipe into WebSmith's `catalogs/`.

**UhhCraft dependency upgrade — DONE** (PRs #36–#39, separate from the phases): Go 1.23→1.26, golangci-lint v1→v2, sqlc/goose bumps + low-risk deps (#36); templ 0.2→0.3 (#37); river 0.11→0.38 (#38); stripe-go v79→v82 (#39). The breaking ones were split one-PR-each on purpose — that's how the stripe-go v82 webhook API-version re-pin got caught (would've silently dropped `payment_intent.succeeded` events; fixed with `ConstructEventWithOptions{IgnoreAPIVersionMismatch: true}`).

**Workflow used:** one PR per phase; wait for *all* CI checks + CodeRabbit to complete and pass before merging; squash-merge. Fold confirmed CodeRabbit findings into [[coderabbit-preflight-checklist]] so the next PR prevents them. See [[coderabbit-rate-limits]] for the review-cadence constraint.

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
