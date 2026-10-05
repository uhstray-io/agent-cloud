# weft — deployment

weft is uhstray's agent harness. On the rack it is **one front door** (identity across companies;
it redirects and never proxies) and **one VM per company workspace** (that company's whole
installation: board, orchestrator, capture, and the agent containers the orchestrator starts),
plus a **staging** front door and workspace that follow weft's `main`. Every image is built once by
weft's CI, signed, and deployed by digest; a release promotes the digests staging ran.

```text
browser ──> Caddy (edge: TLS, hostname routing) ─┬─ <domain>        ──> front door VM  :8081  weft-door
                                                 └─ <id>.<domain>   ──> workspace VM   :8080  weft-board
browser ──(UDP, calls)──> rack firewall ──────────────────────────────> workspace VM   :<udp> weft-board (weft-voice)

workspace VM:  weft-board ─┐
               weft-orchestrator ─┼─ /srv/weft/<id>/ (weft.db, seal.key, weft.toml, models, checkouts)
               weft-capture ─┘     │
               weft-orchestrator ──┴─ podman socket ──> agent + egress-proxy containers (never the directory)
```

## How a version reaches a VM

```text
weft merge to main ─> images.yml: every image + release.json, signed (keyless cosign)
                                     │
Semaphore "Follow weft main" (every 5 min) ──> staging front door, then staging workspace
   newest main commit whose images.yml succeeded; GitHub deployment `staging` = success|failure
                                     │
weft release.yml: Jacob names vX, approves environment `rack` ─> release published
                                     │
Semaphore "Roll Out weft Release" (every 5 min) ──> checks the approval was weft_release_approver's
   front door, then each workspace in inventory order, one at a time, stop at the first failure
```

Each deploy (`platform/playbooks/tasks/weft-deploy.yml`), for a workspace or a front door:

1. **Preflight**: fetch `release.json` by digest, verify its signature (`images.yml@refs/heads/main`),
   verify and pull every image it names, check the disk, read what the host runs. Nothing changes.
2. **Stop** (orchestrator, capture, board), **back up** with the binary's own
   `backup --before-deploy`. A backup that does not verify starts the old version again and stops.
3. **Deploy**: render `.env` (and `weft.toml`, the whisper model) for the new digests; start the
   board first (opening the catalog migrates it), then the orchestrator, then capture.
4. **Health** within 5 minutes: `orchestrator health` through the edge (TLS, route, board, schema,
   a finished tend pass), the calls port bound; `front-door health` and the page through the edge.
5. **Watch** 15 minutes: no container restarts, health still passes.
6. **Passed**: record the release in `deployed.json` on the VM; Discord.

A failure in 4 or 5 rolls back (`tasks/weft-rollback.yml`): stop, restore the deploy's own backup
with the previous image (the current database is set aside as `weft.db.failed-<version>-<time>`,
never deleted), start the previous digests, health again, Discord. If that fails too, everything is
stopped, nothing is deleted, and Discord asks a person. The failed release is recorded as refused
on that host, so no timer tries it again; the fix is a new release.

## Files

```text
deployment/
├── deploy.sh               container lifecycle only: pull, up, stop, backup, restore, health, restarts
├── compose.workspace.yml   board + orchestrator + capture, one image, the workspace directory
├── compose.door.yml        the front door; its key as a 0600 file
├── lib/door_public_key.py  the front door's public key from its signing key (controller only)
└── templates/
    ├── workspace.env.j2    .env for a workspace: image digest, paths, ports (no secret)
    ├── door.env.j2         .env for a front door (no secret)
    ├── door.key.j2         the front door's signing key (0600)
    ├── weft.toml.j2        the workspace's deployment file: agent images by digest, paths
    └── caddy-site.j2       <domain> -> front door, <id>.<domain> -> each workspace
```

## Playbooks (Semaphore templates)

