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
├── compose.mailpit.yml      staging, and the rack until a customer workspace: Mailpit in the door's network namespace, and the door's mail settings
├── lib/door_public_key.py   the public key from the signing key, on stdin (controller only)
└── templates/
    ├── door.env.j2          .env: image digest, paths, the workspace address template, the client address header and its proxies (no secret)
    ├── door.key.j2          the signing key (0600)
    └── caddy-site.j2        <domain> -> front door, <name>.<domain> -> each workspace, anything else -> front door
```

## What site-config declares for it

The group-level values (`weft_environment`, `weft_domain`, `weft_secret_root`) and the front
door's `weft_release` pin are described in the workspace README. The front door's own:

| Variable | Where | Form | Default |
|---|---|---|---|
| `weft_door_host` | the inventory template | the front door's inventory host (`weft_staging_door_host` for staging) | none |
| `weft_door_bind` | the front door host | the IP address the Caddy host reaches it on (IPv4 or IPv6); a host name, a wildcard or a loopback address is refused before anything stops (`tasks/weft-bind.yml`), outside local mode | `ansible_host` |
| `weft_door_port` | the front door host | its published port | `8081` |
| `weft_door_dir` | optional | the records' directory on the VM's own disk | `/srv/weft-door` |
| `weft_door_trusted_proxies` | the front door host, required | the edge Caddy host's address, as a list of CIDR blocks, the only peers the client address header is believed from. The Caddy host alone, as a `/32`: the door is on pasta (compose.yml), and anything on this VM reaches it as the VM's default gateway (podman 4.9.3) or `169.254.1.2` (podman 5.3 on), so a range holding either would believe the header from every process on the VM. An entry wider than one address, or holding either, is refused before anything stops (`tasks/weft-bind.yml`), outside local mode | none: a deploy refuses before anything stops |
| `weft_door_client_address_header` | optional | the header the edge overwrites with the client's address; every weft name is proxied by Cloudflare | `CF-Connecting-IP` |
| `weft_caddy_tls` | optional | the `tls` block of the Caddy fragment | `dns cloudflare {$CLOUDFLARE_API_KEY}` |
| `weft_door_mailpit` | staging's front door, or the rack's while no workspace is a customer's | `true` runs Mailpit and sends the door's mail to it; refused outside staging and the rack, and on the rack once any workspace is not marked `weft_workspace_owner: operator` | `false` |
| `weft_door_mailpit_ui_port` | optional | the VM loopback port Mailpit's UI is published on | `8025` |
| `weft_door_mail_from` | optional | the address the door's emails are from | `no-reply@<weft_domain>` |

The fragment also reads each workspace's `weft_workspace_name`, `weft_board_bind` and
`weft_board_port`, so a workspace's route changes when those do: run Deploy weft Front Door after
a rename (the workspace README, "Renaming a workspace") or Stand Up weft Workspace for a new one.

## The mail catcher

From weft #541 on, the front door refuses to start without mail. Until the platform has a
transactional provider, a front door may set `weft_door_mailpit: true` and hand every message to
[Mailpit](https://mailpit.axllent.org/), which keeps it and sends nothing: no relay or forward is
configured, so no address typed into its sign-up form receives anything. Staging may always. The
rack's front door may only while the operator's own people are its only users (Jacob, 2026-11-17,
weft #332).

- **Where it runs.** In the door's network namespace (`compose.mailpit.yml`), because the door
  speaks plain SMTP only to its own loopback. `deploy.sh` loads the overlay only when `.env` names
  the image, and stops and recreates the two together: podman will not remove a container another
  shares the namespace of. The messages do not survive a deploy.
- **On the rack, refused once site-config declares a customer workspace.** `tasks/weft-door-mail.yml`
  refuses `weft_door_mailpit`, in a deploy and in a rollback, before anything stops: on a host
  whose `weft_environment` is neither `staging` nor `rack`, and on the rack's door once any host in
  `weft_workspaces` is not marked `weft_workspace_owner: operator`. An unmarked workspace counts as
  a customer's. It also refuses to deploy or roll back a customer's workspace (any host in
  `weft_workspaces`, whatever its `weft_environment`) while the rack's door sets
  `weft_door_mailpit` in site-config. Every check reads site-config, not the running hosts: a door
  keeps Mailpit until it is redeployed without it, a workspace dropped from inventory but still
  running is not seen, and weft's front door cannot close sign-up (weft #608), so anyone who reaches
  it can sign up and their link lands in the catcher. So the rack's door gets a real provider and
  is deployed with it before the first customer's Stand Up, and is not offered to customers before
  then (OpenSpec task 6.3 and `design.md`'s known gaps).
- **The image** is `_weft_mailpit_image` in `playbooks/vars/weft.yml`, by digest; site-config may
  override it with `weft_mailpit_image`, which must also be a digest.
- **Reading it.** The UI has no sign-in and shows working sign-up and reset links, so it is
  published on the door VM's loopback only, on staging and on the rack alike. Reach it with an SSH
  tunnel through the door VM, then open `http://localhost:8025/`:

  ```sh
  ssh -N -L 8025:127.0.0.1:8025 <deploy user>@<front door VM>
  ```

## What OpenBao holds for it

| Path | Key | Made by |
|---|---|---|
| `secret/services/<root>/door` | `signing_key` (64 hex digits) | the first front door deploy, generated once and reused forever |
| `secret/services/<root>/door` | `public_key` | every front door deploy, derived from `signing_key`; read by each workspace's deploy |

`<root>` is `weft` for the rack and `weft-staging` for staging; the two share nothing.
