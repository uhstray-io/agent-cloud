# CLAUDE.md — platform/services/weft-door/deployment

weft's **front door**: identity across companies, workspace membership, and a URL per workspace.
It redirects and never proxies: a browser signs in here, receives a signed one-minute assertion,
and then talks to its workspace directly. One front door per environment (the rack's and
staging's), each on its own VM. The workspaces are `../../weft-workspace/deployment/`, whose
`CLAUDE.md` holds the rules every weft host shares (pins, signatures, no automatic rollback, the
state directory); this file holds what is the front door's own.

Every weft deploy is launched by a person. Nothing is scheduled, and nothing rolls back by itself.

## What runs where

| Container (image `weft-door`, by digest) | On the VM's own disk |
|---|---|
| `weft-door` | `/srv/weft-door/`: `front-door.db` and its backup sets |

`door.key` and `.env` are rendered beside `compose.yml`.

## Rules that are load-bearing

- **Deployed first, in every rollout.** It must keep accepting the previous release's boards while
  the workspaces move one at a time, and its deploy is what writes the public key every
  workspace's `weft.toml` names. A failed front door ends the run before any workspace.
- **The signing key is a 0600 file, never an environment variable.** `manage-secrets` generates
  `signing_key` (`type: hex`, 64 hex digits) once at `secret/services/<weft|weft-staging>/door`
  and reuses it forever: a new key makes every workspace's `[front_door] public_key` wrong, and
  every sign-in fails at once. It is mounted read-only; the front door refuses a key file others
  can read. No backup set holds it, and `deploy.sh`'s one-off commands never mount it.
- **The public half is derived on the controller**, by `lib/door_public_key.py` with the key on
  stdin, and stored beside it as `public_key` for the workspaces' deploys to read.
- **The edge is rendered from all of an environment's workspaces.** `templates/caddy-site.j2`
  routes `<domain>` to this VM, `<name>.<domain>` to each workspace's VM, and every other
  subdomain here, which redirects an old name or an id address, answers 503 for a name not routed
  yet and 404 otherwise. A fragment rendered from a subset would drop the routes of the
  workspaces left out. `weft-edge.yml` renders it before every front door deploy and for Stand Up
  weft Workspace.
- **A company's content is never logged**, as agentgateway never logs prompt and completion
  content (`../../agentgateway/context/agent-practices.md`, "Secrets boundary"). The front door
  writes process logs to stdout, which reach Loki like every service's; no text a person wrote and
  no credential is ever a log line, a metric label or a span attribute.
- **`deploy.sh` is container lifecycle only, for this one role.** It never reads OpenBao, never
  generates a secret and never chooses a version.
- **Health is the binary's own:** `front-door health` asks every workspace on the records whether
  it agrees about its id. One that names another id fails it; one that does not answer yet is
  only reported, because its row exists before its VM does. The page must also answer through the
  edge.

## What not to do

- Don't regenerate or rotate the signing key without redeploying every workspace of that
  environment in the same change.
- Don't pass the key through an environment variable, or mount it into a one-off container.
- Don't edit `.env`, `door.key` or the Caddy fragment by hand; they are re-rendered on every deploy.
- Don't schedule a weft template or make a deploy roll itself back (the AI Invariant,
  `PRINCIPLES.md` §4).

## Related

- Operator guide: [`README.md`](README.md); the shared site-config shape and the deploy's steps:
  [`../../weft-workspace/deployment/README.md`](../../weft-workspace/deployment/README.md)
- Playbooks (in [`../../../playbooks/`](../../../playbooks/)): `deploy-weft-door.yml`,
  `roll-out-weft-release.yml`, `deploy-weft-staging.yml`, `rollback-weft-host.yml`; each imports
  `weft-door.yml`, which imports `weft-edge.yml` and runs `tasks/weft-deploy.yml`
- Defaults every weft play shares: [`../../../playbooks/vars/weft.yml`](../../../playbooks/vars/weft.yml)
- Tests: `platform/tests/test_service_weft.bats`, `platform/tests/test_weft_door_public_key.py`
- Plan: `plan/development/25-weft-service.md`; OpenSpec change `add-weft-service`
- Root conventions: [`../../../../CLAUDE.md`](../../../../CLAUDE.md)
