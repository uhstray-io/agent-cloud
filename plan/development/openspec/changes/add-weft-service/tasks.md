Author: Jacob Haig <jacob.haig@uhstray.io>

Every task is safe to rerun after an interruption. Deploy work goes through the idempotent playbooks and their Semaphore templates, never a one-off write on a host. Each phase ends with a validation gate whose final task names the scenarios that prove it. The step numbers in brackets are those of weft's deployment plan (`docs/plan/deploy-on-agent-cloud.md` in the weft repository), so the two can be read side by side.

## 1. #743 current with `dev`, and its two blocking defects fixed [steps 16, 17]

- [x] 1.1 Merge `origin/dev` into the weft branch (a merge, not a rebase: #743 is a shared pull-request branch): 2026-11-11, 2026-11-12 before the rework (558 commits; dev had retired the rows #743's base still carried, and moved `test_common.bats` into the Rust suite) and 2026-11-13. The work then goes to `dev` as a stack of pull requests built on current `dev`, this change first; #743 stays open until the stack is reviewed.
- [x] 1.2 B1: `deploy.sh stop` treats a missing `.env` as nothing running, so the deploy's stop, which runs before `.env` is rendered, succeeds on a fresh host.
- [x] 1.3 B2: `weft.toml` lives in the role's state directory under `weft_state_root` (default `/srv/weft-state`), root's (0755, the file 0644, every write with `become`); a state directory that is a mounted directory or inside one is refused. `weft.toml` is mounted read-only into the board, the orchestrator and the `deploy.sh` one-off. `release.json` is fetched to stdout, never to a file.
- [x] 1.4 One task, `tasks/weft-verify-images.yml`, checks a release's images (weft's registry, by digest, the role's images present, signed by the pinned identity), for every deploy and every rollback.
- [x] 1.5 A host left alone on re-launch only when the deploy's own root-owned marker (`deploy.state`) says it is healthy at its pinned digest and its containers run the release's image; a host whose deploy failed is deployed again. The whisper model is fetched and checked before the stop.
- [x] 1.6 Add `weft_svc`, with the four weft groups as children, to `platform/inventory/production.yml`, with each group's variables as comments.
- [x] 1.7 The plan is `plan/development/25-weft-service.md` (16, then 23, were taken on `dev`); weft's row in `plan/architecture/service-architecture.md`.
- [x] 1.8 Record that weft has no Clean Deploy (Jacob, 2026-11-11, deviating from `02-service-onboarding.md` Phase 4): a Clean Deploy in the platform's shape either keeps a company's bind-mounted catalog or deletes it behind one confirmation. The destructive path is offboarding (task 8.5, weft's step 44), which takes a final backup and honours retention first. **Until 8.5 lands there is no supported path to retire a host's data**; the Rollback Plan's step 3 (stop, remove the route and records, keep the data) is the interim. Declared in `design.md`.
- [x] 1.8a `platform/services/weft-workspace/context/architecture.md`, the AI tier's context directory: weft's surfaces (`/mcp` among them) and where its own architecture lives.
- [x] 1.10 Joe's pattern everywhere (Jacob, 2026-11-12; `design.md`):
  - [x] 1.10.1 No timers: "Follow weft main (Dev)" is deleted, and "Roll Out weft Release" is launched by a person. Staging is "Deploy weft Staging", whose survey takes the commit or pin.
  - [x] 1.10.2 Each rack host is pinned in site-config to `<version>@sha256:<release.json digest>`; the rollout refuses any other pin, verifies the signature and `release.json` against the digest, and deploys each host to its own pin. The GitHub approval polling, `weft_release_approver`, `serial: [1, 1, 5]`, `weft_hold_release` and the window are gone.
  - [x] 1.10.3 No automatic rollback, and none of the first version's VM-local record (`deployed.json`, the replaced `release.json`, the `deploying/` lock, `refused`): a failed deploy fails the run and leaves the host as it is; "Roll Back weft Host" takes a workspace or the front door back to its reverted pin from a named backup set.
  - [x] 1.10.4 The seal's one origin is OpenBao: `manage-secrets` generates `seal` (`type: hex`) and the play renders `seal.key` (0600) before the first start. `weft-seal.yml`, its copy from the VM to OpenBao and its compare-and-refuse are deleted.
  - [x] 1.10.5 Two service directories, `platform/services/weft-door/` and `platform/services/weft-workspace/`, each with one `compose.yml` and a lifecycle-only `deploy.sh`; the `COMPOSE_BASE` change to `platform/lib/common.sh` is reverted.
  - [x] 1.10.6 The staging deployment record is written with a GitHub App installation token minted per run (`tasks/weft-github-app-token.yml`); the long-lived `secret/services/weft/github:token` is gone.
  - [x] 1.10.7 No Discord: `notify-discord.yml`, `reconcile-discord-webhook.yml`, `weft-credentials.yml`, "Seed weft Secrets" and `secret/services/weft/discord` are gone.
- [ ] 1.9 Validation gate: CI on each pull request of the stack is green (`bats platform/tests/`, the Python suite, the Rust suite, `ansible-lint`, `yamllint`, the security scans), and `openspec validate --all` passes. Scenarios "A fresh host has no .env", "weft.toml is mounted by no container writable", "A pin that is not a signed digest is refused", "A tag is refused", "A failed deploy rolls nothing back by itself" and "Nothing deploys weft unless a person launches it" hold in `platform/tests/test_weft_deploy_runs.py` and `platform/tests/test_service_weft.bats`.

## 2. Deviations declared [step 18]

- [x] 2.1 A company's credentials sealed in its own catalog are declared as a deviation from `PRINCIPLES.md` §3 in `design.md`. §3 is not amended (Jacob, 2026-11-12; `design.md`, "Decided").
- [x] 2.2 A company's content is never logged, stated in each weft service's `CLAUDE.md` the way agentgateway states it for prompt and completion content. 06 is not changed: process logs in Loki meet its minimum tier as written, so there is no exemption to record.
- [x] 2.3 `platform/services/weft-workspace/deployment/CLAUDE.md` records the orchestrator's podman socket as the GitHub runner's pattern (one unprivileged account's rootless socket), and the deploy user's socket until task 5.5 as the gap from it.
- [ ] 2.4 An access policy per weft hostname with its `auth_mode`. Open and declared (Jacob, 2026-11-12): it waits on Joe accepting a mode for a service that signs in its own users (`design.md`, Open decisions). `/mcp` is `api_key` meanwhile.
- [ ] 2.5 Validation gate: scenario "The workspace stack names no credential" holds, and each deviation in `design.md` cites the line that now declares it.

## 3. Ready for staging [steps 19, 20]

- [ ] 3.1 site-config#155 declares `weft_door`, `weft_workspaces`, `weft_staging_door` and `weft_staging_workspace` under `weft_svc`, and each rack host's `weft_release`; `sync-inventory.yml` pushes them into Semaphore. The VM sizes are confirmed with Joe first (`design.md`, declared deviations).
- [ ] 3.2 The GitHub App for weft's deployments (Deployments: read and write, installed on weft only), its key and client id seeded at `secret/services/github` (`weft_app_key`, `weft_app_client_id`) through Seed OpenBao Key.
- [ ] 3.3 F34: the front door's key is rendered outside the monorepo clone.
- [ ] 3.4 N3: the rack refuses a pin older than the release a host runs, unless the launch says it is a rollback.
- [ ] 3.5 Validation gate: scenarios "A branch-built image is refused" and "A failed deploy rolls nothing back by itself" hold on the local control plane.

## 4. The edge [steps 21, 22, 22a, 23]

- [ ] 4.1 weft's fragment on production Caddy's flat layout, managed by `manage-caddy-sites.yml` (`design.md`, C4).
- [ ] 4.2 weft's four records in `platform/infra/cloudflare/dns.tf` with `proxied = true`; the edge certificate for each two-level wildcard proved on staging; a WAF challenge skip for weft's non-browser paths (`/api/*`, `/mcp`, the hand-off), extending the existing skip without reordering rules (`design.md`, C6).
- [ ] 4.3 The client address from `CF-Connecting-IP`, trusted only from Cloudflare's published ranges, and the forwarding header to the front door overwritten with that one address: proposed to Joe for Caddy's global configuration (`design.md`, C5), with the range list kept as data with a refresh task.
- [ ] 4.4 Host firewall on every weft VM: Caddy-only ports, single-host egress denies to OpenBao, Semaphore, the hypervisors and every other weft VM, `o11y_svc` on 9100, and a bats test for the denies. The voice UDP allow waits on decision V (how live voice reaches a workspace when every name is proxied).
- [ ] 4.5 Validation gate: scenarios "The plan holds every weft name proxied", "A client-supplied forwarding header does not reach the door" and "A weft VM attempting OpenBao is refused" hold on staging.

## 5. Staging [steps 24 to 27, 42]

- [ ] 5.1 weft's slow checks on the platform's ephemeral runners (`github_runner_lifecycle: ephemeral`); weft's repository added to the runner group in `manage-github-runner-group.yml`, reviewed by Joe. No runner VM of weft's own.
- [x] 5.2 Until V is decided, the workspace's deploy does not assert the voice UDP port.
- [ ] 5.3 A failing-health drill, a migration-failure drill followed by "Roll Back weft Host", `reboot-host.yml` on each staging VM, and the hairpin check, each recorded as a Semaphore task id.
- [ ] 5.4 A weft-only package-read credential, read by `tasks/registry-login.yml` through `registry_login_key`, in place of the shared `secret/services/github:pat`, which until then sits in the deploy user's `auth.json` on every weft VM: a GitHub App installation token if GitHub's container registry accepts one for these packages. Before the rack.
- [ ] 5.5 The orchestrator gets the podman socket of a separate unprivileged user, who runs only agent and egress-proxy containers, so its compromise no longer owns the deploy user's image store and containers (`design.md`, known gaps). Before the rack.
- [ ] 5.6 Validation gate: scenarios "Staging runs the commit a person launched" and "Health fails within the watch window" hold on staging.

## 6. The rack [step 28]

- [ ] 6.0 Before the rack: `roll-out-weft-release.yml` imports `snapshot-vm.yml` for each host whose pin changed, before that host's deploy, meeting `PRINCIPLES.md` §5's snapshot [TARGET] for weft (`design.md`, D10). Until then the snapshot is a person's step with "Snapshot VM".
- [ ] 6.1 The rack's first rollout: each host pinned in site-config, a Proxmox snapshot of each, then "Roll Out weft Release" (Dev until promotion).
- [ ] 6.2 Seal custody (#108) settled; a migration-failure drill and a rollback drill on Bench.
- [ ] 6.3 Validation gate: scenario "A rack host moves only to its pin" holds on the rack.

## 7. Observability [steps 34, 35, 38a, 45]

- [ ] 7.1 `instrument-host-o11y.yml` for the weft groups; a root-disk rule; the `estate_coverage_inventory` entry in site-config; a Coverage Census run.
- [ ] 7.2 A log size cap per container and journald `SystemMaxUse` on weft hosts; an image prune after a healthy watch.
- [ ] 7.3 The `prometheus.io/scrape` and `prometheus.io/port` labels in both compose files (once weft serves `/metrics`), a weft dashboard, its alert rules in the o11y deployment, and the alert on each weft scrape target's `up` (weft's step 38a) in place of a liveness playbook.
- [ ] 7.4 An alert on OpenBao audit reads under `weft*/workspaces/*` outside a deploy or a rollback.
- [ ] 7.5 Compose healthchecks for the orchestrator, capture and the front door, once weft's images ship a probe they can run.
- [ ] 7.6 Validation gate: scenarios "A fresh Loki line carries the service label" and "A workspace that stops answering is reported within 15 minutes" hold on staging.

## 8. Customer-ready [steps 39 to 41, 44, 46]

- [ ] 8.1 Off-VM durability: weft's hosts in the platform's Proxmox Backup Server jobs once `pbs-vm-and-restore-gate` lands.
- [ ] 8.2 TLS from Caddy to each weft VM with the internal CA (decision D17).
- [ ] 8.3 The voice path, by decision V.
- [ ] 8.5 "Onboard weft workspace" and "Offboard weft workspace". Offboarding is weft's only destructive path (it has no Clean Deploy): it takes a final backup, keeps the company's data for the retention period (D19), and only then runs `destroy-vm.yml`, confirmed like any destructive step.
- [ ] 8.6 A reboot procedure over `reboot-host.yml`, launched by a person per host.
- [ ] 8.7 Validation gate: scenario "A rack host moves only to its pin" holds for a held host, and scenario "Health fails within the watch window" holds for a workspace onboarded and offboarded on a scratch host.
