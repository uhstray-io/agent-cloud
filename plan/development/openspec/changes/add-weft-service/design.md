Author: Jacob Haig <jacob.haig@uhstray.io>

## Context

weft's deployment is written on `feat/weft-service` (PR #743, agent-cloud#890 for its blockers). Its plan is `plan/development/23-weft-service.md`, and weft's own plans are `docs/cicd.md` (decisions D533 to D537) and `docs/plan/deploy-on-agent-cloud.md` in the weft repository. Nothing in it has run on a host: every check so far is structural. This design records the decisions the code depends on, the deviations from the platform's standards that weft declares rather than hides (`PRINCIPLES.md`: a deviation is declared), and what the first runs must prove.

## Decisions

### D1. A deploy takes a version as input

A weft host runs the images a signed `release.json` names, by digest. `release.json` is an OCI artifact beside the images, fetched by digest after its signature is checked; every image it names is checked the same way, on the target, by a digest-pinned cosign container (`tasks/verify-cosign.yml`). The identity is exact: `images.yml@refs/heads/main` and GitHub's issuer. An image built from a branch carries that branch's identity and is refused.

This replaces "a tag bump is an inventory change" (`update-service.yml`) for weft. The version a weft host runs is recorded on its VM and as a GitHub deployment, not in site-config, because the pipeline changes it and a person does not.

### D2. One deploy for every host, with its own rollback

`tasks/weft-deploy.yml` is the only deploy: preflight, stop, the binary's own `backup --before-deploy`, start, health through the edge within 5 minutes, a 15-minute watch, and, if health or the watch fails, `tasks/weft-rollback.yml` with that deploy's own backup. After the watch people have used the new version, so a later rollback is a person's (`rollback-weft-workspace.yml`), naming the host twice and the backup set. A release a host refused is never deployed there again by a timer.

### D3. What decides what runs is root's, and outside every container mount (B2)

The workspace directory is bind-mounted read-write into the board, which faces the internet. While `deployed.json` lived there, a compromised board could name an attacker's digest as "the previous release" and have the orchestrator, with the podman socket, run it on the next failed deploy; it could also fake "already on this release" or create `deploying/` to stop every timer. `weft.toml` lived there too, and it names the builder, proxy and session images, the orchestrator's command, and the checkout and clone paths the orchestrator runs through the socket.

There are two writers to keep out, not one. The board writes the workspace directory. The orchestrator holds the deploy user's podman socket, so it can do whatever the deploy user can, and a record the deploy user owns is one it could forge: skip a security release, add a release to `refused`, hold the lock, or name an older signed release for the rollback.

So:

- The record and `weft.toml` live in a state directory per role under `weft_state_root` (default `/srv/weft-state`): `/srv/weft-state/workspaces/<id>/` and `/srv/weft-state/door/`. `tasks/weft-release.yml` refuses a state directory that is a mounted directory or inside one.
- Root owns it: the directory is 0755 and every file 0644, and every write is a task with `become`, the sudo password read from OpenBao (`tasks/resolve-become-password.yml`) as every escalating play does. Nothing in it is a credential (weft D539), so the deploy user and the containers may read it.
- No container mounts the directory. Two files are mounted read-only: `weft.toml`, into the board and the orchestrator (capture reads no deployment file) and the `deploy.sh` one-off, and `release.replaced.json`, into the backup one-off alone.
- `release.json` is never a file on the VM: the manifest and the blob are fetched by digest to stdout, so nothing can swap it between the fetch and the read.
- A rollback treats the record as a claim, not a fact: `tasks/weft-verify-images.yml` re-checks the previous release's images (weft's registry, by digest, the role's images present, the pinned signer, cosign) before anything is rendered for them.

The backup sets under the workspace directory (`deploys/`) stay where the binary writes them. They decide what data is restored, not which image runs, and `restore` refuses a copy that does not verify or that is newer than the image restoring it.

**Accepted: the whisper model is board-writable.** The model file stays in `/srv/weft/<id>/models/`, because `weft.toml`'s paths all live under the workspace directory and the model is data rather than a decision. Between deploys a compromised board could swap it, and `weft-whisper` would parse the swapped file. It chooses no image and no command, and every deploy and rollback checks it against the sum in the signed `release.json` again.

