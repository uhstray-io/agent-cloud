Author: Jacob Haig <jacob.haig@uhstray.io>

## Context

weft's deployment was first written on `feat/weft-service` (PR #743, agent-cloud#890 for its blockers) and goes to `dev` as a short stack of pull requests, this change at the bottom. Its plan is `plan/development/25-weft-service.md`, and weft's own plans are `docs/operations/cicd.md` (decisions D533 to D537) and `docs/plan/deploy-on-agent-cloud.md` in the weft repository. Nothing in it has run on a host: every check so far is structural. This design records the decisions the code depends on, the deviations from the platform's standards that weft declares rather than hides (`PRINCIPLES.md`: a deviation is declared), and what the first runs must prove.

An alignment review against `PRINCIPLES.md` and the services Joe built (skynet, the launch gate, Postiz, the ephemeral runners) found that the first version of this change broke the platform's principles in several places: timers that deployed without a person, a rollout that polled GitHub for an approval, a seal with two origins, one service directory for two kinds of host, a long-lived GitHub token, Discord messages forked from another service's tasks, and an off-site backup store the platform does not run. Jacob decided on 2026-11-12 that weft follows Joe's pattern everywhere. This design is the result.

## Decisions

### D1. A person pins each host to a signed release

A rack host's release is a site-config value, set per host: `weft_release: "<version>@sha256:<digest>"`, the version a person chose and the digest of that version's signed `release.json` artifact. This is skynet's pattern (`skynet_image` is a digest-pinned reference in site-config, and `deploy-skynet.yml` refuses anything else): a host moves when a person changes its pin in a site-config pull request and syncs the inventory, and then launches a deploy.

So the waves are the hosts whose pins changed, a hold is a pin left where it is, and every host's previous release is in site-config's history. Nothing in agent-cloud asks GitHub which release is newest or who approved it: the pin is the decision, and the person who wrote it is on the pull request.

A deploy refuses a rack pin that is not `<version>@sha256:<64 hex>`, before anything changes on the host (`tasks/weft-release.yml`, and `roll-out-weft-release.yml` for every rack host before the first is touched). It verifies the cosign signature of the `weft-release` artifact at that digest, fetches its manifest and `release.json` by digest to stdout, hashes both on the controller against the digests asked for, and refuses a `release.json` whose version is not the pinned one. Every image `release.json` names is then verified the same way and pulled by digest. The signing identity is exact: `images.yml@refs/heads/main` and GitHub's issuer, so an image built from a branch carries that branch's identity and is refused.

Staging has no pin. A person launches "Deploy weft Staging" with a full commit main published, or a `<commit|version>@sha256:<digest>` pin, as its survey value. A bare commit is resolved to the digest of `weft-release:<commit>`, whose signature is then checked like any pin's.

### D2. A person launches every deploy

Every weft template is launched by a person, and none is scheduled:

| Template | Playbook | What it does |
|---|---|---|
| Deploy weft Front Door | `deploy-weft-door.yml` | the rack's front door, to its pin |
| Deploy weft Workspace | `deploy-weft-workspace.yml` | the rack's workspaces, to their pins, one at a time |
| Roll Out weft Release | `roll-out-weft-release.yml` | the front door, then each workspace one at a time; refuses a bad pin before anything changes |
| Deploy weft Staging | `deploy-weft-staging.yml` | the staging pair, to the survey's commit or pin; records the GitHub `staging` deployment |
| Roll Back weft Host | `rollback-weft-host.yml` | one host back to its (reverted) pin, from a named backup set |
| Stand Up weft Workspace | `stand-up-weft-workspace.yml` | a new rack workspace: its route, then a deploy to its pin |

Each has a `(Dev)` variant until promotion (`dev_variant`). A host already on its pin is left alone (`weft_force_redeploy` deploys it again), so re-launching a rollout continues it where it stopped.

**The AI Invariant, recorded:** no target an AI wrote is converged without a person launching it (`PRINCIPLES.md` §4). An agent may open a pull request that changes a pin; that pull request is reviewed and merged by a person, and nothing deploys it until a person launches a template. There is no timer for an agent's pin to ride, and no playbook reads a target from GitHub, where an agent with a token could write one.

