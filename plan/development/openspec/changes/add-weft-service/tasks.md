Author: Jacob Haig <jacob.haig@uhstray.io>

Every task is safe to rerun after an interruption. Deploy work goes through the idempotent playbooks and their Semaphore templates, never a one-off write on a host. Each phase ends with a validation gate whose final task names the scenarios that prove it. The step numbers in brackets are those of weft's deployment plan (`docs/plan/deploy-on-agent-cloud.md` in the weft repository), so the two can be read side by side.

## 1. #743 current with `dev`, and its two blocking defects fixed [steps 16, 17]

- [x] 1.1 Merge `origin/dev` into `feat/weft-service` (a merge, not a rebase: it is a shared pull-request branch). Resolve `validate-all.yml` keeping Plane's plays and weft's, take `dev`'s `AGENTS.md` and `README.md` text with weft's rows added after it, and take `dev`'s `provision-vm.yml`, `vm-specs.example.yml` and `test_provision_vm.bats`: `dev` converges `vm_cpu` since `cfc8ad69`, and the automatic merge defined `_cpu` twice and added a second CPU write.
- [x] 1.2 B1: `deploy.sh <role> stop` treats a missing `.env` as nothing running, so the deploy's stop, which runs before `.env` is rendered, succeeds on a fresh host.
- [x] 1.3 B2: move `deployed.json`, `release.replaced.json`, `release-incoming/` and `deploying/` to the role's state directory under `weft_state_root` (default `/srv/weft-state`), made by the front door and workspace deploy plays, and refuse a state directory that is a mounted directory or inside one. Mount `release.replaced.json` read-only into the one-off backup container, which needs it.
- [x] 1.4 B2: one task, `tasks/weft-verify-images.yml`, checks a release's images (weft's registry, by digest, the role's images present, signed by the pinned identity); `tasks/weft-release.yml` and `tasks/weft-rollback.yml` both call it, the rollback on the previous release before it renders `.env`.
- [x] 1.5 "Follow weft main" becomes the directly Dev-bound scheduled template "Follow weft main (Dev)" (the pattern of "Check o11y Liveness (Dev)"), and places the branch it runs from, because a scheduled task gets no survey default (`tasks/assert-placement-branch.yml`). "Roll Out weft Release" keeps its `dev_variant` and loses its schedule until promotion (task 6.1).
- [x] 1.6 Add `weft_svc`, with the four weft groups as children, to `platform/inventory/production.yml`, with each group's variables as comments.
- [x] 1.7 Rename `plan/development/16-weft-service.md` to `23-weft-service.md` and fix its references; add weft's row to `plan/architecture/service-architecture.md`.
- [x] 1.8 Record that weft has no Clean Deploy (Jacob, 2026-11-11): a Clean Deploy in the platform's shape either keeps a company's bind-mounted catalog or deletes it behind one confirmation. The destructive path is offboarding (task 8.5), which takes a final backup and honours retention first. Declared in `design.md`, "Clean Deploy" and "Declared deviations".
- [ ] 1.9 Validation gate: CI on the pull request is green (`bats platform/tests/`, the Python suite, `ansible-lint`, `yamllint`, the security scans), and `openspec validate --all --store agent-cloud` passes. Scenarios "A fresh host has no .env", "The state directory is mounted by no container", "A rollback refuses an unverifiable previous image" and "A tag is refused" hold in `platform/tests/test_weft_deploy_runs.py` and `platform/tests/test_service_weft.bats`. Local run of 2026-11-11, before the push: bats 1307 of 1307; pytest 10846 passed, with two failures outside this change (a password-prompt test that needs a terminal and also fails on `dev` here, and an agentgateway stream-timing test that passes two runs in three); `ansible-lint` 0 failures; `yamllint` 0 errors on tracked files; openspec 27 of 27.

## 2. Deviations declared [step 18]

- [ ] 2.1 `PRINCIPLES.md` §3: "every **platform** credential" is OpenBao's, and a customer's credentials are sealed in its own catalog (the two-plane rule).
- [ ] 2.2 `plan/architecture/06-observability-instrumentation.md`: a service whose content is a customer's ships scrubbed process logs and content-free metrics, and its content stays on its host.
- [ ] 2.3 `platform/services/weft/deployment/CLAUDE.md` names the orchestrator's use of the workspace user's podman socket as the capability `PRINCIPLES.md` asks a non-rootless need to name.
- [ ] 2.4 An access policy per weft hostname with its `auth_mode`.
- [ ] 2.5 Validation gate: scenario "The workspace stack names no credential" holds, and each deviation in `design.md` cites the line that now declares it.

## 3. Ready for staging [steps 19, 20]

