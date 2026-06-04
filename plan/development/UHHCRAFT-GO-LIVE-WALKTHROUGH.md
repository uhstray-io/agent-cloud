---
title: UhhCraft Go-Live — Session Walkthrough (You + Claude)
date: 2026-06-04
status: ACTIVE — ready to run
audience: the operator (you) running this go-live in a live session with Claude
companion_to: UHHCRAFT-GO-LIVE-PLAN.md
tags: [uhhcraft, go-live, phase-10, walkthrough, runbook, operator]
---

# UhhCraft Go-Live — Session Walkthrough (You + Claude)

This is the **step-by-step script for doing the go-live together**, in a live chat session. It's the *how-we-execute* companion to [`UHHCRAFT-GO-LIVE-PLAN.md`](UHHCRAFT-GO-LIVE-PLAN.md) (the *what-needs-to-be-true* reference). Follow this top to bottom; at each step it's clear **who does what** and **how we hand off**.

## Legend

- **[YOU]** — you do it (Proxmox console, a dashboard, a physical/host action, or a decision). Things I can't reach or shouldn't do.
- **[CLAUDE]** — I do it, in our chat (run a command, trigger a Semaphore job, read logs, write a fix). Assumes Step 1 confirmed I can reach the relevant service.
- **[BOTH]** — we do it together in the chat (talk through a decision, walk a checklist).
- **Hand-off cue** — the sentence in *italics* tells you exactly what to say/paste to move to the next step.

> **Ground rule (unchanged):** real deploys run through **Semaphore**, never a manual SSH `deploy.sh`. Where I "deploy," I mean **triggering a Semaphore template via its API** (with your OK) and polling the task — not SSHing into a VM.

---

## Step 0 — Make the four decisions  ·  [BOTH], in chat

Before any infrastructure, settle the four gating decisions (full tradeoffs in the plan, §A). We do this conversationally — I lay out each, you decide, I record your answers.

1. **Stripe** — test mode vs live for first go-live. (Rec: test.)
2. **CSP** — confirm/allow Three.js WASM workers (`worker-src 'self' blob:`).
3. **Registry** — GHCR vs Harbor. (Rec: GHCR now.)
4. **GPU §1** — which Proxmox host the GPUs live on (resolve in `UHHCRAFT-GPU-PASSTHROUGH.md` §1).

*Hand-off: say "let's do the decisions" and we'll work through them; I'll write your answers into the plan.*

---

## Step 1 — Establish what I can reach (gates the rest)  ·  [CLAUDE], with your OK

This determines whether later steps are **Claude-driven** (I trigger Semaphore jobs + poll) or **You-driven, Claude-triages** (you click in Semaphore, paste me output). `site-config/secrets/` is present in this environment, so the question is purely network reachability.

- **[YOU]** confirm: "yes, you may use the site-config credentials for this session."
- **[CLAUDE]** I then test, read-only: can I reach the Semaphore API (token in `site-config/secrets/semaphore/`)? OpenBao? Proxmox API? I report exactly which are reachable.
- **Outcome:** we mark each later "deploy/secret/provision" step as **[CLAUDE]** (reachable) or **[YOU]** (not reachable → you run it in the UI, I guide + triage).

*Hand-off: say "you can use the site-config creds — check what you can reach," and I'll report the reachability matrix.*

---

## Step 2 — Provision the four VMs  ·  [YOU] on Proxmox, [CLAUDE] verifies

- **[YOU]** create the VMs per `../../platform/hypervisor/proxmox/vm-specs.example.yml` (real sizing/IPs in site-config): `uhhcraft_svc` (CPU), `inference_comfyui_svc` (GPU), `inference_hunyuan3d_svc` (GPU), `caddy_svc`. Base: Ubuntu 24.04. (If I can reach Proxmox, I can help drive `provision-vm.yml`; otherwise you create them in the Proxmox UI / `qm`.)
- **[YOU]** add the real hosts to `site-config` `production.yml` (the four `*_svc` groups, with `service_url`, `health_path: /healthz`, `container_engine: podman`, and the cross-service URL vars).
- **[CLAUDE]** I verify the inventory groups/vars line up with what the playbooks read, and flag anything missing — before we deploy onto them.

*Hand-off: "VMs are up and in production.yml" → I run the inventory pre-flight check.*

---

## Step 3 — GPU passthrough on the two inference VMs  ·  [YOU] on the host, [CLAUDE] guides

- **[CLAUDE]** I read you the exact steps from `UHHCRAFT-GPU-PASSTHROUGH.md` (BIOS/IOMMU → VFIO bind → q35/OVMF → driver), one at a time.
- **[YOU]** run each on the Proxmox host / in the VM; paste me the output of the verification commands (`lspci -nnk`, `nvidia-smi`).
- **[CLAUDE]** I confirm each checkpoint (GPU bound to `vfio-pci`; `nvidia-smi` works in the VM; CDI `nvidia.com/gpu=all` resolvable) before moving on.

*Hand-off: paste the `nvidia-smi` output from each GPU VM and I'll confirm or debug.*

---

## Step 4 — DNS + Stripe webhook  ·  [YOU] in dashboards, [CLAUDE] tells you exactly what

- **[CLAUDE]** I give you the precise CloudFlare record (`uhhcraft.uhstray.io` A → `caddy_svc` IP) and the exact Stripe webhook endpoint URL + which events to enable.
- **[YOU]** create the CloudFlare DNS record + the DNS-01 API token; create the Stripe **test-mode** webhook endpoint and copy its signing secret.
- **[YOU]** hand me (or seed yourself in Step 5) the `stripe_webhook_secret`.