### D3. One deploy, and no automatic rollback

`tasks/weft-deploy.yml` is the only deploy: preflight (the release, its images and a workspace's whisper model, all verified before anything stops), stop, the binary's own `backup --before-deploy`, start, health through the edge within 5 minutes, and a 15-minute watch on restart counts and health. A backup that does not verify stops the deploy and starts what was running again.

A failure in health or the watch fails the run and leaves the host as it is. Nothing goes back by itself, because the release to go back to is the host's previous pin, a person's choice in site-config, and going back restores data. The failure names the backup set the deploy took and says how to go back: revert the host's `weft_release` in site-config, sync the inventory, and launch "Roll Back weft Host" with that set. The rollback names the host twice and the set, deploys what the host is pinned to now (verified like any deploy), and restores the set with that release's binary; `restore` sets the current database aside and never deletes it. `Roll Out weft Release` and `Deploy weft Workspace` stop at the first failure (`serial: 1`, `max_fail_percentage: 0`, `any_errors_fatal`), so the hosts after it are untouched.

**One VM-local marker, and nothing that chooses an image.** The first version kept `deployed.json`, the replaced `release.json`, a `deploying/` lock and a `refused` list on each VM, so that a forged record could not choose a rollback target or stop a timer. With the target in site-config and no timers, none of it is needed, and it is gone. What remains is `deploy.state` in the role's root-owned state directory: `deploying <digest>` written before the stop, and `healthy <digest>` written only after the watch passes (and after a person's rollback comes back healthy). A host is left alone on re-launch only when it is marked healthy at its pinned digest and every container runs the release's image; a host whose deploy failed runs the pinned image too, and without the marker a re-launch would take it as done. The marker can only cause a skip, never choose an image, and the deploy user cannot write it. A deploy also reads the `WEFT_RELEASE` line of the rendered `.env`, which only names the backup set.

### D4. weft.toml is root's, and outside every container mount

The workspace directory is bind-mounted read-write into the board, which faces the internet, and the orchestrator holds the deploy user's podman socket, so it can write anything the deploy user can. `weft.toml` names the builder, proxy and session images, the orchestrator's command, and the checkout and clone paths the orchestrator runs through the socket. So it lives in a state directory under `weft_state_root` (default `/srv/weft-state/workspaces/<id>/`), which root owns (0755, the file 0644, every write with `become`) and no container mounts; `weft.toml` alone is bind-mounted read-only into the board, the orchestrator and the `deploy.sh` one-off. `tasks/weft-release.yml` refuses a state directory that is a mounted directory or inside one.

`release.json` is never a file on the VM. The verifiers run as root, from root's image store: cosign (`tasks/verify-cosign.yml`) and the three oras runs, because the deploy user's rootless store is one the orchestrator can write through its socket and podman does not re-verify the layers of a digest-pinned image it already holds. oras is not trusted even as root: the controller hashes what it fetched.

**Accepted: the whisper model is board-writable.** The model file stays in `/srv/weft/<id>/models/`, because the model is data rather than a decision. Between deploys a compromised board could swap it, and `weft-whisper` would parse the swapped file. It chooses no image and no command, and every deploy and rollback checks it against the sum in the signed `release.json` again.

### D5. A fresh host is not stopped (B1)

`deploy.sh` reads `.env` for every command but `images`, and `.env` is rendered after the stop, so the first deploy on a new VM failed at its stop. `deploy.sh stop` treats a missing `.env` as nothing running, since every weft container is started from a rendered `.env`, and the deploy's stop stays unconditional.

### D6. The seal has one origin

`seal.key` opens the credentials a company's admin set on their own board. Its origin is OpenBao: the workspace's deploy calls `tasks/manage-secrets.yml` with `seal` (`type: hex`, 64 hex digits, 32 bytes) at `secret/services/<weft|weft-staging>/workspaces/<id>`, which generates it once and reuses it forever, and renders `seal.key` (32 raw bytes, 0600) in the workspace directory before the first start. Nothing reads it off a VM into OpenBao. weft keeps `seal.key` out of every backup set, so a copy of the catalog is never also a working set of somebody's credentials; a workspace restored onto a new VM gets the seal from OpenBao like any deploy.