| Template | Playbook | What it does |
|---|---|---|
| Seed weft Secrets | `seed-weft-secrets.yml` | Makes the bot-owned "weft deploys" webhook in the declared channel, stores its URL; lists, by name, what is still to seed |
| Deploy weft Front Door | `deploy-weft-door.yml` | Signing key (generated once) and its public half; the Caddy fragment; one deploy |
| Deploy weft Workspace | `deploy-weft-workspace.yml` | Directory, podman socket, the front door's public key; one deploy per workspace, one at a time |
| Stand Up weft Workspace | `stand-up-weft-workspace.yml` | A new workspace VM: routes its hostname, deploys the release its front door runs; its admin then enters the model keys and forge token on Settings › Credentials |
| Follow weft main | `follow-weft-main.yml` | Timer: staging follows `main`; records the GitHub deployment `staging` |
| Roll Out weft Release | `roll-out-weft-release.yml` | Timer: an approved release, front door first, then each workspace |
| Roll Back weft Workspace | `rollback-weft-workspace.yml` | A person's rollback to a named release from a named backup set |

## What site-config declares

Groups: `weft_door` (one host), `weft_workspaces` (in rollout order; uhstray's own workspace first,
as the canary), `weft_staging_door` (one host), `weft_staging_workspace` (one host). The VM specs
(`vm_cpu: x86-64-v3` or `host`: the workspace image needs AVX2) go in `proxmox/vm-specs.yml`.

| Variable | Where | Example shape |
|---|---|---|
| `weft_environment` | each group | `rack` or `staging` |
| `weft_domain` | each group | the rack's weft domain, or staging's |
| `weft_secret_root` | each group | `weft` (rack) or `weft-staging` |
| `weft_public_address` | workspace groups | the rack's public IP, browsers send call audio there |
| `weft_release_approver` | `all` | the GitHub login whose `rack` approval counts |
| `weft_discord_guild_id`, `weft_discord_channel_id` | `weft_door` | the weft-dev channel's numeric ids |
| `weft_workspace_id` | each workspace host | the id the front door minted (a DNS label) |
| `weft_voice_udp` | each workspace host | its own UDP port; the rack forwards the same number |
| `weft_providers`, `weft_default_provider` | each workspace host | the `[providers.*]` tables (name, endpoint, wire, budget, models), as a dict; no key and no key variable |
| `weft_daily_tokens`, `weft_tracker_bot_login` | each workspace host | weft has no default for either |
| `weft_clones` | each workspace host | `{"<org>/<repo>": "<name>"}`, cloned under the workspace directory |
| `weft_board_bind` / `weft_door_bind` | optional | defaults to `ansible_host`; the Caddy host must reach it |
| `weft_board_trusted_proxies`, `weft_board_ingest_from` | optional | CIDR lists for `[board]` |

## What OpenBao holds

| Path | Key | Made by |
|---|---|---|
| `secret/services/weft/discord` | `webhook_url` | Seed weft Secrets (the bot makes the webhook) |
| `secret/services/weft/github` | `token` | an operator, through Seed OpenBao Key: fine-grained, weft repository, contents and actions read, deployments write |
| `secret/services/<root>/door` | `signing_key`, `public_key` | Deploy weft Front Door (generated once) |
| `secret/services/<root>/workspaces/<id>` | `seal_key_b64` | Deploy weft Workspace, after the first start |
| `secret/services/github` | `pat` (existing) | read by `registry-login.yml`; needs `read:packages` for weft's images |
| `secret/services/discord` | `bot_token` (existing) | read by Seed weft Secrets |

A workspace's **model keys and forge token are in none of these** (weft's D539): its admin enters
them on Settings › Credentials after the first sign-in, and they stay sealed in its catalog. The
seal's copy above is what lets a restore onto a new VM open them again.

## Not built yet

- **A Proxmox snapshot before each deploy** (weft's §6.1). `snapshot-vm.yml` is a playbook for one
  VM on the controller, not a task a per-host deploy can include; promoting it to a task is the
  follow-up. The catalog backup is taken either way.
- **The tour against staging** after a healthy deploy: it needs weft's browser tooling, which runs
  on the weft CI runner, not on Semaphore.
- **A no-op builder container on the internal network** as a health check; the agent images are
  verified and pulled, but not started.
- **A run on the local control plane.** Every check here is structural; the first real run is
  staging.
