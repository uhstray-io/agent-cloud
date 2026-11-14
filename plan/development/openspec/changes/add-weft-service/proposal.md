Author: Jacob Haig <jacob.haig@uhstray.io>. Decisions by Jacob, 2026-10-01 (weft's D533 to D537), 2026-11-10 (every weft name proxied; scrubbed platform logs to Loki), 2026-11-11 (no Clean Deploy) and 2026-11-12 (Joe's pattern everywhere: person-launched deploys, per-host pins in site-config, one origin per secret, one directory per service).

## Why

weft is uhstray's agent harness, sold as a subscription. On the rack it is one front door, which holds identity across companies, and one VM per company workspace, plus a staging pair. Its deployment is written on `feat/weft-service` (PR #743) and recorded in `plan/development/25-weft-service.md`, but nothing in it has run on a host, and a new platform service goes through an OpenSpec change before it is built further (`plan/development/openspec/config.yaml`).

weft's CI builds every image once, signs it with keyless cosign, and publishes a signed `release.json` that names each image by digest. A weft host is pinned in site-config to a version and the digest of its `release.json`, the way skynet is pinned to an image digest, and a person moves it by changing the pin and launching a deploy. The review of #743 found two defects that block a first run (agent-cloud#890): the first deploy to a fresh host stopped a stack that had no `.env` and failed (B1), and files the internet-facing board can write decided which image ran (B2). An alignment review against `PRINCIPLES.md` then found that the first version deployed on timers without a person, polled GitHub for approvals, gave the seal two origins and put two services in one directory. This change records the service, its requirements and those fixes, and carries the remaining work to the first paying customer as gated phases.

## What Changes

- **Two new platform services**, `platform/services/weft-door/deployment/` and `platform/services/weft-workspace/deployment/`, each with one `compose.yml`, one lifecycle-only `deploy.sh`, its templates, `CLAUDE.md` and `README.md`; and `platform/services/weft-workspace/context/architecture.md`, the AI tier's context.
- **Per-host pins, deployed by a person.** Each rack host's `weft_release` in site-config is `<version>@sha256:<release.json digest>`. `tasks/weft-release.yml` refuses anything else, verifies the artifact's signature at that digest with `tasks/verify-cosign.yml` against the exact identity `images.yml@refs/heads/main`, fetches `release.json` by digest to stdout, and verifies and pulls every image it names. Templates: "Deploy weft Front Door", "Deploy weft Workspace", "Roll Out weft Release", "Deploy weft Staging", "Roll Back weft Host" and "Stand Up weft Workspace", none scheduled.
- **One deploy, the same for every host** (`tasks/weft-deploy.yml`): preflight, stop, the binary's own backup, start, health through the edge, a 15-minute watch. A failure fails the run and leaves the host as it is; going back is "Roll Back weft Host", to the host's reverted pin, from a backup set the person names.
- **weft.toml is root's, and outside every container mount** (B2). It lives in `/srv/weft-state/`, which root owns and no weft container mounts, and is mounted read-only into the containers that read it. `release.json` is never a file on the VM.
- **A fresh host deploys** (B1). `deploy.sh stop` treats a missing `.env` as nothing running.
- **The seal's one origin is OpenBao**: generated once by `tasks/manage-secrets.yml`, which gains `type: hex`, and rendered as `seal.key` before the first start.
- **Staging's GitHub record** (`staging` deployment, which weft's release.yml requires) is written by "Deploy weft Staging" after both staging hosts pass their watch, with a GitHub App installation token minted for the run.
- **Inventory shape.** The public inventory template gains `weft_svc` with the four weft groups as children: `weft_door`, `weft_workspaces`, `weft_staging_door` and `weft_staging_workspace`.
- **Edge, observability and hardening as later phases**, by Jacob's decisions of 2026-11-10: every weft hostname is proxied by Cloudflare like every platform hostname, and weft sends scrubbed platform-process logs to Loki and serves `/metrics`, while customer content stays on its VM.
- **Documentation.** `AGENTS.md` (the service-directory lines, OpenBao layout rows, composable tasks), the service catalog row in `plan/architecture/service-architecture.md`, the plan table in `plan/architecture/00-foundation-standards.md`, and the plan at `plan/development/25-weft-service.md`.