### D7. Two services, two directories

The front door and a workspace are two kinds of host with two stacks, so they are two services: `platform/services/weft-door/deployment/` and `platform/services/weft-workspace/deployment/`, each with one `compose.yml` and one lifecycle-only `deploy.sh` (`PRINCIPLES.md` §1, "one `deploy.sh`, one `compose.yml` per service"). `platform/lib/common.sh` is unchanged. The inventory's `service_name` is `weft-door` or `weft-workspace`.

### D8. Staging's GitHub record, with a token minted for the run

weft's `release.yml` releases only a commit whose GitHub `staging` deployment succeeded. "Deploy weft Staging" writes that record after both staging hosts pass their watch, and only then. The token is a GitHub App installation token minted on the controller for that run (`tasks/weft-github-app-token.yml`, the pattern of `refresh-console-github-token.yml` without its store): weft's repository alone, `deployments: write` and the `metadata: read` GitHub adds, and `platform/lib/github_app_token.py` refuses a token whose grant is wider. The App's key and client id are at `secret/services/github` (`weft_app_key`, `weft_app_client_id`), seeded once through Seed OpenBao Key. There is no long-lived GitHub token in this change.

### D9. No deploy messages

A deploy says nothing to Discord. Semaphore's task history is the record of who launched what and what it did, as it is for every other service.

### D10. Backups

Before every deploy the binary takes its own backup set beside the data and verifies it (`backup --before-deploy`); a copy that does not verify stops the deploy before anything changes. Before a rack rollout a person takes a Proxmox snapshot of each host whose pin changed with the existing "Snapshot VM" template (`snapshot-vm.yml`): a manual step, and so the open `PRINCIPLES.md` §5 [TARGET] ("pairs the run with a volume/Proxmox snapshot") is not met by weft yet; it is declared below. Before the rack, `roll-out-weft-release.yml` imports `snapshot-vm.yml` for each host whose pin changed (task 6.0). Off-VM durability is the platform's Proxmox Backup Server once Joe's `pbs-vm-and-restore-gate` change lands; weft adds no off-site store of its own.

### D11. Every weft name is proxied (Jacob, 2026-11-10)

Every platform hostname is proxied by Cloudflare, and grey-clouding was rejected because it publishes the origin address in DNS (`plan/architecture/05-platform-infra.md`). weft's names follow, with no exception. The costs and where each lands: live voice over UDP cannot pass the HTTP proxy and waits on decision V (tasks 4.4, 5.2, 8.3); the front door's per-address sign-in counter needs the real client (C5 below, task 4.3); the WAF challenge faces weft's non-browser paths and needs a skip (C6 below, task 4.2); the proxy's request timeout and body limit apply to uploads.

Until task 4.3 lands, the front door's `WEFT_FRONT_DOOR_BEHIND_A_PROXY` reads `X-Forwarded-For` as Caddy sets it, which behind Cloudflare is Cloudflare's address: one counter per edge node, not per client. That is not safe for the rack, and the rack is not deployed before task 4.3.

### D12. Scrubbed platform logs to Loki, and liveness from the scrape (Jacob, 2026-11-10)