- [ ] 3.1 site-config#155 declares `weft_door`, `weft_workspaces`, `weft_staging_door` and `weft_staging_workspace` under `weft_svc`; `sync-inventory.yml` pushes them into Semaphore.
- [ ] 3.2 `snapshot-vm.yml` becomes a task each deploy takes before the stop.
- [ ] 3.3 F34: the front door's key is rendered outside the monorepo clone.
- [ ] 3.4 N1: "Roll Back weft Workspace" honours the deploy lock. N2: a preflight failure marks the GitHub deployment failed and tells Discord. N3: the rack refuses a release older than the one it runs.
- [ ] 3.5 Validation gate: scenarios "A branch-built image is refused" and "A rollback refuses an unverifiable previous image" hold on the local control plane.

## 4. The edge [steps 21, 22, 22a, 23]

- [ ] 4.1 Production Caddy on the composable layout before weft's wildcard fragment (`migrate-production-caddy-composable`).
- [ ] 4.2 weft's four records in `platform/infra/cloudflare/dns.tf` with `proxied = true`; the edge certificate for each wildcard proved on staging; a WAF challenge skip for weft's non-browser paths (`/api/*`, `/mcp`, the hand-off), extending the existing skip without reordering rules.
- [ ] 4.3 `caddy-site.j2`: `trusted_proxies` set to Cloudflare's published ranges, the client read from `CF-Connecting-IP`, and the header to the front door overwritten with that one address; the range list kept as data with a refresh task.
- [ ] 4.4 Host firewall on every weft VM: Caddy-only ports, single-host egress denies to OpenBao, Semaphore, the hypervisors and every other weft VM, `o11y_svc` on 9100, and a bats test for the denies. The voice UDP allow waits on decision V (how live voice reaches a workspace when every name is proxied).
- [ ] 4.5 Validation gate: scenarios "The plan holds every weft name proxied", "A client-supplied forwarding header does not reach the door" and "A weft VM attempting OpenBao is refused" hold on staging.

## 5. Staging follows main [steps 24 to 27]

- [ ] 5.1 Runner groups `weft-ci` and `weft-release` with `restricted_to_workflows`, and `weft-ci-svc-01`.
- [ ] 5.2 Until V is decided, the workspace's health no longer asserts the voice UDP port.
- [ ] 5.3 A failing-health drill, a migration-failure drill, `reboot-host.yml` on each staging VM, and the hairpin check, each recorded as a Semaphore task id.
- [ ] 5.4 Validation gate: scenarios "Staging runs a signed commit within one tick" and "Health fails within the watch window" hold on staging.

## 6. The rack [step 28]

- [ ] 6.1 After promotion, "Roll Out weft Release" gains its main-bound schedule.
- [ ] 6.2 The seal copy written to OpenBao on the rack; a migration-failure drill and a rollback drill on Bench.
- [ ] 6.3 Validation gate: scenario "No rack approval, no deploy" holds on the rack.

## 7. Observability [steps 34, 35, 38a, 45]

- [ ] 7.1 `check-weft-liveness.yml` in the shape of `check-o11y-liveness.yml`; `instrument-host-o11y.yml` for the weft groups; a root-disk rule; the `estate_coverage_inventory` entry in site-config; a Coverage Census run.
- [ ] 7.2 A log size cap per container and journald `SystemMaxUse` on weft hosts; an image prune after a healthy watch.
- [ ] 7.3 The `prometheus.io/scrape` and `prometheus.io/port` labels in both compose files (once weft serves `/metrics`), a weft dashboard and its alert rules in the o11y deployment.
- [ ] 7.4 An alert on OpenBao audit reads under `weft*/workspaces/*` outside a restore run.
- [ ] 7.5 Validation gate: scenarios "A fresh Loki line carries the service label" and "A workspace that stops answering is reported within 15 minutes" hold on staging.

## 8. Customer-ready [steps 39 to 42, 44, 46]

- [ ] 8.1 Rollout waves with a window and a hold (`serial: [1, 1, 5]`, `weft_hold_release`).
- [ ] 8.2 TLS from Caddy to each weft VM with the internal CA (decision D17).
- [ ] 8.3 The voice path, by decision V.
- [ ] 8.4 A weft-only package-read token for `registry-login.yml`.
- [ ] 8.5 "Onboard weft workspace" and "Offboard weft workspace". Offboarding is weft's only destructive path (it has no Clean Deploy): it takes a final backup, keeps the company's data for the retention period (D19), and only then runs `destroy-vm.yml`, confirmed like any destructive step.
- [ ] 8.6 A scheduled reboot window over `reboot-host.yml` that honours the deploy lock.
- [ ] 8.7 Validation gate: scenario "No rack approval, no deploy" holds for a held host, and scenario "Health fails within the watch window" holds for a workspace onboarded and offboarded on a scratch host.
