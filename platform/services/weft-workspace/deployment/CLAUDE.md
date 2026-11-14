# CLAUDE.md — platform/services/weft-workspace/deployment

A weft **workspace**: one company's whole installation on its own VM. weft is uhstray's agent
harness; on the rack it is one front door (`../../weft-door/deployment/`) and one workspace VM per
company, plus a staging front door and a staging workspace. A weft deploy takes a **version as
input**: it verifies weft's signed `release.json`, verifies every image it names with cosign, and
runs them by digest. weft's own plan for this is `docs/cicd.md` in the weft repository.

Every weft deploy is launched by a person. Nothing is scheduled, and nothing rolls back by itself.

## What runs where

| Containers (one image, `weft-workspace`, by digest) | On the VM's own disk |
|---|---|
| `weft-board`, `weft-orchestrator`, `weft-capture` | `/srv/weft/<id>/`: `weft.db`, `seal.key`, models, checkouts, clones, backup sets |

`weft.toml` is in `/srv/weft-state/workspaces/<id>/`: root's, written with `become`, mounted by no
container except as that one file, read-only, into the board and the orchestrator. It is the only
thing in `/srv/weft-state/`. Its first line names the `release.json` digest it was rendered for,
which is how a deploy recognises a host already on its pin; it chooses nothing.

`.env` (beside `compose.yml`) names the image digest and the paths. Its `WEFT_RELEASE=<name>` only
labels the next backup set.

The orchestrator starts agent and egress-proxy containers (`weft-builder`, `weft-proxy`,
`weft-harness-*`) through the deploy user's rootless podman socket; the deploy verifies and pulls
those images too, so a session is never the first thing to find one missing.

## Rules that are load-bearing

- **A rack host runs what site-config pins it to.** `weft_release: "<version>@sha256:<64 hex>"`,
  the version and the digest of its signed `release.json` artifact, written by a person in a
  site-config pull request. `tasks/weft-release.yml` refuses anything else; staging alone also
  takes a full 40-hex commit, resolved to a digest and then verified like a pin.
- **Images by digest, signed by `images.yml@refs/heads/main`.** cosign verifies `release.json`'s
  signature against the pinned digest; oras fetches the manifest and the file to stdout, never to
  disk, and the controller hashes both against the digests asked for. The verifiers run from
  root's image store, never the deploy user's, which the orchestrator can write through its
  socket. `deploy.sh` refuses a reference that is not a digest.
- **A failed deploy fails the run and leaves the host as it is.** Health within 5 minutes, then a
  15-minute watch (no container restarts, health still passes). A failure in either prints the
  backup set the deploy took and stops; a rollout goes no further. Going back is a person's:
  revert the host's pin in site-config, sync the inventory, launch Roll Back weft Host with that
  set.
