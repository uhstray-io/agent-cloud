# weft workspace — deployment

weft is uhstray's agent harness. On the rack it is **one front door** (identity across companies;
it redirects and never proxies; `platform/services/weft-door/deployment/`) and **one VM per company
workspace** (this directory: that company's whole installation, the board, the orchestrator,
capture, and the agent containers the orchestrator starts), plus a **staging** front door and
workspace. Every image is built once by weft's CI, signed, and deployed by digest. This README
also holds the site-config shape for both roles.

```text
browser ──> Caddy (edge: TLS, hostname routing) ─┬─ <domain>        ──> front door VM  :8081  weft-door
                                                 └─ <name>.<domain> ──> workspace VM   :8080  weft-board
browser ──(UDP, calls)──> rack firewall ──────────────────────────────> workspace VM   :<udp> weft-board (weft-voice)

workspace VM:  weft-board ─┐
               weft-orchestrator ─┼─ /srv/weft/<id>/ (weft.db, seal.key, models, checkouts)
               weft-capture ─┘     │
               weft-orchestrator ──┴─ podman socket ──> agent + egress-proxy containers (never the directory)
```

## How a version reaches a VM

Every step is a person's. No weft template has a schedule, and no target an AI wrote is converged
without a person launching it (the AI Invariant, `PRINCIPLES.md` §4).

```text
weft merge to main ─> images.yml: every image + release.json (weft-release:<commit>), signed
        │
person: "Deploy weft Staging" with a full commit or <commit|version>@sha256:<digest>
        ─> staging front door, then staging workspace; both pass their watch
        ─> GitHub deployment `staging` = success on that commit (weft's release.yml requires it)
        │
weft release.yml: a person names vX ─> weft-release:vX, signed
        │
person: site-config PR setting each host's weft_release to vX@sha256:<release.json digest>,
        then platform/semaphore/sync-inventory.yml
        │
person: "Roll Out weft Release" ─> front door, then each workspace in inventory order, one at a time
```

**Waves** are the hosts whose pins changed in a site-config pull request; a **hold** is a pin left
where it is; the previous release of every host is in site-config's history. A host already on its
pin is left alone, so re-launching a rollout continues where it stopped (`weft_force_redeploy:
true` on a launch deploys it again anyway).

Before a rack rollout, take a Proxmox snapshot of each VM it will change with the existing
**Snapshot VM** template (`snapshot-vm.yml`).

Before its deploy, a workspace's play makes its directories, enables the deploy user's podman
socket, renders `seal.key` from OpenBao and reads the front door's public key. Then each deploy
(`platform/playbooks/tasks/weft-deploy.yml`), for a workspace or a front door:

1. **Verify**: refuse a pin that is not a digest pin; cosign verifies `release.json`'s signature
   (`images.yml@refs/heads/main`) against the digest; oras fetches it to stdout and the controller
   hashes it; every image the role runs is verified and pulled by digest; check the disk. Nothing
   changes.
2. **Stop** (orchestrator, capture, board), then **back up** with the binary's own
   `backup --before-deploy`. A backup that does not verify starts what was running again and
   stops the run.
3. **Deploy**: render `.env`, the whisper model the release pins (checked against its signed sum)
   and `weft.toml`; start the board first
   (opening the catalog migrates it), then the orchestrator, then capture.
4. **Health** within 5 minutes: `orchestrator health` through the edge (TLS, route, board, schema,
   a tend pass finished after the deploy started); a front door runs `front-door health` and its
   page answers through the edge.
5. **Watch** 15 minutes: no container restarts, health still passes.

A failure in 4 or 5 **fails the run and leaves the host as it is**. Nothing rolls back by itself.
The run prints the backup set it took and how to go back:

1. revert the host's `weft_release` in site-config and sync the inventory;
2. launch **Roll Back weft Host**: the host, its name again, that backup set, a reason. It verifies
   the release the host is pinned to now like any deploy, stops the host, restores the set (the
   current database is set aside as its `failed-<release>` copy, never deleted), starts it and
   checks its health. If that fails too, everything is stopped and no file is deleted.

On staging, which has no pin in site-config, the rollback takes the commit or pin to go back to in
its survey.

Don't launch a deploy and a rollback of the same host at once; check Semaphore's running tasks first.

## Files

