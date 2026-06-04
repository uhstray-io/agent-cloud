---
title: UhhCraft Go-Live — Phase 10 Production Validation Plan
date: 2026-06-02
status: ACTIVE
audience: operator running Semaphore against the live cluster, and any agent preparing/triaging the rollout
tags: [uhhcraft, inference-comfyui, inference-hunyuan3d, caddy, phase-10, go-live, validation, proxmox, openbao]
---

# UhhCraft Go-Live — Phase 10 Production Validation Plan

This is the **single, comprehensive checklist** for taking the UhhCraft platform (the `uhhcraft` storefront + the `inference-comfyui` and `inference-hunyuan3d` GPU sidecars, fronted by central Caddy) from "all code merged" to "live, validated, and signed off." It is **Phase 10** of [`WEBSMITH-INTEGRATION-PLAN.md`](WEBSMITH-INTEGRATION-PLAN.md) — the only remaining substantive phase.

> **Why this doc exists:** every prior phase (1–9, 11) was code/docs that merged through CI. Phase 10 is different — it can only be completed against **real hardware**, and it depends on a few **human decisions** and a **provisioning chain** that must happen in order. This document makes all of that explicit so nothing is missed and so the work can be handed to another agent or operator succinctly.

> **Running it as a live session?** Use the step-by-step **[`UHHCRAFT-GO-LIVE-WALKTHROUGH.md`](UHHCRAFT-GO-LIVE-WALKTHROUGH.md)** — the sequential `[YOU]`/`[CLAUDE]` operator script with hand-off cues. This doc is the reference (the *what*); the walkthrough is the *how-we-execute-together*.

---

## 0. How to use this document

- **Sections A–B are prerequisites** — decisions and provisioning that must be done *before* the first deploy. Skipping any of them makes the deploy fail or produce an unvalidated result.
- **Sections C–F are the execution** — deploy, smoke-test, rollback drill, and the formal acceptance checklist.
- **Sections G–I are the safety net** — known gotchas, the responsibility split, and the open gaps to close.
- Boxes use `- [ ]` so progress is trackable. Work top-to-bottom; later steps assume earlier ones are green.

### The operator ↔ agent boundary (read first)

Per the root [`CLAUDE.md`](../../CLAUDE.md) critical rules, **all deploys go through Semaphore — never SSH into a VM and run `deploy.sh` directly.** That means:

- **Only a human operator (or a Semaphore-authorized automation) can execute the deploy/smoke/rollback steps** in Sections C–E. They run against the live cluster, which an assisting agent does not (and should not) have credentials for.
- **An assisting agent CAN:** prepare this plan, lay out decisions, audit that playbooks/templates/inventory line up, draft the smoke checklist, and **triage failures** the operator pastes back. It cannot push to the server.
- **Dashboard/registrar steps** (Stripe webhook config, CloudFlare DNS, Proxmox VM creation, GPU host BIOS/IOMMU) are operator actions outside Semaphore entirely.

---

## 1. Scope & definition of done

**In scope:** `uhhcraft` (CPU VM), `inference-comfyui` (GPU VM), `inference-hunyuan3d` (GPU VM), and the central `caddy` host that fronts them.

**Out of scope (explicitly):** the NocoDB / n8n composable migration (separate, **HELD** — see [`nocodb-n8n-composable-migration.md`](nocodb-n8n-composable-migration.md)); the Planned agent items (NemoClaw automation, Cowork, cross-agent, Kubernetes).

**Done when all of these hold** (from `WEBSMITH-INTEGRATION-PLAN.md` §6, the unchecked items):

- [ ] `https://uhhcraft.uhstray.io` serves the Go app behind central Caddy with a valid Let's Encrypt (DNS-01) certificate.
- [ ] OpenBao holds every UhhCraft + inference secret; no secrets in the repo or on VMs outside the templated `.env`.
- [ ] `validate-all.yml` returns green for all four hosts.
- [ ] The full happy-path smoke flow passes (Section D).
- [ ] The rollback procedure is documented **and exercised at least once** (Section E).

---

## 2. Current state (what is already done)