The backup sets under the workspace directory (`deploys/`) stay where the binary writes them. They decide what data is restored, not which image runs, and `restore` refuses a copy that does not verify or that is newer than the image restoring it.

### D4. A fresh host is not stopped (B1)

`deploy.sh` reads `.env` for every command, and `.env` is rendered after the stop, so the first deploy on a new VM failed at its stop and told Discord the backup did not verify. `deploy.sh <role> stop` now treats a missing `.env` as nothing running, since every weft container is started from a rendered `.env`, and the deploy's stop stays unconditional. Keyed on the file rather than on `deployed.json`, a host whose first deploy died after `up` but before its record was written is still stopped.

### D5. The staging timer is Dev-bound; the rack's waits for promotion

A schedule on a main-bound base runs `main`, which has no weft code until promotion, so it would fail every five minutes. "Follow weft main (Dev)" is directly bound to `agent-cloud dev` with its schedule, the pattern of "Check o11y Liveness (Dev)" and "Renew Internal Certs (Dev)". A scheduled task carries no survey default (Semaphore applies one only in its web form), so the timer places the branch its own checkout is on rather than `main`, which `tasks/assert-placement-branch.yml` would refuse. "Roll Out weft Release" has no schedule until promotion; the rack's timer is main-bound (task 6.1).

### D6. Every weft name is proxied (Jacob, 2026-11-10)

Every platform hostname is proxied by Cloudflare, and grey-clouding was rejected because it publishes the origin address in DNS (`plan/architecture/05-platform-infra.md`). weft's names follow, with no exception. The costs and where each lands: live voice over UDP cannot pass the HTTP proxy and waits on decision V (tasks 4.4, 5.2, 8.3); the front door's per-address sign-in counter needs the real client, so Caddy reads `CF-Connecting-IP` from Cloudflare's ranges only (task 4.3); the WAF challenge faces weft's non-browser paths and needs a skip (task 4.2); the proxy's request timeout and body limit apply to uploads.

Until task 4.3 lands, the front door's `WEFT_FRONT_DOOR_BEHIND_A_PROXY` reads `X-Forwarded-For` as Caddy sets it, which behind Cloudflare is Cloudflare's address: one counter per edge node, not per client. That is not safe for the rack, and the rack is not deployed before task 4.3.

### D7. Scrubbed platform logs go to Loki (Jacob, 2026-11-10)

Every service meets the observability minimum tier, including a line in Loki (`plan/architecture/06-observability-instrumentation.md`). weft writes its platform-process logs to stdout in a schema built only from numbers, ids and enums, so a prompt, a transcript, an address or a path is not a value its logger accepts (weft's step 38); serves `/metrics`; and agent-cloud adds a dashboard and alerts (task 7.3). A company's content stays on its VM, and the carve-out is written into 06 (task 2.2).

Until weft's log schema lands, its containers write to stdout as they do today, and the hosts are not instrumented for Loki (task 7.1 follows weft's step 38).

## Declared deviations