```text
deployment/
├── deploy.sh                container lifecycle only: pull, up, stop, images, backup, restore, health, restarts
├── compose.yml              board + orchestrator + capture, one image, the workspace directory
└── templates/
    ├── workspace.env.j2     .env: image digest, paths, ports (no secret)
    └── weft.toml.j2         the workspace's deployment file: agent images by digest, paths
                             (rendered into /srv/weft-state/, mounted read-only)
```

## Playbooks (Semaphore templates)

All are launched by a person. Each has a `(Dev)` copy until promotion.

| Template | Playbook | What it does |
|---|---|---|
| Deploy weft Front Door | `deploy-weft-door.yml` | The rack's front door to its pin (see the front door's README) |
| Deploy weft Workspace | `deploy-weft-workspace.yml` | The rack's workspaces to their pins, one at a time, stopping at the first failure |
| Roll Out weft Release | `roll-out-weft-release.yml` | Refuses any rack host not pinned to `<version>@sha256:<digest>`, then the front door, then each workspace one at a time |
| Deploy weft Staging | `deploy-weft-staging.yml` | Survey `weft_staging_release` (a full commit, or `<commit\|version>@sha256:<digest>`): staging front door, then staging workspace; both healthy, the GitHub `staging` deployment, with a GitHub App token minted for the run |
| Roll Back weft Host | `rollback-weft-host.yml` | One workspace or front door, named twice, back to its current pin from a named backup set, with a reason |
| Stand Up weft Workspace | `stand-up-weft-workspace.yml` | Rack only. A new workspace VM: routes its hostname and deploys it to its pin, which must equal the front door's |

## What site-config declares

Groups: `weft_door` (one host), `weft_workspaces` (in rollout order; uhstray's own workspace first,
as the canary), `weft_staging_door` (one host), `weft_staging_workspace` (one host), all four
children of `weft_svc` (the shape is in `platform/inventory/production.yml`, with
`weft_door_host`, `weft_workspace_host`, `weft_staging_door_host` and `weft_staging_workspace_host`
naming the hosts). An inventory change reaches Semaphore only through
`platform/semaphore/sync-inventory.yml`, run from a workstation. The VM specs (`vm_cpu: x86-64-v3`
or `host`: the workspace image needs AVX2) go in `proxmox/vm-specs.yml`.

The front door's own variables are in its README; the shared ones are here.

### Required

| Variable | Where | Form |
|---|---|---|
| `weft_environment` | each group | `rack` or `staging` |
| `weft_domain` | each group | the rack's weft domain, or staging's |
| `weft_secret_root` | each group | `weft` (rack) or `weft-staging` |
| `weft_release` | each rack host | `<version>@sha256:<64 hex>`: a version (`vX.Y.Z`) and the digest of its signed `weft-release` artifact. Staging has none: it takes the survey value |
| `weft_public_address` | workspace groups | the rack's public IP (an address, never a name): browsers send call audio there |
| `weft_workspace_id` | each workspace host | the id the front door minted, a DNS label: sign-in claims are bound to it; it names the VM's directory |
| `weft_workspace_name` | each workspace host | the company name its admin chose, a DNS label: the subdomain, `<name>.<domain>` (weft D540) |
| `weft_voice_udp` | each workspace host | its own UDP port; the rack forwards the same number |
| `weft_providers` | each workspace host | a dict of `[providers.*]` tables: `name`, `endpoint`, `wire`, `budget`, `models` (`economy`, `standard`, `frontier`). No key and no key variable |
| `weft_default_provider` | each workspace host | one of the keys of `weft_providers` |
| `weft_daily_tokens` | each workspace host | an integer; weft has no default |
| `weft_tracker_bot_login` | each workspace host | the forge login weft's tracker acts as; weft has no default |

### Optional, with their defaults