- **Only the three workspace containers mount the workspace directory** (weft's D537), and only
  the orchestrator gets the podman socket. The board faces the internet; with the socket a
  compromised board could start a container with any mount.
- **The orchestrator's podman socket follows the GitHub runner's pattern.** The runner starts
  job containers through the rootless podman API socket of one unprivileged account
  (`../../github-runner/deployment/templates/runner-dotenv.j2`,
  `../../../playbooks/tasks/podman-user-docker-socket.yml`): whoever holds that socket can do
  what the account can do, and no more. The orchestrator starts agent and egress-proxy
  containers the same way, through `WEFT_PODMAN_SOCKET` in `compose.yml`, so its container is
  not a sandbox. The difference is the account: until task 5.5 the socket is the deploy user's
  (a known gap in the OpenSpec change's design.md); task 5.5 gives it a separate unprivileged
  user that runs only agent and egress-proxy containers, the runner's shape. The board and
  capture never get it (the rule above).
- **A company's content is never logged**, as agentgateway never logs prompt and completion
  content (`../../agentgateway/context/agent-practices.md`, "Secrets boundary"). weft's
  containers write process logs (lifecycle, deploys, errors) to stdout, and those reach Loki like
  every service's; transcripts, prompts, issues and recordings stay in the workspace's catalog on
  its VM and are never a log line, a metric label or a span attribute.
- **The catalog opens only on the VM's own disk.** No named volume, no network share: weft refuses
  both, because SQLite's WAL index is coherent only within one kernel.
- **`deploy.sh` is container lifecycle only, for this one role.** It never reads OpenBao, never
  generates a secret and never chooses a version: the image it runs is the one `.env` names. Its
  `backup`, `restore` and `health` run the release's own binary in a one-off container.
- **What decides what runs is outside every container mount.** The board mounts the workspace
  directory read-write, and the orchestrator can act as the deploy user. So `weft.toml` is root's
  in `/srv/weft-state/`, and `tasks/weft-release.yml` refuses a state directory inside a mounted
  one.
- **The seal has one origin.** `manage-secrets` generates `seal` (`type: hex`, 64 hex digits, 32
  bytes) once at `secret/services/<weft|weft-staging>/workspaces/<id>`, and the play renders it as
  `seal.key` (32 raw bytes, 0600) before the first start, so weft never mints a seal OpenBao does
  not hold. weft keeps it out of every backup set; a workspace restored onto a new VM gets it from
  OpenBao.
- **A restore never deletes.** `restore --label failed-<release>` sets the current database aside;
  what people wrote while the failed release ran is kept there.
- **A workspace's model keys and forge token never touch this platform** (weft's D539). Its admin
  enters them on Settings › Credentials after the first sign-in; they are sealed in its catalog.
  Nothing here renders, stores or names one: no env file, no `key_from_env`, no OpenBao path.
- **The front door is deployed first.** Its deploy writes its public key to OpenBao, and every
  workspace's `weft.toml` names that key.

## What not to do

- Don't deploy by tag, pin a tag or a short commit in site-config, or add `:latest` anywhere:
  digests come from the pinned `release.json` only.
- Don't schedule a weft template or make a deploy roll itself back: a host changes only when a
  person launches it (the AI Invariant, `PRINCIPLES.md` §4).
- Don't mount the workspace directory or the podman socket into another container, and don't
  mount `/srv/weft-state/` into any.
- Don't put a credential in `weft.toml.j2`, or a variable name for one, or a workspace's key in
  OpenBao: they belong to the company and live in its catalog.
- Don't let the deploy user write `/srv/weft-state/`: the orchestrator can act as that user.
- Don't edit `.env`, `seal.key` or `weft.toml` on a VM; they are re-rendered on every deploy.

## Related

- Operator guide and every site-config variable: [`README.md`](README.md)
- The front door: [`../../weft-door/deployment/CLAUDE.md`](../../weft-door/deployment/CLAUDE.md)
- Context (AI tier): [`../context/architecture.md`](../context/architecture.md): the surfaces a
  request reaches, `/mcp` among them, and where weft's own architecture lives
- Playbooks (in [`../../../playbooks/`](../../../playbooks/)): `deploy-weft-workspace.yml`,
  `roll-out-weft-release.yml`, `deploy-weft-staging.yml`, `rollback-weft-host.yml`,
  `stand-up-weft-workspace.yml`. Each launcher imports `weft-workspace.yml`, which runs
  `tasks/weft-deploy.yml` per host
- weft's tasks: `tasks/weft-*.yml`; shared: `tasks/verify-cosign.yml`, `tasks/manage-secrets.yml`
- Defaults every weft play shares: [`../../../playbooks/vars/weft.yml`](../../../playbooks/vars/weft.yml)
- Tests: `platform/tests/test_service_weft.bats`, `platform/tests/test_weft_deploy_runs.py`
- Plan: `plan/development/25-weft-service.md`; OpenSpec change `add-weft-service`
- Root conventions: [`../../../../CLAUDE.md`](../../../../CLAUDE.md)
