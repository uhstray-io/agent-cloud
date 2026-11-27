# 25 — weft as a platform service (front door, workspaces, staging)

> **Depends on:** 01 (secrets), 10 (infra resilience), 15 (the service deployment workflow, which
> provisions and hardens the VMs)
>
> The plan of record is weft's own `docs/operations/cicd.md` (reviewed by Jacob on 2026-10-01; decisions
> D533–D537 in weft's ledger). This document records only what that plan means in agent-cloud,
> which is its slice 11; it does not restate it. The OpenSpec change is
> `openspec/changes/add-weft-service/`: its `tasks.md` is the remaining work, as gated phases.

**Date:** 2026-10-03, renumbered from 16 on 2026-11-11 and from 23 on 2026-11-12 (23 is the
release train's)
**Status:** ACTIVE — written and tested on the controller; nothing has run on a host.

**Context:** weft is uhstray's agent harness. On the rack it is one front door (identity across
companies; it redirects and never proxies) and one VM per company workspace, plus a staging front
door and workspace. weft's CI builds every image once, signs it with keyless cosign, and publishes
a signed `release.json` naming each image by digest; a deploy verifies and runs exactly those
digests. The service directories are `platform/services/weft-door/deployment/` and
`platform/services/weft-workspace/deployment/`, whose READMEs are the operator guide.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Every weft template is launched by a person; none is scheduled** | No target an AI wrote is converged without a person launching it (the AI Invariant, `PRINCIPLES.md` §4). Semaphore's task history is the audit trail. |
| D2 | **Each rack host is pinned in site-config**: `weft_release: "<version>@sha256:<64 hex>"`, the version and the digest of its signed `release.json` | A host moves when a person changes its pin in a site-config pull request, syncs the inventory and launches a deploy. Waves are the hosts whose pins changed; a hold is a pin left alone; the previous release is in site-config's history. Staging takes a full commit or a pin in "Deploy weft Staging"'s survey. |
| D3 | **Signatures checked on the target, by a digest-pinned cosign container** (`tasks/verify-cosign.yml`); oras fetches `release.json` to stdout and the controller hashes it | Exact identity `images.yml@refs/heads/main`, exact issuer. The verifiers run from root's image store, which the orchestrator's socket cannot write. |
| D4 | **No automatic rollback.** A failed health check or watch fails the run, leaves the host as it is and prints its backup set | The release to go back to is a person's choice, written in site-config; going back restores data. "Roll Back weft Host" takes the host twice, the backup set and a reason. |
| D5 | **The seal has one origin**: `manage-secrets` generates `seal` (64 hex digits) at `secret/services/<weft\|weft-staging>/workspaces/<id>`, rendered as `seal.key` before the first start | weft keeps it out of every backup set; a workspace restored onto a new VM gets its connections back from OpenBao. |
| D6 | **The GitHub `staging` deployment is written only by "Deploy weft Staging"**, after both staging hosts pass their watch, with a GitHub App installation token minted per run (`tasks/weft-github-app-token.yml`) | weft's `release.yml` releases only a commit staging ran healthily. The App is installed on weft's repository alone, deployments write and metadata read; no long-lived token is stored. |
| D7 | **VM creation stays the platform's onboarding workflow** (Provision VM → Distribute SSH Keys → Harden SSH → Apply Firewall); "Stand Up weft Workspace" does the weft half | Each concern is its own workflow (Critical Deployment Rule 3). `vm_cpu` gives a workspace VM AVX2 (`x86-64-v3` or `host`). |
| D8 | **A workspace is reached at `<company>.<domain>`** (`weft_workspace_name`); `weft_workspace_id` stays the claim audience, the VM and the directory name | weft D540. A rename is two steps: the admin renames, then site-config follows; between them the new name answers 503. |
| D9 | **What decides what runs lives where no container mounts it**: a workspace's `weft.toml` in root's `/srv/weft-state/` | The board faces the internet and mounts the workspace directory read-write; the orchestrator can act as the deploy user (agent-cloud#890 B2). |
| D10 | **Every weft hostname is proxied by Cloudflare, and weft sends scrubbed platform-process logs to Loki and serves `/metrics`** (Jacob, 2026-11-10) | Every platform hostname is proxied (05); every service meets the observability minimum tier (06). Customer content stays on its VM. Liveness is a Grafana alert on the scrape target `up` (weft's step 38a). |
| D11 | **Backups**: weft's own verified backup set on the VM before every deploy; a Proxmox snapshot before a rack rollout with the existing "Snapshot VM" template; off-VM durability is PBS | PBS arrives with the `pbs-vm-and-restore-gate` change. |
| D12 | **weft's CI runs on ephemeral JIT runners** (`github_runner_lifecycle: ephemeral`) | No persistent weft CI VM. |

## Accepted costs

- A deploy interrupts agent sessions in progress; the next pass starts them again (weft §6.1).
- OpenBao holds every workspace's seal; a compromise of OpenBao opens every workspace's stored
  credentials. Only the controller's AppRole reads them, and no weft VM reaches OpenBao.
- A new workspace has no model to call until its admin signs in and enters its keys on Settings ›
  Credentials (weft D539): the keys are the company's and never pass through Semaphore or OpenBao.
- A failed deploy leaves its host down or degraded until a person launches the rollback.
- Until PBS lands, a backup set lives on the same VM as the database it copies.

## Remaining

The phases, gates and scenarios are in `openspec/changes/add-weft-service/tasks.md`. In short:

- [x] `vm_cpu` is honoured by `provision-vm.yml` and converged by `resize-vm.yml`.
- [ ] Create the weft GitHub App and seed `weft_app_key` and `weft_app_client_id`.
- [ ] Declare the deviations (OpenSpec tasks 2.1 to 2.5; `PRINCIPLES.md` and 06 need no change); the edge (proxied records, the client
      address from `CF-Connecting-IP`, the firewall); observability and the `up` alert; the first
      staging run; the first rollout, and Bench onto the rack.