- **Merged:** WebSmith integration Phases 1–9 + 11; the UhhCraft dependency upgrade series (Go 1.26, golangci-lint v2, templ 0.3, river 0.38, stripe-go v82); orb-agent dedicated AppRole provisioning (#46); doc/hygiene cleanup (#48).
- **Code artifacts in place (verified):** `deploy-uhhcraft.yml`, `update-uhhcraft.yml`, `clean-deploy-uhhcraft.yml`, `deploy-inference-comfyui.yml`, `deploy-inference-hunyuan3d.yml`, `update-inference-*.yml`, `apply-policy-{uhhcraft,inference-comfyui,inference-hunyuan3d}.yml`, `provision-orb-agent-approle.yml`, `distribute-ssh-keys.yml`, `harden-ssh.yml`, `install-podman.yml`, `provision-vm.yml`, `validate-all.yml`; the matching Semaphore templates; the per-site Caddy fragment `templates/caddy-site.j2` + `tasks/distribute-caddy-site.yml`; the `healthcheck` and `river migrate-up` subcommands in `cmd/server/main.go`.
- **Not done:** everything in this document below — the live provisioning, deploy, and validation.

---

## A. Decisions to make first (gates execution)

These four are unresolved and block or shape the rollout. Each lists the options and a recommendation.

### A1. Stripe — test mode vs live mode for the first deploy

- **Context:** `stripe-go` is on v82 (API version `2025-08-27.basil`). The webhook handler uses `ConstructEventWithOptions{IgnoreAPIVersionMismatch: true}` so it tolerates a dashboard endpoint on any API version.
- **Recommendation:** **Test mode** for the first go-live + smoke. Promote to live only after a separate human-signed checkout checklist. Seed the **test** `stripe_secret_key` / `stripe_publishable_key` / `stripe_webhook_secret`.
- **Decision:** `[ ]` test  `[ ]` live
- **Also required regardless:** in the Stripe **dashboard**, create a webhook endpoint pointing at `https://uhhcraft.uhstray.io/<webhook path>` and copy its signing secret into OpenBao as `stripe_webhook_secret`. Without this, `payment_intent.succeeded` never reaches the app and orders never get created.

### A2. CSP for the Three.js / WASM 3D canvas

- **Context:** UhhCraft's Caddy fragment ships a tight Content-Security-Policy. The 3D preview uses Three.js with WASM workers, which need `worker-src 'self' blob:` (and possibly `script-src 'wasm-unsafe-eval'`).
- **Recommendation:** before the 3D smoke step, confirm the rendered `caddy-site.j2` CSP allows WASM workers; add `worker-src 'self' blob:` if missing. Verify with the browser console on the canvas page (no CSP violations).
- **Decision / action:** `[ ]` CSP verified to allow Three.js WASM workers (or amended).

### A3. Container image registry — GHCR vs Harbor

- **Context:** `update-uhhcraft.yml` rolls back via `uhhcraft_image=ghcr.io/uhstray-io/uhhcraft:<sha>`, implying CI-built images pushed to a registry. The K8s roadmap mentions Harbor.
- **Recommendation:** **GHCR now** (`ghcr.io/uhstray-io/…`), Harbor later. Confirm CI actually builds + pushes the image (the `go-build` job builds it; verify it also *pushes* on merge to main, and that the VMs can `podman pull` from GHCR — i.e., a pull secret/login is configured if the package is private).
- **Decision:** `[ ]` GHCR  `[ ]` Harbor  — and `[ ]` confirmed VMs can pull the image.

### A4. GPU passthrough §1 host decision

- **Context:** the two inference VMs need PCIe GPU passthrough. [`UHHCRAFT-GPU-PASSTHROUGH.md`](UHHCRAFT-GPU-PASSTHROUGH.md) §1 records a host-selection decision that is **still pending** — the passthrough setup path differs depending on whether the GPU host is already in the Proxmox cluster.
- **Decision:** resolve §1 in that doc before provisioning the GPU VMs.

---

## B. Provisioning prerequisites (before any deploy)

This is the chain that must exist before Section C. Most steps are **operator actions** (Proxmox, dashboards) or **Semaphore template runs**.

### B1. Proxmox VMs

- [ ] Create the four VMs per [`../../platform/hypervisor/proxmox/vm-specs.example.yml`](../../platform/hypervisor/proxmox/vm-specs.example.yml) (real sizing/IPs live in **site-config**, never this repo):
  - `uhhcraft_svc` — CPU VM (Go app + Postgres + Redis + MinIO containers).
  - `inference_comfyui_svc` — GPU VM.
  - `inference_hunyuan3d_svc` — GPU VM.
  - `caddy_svc` — central reverse-proxy host (see **B5 / Section I** — confirm this host's own deploy path).
- [ ] Base image: Ubuntu 24.04 cloud-init template; provision via `provision-vm.yml` (Semaphore: **Provision VM**).
- [ ] `install-podman.yml` (Semaphore: **Install Podman**) on each service host.

### B2. GPU passthrough (the two inference VMs)

- [ ] Resolve decision **A4** first.
- [ ] Follow [`UHHCRAFT-GPU-PASSTHROUGH.md`](UHHCRAFT-GPU-PASSTHROUGH.md): host IOMMU/VFIO, VM `hostpci` entry, then in-VM NVIDIA driver + Container Toolkit + CDI (`tasks/install-nvidia-toolkit.yml`).
- [ ] Verify inside each GPU VM: `nvidia-smi` sees the card, and a CDI device (`nvidia.com/gpu=all`) is resolvable by Podman.

### B3. OpenBao secrets (source of truth)

The `random`-type secrets auto-generate on first deploy. The **`user`-type secrets MUST be seeded into OpenBao before deploy** or the app boots with blank/invalid credentials. For `uhhcraft` (`secret/services/uhhcraft`):

| Secret (key) | Type | Who provides |
|---|---|---|
| `postgres_password`, `redis_password`, `minio_root_user`, `minio_root_password`, `session_secret` | random | auto-generated by `manage-secrets.yml` |
| `stripe_secret_key`, `stripe_publishable_key`, `stripe_webhook_secret` | user | Stripe dashboard (test mode per A1) |
| `resend_api_key` | user | Resend |
| `discord_orders_webhook_url`, `discord_ops_webhook_url` | user | Discord server webhooks |
| `usps_client_id`, `usps_client_secret` | user | USPS v3 OAuth2 app |
| `printify_api_key`, `printify_shop_id`, `hubs_api_key` | user | fulfillment vendors (optional — leave empty if unused) |

For the inference sidecars (`secret/services/inference-comfyui`, `secret/services/inference-hunyuan3d`): each owns its own MinIO root creds (random) plus its service URL / model path.

- [ ] Seed all `user`-type secrets into OpenBao (operator; use the **Sync Secrets to OpenBao** / `manage-secrets` path — never commit them).
- [ ] `Check Secrets` (read-only inventory) shows every required key present/non-empty.
- [ ] Apply OpenBao policies: **Apply Policy - UhhCraft / ComfyUI Sidecar / Hunyuan3D Sidecar**, and **Provision AppRole - Orb Agent** (already code-managed; run once against live OpenBao to replace any hand-made creds).

### B4. SSH + hardening

- [ ] `distribute-ssh-keys.yml` (**Distribute SSH Keys**) — per-service ed25519 keys from OpenBao to each host.
- [ ] Verify key auth works, then `harden-ssh.yml` (**Harden SSH**) — NOPASSWD sudo + sshd lockdown. (Hardening only **after** key auth is confirmed, per the critical rules.)

### B5. DNS + central Caddy

- [ ] CloudFlare: `uhhcraft.uhstray.io` A record → the `caddy_svc` public IP (and any `/generated/*` routing is via Caddy, same host).
- [ ] CloudFlare API token for **DNS-01** ACME present in OpenBao / Caddy env (central Caddy issues the Let's Encrypt cert via DNS-01).
- [ ] **Central Caddy host must be running** before the per-site fragment is distributed. ⚠️ See **Section I** — there is currently no `deploy-caddy.yml` / Caddy Semaphore template; confirm how the Caddy host itself is stood up.
- [ ] The `uhhcraft` deploy renders `templates/caddy-site.j2` → `sites/uhhcraft.caddy` and `tasks/distribute-caddy-site.yml` pushes it to the Caddy host + validates + reloads.

### B6. Inventory + Semaphore

- [ ] **site-config** `production.yml` has real host entries for all four groups with `service_name`, `monorepo_deploy_path`, `service_url`, `health_path: /healthz`, `container_engine: podman`, and the cross-service vars (`uhhcraft_ai_image_url`, `uhhcraft_ai_3d_url`, `inference_*_minio_upstream`).
- [ ] Semaphore templates pushed (`setup-templates.yml`) — the Deploy/Update/Clean/Apply-Policy/Provision templates listed in `platform/semaphore/templates.yml` exist in the Semaphore UI.

---

## C. Deploy sequence (Semaphore, in order)

Run each as its Semaphore template. Order matters (sidecars before the app, so the app's AI URLs resolve; Caddy fragment after the app is healthy).

1. [ ] **Apply Policy - ComfyUI Sidecar**, **Apply Policy - Hunyuan3D Sidecar**, **Apply Policy - UhhCraft** (idempotent; ensures OpenBao policies).
2. [ ] **Deploy ComfyUI Sidecar** → wait healthy; note its Caddy-routed public URL.
3. [ ] **Deploy Hunyuan3D Sidecar** → wait healthy.
4. [ ] **Deploy UhhCraft** — Phase 1 secrets/template → Phase 2 `deploy.sh` (podman compose up + wait healthy) → Phase 3 `post-deploy.sh` (`goose` migrate + `uhhcraft river migrate-up` + smoke) → Phase 4 render + distribute Caddy fragment → Phase 5 verify `/healthz`.
5. [ ] Confirm the `sites/uhhcraft.caddy` fragment landed on the Caddy host and Caddy reloaded cleanly (the distribute task validates-before-persist and rolls back on a bad fragment).
6. [ ] **(Optional) Deploy Orb Agent** if discovery of these hosts is wanted in NetBox.

---

## D. Smoke test — happy path (operator, in a browser + Semaphore)

- [ ] `/healthz` returns 200 on `uhhcraft`, `inference-comfyui`, `inference-hunyuan3d` (and Caddy serves them).
- [ ] `https://uhhcraft.uhstray.io` loads behind Caddy with a **valid cert** (no TLS warning).
- [ ] **Browse catalog** — pages render, static assets (Tailwind `app.css`, fonts) load, no CSP console errors.
- [ ] **Generate from a prompt** → ComfyUI returns 200 → image is stored in the ComfyUI MinIO and served via the Caddy `/generated/img/*` path (store-the-URL, not bytes).
- [ ] **3D canvas view** → Hunyuan3D returns 200 → GLB stored + served via `/generated/3d/*`; the Three.js canvas renders it with **no CSP violations** (decision A2).
- [ ] **Add to cart → checkout** → Stripe **test** PaymentIntent succeeds → order row created in Postgres.
- [ ] **Stripe webhook** → `payment_intent.succeeded` received & signature-verified → River job enqueued → fulfillment dispatch (Printify test / Hubs) fires; ops Discord webhook posts.
- [ ] **AI-offline behavior:** stop a sidecar, confirm the app renders the "AI is offline; try later" state and does **not** 500.
- [ ] Rate-limiting (Redis-backed) behaves on repeated generate attempts.
- [ ] **Validate All Services** (`validate-all.yml`) → green on all four hosts.

---

## E. Rollback drill (must be exercised once)

- [ ] **No-data-loss rollback:** intentionally deploy a known-bad `uhhcraft_image=ghcr.io/uhstray-io/uhhcraft:<bad-sha>`, observe failure, then **Update UhhCraft** with `uhhcraft_image=<previous-good-sha>` and confirm recovery with data intact.
- [ ] Document the result (date, SHAs, outcome) in `platform/services/uhhcraft/deployment/README.md` "Production rollback".
- [ ] Note: **Clean Deploy UhhCraft** is the *destructive* reset (wipes Postgres/Redis/MinIO volumes) — only on a known-broken stack with a fresh backup. Do **not** use it as routine rollback.

---

## F. Acceptance checklist (Definition of Done)

- [ ] `uhhcraft.uhstray.io` live behind Caddy, valid cert.
- [ ] All secrets in OpenBao; none on disk outside the templated `.env`; `Validate Secrets` passes.
- [ ] `validate-all.yml` green on all four hosts.
- [ ] Happy-path smoke (Section D) fully passed.
- [ ] Rollback drill (Section E) exercised + documented.
- [ ] `/simplify` and `/security-review` run on the full live diff before merge (per the repo branch workflow in root `CLAUDE.md`).
- [ ] `WEBSMITH-INTEGRATION-PLAN.md` §6 boxes checked; mark Phase 10 complete and update root `CLAUDE.md` deployment status.

---

## G. Known risks & gotchas (hard-won this cycle)

- **podman-compose `depends_on: service_healthy` is parsed-not-enforced on 1.0.6** (see [`../architecture/PODMAN-VS-DOCKER-COMPOSE.md`](../architecture/PODMAN-VS-DOCKER-COMPOSE.md) §4). Readiness is gated by explicit health-waits in `deploy.sh`/`post-deploy.sh`, not by compose ordering. If the app starts before Postgres is ready, that's the cause — check the host's podman-compose version.
- **Stripe webhook version skew is already tolerated** in code (`IgnoreAPIVersionMismatch: true`), but the dashboard endpoint + `stripe_webhook_secret` must still be configured (A1) or events silently never arrive.
- **Fully-qualified image names + no `version:` key** in compose (Podman requirement) — already handled in UhhCraft's compose; keep it if editing.
- **Caddy fragment safety:** `distribute-caddy-site.yml` validates the full config before persisting and rolls back on a parse error, but distinguishes engine faults from config errors — if it refuses to roll back, the container may simply be down (check Caddy reachability first).
- **Cross-service URLs come from inventory, not fact-fallbacks** — a missing `inference_*_minio_upstream` / `uhhcraft_ai_*_url` renders a valid-but-wrong fragment routing to loopback. Assert they're set (B6).
- **Generated code is CI-only** — VMs run the CI-built image; never expect `_templ.go`/`sqlcdb` in the repo.
- **CI Security Scan flakes** when trufflehog ships an assetless release upstream — not a leak and not ours; diagnose (release asset count + run timeline) before "fixing." See repo memory.

---

## H. Responsibility split

| Step | Operator (Semaphore / dashboards / Proxmox) | Assisting agent |
|---|---|---|
| Decisions A1–A4 | **Decides** | Lays out tradeoffs |
| Proxmox VMs, GPU host IOMMU (B1–B2) | **Does** | Verifies specs/docs |
| Seed `user` secrets, DNS, Stripe webhook | **Does** | Lists exactly what/where |
| Semaphore deploy/smoke/rollback (C–E) | **Runs** | Drafts checklists; **triages failures** |
| Definition of Done sign-off (F) | **Signs** | Updates plan/status docs |

---

## I. Open gaps to close before/while executing

1. **Central Caddy host deploy path is unclear.** There are deploy playbooks/templates for `uhhcraft` and both sidecars, and a per-site fragment distributor — but **no `deploy-caddy.yml` / "Deploy Caddy" Semaphore template** was found. Confirm how `caddy_svc` itself is stood up (manual? an existing service? a missing playbook). The per-site fragment distribution assumes a *running* Caddy. **This must be resolved before B5 / Section C step 5.**
2. **No `clean-deploy` for the inference sidecars** (only `uhhcraft` has one) — fine for go-live, but note it for destructive resets.
3. **Confirm CI pushes the image to GHCR on merge** (A3) and that VMs can `podman pull` it (login/pull-secret if private).
4. **Stripe webhook path** — confirm the exact route the handler listens on so the dashboard endpoint URL is correct.

---

## Cross-references

- [`WEBSMITH-INTEGRATION-PLAN.md`](WEBSMITH-INTEGRATION-PLAN.md) — Phase 10 is §Phase 10 + the §6 Definition of Done this plan operationalizes.
- [`UHHCRAFT-GPU-PASSTHROUGH.md`](UHHCRAFT-GPU-PASSTHROUGH.md) — GPU host prep + the pending §1 decision (A4).
- [`nocodb-n8n-composable-migration.md`](nocodb-n8n-composable-migration.md) — explicitly **out of scope** here (held).
- [`../architecture/BRANCH-TESTING-WORKFLOW.md`](../architecture/BRANCH-TESTING-WORKFLOW.md) — deploying a feature branch to the VMs for pre-merge validation.
- [`../architecture/CADDY-REVERSE-PROXY.md`](../architecture/CADDY-REVERSE-PROXY.md) — per-site fragment pattern + DNS-01.
- [`../architecture/PODMAN-VS-DOCKER-COMPOSE.md`](../architecture/PODMAN-VS-DOCKER-COMPOSE.md) — runtime gotchas (§4 readiness).
- [`../../platform/services/uhhcraft/deployment/README.md`](../../platform/services/uhhcraft/deployment/README.md) — service deploy story + rollback commands.
- [`../../platform/services/uhhcraft/context/spec/SPEC.md`](../../platform/services/uhhcraft/context/spec/SPEC.md) — signed spec + alignment.

---

## Revision history

| Date | Change |
| --- | --- |
| 2026-06-02 | Initial comprehensive go-live plan. Consolidates the Phase 10 work items, the four open decisions, the full provisioning chain, deploy sequence, smoke + rollback, Definition of Done, and the open gaps (notably the unclear central-Caddy deploy path). |
