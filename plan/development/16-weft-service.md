# 16 — weft as a platform service (front door, workspaces, staging)

> **Depends on:** 01 (secrets), 05 (observability: the Discord bot and webhook pattern),
> 10 (infra resilience), 15 (the service deployment workflow, which provisions and hardens the VMs)
>
> The plan of record is weft's own `docs/cicd.md` (reviewed by Jacob on 2026-10-01; decisions
> D533–D537 in weft's ledger). This document records only what that plan means in agent-cloud,
> which is its slice 11; it does not restate it.

**Date:** 2026-10-03
**Status:** ACTIVE — slice 11 written and structurally tested; nothing has run against the rack.

**Context:** weft is uhstray's agent harness. On the rack it is one front door (identity across
companies; it redirects and never proxies) and one VM per company workspace, plus a staging pair
that follows weft's `main`. It is the first service here whose deploy takes a **version as input**:
weft's CI builds every image once, signs it with keyless cosign, and publishes a signed
`release.json` naming each image by digest; a deploy verifies and runs exactly those digests.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **The version a weft host runs is recorded on its VM (`deployed.json`) and as a GitHub deployment, not in site-config** | The pipeline changes it and a person does not. A declared deviation from "a tag bump is an inventory change" (`update-service.yml`). |
| D2 | **Signatures checked on the target, by a digest-pinned cosign container** (`tasks/verify-cosign.yml`) | The check happens on the machine that pulls, needs nothing installed but podman, and pins the checker itself by digest. Exact identity `images.yml@refs/heads/main`, exact issuer. |
| D3 | **Automatic rollback only inside a deploy's own watch window, with its own backup** (weft D535) | After the window people have used the new version; a rollback would lose what they wrote. Every other rollback is `rollback-weft-workspace.yml`, a person's. |
| D4 | **Timers pull; GitHub never reaches Semaphore** | No GitHub job holds a deploy credential. Semaphore reads GitHub with `secret/services/weft/github:token` and writes deployment records back. |
| D5 | **The rollout checks the `rack` approval itself**, through GitHub's API, against `weft_release_approver` | Publishing a release is something any token with contents:write can do. |
| D6 | **A release a host rolled back is never retried on that host by a timer** | Without it the five-minute timer would redeploy a failing release every tick. The fix is a new release. |
| D7 | **Per-host deploy lock** (`<dir>/deploying`), honoured by both timers | A deploy takes about twenty minutes and the timers fire every five. |
| D8 | **VM creation stays the platform's onboarding workflow** (Provision VM → Distribute SSH Keys → Harden SSH → Apply Firewall); `Stand Up weft Workspace` does the weft half | Each concern is its own workflow (Critical Deployment Rule 3). `vm_cpu` gives a workspace VM AVX2 (`x86-64-v3` or `host`). |
| D9 | **A workspace is reached at `<company>.<domain>`** (`weft_workspace_name`, the name its admin chose); `weft_workspace_id` stays the claim audience and the directory name | Jacob's decision 2026-10-05. Needs weft's change that lets the front door address a workspace by that name; until it lands the front door's template refuses `{name}` |

## Accepted costs

- A deploy interrupts agent sessions in progress; the next pass starts them again (weft §6.1).
- OpenBao holds every workspace's seal copy; a compromise of OpenBao opens every workspace's stored
  credentials. Only the controller's AppRole reads them, and no weft VM reaches OpenBao.
- A new workspace has no model to call until its admin signs in and enters its keys on Settings ›
  Credentials (weft D539): the keys are the company's and never pass through Semaphore or OpenBao.
- Until `dev` is promoted, the two scheduled templates' main-bound bases fail visibly on main's
  absent playbooks (the `setup-templates.yml` rule for pre-promotion schedules).

## Remaining

- [ ] Promote `snapshot-vm.yml` to a task so each deploy can take a Proxmox snapshot first (weft §6.1).
- [ ] Move `seed-o11y-alert-webhook.yml` onto `tasks/reconcile-discord-webhook.yml`.
- [ ] Converge `vm_cpu` in `resize-vm.yml` as well (it is applied at provisioning only).
- [ ] Slice 12 (VMs, firewall, UDP forwards, Cloudflare records), 13 (first staging run), 14 (first
      rollout, and Bench onto the rack), each against the real rack.