| Variable | Default | Form |
|---|---|---|
| `weft_clones` | `{}` | `{"<org>/<repo>": "<name>"}`, cloned under `<workspace dir>/repos/` |
| `weft_board_bind`, `weft_board_port` | `ansible_host`, `8080` | where the board is published; the Caddy host must reach it |
| `weft_board_trusted_proxies`, `weft_board_ingest_from` | `[]` | CIDR lists for `[board]` |
| `weft_board_client_address_header` | `CF-Connecting-IP` | `[board] client_address_header`, rendered only with a non-empty `weft_board_trusted_proxies`, and only when the pinned release's `release.json` lists `board.client_address_header`. From the weft release that carries weft #511 the board refuses `trusted_proxies` without it; a deploy refuses trusted proxies for an earlier release, before anything stops |
| `weft_board_columns` | `["Ready", "In progress", "In review", "Done"]` | the board's columns |
| `weft_whisper_model` | the release's default | a model the release pins |
| `weft_soft_percent`, `weft_cached_daily_tokens` | `80`, unset | `[spend]` |
| `weft_utc_offset_hours`, `weft_utc_offset_minutes` | `0`, `0` | `[day]` |
| `weft_lock_renew_minutes` | `30` | `[locks]` |
| `weft_reap_after_days` | `3` | `[checkouts]` |
| `weft_audio_original_days`, `weft_audio_compact_days` | `7`, `30` | `[audio]` |
| `weft_quiet_after_minutes`, `weft_max_span_hours` | `30`, `8` | `[sealing]` |
| `weft_tend_every_seconds`, `weft_max_turns_per_pass` | `300`, `10` | `[tend]` |
| `weft_backup_every_hours`, `weft_backup_keep` | `24`, `7` | `[backups]`, the scheduled copies on the VM |
| `weft_longest_turn_minutes`, `weft_round_cap` | `120`, `3` | `[builders]` |
| `weft_builder_memory`, `weft_builder_cpus`, `weft_builder_processes` | `4g`, `2`, `512` | `[builders.limits]` |
| `weft_orchestrator_command`, `weft_sessions_every_seconds`, `weft_sessions_idle_minutes` | `["claude-agent-acp"]`, `30`, `10` | `[sessions]` |
| `weft_health_minutes`, `weft_watch_minutes` | `5`, `15` | the deploy's health window and watch |
| `weft_root`, `weft_state_root` | `/srv/weft`, `/srv/weft-state` | the workspace directory's parent and the root-owned state directory; the state root may not be inside a mounted directory |
| `weft_registry`, `weft_github_repository`, `weft_signer_identity`, `weft_oras_image` | `platform/playbooks/vars/weft.yml` | where weft's images live and who must have signed them |

`weft_force_redeploy` is a launch-time extra var, not an inventory value.

## Renaming a workspace

Two steps, in this order:

1. The workspace's admin renames it on Settings › People. The front door answers the old name
   with a 307 to the new one at once, so links keep working.
2. Change `weft_workspace_name` in site-config, sync the inventory and run Deploy weft Front Door
   (it re-renders the edge; a front door already on its pin is otherwise left alone). Until then
   the new name answers 503 from the front door, because the edge still routes only the old one.

The id, the VM, its directory and `known_as` do not change.

## What OpenBao holds

| Path | Key | Made by |
|---|---|---|
| `secret/services/<root>/door` | `signing_key`, `public_key` | Deploy weft Front Door (generated once) |
| `secret/services/<root>/workspaces/<id>` | `seal` (64 hex digits, 32 bytes) | the workspace's first deploy, generated once; its only origin, never in a backup set |
| `secret/services/github` | `weft_app_key`, `weft_app_client_id` | an operator, through Seed OpenBao Key, after creating the GitHub App (installed on weft's repository only; deployments write, metadata read). Read by Deploy weft Staging |
| `secret/services/github` | `pat` (existing) | read by `registry-login.yml`; needs `read:packages` for weft's images |

`<root>` is `weft_secret_root`. A workspace's **model keys and forge token are in none of these**
(weft's D539): its admin enters them on Settings › Credentials after the first sign-in, and they
stay sealed in its catalog. The seal at its path is what lets a restore onto a new VM open them
again; a compromise of OpenBao therefore opens every workspace's stored credentials (an accepted
cost: only the controller's AppRole reads it, and no weft VM reaches OpenBao).

## Not built yet

- **Off-VM backups.** weft's own verified backup set on the VM before every deploy is the copy a
  rollback uses; off-VM durability is PBS, once the `pbs-vm-and-restore-gate` change lands.
- **A liveness alert**: a Grafana alert on weft's scrape target `up` (weft's step 38a).
- **The tour against staging** after a healthy deploy: it needs weft's browser tooling, which runs
  on weft's CI runner, not on Semaphore.
- **A no-op builder container on the internal network** as a health check; the agent images are
  verified and pulled, but not started.
- **A run on a host.** Every check here is structural or runs against a fake container engine;
  the first real run is staging.