Every service meets the observability minimum tier, including a line in Loki (`plan/architecture/06-observability-instrumentation.md`). weft writes its platform-process logs to stdout in a schema built only from numbers, ids and enums, so a prompt, a transcript, an address or a path is not a value its logger accepts (weft's step 38); serves `/metrics`; and agent-cloud adds a dashboard and alerts (task 7.3). A workspace that stops answering is reported by a Grafana alert on its scrape target's `up` (weft's step 38a); weft adds no liveness playbook. A company's content stays on its VM and is never logged, as agentgateway never logs prompt and completion content; that meets 06 as written, so 06 is not changed (task 2.2).

### D13. weft's CI runs on ephemeral runners

weft's slow checks run on the platform's runner service with `github_runner_lifecycle: ephemeral`, the just-in-time pattern of `add-ephemeral-github-runners`: one runner, one job. weft adds no runner VM of its own. Any change to `manage-github-runner-group.yml` that weft needs (its repository in the group's list) is Joe's to review.

## Declared deviations

| Standard | weft | Declared in |
|---|---|---|
| A Clean Deploy per service: `clean-deploy-<service>.yml` on `tasks/clean-service.yml`, and its template (`plan/architecture/02-service-onboarding.md`, Phase 4) | No Clean Deploy (Jacob, 2026-11-11). Offboarding is the only destructive path (task 8.5; weft's step 44): it takes a final backup and honours retention first (D19). Until it lands there is no supported path to retire a host's data; the Rollback Plan's step 3 retires the service from a host and keeps the data | "Clean Deploy" below |
| Image signing with keyed cosign, verified against a public key (the platform's plan: `plan/development/03-guardrails-governance.md`, 2.2 and 3.4) | Keyless cosign: the signature's certificate names `images.yml@refs/heads/main` and GitHub's issuer, and the check is exact (`tasks/verify-cosign.yml`). There is no key to hold or rotate, and the trust is in GitHub's OIDC issuer and Sigstore's public log | D1 |
| A compose healthcheck on every container that supports one (02, Phase 2) | The board has one. The orchestrator, capture and the front door have none until their images ship a probe they can run (task 7.5). Each deploy checks `health` through the edge and watches restart counts for 15 minutes in its place | task 7.5 |
| A company's credentials: OpenBao is the source of truth for every credential (`PRINCIPLES.md` §3) | A company's model keys and forge token are sealed in its own catalog and never in OpenBao (weft D539); OpenBao holds weft's platform credentials and each workspace's seal | this table; §3 is not amended ("Decided", task 2.1) |
| A deploy that migrates data is paired with a volume or Proxmox snapshot (`PRINCIPLES.md` §5, [TARGET]) | A person takes the snapshot with "Snapshot VM" before a rack rollout; no weft playbook takes it yet. weft's own verified backup set is taken before every deploy | D10; task 6.0 imports `snapshot-vm.yml` into the rollout before the rack |
| A database sidecar with an exporter | SQLite per workspace on the VM's own disk (weft D537); weft exports its own numbers | task 7.3 |
| Logs to Loki (06, minimum tier) | Process logs reach Loki like every service's, and a company's content is never logged, as agentgateway never logs prompt and completion content (D12) | met; each weft service's `CLAUDE.md`, "A company's content is never logged" (task 2.2) |
| Each hostname declares an `auth_mode` | weft's own password sign-in at the front door and the board's session fit none of the schema's modes; `/mcp` is `api_key` | open, below (task 2.4) |
| VM sizes within the platform's tiers (Large: 4+ vCPU, 8+ GB, 60-100 GB, `plan/archive/architecture/SERVICE-INTEGRATION-PLAN.md`) | A workspace VM is 8 vCPU, 16 GB and 200 GB in weft's plan (`docs/operations/cicd.md` in the weft repository): it runs the agent and builder containers its orchestrator starts. To be confirmed with Joe before site-config declares the VMs | task 3.1 |

**Known gaps that must close before the rack** (each a task in phase 5 or earlier, and none blocks staging):

- **`door.key` is rendered inside the monorepo clone** (`platform/services/weft-door/deployment/door.key`), which a re-placed clone could carry or lose. It moves outside the clone (task 3.3).
- **The shared package-read PAT.** Until task 5.4, `tasks/registry-login.yml` logs in with the shared `secret/services/github:pat`, which then sits in the deploy user's `auth.json` on every weft VM, where the orchestrator, through its socket, can read it. Task 5.4 replaces it with a weft-only read credential, a GitHub App installation token if GitHub's container registry accepts one for these packages, before the rack.
- **The orchestrator holds the deploy user's podman socket.** A compromised orchestrator can run anything as the deploy user: its image store, its containers, `.env` and the clone between the render and `deploy.sh up`, until the VM is rebuilt. `weft.toml` and the verifiers are out of its reach, so it cannot make a deploy choose a different release, but it can change what that release runs. Task 5.5 gives it the socket of a separate unprivileged user who runs only agent and egress-proxy containers: the GitHub runner's pattern, one unprivileged account's rootless socket (`runner-dotenv.j2`, `tasks/podman-user-docker-socket.yml`), recorded in `platform/services/weft-workspace/deployment/CLAUDE.md` (task 2.3).

### For Joe's review

- **C4, the Caddy layout.** weft's fragment goes to central Caddy through `tasks/distribute-caddy-site.yml`, the flat layout `manage-caddy-sites.yml` manages (`weft-edge.yml`). weft does not migrate production Caddy to the composable layout; that stays Joe's.
- **C5, the client address.** Behind Cloudflare, the client is in `CF-Connecting-IP`, trusted only from Cloudflare's published ranges. The proposal is to fix that once in Caddy's global configuration, for every site, rather than in weft's fragment alone (task 4.3).
- **C6, unverified at the edge.** The WAF challenge skip for weft's non-browser paths (`/api/*`, `/mcp`, the sign-in hand-off) and the edge certificates for weft's two-level wildcard names (`*.<weft domain>` under the zone) have not been tried. Both are proved on staging before the rack (task 4.2).
- **VM sizing** above the Large tier, in the table above.
- **The runner group.** If weft's repository needs adding to `manage-github-runner-group.yml`, that change is Joe's to review (D13).

## Clean Deploy (Jacob, 2026-11-11)

weft has no Clean Deploy. The onboarding standard asks for `clean-deploy-<service>.yml` on `tasks/clean-service.yml` (`plan/architecture/02-service-onboarding.md`, Phase 4), and `platform/tests/test_clean_deploy_confirm.py` holds every Clean Deploy to one shape: a single play on `<service>_svc` whose only task includes `clean-service.yml`. For weft that shape either keeps the data or destroys a company's. `clean-service.yml` removes containers, volumes and the clone, and weft keeps its catalog in a bind mount (D537), so it would leave every company's data in place and not be clean. Deleting the workspace or front door directory as well would destroy a company's catalog, or every company's sign-in records, behind one confirmation.

So the destructive path for weft is offboarding (task 8.5, weft's step 44): it takes a final backup and keeps a company's data for the retention period (D19) before anything is deleted. A redeploy from scratch is a normal deploy with `weft_force_redeploy`.

Until task 8.5 lands there is no supported path to retire a host's data. The interim is the Rollback Plan's step 3, which stops the service on a host and removes its route and records, and keeps the workspace directory, its backups and the OpenBao paths.

## Accepted costs

- A deploy interrupts agent sessions in progress; the next pass starts them again.
- A failed deploy leaves its host on the failed release until a person rolls it back; a workspace may be down or degraded for that time. The run fails loudly and names the backup set.
- A rollback goes back only as far as a backup set on the VM, and loses what people wrote after it, which is kept beside the restored database as its `failed-<version>` copy.
- Until task 5.5, a compromised orchestrator can run anything as the deploy user (above).
- A deploy and a rollback verify signatures, so they need the registry and Sigstore reachable; when they are not, the deploy fails before anything stops.
- OpenBao holds every workspace's seal, so a compromise of OpenBao opens every workspace's stored credentials. Only the controller's AppRole reads them, and no weft VM reaches OpenBao (task 4.4).
- A new workspace has no model to call until its admin signs in and enters its keys.

## Open decisions

- **V**: how live voice reaches a workspace now that every name is proxied. Blocks tasks 4.4 (the UDP allow), 5.2 and 8.3.
- **D17**: TLS on the LAN hop. Blocks task 8.2.
- **D19**: a company's data after it leaves. Blocks task 8.5.
- **#108**: seal custody beyond OpenBao. Blocks task 6.2.
- **`auth_mode` for weft's own sign-in** (open and declared, Jacob, 2026-11-12): `access-policy.json` offers `oidc`, `forward_auth`, `api_key` and `none`. `/mcp` is `api_key`, but neither weft's password sign-in at the front door nor the board's session opened by the front door's assertion is any of the four, and `none` would tell the access step that nothing authenticates. A value for "the service signs in its own users" is proposed to Joe as a change to the access-policy schema and the OPA rules, his to accept. weft declares no mode it cannot honestly claim meanwhile. Blocks task 2.4.

## Decided

- **`PRINCIPLES.md` §3 is not amended** (Jacob, 2026-11-12). "Every credential" stays as written. A company's model keys and forge token are sealed in its own catalog, and that remains a declared deviation in the table above (task 2.1).
