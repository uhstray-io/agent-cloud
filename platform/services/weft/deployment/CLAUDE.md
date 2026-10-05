# CLAUDE.md — platform/services/weft/deployment

weft — uhstray's agent harness, deployed as one **front door** VM and one VM **per company
workspace**, plus a staging pair that follows weft's `main`. Unlike every other service here, a weft
deploy takes a **version as input**: it reads weft's signed `release.json`, verifies every image
with cosign, runs them by digest, backs up before it changes anything, and rolls itself back when
its health check or its watch fails. weft's own plan for this is `docs/cicd.md` in the weft
repository; this directory is its slice 11.

## What runs where

| VM | Containers (one image each, by digest) | Its directory on the VM's own disk |
|---|---|---|
| front door | `weft-door` (`weft-door` image) | `/srv/weft-door/` (`front-door.db`) |
| workspace `<id>` | `weft-board`, `weft-orchestrator`, `weft-capture` (all `weft-workspace`) | `/srv/weft/<id>/` (`weft.db`, `seal.key`, `weft.toml`, models, checkouts, backups) |

The orchestrator starts agent and egress-proxy containers (`weft-builder`, `weft-proxy`,
`weft-harness-*`) through the deploy user's rootless podman socket; the deploy pulls and verifies
those images too.

## Rules that are load-bearing

- **Images by digest, signed by `images.yml@refs/heads/main`.** `tasks/weft-release.yml` fetches
  `release.json` (an OCI artifact, `weft-release:<commit|version>`), verifies it, then verifies and
  pulls every image it names. `deploy.sh` refuses a reference that is not a digest. A branch-built
  image carries its branch's identity and is refused.
- **Only the three workspace containers mount the workspace directory** (weft's D537), and only the
  orchestrator gets the podman socket. The board faces the internet; with the socket a compromised
  board could start a container with any mount. `test_service_weft.bats` holds both.
- **The catalog opens only on the VM's own disk.** No named volume, no network share: weft refuses
  both, because SQLite's WAL index is coherent only within one kernel.
- **`deploy.sh` is container lifecycle only.** It never reads OpenBao and never chooses a version:
  the image it runs is the one `.env` names, so a rollback is a re-rendered `.env` and the same
  commands. Its `backup`, `restore` and `health` run the release's own binary in a one-off
  container.
- **A restore never deletes.** `restore --label failed-<version>` sets the current database aside;
  what people wrote while a failed version ran is kept there.
- **Automatic rollback only inside a deploy's watch window**, with the copy that deploy took
  (weft's D535). Every other rollback is a person's, through `rollback-weft-workspace.yml`, which
  makes them name the host twice and the backup set.
- **The front door's signing key is a 0600 file**, generated once by `manage-secrets` (`type: hex`)
  and never an environment variable. Its public half is derived on the controller
  (`lib/door_public_key.py`) and stored beside it for every workspace's `weft.toml`.
- **The seal has one other copy, in OpenBao** (`seal_key_b64` at the workspace's path), never in a
  backup set. `tasks/weft-seal.yml` refuses to start a workspace whose VM and OpenBao hold
  different seals.
- **A workspace's model keys and forge token never touch this platform** (weft's D539). Its admin
  enters them on Settings › Credentials after the first sign-in; they are sealed in its catalog.
  Nothing here renders, stores or names one: no env file, no `key_from_env`, no OpenBao path.

## What not to do

- Don't deploy by tag, pin a tag in inventory, or add `:latest` anywhere: digests come from
  `release.json` only.
- Don't mount the workspace directory or the podman socket into another container.
- Don't put a credential in `weft.toml.j2`, or a variable name for one, or a key in OpenBao for
  a workspace: they belong to the company and live in its catalog.
- Don't make a restore automatic outside `tasks/weft-deploy.yml`'s rescue.
- Don't edit `.env`, `door.key` or `weft.toml` on a VM; they are re-rendered on
  every deploy.

## Related

- Playbooks: `deploy-weft-door.yml`, `deploy-weft-workspace.yml`, `rollback-weft-workspace.yml`,
  `follow-weft-main.yml`, `roll-out-weft-release.yml`, `stand-up-weft-workspace.yml`,
  `seed-weft-secrets.yml` (all in [`../../../playbooks/`](../../../playbooks/)). The two
  `deploy-weft-*` launchers refuse an empty target group, then import `weft-door.yml` /
  `weft-workspace.yml`; the timers and Stand Up import those plays directly, with a group of
  exactly the hosts they decided to deploy, which is empty when there is nothing to do.
- weft's tasks: `tasks/weft-*.yml`; shared tasks it added: `tasks/verify-cosign.yml`,
  `tasks/notify-discord.yml`, `tasks/reconcile-discord-webhook.yml`
- Defaults every weft play shares: [`../../../playbooks/vars/weft.yml`](../../../playbooks/vars/weft.yml)
- Tests: [`../../../tests/test_service_weft.bats`](../../../tests/test_service_weft.bats),
  [`../../../tests/test_weft_door_public_key.py`](../../../tests/test_weft_door_public_key.py)
- Root conventions: [`../../../../CLAUDE.md`](../../../../CLAUDE.md)