*Hand-off: "DNS + Stripe webhook created" → on to secrets.*

---

## Step 5 — Seed the secrets  ·  [YOU] provides values, [CLAUDE] checks/seeds

The `random` secrets auto-generate; the **`user`** secrets must exist in OpenBao first (full table in plan §B3): Stripe (test) keys + webhook secret, Resend key, two Discord webhook URLs, USPS client id/secret, (optional) Printify/Hubs.

- **[YOU]** provide the values (or seed them yourself if I can't reach OpenBao).
- **[CLAUDE]** if OpenBao is reachable: I run the `Check Secrets` / seed path and confirm every required key is present + non-empty. If not reachable: I give you the exact `bao kv put` / Semaphore "Sync Secrets" steps and you confirm.

*Hand-off: "secrets are seeded" → I run/confirm the secret inventory.*

---

## Step 6 — Policies, AppRoles, SSH, Podman  ·  [CLAUDE] triggers (or [YOU] clicks)

Run these Semaphore templates (I trigger + poll if reachable; otherwise you click and paste results):

1. `Distribute SSH Keys` → verify key auth → `Harden SSH`.
2. `Install Podman` on each host.
3. `Apply Policy - UhhCraft / ComfyUI Sidecar / Hunyuan3D Sidecar`; `Provision AppRole - Orb Agent`.

*Hand-off: "go" and I'll trigger them in order (or hand you the click-list).*

---

## Step 7 — Deploy, in order  ·  [CLAUDE] triggers + polls, triage on failure

Per the plan §C ordering. For each: I trigger the Semaphore template, poll the task, and report green/failure. **On any failure → triage loop:** I read the task log, diagnose, and push a fix PR; we re-run.

1. `Deploy ComfyUI Sidecar` → healthy.
2. `Deploy Hunyuan3D Sidecar` → healthy.
3. `Deploy UhhCraft` (5-phase: secrets → containers → migrations → Caddy fragment → verify).
4. Confirm `sites/uhhcraft.caddy` distributed + Caddy reloaded clean.

> ⚠️ **Open gap (plan §I):** there is no `deploy-caddy` playbook/template — confirm how `caddy_svc` itself is running **before** step 3's fragment distribution. We resolve this when we hit it.

*Hand-off: "start the deploy" → I run them one at a time and report after each.*

---

## Step 8 — Smoke test (happy path)  ·  [YOU] in browser, [CLAUDE] checks endpoints

Walk the plan §D checklist together: catalog → generate (ComfyUI) → 3D (Hunyuan3D) → cart → Stripe test → order → webhook → fulfillment, plus the AI-offline graceful state and `validate-all`.

- **[YOU]** click through the storefront; tell me what you see at each step (esp. any error or CSP console warning).
- **[CLAUDE]** I hit `/healthz` on each host, run `Validate All Services`, and check stored-asset URLs / order rows where reachable.

*Hand-off: go step by step in the UI; paste me anything that errors and I'll diagnose.*

---

## Step 9 — Rollback drill (required once)  ·  [CLAUDE] triggers, [YOU] observes

- **[CLAUDE]** deploy a known-bad image tag via `Update UhhCraft`, observe the failure, then roll back to the previous good SHA; confirm data intact.
- **[BOTH]** record the result (date/SHAs/outcome) in the deployment README.

*Hand-off: "do the rollback drill" → I run it and we record the result.*

---

## Step 10 — Sign-off  ·  [BOTH]

- **[CLAUDE]** run `/simplify` + `/security-review` on the live diff; tick the Definition-of-Done boxes (plan §F); mark Phase 10 complete in `WEBSMITH-INTEGRATION-PLAN.md` §6 and update root `CLAUDE.md` deployment status.
- **[YOU]** give the final sign-off that the happy path + rollback are genuinely good.

*Hand-off: "sign off" → I update the plan/status docs and we're done.*

---

## Quick reference — who owns what

| Step | [YOU] | [CLAUDE] |
|---|---|---|
| 0 Decisions | decide | lay out tradeoffs, record |
| 1 Reachability | authorize creds | test + report what I can reach |
| 2 VMs | create on Proxmox + inventory | verify inventory wiring |
| 3 GPU passthrough | run on host | guide + verify output |
| 4 DNS/Stripe | dashboards | exact records/URLs |
| 5 Secrets | provide values | check/seed + confirm inventory |
| 6 Policies/SSH/Podman | (or click) | trigger templates + poll |
| 7 Deploy | (or click) | trigger + poll + **triage** |
| 8 Smoke | browser walk | endpoint checks + diagnose |
| 9 Rollback | observe | trigger + record |
| 10 Sign-off | final approval | update plan/status docs |

## Cross-references
- [`UHHCRAFT-GO-LIVE-PLAN.md`](UHHCRAFT-GO-LIVE-PLAN.md) — the comprehensive reference (decisions, secret table, deploy sequence, smoke checklist, DoD, gaps).
- [`UHHCRAFT-GPU-PASSTHROUGH.md`](UHHCRAFT-GPU-PASSTHROUGH.md) — GPU step detail (Step 3).
- Root [`CLAUDE.md`](../../CLAUDE.md) — Operational Access (using site-config creds) + Semaphore-only deploy rule.

## Revision history
| Date | Change |
| --- | --- |
| 2026-06-04 | Initial session walkthrough — sequential [YOU]/[CLAUDE] steps with hand-off cues, companion to UHHCRAFT-GO-LIVE-PLAN.md. |
