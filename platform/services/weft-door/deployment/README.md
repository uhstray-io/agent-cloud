# weft front door — deployment

The front door is weft's identity across companies: sign-in, workspace membership, and a URL per
workspace. It redirects and never proxies. Each environment has one, on its own VM: the rack's
(`weft_door`) and staging's (`weft_staging_door`).

The deploy's steps, the pins, the rollback and the variables every weft host shares are in the
workspace README: [`../../weft-workspace/deployment/README.md`](../../weft-workspace/deployment/README.md).
This file covers what is the front door's own.

## How it is deployed

Every step is launched by a person; no weft template has a schedule.

- **Deploy weft Front Door** (`deploy-weft-door.yml`): the rack's front door to its pin.
- **Roll Out weft Release** (`roll-out-weft-release.yml`): the front door first, then each workspace.
- **Deploy weft Staging** (`deploy-weft-staging.yml`): staging's front door, then staging's workspace.
- **Roll Back weft Host** (`rollback-weft-host.yml`): a front door is one of the hosts it takes.

Don't launch a deploy and a rollback of the same host at once; check Semaphore's running tasks first.

Each runs `weft-door.yml`:

1. place the repository; generate the signing key once into OpenBao and render it as `door.key`
   (0600); derive the public key on the controller and store it beside the signing key;
2. render weft's Caddy fragment for the environment from all of its workspaces and push it to
   central Caddy (`weft-edge.yml`, through `tasks/distribute-caddy-site.yml`);
3. one deploy (`tasks/weft-deploy.yml`): verify, stop, `backup --before-deploy` of
   `front-door.db`, render `.env`, start, `front-door health` and the page through the edge within
   5 minutes, a 15-minute watch. A front door already on its pin is left alone after its edge is
   re-rendered. A failure fails the run, leaves the host as it is and prints the backup set; no
   workspace is deployed after it.

## Files

```text
deployment/
├── deploy.sh                container lifecycle only: pull, up, stop, images, backup, restore, health, restarts
├── compose.yml              the front door; its key as a 0600 file, read-only
├── lib/door_public_key.py   the public key from the signing key, on stdin (controller only)
└── templates/
    ├── door.env.j2          .env: image digest, paths, the workspace address template (no secret)
    ├── door.key.j2          the signing key (0600)
    └── caddy-site.j2        <domain> -> front door, <name>.<domain> -> each workspace, anything else -> front door
```

## What site-config declares for it

The group-level values (`weft_environment`, `weft_domain`, `weft_secret_root`) and the front
door's `weft_release` pin are described in the workspace README. The front door's own:

| Variable | Where | Form | Default |
|---|---|---|---|
| `weft_door_host` | the inventory template | the front door's inventory host (`weft_staging_door_host` for staging) | none |
| `weft_door_bind` | the front door host | the address the Caddy host reaches it on | `ansible_host` |
| `weft_door_port` | the front door host | its published port | `8081` |
| `weft_door_dir` | optional | the records' directory on the VM's own disk | `/srv/weft-door` |
| `weft_caddy_tls` | optional | the `tls` block of the Caddy fragment | `dns cloudflare {$CLOUDFLARE_API_KEY}` |

The fragment also reads each workspace's `weft_workspace_name`, `weft_board_bind` and
`weft_board_port`, so a workspace's route changes when those do: run Deploy weft Front Door after
a rename (the workspace README, "Renaming a workspace") or Stand Up weft Workspace for a new one.

## What OpenBao holds for it

| Path | Key | Made by |
|---|---|---|
| `secret/services/<root>/door` | `signing_key` (64 hex digits) | the first front door deploy, generated once and reused forever |
| `secret/services/<root>/door` | `public_key` | every front door deploy, derived from `signing_key`; read by each workspace's deploy |

`<root>` is `weft` for the rack and `weft-staging` for staging; the two share nothing.
