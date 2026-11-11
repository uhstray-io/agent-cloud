Author: Jacob Haig <jacob.haig@uhstray.io>. Decisions by Jacob, 2026-10-01 (weft's D533 to D537) and 2026-11-10 (every weft name proxied; scrubbed platform logs to Loki).

## Why

weft is uhstray's agent harness, sold as a subscription. On the rack it is one front door, which holds identity across companies, and one VM per company workspace, plus a staging pair that follows weft's `main`. Its deployment is written on `feat/weft-service` (PR #743) and recorded in `plan/development/23-weft-service.md`, but nothing in it has run on a host, and a new platform service goes through an OpenSpec change before it is built further (`plan/development/openspec/config.yaml`).

weft is the first service here whose deploy takes a **version as input**. weft's CI builds every image once, signs it with keyless cosign, and publishes a signed `release.json` that names each image by digest. A deploy verifies and runs exactly those digests, backs up first, and rolls itself back when its health check or its watch fails. The review of #743 found two defects that block a first run (agent-cloud#890): the first deploy to a fresh host stops a stack that has no `.env` and fails (B1), and the rollback decides which image runs from files the internet-facing board can write (B2). This change records the service, its requirements and those fixes, and carries the remaining work to the first paying customer as gated phases.

## What Changes

- **New platform service `weft`** under `platform/services/weft/`: `deploy.sh` (container lifecycle only), `compose.door.yml`, `compose.workspace.yml`, templates for each role's `.env`, the front door's key file, `weft.toml` and the Caddy fragment, and `CLAUDE.md` and `README.md`.
- **Signed releases, deployed by digest.** `tasks/weft-release.yml` fetches `release.json` by digest, verifies it and every image it names with `tasks/verify-cosign.yml` against the exact identity `images.yml@refs/heads/main`, and pulls the digests. `deploy.sh` refuses a reference that is not a digest.
- **One deploy, the same for every host** (`tasks/weft-deploy.yml`): preflight, stop, the binary's own backup, start, health through the edge, a 15-minute watch, and an automatic rollback with that deploy's own backup when health or the watch fails (`tasks/weft-rollback.yml`).
- **The deploy's own record lives outside every container mount** (B2). `deployed.json`, `release.replaced.json`, `release-incoming/` and the deploy lock move to `/srv/weft-state/`, which no weft container mounts, and a rollback re-verifies the previous release's images before it renders them.
- **A fresh host deploys** (B1). `deploy.sh stop` treats a missing `.env` as nothing running, so the stop the deploy runs before it renders `.env` no longer fails on a new VM.
- **Timers.** "Follow weft main (Dev)" is a directly Dev-bound scheduled template, the pattern `templates.yml` uses for pre-promotion schedules, and places the branch it runs from. "Roll Out weft Release" carries no schedule until promotion: the rack's timer is main-bound and is added with the rack rollout phase.
- **Inventory shape.** The public inventory template gains `weft_svc` with the four weft groups as children: `weft_door`, `weft_workspaces`, `weft_staging_door` and `weft_staging_workspace`.
- **Edge, observability and hardening as later phases**, by Jacob's decisions of 2026-11-10: every weft hostname is proxied by Cloudflare like every platform hostname, and weft sends scrubbed platform-process logs to Loki and serves `/metrics`, while customer content stays on its VM.
- **Documentation.** `AGENTS.md` (sub-directory line, OpenBao layout rows, composable tasks, workflow rows), the service catalog row in `plan/architecture/service-architecture.md`, and the plan renamed from `16-weft-service.md` to `23-weft-service.md`, the next number no file on `dev` or an open pull request holds.

## Capabilities

### New Capabilities

- `platform/weft-service`: weft as a platform capability. It covers signed releases deployed by digest, the one deploy and its automatic rollback, the deploy record kept outside every container mount, the staging and rack timers, the proxied edge, the firewall that keeps weft VMs off OpenBao, and the observability that carries no customer content.

### Modified Capabilities

None. The existing specs in this store are `platform/inference-edge` and `platform/n8n-automation`. Neither one covers weft.

## Impact

- **New files.** `platform/services/weft/deployment/` as above. Playbooks `deploy-weft-door.yml`, `deploy-weft-workspace.yml`, `weft-door.yml`, `weft-workspace.yml`, `follow-weft-main.yml`, `roll-out-weft-release.yml`, `rollback-weft-workspace.yml`, `stand-up-weft-workspace.yml`, `seed-weft-secrets.yml`, and `vars/weft.yml`. Tasks `weft-*.yml`, and the shared tasks `verify-cosign.yml`, `notify-discord.yml` and `reconcile-discord-webhook.yml`. Tests `platform/tests/test_service_weft.bats` and `platform/tests/test_weft_door_public_key.py`.
- **Modified files.** `platform/lib/common.sh` (`COMPOSE_BASE`, a named compose base per stack), `tasks/manage-secrets.yml` (`type: hex`), `validate-all.yml`, `validate-secrets.yml`, `platform/semaphore/templates.yml`, `platform/inventory/production.yml`, `AGENTS.md`, `plan/architecture/service-architecture.md`.
- **Secret store.** `secret/services/weft/discord` (`webhook_url`), `secret/services/weft/github` (`token`), `secret/services/<root>/door` (`signing_key`, `public_key`) and `secret/services/<root>/workspaces/<id>` (`seal_key_b64`), where `<root>` is `weft` or `weft-staging`. Read by Semaphore's orchestrator AppRole, whose policy already grants `secret/data/services/*`; weft adds no policy and no AppRole, and no weft VM reaches OpenBao. A workspace's model keys and forge token are in none of these: its admin enters them in weft and they stay sealed in its catalog (weft's D539).
- **Out of repository.** site-config declares the four weft groups (site-config#155, named `weft_staging_door` and `weft_staging_workspace`), and `sync-inventory.yml` pushes them into Semaphore. The weft repository publishes the signed images and `release.json`.
- **Not affected.** `update-service.yml` skips weft, which has no `compose.yml`: a weft host moves only by a signed digest, never a tag pull.

## Rollback Plan

Nothing has run on a host yet, so until the first staging deploy the rollback is a revert of the merge into `dev` and `retire-semaphore-templates.yml` for the weft templates.

Once hosts run weft:

1. **Stop the timers first.** Retire "Follow weft main (Dev)" and, after promotion, the rack's timer through `retired-templates.yml` and `retire-semaphore-templates.yml`, so no tick starts a deploy during the rollback.
2. **A bad release on one host** is the service's own mechanism, not a revert: the deploy restores its own backup with the previous image inside its watch window, and after the window a person runs "Roll Back weft Workspace" with a named backup set. The previous digests come from each host's record in `/srv/weft-state/`, and are verified again before they run.
3. **Retire the service from a host.** Run `deploy.sh <role> stop`, then remove the Caddy fragment through `tasks/distribute-caddy-site.yml` and the DNS records through Apply Cloudflare Tofu with a zero-diff plan after the apply. The workspace directory, its backups and the OpenBao paths are kept: they hold a company's data and the one copy of its seal outside the VM, and deleting them is the offboarding decision D19, not a rollback.
4. **Revert the platform changes** (`COMPOSE_BASE`, `type: hex`, the shared tasks) only after no template references them. Each is additive: with `COMPOSE_BASE` unset `compose()` is byte-identical, and no existing secret declaration uses `type: hex`.
