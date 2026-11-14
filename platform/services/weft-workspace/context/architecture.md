# weft — the agent harness, as agent-cloud runs it

Status: written and tested on the controller; nothing has run on a host. Plan:
`plan/development/25-weft-service.md`. OpenSpec change:
`plan/development/openspec/changes/add-weft-service/`, whose `design.md` holds the decisions and
the declared deviations. Tier: AI (it runs agents), per `plan/architecture/02-service-onboarding.md`.

weft is uhstray's agent harness, sold as a subscription. Its own architecture lives in the weft
repository (`uhstray-io/weft`), and agent-cloud does not restate it:

| What | Where, in the weft repository |
|---|---|
| How a version is built, signed and deployed; backup and rollback | `docs/cicd.md` (decisions D533 to D537) |
| The front door, workspaces and the three divisions (organisation, workspace, project) | `docs/deployment.md`, `docs/tenancy.md` |
| Agents outside weft, the forge, chat and capture sources | `docs/plan/10-integrations.md` |
| The vocabulary | `CONTEXT.md` |

This file records what agent-cloud has to know to run it, operate it, and reason about its edges.

## Shape

| Piece | Where | Notes |
|---|---|---|
| Front door | its own VM, container `weft-door` (image `weft-door`); `platform/services/weft-door/deployment/` | Identity across companies, workspace membership, a URL per workspace. It redirects and never proxies: a browser signs in here, receives a signed one-minute assertion, and then talks to its workspace directly. Records in `/srv/weft-door/front-door.db`. Its signing key is a 0600 file, never an environment variable |
| Workspace | one VM per company, containers `weft-board`, `weft-orchestrator`, `weft-capture` (image `weft-workspace`); `platform/services/weft-workspace/deployment/` | That company's whole installation. The catalog (`weft.db`), its seal, the whisper model and the checkouts are in `/srv/weft/<id>/`, on the VM's own disk (SQLite's WAL needs one kernel, weft D537) |
| Agent containers | started by the orchestrator through the deploy user's rootless podman socket | `weft-builder`, `weft-proxy` (the egress proxy, a cycle's only route to a model), `weft-harness-*`. They never get the workspace directory |
| `weft.toml` | `/srv/weft-state/workspaces/<id>/` | Root's, mounted by no container except as that one file, read-only, into the board and the orchestrator. It names the agent images and the commands the orchestrator runs, so no container may write it |
| Staging | one front door and one workspace | Runs the commit or pin a person launches "Deploy weft Staging" with |

## How a host changes

- **Every weft deploy is launched by a person**; no weft template has a schedule (the AI Invariant,
  `PRINCIPLES.md` §4).
- **A rack host runs what site-config pins it to**: `weft_release: "<version>@sha256:<digest>"`,
  the digest of that version's signed `release.json`. A rollout's waves are the hosts whose pins a
  site-config pull request changed; the previous release is in site-config's history.
- **A failed health check or watch fails the run and leaves the host as it is.** Going back is
  "Roll Back weft Host", after the person reverts the host's pin.
- **Backups**: weft's own verified backup set on the VM before every deploy; a Proxmox snapshot
  before a rack rollout ("Snapshot VM"); off-VM durability is PBS once that change lands.
- **Staging's GitHub `staging` deployment**, which weft's `release.yml` requires before it
  releases a commit, is written only by "Deploy weft Staging" after both staging hosts pass their
  watch, with a GitHub App installation token minted for that run.

## Surfaces a request reaches

Every weft hostname is proxied by Cloudflare (Jacob, 2026-11-10), and Caddy routes by hostname.

| Hostname | Goes to | Who calls it, and how it is authenticated |
|---|---|---|
| `<weft domain>` | the front door | People, in a browser; weft's own password sign-in, SSO beside it later |
| `<company>.<weft domain>` | that workspace's board | People, in a browser, with the session the front door's assertion opened. `GET /api/workspace` is the one unauthenticated route, used for health; it names the workspace's id |
| `<company>.<weft domain>/mcp` | the board's MCP endpoint | A company's own agents (Claude Code, OpenCode, any harness). Each holds an agent token an admin minted inside weft, bound to one project and revocable, hashed at rest; one workspace per token. Read-only first: lookups, each leaving a receipt, content returned as data and never as instructions. Writes wait for weft's outside-writes security design. It calls the same function a weft agent does, so its bounds and refusals are the same (weft `docs/plan/10-integrations.md` §2; on a weft branch, not released) |
| any other `*.<weft domain>` | the front door | It redirects an old name or an id address to the current name, answers 503 for a name not routed yet, and 404 otherwise |

`/api/*`, `/mcp` and the hand-off are non-browser paths, so the WAF challenge needs a skip for them
(see the OpenSpec change's `tasks.md`). Calls over UDP cannot pass Cloudflare's HTTP proxy; how
live voice reaches a workspace is an open decision in its `design.md`.

## What agent-cloud holds, and what it never does

- OpenBao holds the front door's signing key and its public half, and each workspace's seal, whose
  only origin it is. A workspace's model keys and forge token are never on the platform: its admin
  enters them in weft, sealed in its catalog (weft D539).
- The GitHub App that records staging deployments has its key at `secret/services/github`
  (`weft_app_key`, `weft_app_client_id`), installed on weft's repository only with deployments
  write and metadata read. No long-lived GitHub token is stored for weft.
- No weft VM reaches OpenBao; Semaphore's orchestrator reads every weft secret on the controller.
- Platform logs are weft's scrubbed process logs; a company's content stays on its VM. Liveness is
  a Grafana alert on weft's scrape target `up` (weft's step 38a), not built yet.
