# 23 — weft as a platform service (front door, workspaces, staging)

> **Depends on:** 01 (secrets), 05 (observability: the Discord bot and webhook pattern),
> 10 (infra resilience), 15 (the service deployment workflow, which provisions and hardens the VMs)
>
> The plan of record is weft's own `docs/cicd.md` (reviewed by Jacob on 2026-10-01; decisions
> D533–D537 in weft's ledger). This document records only what that plan means in agent-cloud,
> which is its slice 11; it does not restate it. The OpenSpec change is
> `openspec/changes/add-weft-service/`: its `tasks.md` is the remaining work, as gated phases.

**Date:** 2026-10-03, renumbered from 16 on 2026-11-11 (16 is Plane's; 21 and 22 are taken)
**Status:** ACTIVE — slice 11 written and tested on the controller; nothing has run on a host.

**Context:** weft is uhstray's agent harness. On the rack it is one front door (identity across
companies; it redirects and never proxies) and one VM per company workspace, plus a staging pair
that follows weft's `main`. It is the first service here whose deploy takes a **version as input**:
weft's CI builds every image once, signs it with keyless cosign, and publishes a signed
`release.json` naming each image by digest; a deploy verifies and runs exactly those digests.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **The version a weft host runs is recorded on its VM (`deployed.json`, in `/srv/weft-state/`) and as a GitHub deployment, not in site-config** | The pipeline changes it and a person does not. A declared deviation from "a tag bump is an inventory change" (`update-service.yml`). |
| D2 | **Signatures checked on the target, by a digest-pinned cosign container** (`tasks/verify-cosign.yml`) | The check happens on the machine that pulls, needs nothing installed but podman, and pins the checker itself by digest. Exact identity `images.yml@refs/heads/main`, exact issuer. |
| D3 | **Automatic rollback only inside a deploy's own watch window, with its own backup** (weft D535) | After the window people have used the new version; a rollback would lose what they wrote. Every other rollback is `rollback-weft-workspace.yml`, a person's. |
| D4 | **Timers pull; GitHub never reaches Semaphore** | No GitHub job holds a deploy credential. Semaphore reads GitHub with `secret/services/weft/github:token` and writes deployment records back. |
| D5 | **The rollout checks the `rack` approval itself**, through GitHub's API, against `weft_release_approver` | Publishing a release is something any token with contents:write can do. |
| D6 | **A release a host rolled back is never retried on that host by a timer** | Without it the five-minute timer would redeploy a failing release every tick. The fix is a new release. |
| D7 | **Per-host deploy lock** (`deploying/` in the host's `/srv/weft-state/` directory), honoured by both timers | A deploy takes about twenty minutes and the timers fire every five. |
| D8 | **VM creation stays the platform's onboarding workflow** (Provision VM → Distribute SSH Keys → Harden SSH → Apply Firewall); `Stand Up weft Workspace` does the weft half | Each concern is its own workflow (Critical Deployment Rule 3). `vm_cpu` gives a workspace VM AVX2 (`x86-64-v3` or `host`). |
| D9 | **A workspace is reached at `<company>.<domain>`** (`weft_workspace_name`, the name its admin chose); `weft_workspace_id` stays the claim audience, the VM and the directory name. Every other subdomain goes to the front door, which redirects old names and id addresses | weft D540 (weft#260). A rename is two steps: the admin renames, then inventory follows; between them the new name answers 503 |
| D10 | **The deploy's record lives where no container mounts it** (`/srv/weft-state/`), and a rollback verifies the previous release's images before it runs them | The board faces the internet and mounts the workspace directory read-write; a record there could name an attacker's digest as "the previous release" (agent-cloud#890 B2). |
| D11 | **The staging timer is directly Dev-bound until promotion, and places the branch it runs from**; the rack's timer is main-bound and arrives with promotion | A schedule on a main-bound base runs `main`, which has no weft code until promotion. A scheduled task answers no survey, so the playbook names the branch. |
| D12 | **Every weft hostname is proxied by Cloudflare, and weft sends scrubbed platform-process logs to Loki and serves `/metrics`** (Jacob, 2026-11-10) | Every platform hostname is proxied (05); every service meets the observability minimum tier (06). Customer content stays on its VM. The costs are in the OpenSpec change's `design.md`. |

## Accepted costs

- A deploy interrupts agent sessions in progress; the next pass starts them again (weft §6.1).
- OpenBao holds every workspace's seal copy; a compromise of OpenBao opens every workspace's stored
  credentials. Only the controller's AppRole reads them, and no weft VM reaches OpenBao.
- A new workspace has no model to call until its admin signs in and enters its keys on Settings ›
  Credentials (weft D539): the keys are the company's and never pass through Semaphore or OpenBao.
- A rollback re-verifies the previous release's signatures, so it needs the registry reachable;
  when it is not, the rollback stops everything, deletes nothing and asks a person.

## Remaining

The phases, gates and scenarios are in `openspec/changes/add-weft-service/tasks.md`. In short:

- [x] `vm_cpu` is honoured by `provision-vm.yml` and converged by `resize-vm.yml` (on `dev` since
      `cfc8ad69`, with a node-flag check).
- [ ] Promote `snapshot-vm.yml` to a task so each deploy can take a Proxmox snapshot first (weft §6.1).
- [ ] Move `seed-o11y-alert-webhook.yml` onto `tasks/reconcile-discord-webhook.yml`.
- [ ] Declare the deviations in `PRINCIPLES.md` and 06; the edge (proxied records, the client
      address from `CF-Connecting-IP`, the firewall); observability; the first staging run; the
      first rollout, and Bench onto the rack.