## Capabilities

### New Capabilities

- `platform/weft-service`: weft as a platform capability. It covers signed releases pinned per host and deployed by digest, the one deploy and a person's rollback, `weft.toml` kept outside every container mount, the seal's one origin, staging's GitHub record, the proxied edge, the firewall that keeps weft VMs off OpenBao, and the observability that carries no customer content.

### Modified Capabilities

None. The existing specs in this store do not cover weft.

## Impact

- **New files.** The two service directories above. Playbooks `deploy-weft-door.yml`, `deploy-weft-workspace.yml`, `deploy-weft-staging.yml`, `roll-out-weft-release.yml`, `rollback-weft-host.yml`, `stand-up-weft-workspace.yml`, the imported plays `weft-door.yml`, `weft-workspace.yml` and `weft-edge.yml`, and `vars/weft.yml`. Tasks `weft-*.yml`, and the shared task `verify-cosign.yml`. Tests `platform/tests/test_service_weft.bats`, `platform/tests/test_weft_deploy_runs.py` and `platform/tests/test_weft_door_public_key.py`.
- **Modified files.** `tasks/manage-secrets.yml` (`type: hex`), `validate-all.yml`, `validate-secrets.yml`, `platform/semaphore/templates.yml`, `platform/inventory/production.yml`, `AGENTS.md`, `plan/architecture/service-architecture.md`, `plan/architecture/00-foundation-standards.md`.
- **Secret store.** `secret/services/<root>/door` (`signing_key`, `public_key`) and `secret/services/<root>/workspaces/<id>` (`seal`), where `<root>` is `weft` or `weft-staging`, and `weft_app_key` and `weft_app_client_id` at `secret/services/github`. Read by Semaphore's orchestrator AppRole, whose policy already grants `secret/data/services/*`; weft adds no policy and no AppRole, and no weft VM reaches OpenBao. A workspace's model keys and forge token are in none of these: its admin enters them in weft and they stay sealed in its catalog (weft's D539).
- **Out of repository.** site-config declares the four weft groups (site-config#155) and each rack host's pin, and `sync-inventory.yml` pushes them into Semaphore. The weft repository publishes the signed images and `release.json`. A GitHub App for weft's deployments is created and installed on weft.
- **No Clean Deploy** (a declared deviation from `02-service-onboarding.md`, Phase 4; Jacob, 2026-11-11). The destructive path is offboarding (task 8.5, weft's step 44). Until it lands there is no supported path to retire a host's data, and the Rollback Plan's step 3 is the interim.
- **Other declared deviations and known gaps** (`design.md`): keyless cosign; no compose healthcheck on the orchestrator, capture or the front door until weft's images ship a probe (task 7.5); a workspace VM above the Large tier, to be confirmed with Joe; and, before the rack, `door.key` inside the clone (3.3), the shared package-read PAT (5.4) and the orchestrator's podman socket (5.5).
- **Not affected.** `update-service.yml`: a weft host moves only to a signed digest it is pinned to, never by a tag pull.

## Rollback Plan

Nothing has run on a host yet, so until the first staging deploy the rollback is a revert of the merges into `dev` and `retire-semaphore-templates.yml` for the weft templates.

Once hosts run weft:

1. **A bad release on one host** is the service's own mechanism, not a revert: revert the host's pin in site-config, sync the inventory, and launch "Roll Back weft Host" with the backup set the failed deploy printed. The release is verified again before it runs.
2. **Retire the service from a host.** Run `deploy.sh stop` in its service directory, then remove the Caddy fragment through `tasks/distribute-caddy-site.yml` and the DNS records through Apply Cloudflare Tofu with a zero-diff plan after the apply. The workspace directory, its backups and the OpenBao paths are kept: they hold a company's data and its seal, and deleting them is the offboarding decision D19, not a rollback.
3. **Revert the platform changes** (`type: hex`, `verify-cosign.yml`) only after no template references them. Each is additive: no existing secret declaration uses `type: hex`, and nothing else calls `verify-cosign.yml`.