| Standard | weft | Declared in |
|---|---|---|
| OpenBao is the sole source of truth for every credential (`PRINCIPLES.md` §3) | A company's model keys and forge token are sealed in its own catalog and never in OpenBao (weft D539); OpenBao holds weft's own platform credentials and one copy of each workspace's seal | task 2.1 |
| Each hostname declares an `auth_mode` | weft's own password sign-in is its main method, with SSO beside it; each weft hostname declares its `auth_mode` | task 2.4 |
| An AI-tier service keeps a `context/` directory (02, Service Classification) | `platform/services/weft/context/architecture.md` points to weft's own architecture, in the weft repository, and records what agent-cloud needs: the surfaces a request reaches, `/mcp` among them | met |
| Rootless podman, or name the capability | The orchestrator starts agent and egress-proxy containers through the workspace user's rootless podman socket; only the orchestrator gets it, never the board | task 2.3 |
| A database sidecar with an exporter | SQLite per workspace on the VM's own disk (weft D537); weft exports its own numbers | task 7.3 |
| `update-service.yml`'s tag pull | A deploy by signed digest only (D1) | this design |
| `clean-deploy-<service>.yml` on `tasks/clean-service.yml`, and its "Clean Deploy" template (`plan/architecture/02-service-onboarding.md`, Phase 4, lines 164 and 172) | No Clean Deploy (Jacob, 2026-11-11). The destructive path is offboarding (task 8.5; weft's step 44), which takes a final backup and honours retention first (D19). Until it lands there is no supported path to retire a host's data; the Rollback Plan's step 3 retires the service from a host and keeps the data | this design, "Clean Deploy" |
| One `compose.yml` per service (`plan/architecture/02-service-onboarding.md`, Phase 2, lines 113-129) | Two compose bases in one directory, `compose.door.yml` and `compose.workspace.yml`, one per kind of host; `deploy.sh` names its base through `COMPOSE_BASE` in `platform/lib/common.sh`, which refuses a named base that does not exist and is byte-identical when unset. `update-service.yml` and `clean-service.yml`, which read `compose.yml`, skip weft | this design |
| Private images through `tasks/registry-login.yml` with the shared `secret/services/github:pat` | Until task 5.4, the shared package-read PAT sits in the deploy user's `auth.json` on every weft VM, where the orchestrator, through its socket, can read it. A weft-only package-read token replaces it before the rack (task 5.4) | task 5.4 |
| A compose healthcheck on every container that supports one (02, Phase 2) | The board has one. The orchestrator, capture and the front door have none: their images ship no probe a healthcheck could run without adding a tool. The deploy stands in for it: `deploy.sh` waits for the front door over HTTP, and each deploy checks `health` through the edge and watches restart counts for 15 minutes. Healthchecks are added when weft's images ship a probe (task 7.5) | task 7.5 |
| Full-tier logs to Loki | Scrubbed process logs only; customer content stays on its VM (D7) | task 2.2 |

## Clean Deploy (Jacob, 2026-11-11)

weft has no Clean Deploy. The onboarding standard asks for `clean-deploy-<service>.yml` on `tasks/clean-service.yml` (`plan/architecture/02-service-onboarding.md`, Phase 4), and `platform/tests/test_clean_deploy_confirm.py` holds every Clean Deploy to one shape: a single play on `<service>_svc` whose only task includes `clean-service.yml`. For weft that shape either keeps the data or destroys a company's. `clean-service.yml` removes containers, volumes and the clone, and weft keeps its catalog in a bind mount (D537), so it would leave every company's data in place and not be clean. Deleting the workspace or front door directory as well would destroy a company's catalog, or every company's sign-in records, behind one confirmation.

So the destructive path for weft is offboarding (task 8.5, weft's step 44): it takes a final backup and keeps a company's data for the retention period (D19) before anything is deleted. A redeploy from scratch is a normal deploy with `weft_force_redeploy`. This is a declared deviation (table above).

Until task 8.5 lands there is no supported path to retire a host's data. The interim is the Rollback Plan's step 3, which stops the service on a host and removes its route and records, and keeps the workspace directory, its backups and the OpenBao paths.

## Accepted costs

- A deploy interrupts agent sessions in progress; the next pass starts them again.
- A rollback re-verifies the previous release's signatures, so it needs the registry and Sigstore reachable. When they are not, the rollback fails safe: every container stops, nothing is deleted, and a person is asked.
- OpenBao holds every workspace's seal copy, so a compromise of OpenBao opens every workspace's stored credentials. Only the controller's AppRole reads them, and no weft VM reaches OpenBao (task 4.4).
- A new workspace has no model to call until its admin signs in and enters its keys.

## Open decisions

- **V**: how live voice reaches a workspace now that every name is proxied. Blocks tasks 4.4 (the UDP allow), 5.2 and 8.3.
- **D17**: TLS on the LAN hop. Blocks task 8.2.
- **D19**: a company's data after it leaves. Blocks task 8.5.
- **#108**: seal custody. Blocks task 6.2.
