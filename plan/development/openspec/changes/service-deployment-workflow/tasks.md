# Tasks: service deployment workflow

Every task is rerun-safe: detect the state it would produce first and converge or skip.
Pushes, pull requests and merges only when Joe asks for them (repo rule). Tasks marked
**[skynet]** land in the private `uhstray-io/skynet` repository.

## 0. Prerequisites and verifications

- [x] 0.1 Feature branch `feat/service-deployment-workflow` from `dev` (via the plan-15 docs
      branch). 2026-09-22: `inference-gateway-agentgateway`'s implementation is NOT on `dev`
      (16 commits ahead on its impl branch, no PR), so its merge became the entry gate of
      section 3, the only section that needs the gateway code; see 3.0
- [x] 0.2 Spike on the local controller, recorded in `design.md` Context: does a task's
      `dry_run` flag reach `ansible-playbook` as `--check`, does `diff` reach it as `--diff`,
      and does a task-level `git_branch` override run that branch's tree for a template bound
      to `main`. 2026-09-22: yes to all three, from source at the running commit; the
      override is not gated server-side (ledger 1.9); live confirmation rides on section 2
- [x] 0.3 Pin the Semaphore image (compose uses `:latest`) to the version the spike ran on.
      2026-09-22, operator decision: pinned one release behind latest instead, `v2.19.11`
      (latest `v2.19.12`; the compare shows one file and no migration between them). The
      spike read v2.18.12, the local controller's version; the flag and branch code it cited
      is re-checked against v2.19.11 in 0.6.
      BLOCKED 2026-09-22: production's running version is readable only through the
      authenticated `GET /api/info` or the UI; pinning blind could downgrade it across its
      database migrations. Operator reads it, then pin at or above it
      2026-09-23 (PR 203 review): the pin no longer depends on that read. Semaphore's
      deploy.sh reads the running controller's version and refuses a pin older than it
      (test_semaphore_downgrade.bats). A production redeploy is therefore safe to attempt,
      but it stops rather than downgrading.
- [x] 0.4 Verify NetBox virtual machines accept custom fields on the pinned NetBox version and
      record the API used to create them. 2026-09-22: yes, see `design.md` Context
- [x] 0.5 Verify agentgateway passes `response_format` with `type: json_schema` through to vLLM
      unchanged (schema-constrained request through the local gateway, then direct).
      2026-09-22: source-level yes at v1.5.0 (`design.md` Context); the live request needs an
      enrolled client key, which has no sanctioned workstation path, so it runs in 3.3
- [ ] 0.7 Local-dev Semaphore to v2.19.11: bootstrap with `-e
      local_semaphore_image=docker.io/semaphoreui/semaphore:v2.19.11-ansible2.16.5`, confirm the
      sqlite store starts (the recorded v2.19-beta panic is gone), then move the default in
      `bootstrap-local-dev.yml`; keeps local and production on one branch-override behaviour
- [x] 0.6 Validation gate: `openspec validate service-deployment-workflow --store agent-cloud`
      passes and 0.2 to 0.5 each carry a dated evidence line; proves no scenario yet and
      unblocks every later section

## 1. Ansible standards and check-mode patterns

- [x] 1.1 Write `plan/architecture/08-ansible-automation-standards.md` from the official
      pages (check and diff mode, variables, inventory, error handling, roles, tips and
      tricks, sample setup, ansible-lint, `set_stats`, `uri` module attributes), each rule
      linked to its source URL, then the platform conventions on top; add its row to the
      architecture index in `00-foundation-standards.md`
- [x] 1.2 Reconcile automation docs with it: `platform/playbooks/README.md`, root `AGENTS.md`
      (Independent Workflows `-e dry_run=true` references), plan 15 (`STEP-RESULT` line
      replaced by `set_stats`), `plan/architecture/01-automation-model.md` where it describes
      dry runs
- [x] 1.3 `tasks/emit-step-result.yml`: one `ansible.builtin.set_stats` call carrying the
      step-result fields; `ANSIBLE_SHOW_CUSTOM_STATS=true` in both controllers' environment.
      2026-09-22: done as a repo-root `ansible.cfg` (`show_custom_stats = True`) instead of an
      env var per controller, since Semaphore runs from the clone root; proven by
      `platform/tests/test_emit_step_result.py` in normal and check mode (mutated once: red).
      2026-10-03: a second, aggregating `set_stats` appends each result to a `step_results`
      list so one run can record several steps (Provision VM now records `cloud-init` from its
      post-boot checks); the collector reads the list and falls back to the single
      `step_result` older task output carries. Proven by `test_emit_step_result.py` and
      `test_vm_lifecycle_step_results.py` (parser and cloud-init skip mutations: red)
- [x] 1.4 pytest check-mode guard: flags state-changing `command`/`shell`/non-GET `uri`
      without `when: not ansible_check_mode` or `check_mode`, and read-only `uri` GET without
      `check_mode: false`; seeded with an allowlist of every current violation so it passes
      today and shrinks per wave. 2026-09-22: `platform/tests/test_check_mode_contract.py` +
      `check_mode_allowlist.txt` (96 of 145 files, 480 tasks); a write under
      `check_mode: false` is also a violation, because it runs for real in a dry run
- [x] 1.5 Mutate once: add an unguarded write to a fixture playbook and watch 1.4 go red
- [x] 1.6 Validation gate: spec scenarios "A reader finds the standard" and "Unguarded write
      is caught" pass

## 2. Check mode on every playbook

- [x] 2.1 Wave 1, registry executors (the templates named in plan 15's step table): apply the
      three patterns; tag verification `verify`; add the legacy `dry_run` mapping to the three
      playbooks that accept it; remove each from the 1.4 allowlist. 2026-09-22: 32 files, allowlist 96 -> 67.
      Deprecated `dry_run` only where it defaulted to false (create-netbox-device); the runner
      group and NetBox cleanup keep `dry_run=true` as their safety default (spec scenario
      "A dry-by-default playbook keeps its safety default")
- [ ] 2.2 Run every wave-1 playbook on local-dev three ways (normal, `--check`,
      `--tags verify`) and record the result per playbook in `design.md`. 2026-09-22: the
      local-runnable ones are recorded (design "Wave-1 check-mode results"); `--tags verify`
      is proven by `--list-tasks`, since Semaphore cannot launch a tag override. REMAINING:
      a production dry run of each Proxmox, SSH-host and backup playbook's `(Dev)` template
      after this branch reaches `dev` (operator decision)
      - 2026-10-04: production `(Dev)` dry runs on `dev` at 21891372 against o11y. The task ids
        are in design "Production `(Dev)` dry runs — 2026-10-04".
        - Pass: Verify Host Access, Distribute SSH Keys (after its missing key was generated),
          Provision VM, Resize VM, Back Up Service SSH Key (after the key existed) and Back Up
          Credentials.
        - Still open: Apply Firewall fails its check-mode verify (fix in a separate PR); Harden
          SSH waits on the pre-proof change; Destroy VM is held for the operator. Snapshot VM
          and Create VM Template have no `(Dev)` variant. 2.2 stays open.
      - 2026-10-05: Apply Firewall (Dev) dry run passes (2756, after #432); Harden SSH (Dev) dry
        run records `skip` with `key_only_proven` true and a would-change list (2859, after #430
        and #439). Destroy VM is still held for the operator; Snapshot VM and Create VM Template
        still have no `(Dev)` variant. 2.2 stays open. Task ids in design "Production `(Dev)` dry
        runs — 2026-10-04".
      - 2026-10-05: Snapshot VM and Create VM Template now declare `dev_variant: true` in
        `platform/semaphore/templates.yml`, so `Snapshot VM (Dev)` and `Create VM Template (Dev)`
        exist in the catalog. Neither is published to Semaphore nor dry run yet; Destroy VM is
        unchanged and still held for the operator. 2.2 stays open.
      - 2026-10-06 (task ids and output reported by the coordinator; the task output was not
        read here): Snapshot VM (Dev) dry run 3178 passes, reporting it would snapshot the VM
        and that nothing was written (after #462). Create VM Template (Dev) dry run 3195 fails at
        the template read-back step, reporting the template has no cloud-init drive on `ide2`
        (diagnostic PR #464 pending). Destroy VM is still held for the operator. 2.2 stays open.
      - 2026-10-07 (task ids and output as quoted by the coordinator; the task output was not
        read here), all `(Dev)` templates: Validate Proxmox Cluster (Dev) dry run 3284 and
        Deploy agentgateway (Dev) dry run 3285 both succeeded at `dev` `8dc61584`, showing the
        run-start extra-variable guard (#459) passes honest production runs. Create VM Template
        (Dev) dry run 3287 at `cf765af8` recorded step result `fail` with the message
        "template 9000 has no cloud-init drive on ide2 (cloudinit volume found on: ide0)". The
        cause is not established. `provision-template.yml` has attached the cloud-init drive on
        `ide2` since its first monorepo commit (`4d4cfee0`; `ide0` there is the installer
        cdrom; current lines `:180` and `:240-242`), so the live template 9000 was built some other way
        (by hand or by another tool); an older version of this playbook is not the explanation.
        DECISION FOR THE OPERATOR: rebuild the template, or make the playbook accept any drive
        key. 2.2 stays open.
- [x] 2.3 Wave 2, the remaining playbooks, grouped by service; same patterns and runs.
      2026-09-22: 67 files (161 reads, 27 logins, 125 skips) applied from the guard's own
      findings; normal runs are unchanged by construction (every guard is inert without
      --check), BATS 0 failures, pytest 339, ansible-lint and syntax-check clean. Check-mode
      RUNS of wave 2 are not done: each is proven when its service is next dry-run
- [x] 2.4 Allowlist in 1.4 is empty; the guard now fails on any new violation
- [ ] 2.5 Validation gate: spec scenarios "Dry run changes nothing", "Read-only probe is not
      skipped", "Legacy dry-run argument still works" and "Verify-only run" pass

## 3. Local-dev agent runtime (skynet)

- [x] 3.0 Entry gate: `git ls-tree origin/dev platform/services/agentgateway` lists the service
      (the gateway implementation has merged to `dev`); rebase this branch onto `dev`
      - 2026-10-02, `origin/dev` at `31c1aa7c25e9a937807fe2a131623ebceb0b0f48`: `git ls-tree`
        lists `platform/services/agentgateway` as a tree (`e07dd329`), holding
        `context/architecture.md` and `deployment/` (`README.md`, `compose.yml`,
        `compose.local.yml`, `compose.tls.yml`, `deploy.sh`, `gateway-addr.sh`,
        `templates/config.yaml.j2`, `templates/env.j2`). The gate condition holds.
      - The rebase half does not apply as written: this change has no long-lived branch. Each
        increment is its own branch cut from `origin/dev` and merged by pull request, so every
        later section-3 branch starts from a `dev` that already carries the gateway.
      - 2026-10-03: recorded by PR #395 (merged)
- [ ] 3.1 Prove the DGX API is reachable from inside the local controller's container (not
      only the host); record the result; if unreachable, record the degraded path in design
- [ ] 3.2 Local inventory: agentgateway upstream set to the DGX API in `local-dev.yml` only;
      `vllm_api_key` seeded into local OpenBao with `Seed OpenBao Key` from the environment
      secret, never an argv; `agw_clients` gains the four role identities and `skynet-eval`
- [ ] 3.3 agentgateway route for skynet's orchestration API on the default gateway, key-gated,
      restricted to the operator identity; deploy and verify locally. The verify also sends
      one `response_format: json_schema` request through the gateway to the DGX and asserts
      the reply parses against the schema (live half of 0.5)
- [ ] 3.4 **[skynet]** Drop Bifrost from the agent-cloud path: Tier 2 calls the gateway's `/v1`
      with the calling role's key; Tier 1 stays only for skynet-local models
- [ ] 3.5 **[skynet]** Postgres checkpointer in place of the in-memory saver; its own small
      Postgres in skynet's compose
- [ ] 3.6 **[skynet]** A deployable image and compose file for Tier 2
- [ ] 3.7 `skynet_svc` in `local-dev.yml.example`, `deploy-skynet.yml` and
      `clean-deploy-skynet.yml` from the shared tasks, local templates in
      `templates-local.yml`; secrets through manage-secrets; BATS test
- [ ] 3.8 Validation gate: spec scenarios "Agent calls are attributable per role",
      "Orchestration route is key-gated", "Restart mid-run" and "No private address in the
      public repo" pass on local-dev

## 4. Registry, contracts and OPA

- [x] 4.1 `platform/workflows/service-onboarding/registry.yml` with the twenty-two entries
      from plan 15, `reviewed: null` everywhere
- [x] 4.2 Schemas under `platform/workflows/service-onboarding/schemas/` (moved out of the OPA policy
      tree, where `opa test` rejected them with `merge error`): step result, proposal
      envelope, service assessment, firewall policy, access policy, plus a shared verdict
- [x] 4.3 Registry test: every named template exists in the catalog, every reasoning step has
      a snapshot template and schema, ids match the diagram
- [x] 4.4 `data.json`: four identities with `allowed_actions` and `allowed_templates`;
      `netclaw` and `nemoclaw` frozen with a comment
- [x] 4.5 Rego: template allowlist rule; firewall content rule (controller SSH source kept,
      SSH never wider than the declared sources); service-assessment content rule (no
      destructive runtime action, VM spec within tier bounds); branch rule (no `main` for an
      unreviewed step); rego tests with fixtures. 2026-09-22: 36/36 in OPA 1.0.0, and CI now
      runs `opa check --strict` and `opa test`; `not a <= b` with `b` undefined fails OPEN
      in Rego, so the bounds rule negates a helper built with `every`
- [x] 4.6 `firewall_controller_cidr` inventory variable and an `apply-firewall.yml` assertion
      that it is in the SSH allow set before enable; BATS test. 2026-09-22: checked when set,
      warned when absent, because making it required would fail every live Apply Firewall
      run until site-config declares it. Follow-up: declare it per host in site-config, then
      make it required
- [x] 4.7 Validation gate: spec scenarios "Registry and catalog agree", "Role launches only
      its own steps", "Destructive template still needs a human", "Orchestrator keeps SSH",
      "SSH stays scoped" and "Unreviewed step cannot run from main" pass

## 5. Semaphore environments

- [x] 5.1 PENDING one live launch of a `dev` task by branch on a base template. On v2.19.11 the
      template must set `allow_override_branch_in_task` (design Context). Then: set it in
      `setup-templates.yml`, remove `dev_variant` generation, launch on `dev` by branch; update the operating guide. If not:
      record the result and keep the twins (design risk entry)
      - 2026-09-26: still pending, and the catalog keeps growing twins: 62 `dev_variant`
        declarations on dev, 47 before 2026-09-25. Every production step of 7.2, 7.5 and 7.9
        ran through a `(Dev)` twin, because `main` lags `dev` by several hundred commits
      - 2026-09-28: 67. Five workflow step templates had no twin at all (Lookup Service
        Inventory, Validate Address Free and the three snapshots), so none could run in
        production; `test_workflow_templates_are_dev_bound_until_promoted` now requires a
        twin for every step template that does not predate the workflow
      - 2026-10-04: NOT APPLICABLE by operator decision (Joseph A. Wisneski IV): keep the
        `main`/`dev` twin templates and do not enable `allow_override_branch_in_task`
        (`docs/MISTAKES.md` 1.9 risk). No launch by branch is attempted. Left unticked, because
        this file records no precedent for ticking a not-applicable item
      - 2026-10-08: closed as NOT APPLICABLE and ticked, on the operator decision recorded
        in the 2026-10-04 line above (Joseph A. Wisneski IV): the `main`/`dev` twins stay and
        `allow_override_branch_in_task` is not enabled, so there is no launch by branch to
        make. The decision text is the one in that line; nothing new is asserted here. The
        spec delta "One template per playbook" is amended to match (see 5.3). Ticking a
        not-applicable item is a new convention for this file (the 2026-10-04 line found no
        precedent); it is the coordinating session's call, flagged for the operator to reverse.
- [x] 5.2 Test that no `templates-local.yml` entry reaches the production catalog
      (`platform/tests/test_local_templates_isolation.py`, mutated once: red)
- [x] 5.3 Validation gate: spec scenarios "Integration run without a twin" and "Local template
      cannot reach production" pass (the first is marked not-applicable if 5.1 kept the twins)
      - 2026-10-04: "Integration run without a twin" is NOT APPLICABLE: 5.1 kept the twins by
        operator decision. "Local template cannot reach production" is covered by 5.2's test,
        but this gate has not been run as a whole. Left unticked
      - 2026-10-08: ticked. "Integration run without a twin" is not applicable by 5.1, and the
        spec requirement is amended to "One base template per playbook, with a dev-bound
        variant" (scenarios "Integration run on the dev variant" and "A task cannot choose its
        own branch"). "Local template cannot reach production" is covered by
        `platform/tests/test_local_templates_isolation.py`, which passed 4 of 4 on 2026-10-08

## 6. Local NetBox

- [x] 6.1 Execute plan 04's local-engine fix: app tier under podman through the local
      controller; `netbox_svc` in `local-dev.yml.example`; local Caddy route behind Authentik.
      2026-09-22: full stack (NetBox + Diode + Hydra) deployed through the local Semaphore,
      tasks 982 and 985 (two consecutive successes); fixes recorded in design
- [x] 6.2 Discovery allowlist: the deploy reads the local podman networks' subnets, refuses
      any declared target outside them and disables discovery when none are declared.
      2026-09-22: `tasks/assert-local-discovery-scope.yml` + `lib/discovery_scope.py` (pytest);
      against the live engine: in-scope enabled, out-of-scope refused naming the target, none
      declared disabled; the live orb-agent deploy (1013) passed the check with a local target
- [x] 6.3 Orb agent on the local rootful socket; if the capabilities it needs are refused,
      keep discovery disabled and record why. 2026-09-22: runs privileged on the local socket
      (1013), subnet_scan applied, pfSense/Proxmox workers off; its dry run leaves it running
      (1014) after ledger 5.9. Needed: the local controller's policy is production's file
      (the inline fork lacked AppRole management), and manage-approle's dead sys/auth check
      is gone (it failed every run, production included)
- [x] 6.4 NetBox custom fields for the collector created by a playbook, locally.
      2026-09-22: via the Django shell (the scoped token has no schema permission, 403);
      created in task 995, dry and real re-runs unchanged (997, 998). Also fixed the token
      mint for NetBox 4.5 (no is_staff; v2 tokens stored as nbt_<key>.<token>, sent as Bearer)
- [ ] 6.5 Validation gate: spec scenarios "Local deploy is repeatable", "Target outside
      local-dev is refused", "No targets means no discovery" and "Local targets are
      discovered" pass

## 7. Executors, snapshots and tracking

- [ ] 7.1 D10 review of each existing executor against its registry criteria: idempotent
      rerun, result emitted, undo named; stamp `reviewed`
      - 2026-10-02: reviewed (`d10-review.md`). Four pass every check (lookup-inventory,
        validate-address, secrets-approle, service-validate); twelve fail because their
        playbooks record no step result, and systemd-enablement fails on a missing group
        preflight and a run_once emitter fed per-host errors. Each entry carries `review_gap`. The
        four passing stamps wait on the matching OPA `data.json` workflow_steps change.
      - 2026-10-03: review merged in PR #397: 4 pass, 13 fail (twelve with no step result, plus
        systemd-enablement). Stamps wait on the OPA `data.json` change and a decision on the `main` run
      - 2026-10-03: the missing emits landed — access executors (PR #413), VM lifecycle
        executors (#416), service executors (#417), and several results per run with
        `provision-vm.yml` recording `cloud-init` (#419). The per-service deploy step and
        `deploy-authentik.yml` outside its `oidc-config` step still record none. The D10
        re-review against these merges is PR #421 (open).
      - 2026-10-03: the D10 re-review itself (PR #421; `d10-review.md`). Eight more pass
        (vm-template, provision-vm, ssh-keys, ssh-key-backup, vm-rightsize, fw-harden,
        systemd-enablement, credential-backup). Three are gaps that wait on an operator decision:
        access-harden proves key-only access only after password authentication is withdrawn;
        edge-route delegates the Cloudflare zero-diff criterion (it also gained a
        populated-group preflight that records a failed edge-route result); oidc-config
        recreates every container on each run. cloud-init and service-deploy were not
        re-reviewed. Stamps still wait on the OPA `data.json` change.
      - 2026-10-04: operator decisions (`d10-review.md`). The Cloudflare zero-diff criterion
        moved out of edge-route into a new step, edge-dns, executed by Apply Cloudflare Tofu,
        which now records it (`plan_changes`; judged on `tofu plan -detailed-exitcode`, after
        an apply too). Twenty-three steps. edge-route, edge-dns and cloud-init pass D10. No step
        was stamped: a stamp lets the main-bound base template run, and on `main` no executor
        records a step result and six executor playbooks do not exist, so stamps wait on the
        dev to main promotion.
      - 2026-10-05: re-review on `dev` at 95a498a3 (`d10-review.md`). access-harden now passes
        (key-only login proven before anything is edited, #430; a dry run records `skip`, #439);
        oidc-config passes (recreate only on change, #438 and #444; live: Deploy Authentik 2899
        reported no change after the forced recreate 2896); edge-route still passes. edge-dns
        stays a gap: a dry run's `tofu init` writes `.terraform/`. No stamp, for the same
        `main` hazard.
      - 2026-10-05: edge-dns dry-run gap closed in the executor (`d10-review.md`, "edge-dns
        dry-run gap closed"). Under `--check`, init, plan and show run as one command in its
        own `mktemp -d` root with `TF_DATA_DIR` pointed into it and `-lockfile=readonly`, the
        shape the check-mode contract already accepts; a real run is unchanged. Proven with a
        fake tofu (tofu root byte-identical, throwaway root removed); a dry run against real
        tofu and R2 is still to do. No stamp, for the same `main` hazard.
      - 2026-10-06: edge-dns live dry run done (`d10-review.md`, "edge-dns live dry run"). Apply
        Cloudflare Tofu (Dev) dry run 3006 against real tofu and the R2 backend recorded
        `init_rc` 0, `plan_rc` 0, `show_rc` 0, `plan_changes` 0 and an edge-dns `pass` with
        `check_mode` true (after #454). edge-dns passes D10 with no pending proof. No stamp, for
        the same `main` hazard; service-deploy is still not re-reviewed, so 7.1 stays open.
      - 2026-10-08: seventeen passing steps stamped `reviewed` (`d10-review.md`, "Stamped —
        2026-10-08"), in the registry and in OPA `data.json`. The hazard is gone: `dev` was
        promoted to `main` on 2026-10-07 (`origin/main` c81ada2a), and at `origin/dev` a2224685
        every executor playbook is byte-identical on `main` and `dev`, includes
        `tasks/emit-step-result.yml` on `main`, and none of its included tasks differs. Not
        stamped: service-deploy (still not re-reviewed), and the reasoning and planned steps.
        7.1 stays open for service-deploy. OPA must be redeployed to pick up `data.json`.
- [x] 7.2 `provision-vm.yml` sets `onboot`; restart-policy check beside `enable-linger`.
      2026-09-22: onboot with per-host opt-out; `verify-service-persistence.yml` (step
      systemd-enablement) passes on local tududi, normal and check mode (tasks 977, 978)
      - Production, 2026-09-26 (PRs #282, #283, #284, #285; site-config #32, #33). The setup
        half is `ensure-service-persistence.yml` (restarts nothing); every step was a dry run
        first, through `(Dev)` templates. Verify Service Persistence passes on all eight
        services in scope: authentik 1473, n8n 1510, o11y 1481, openbao 1519, semaphore 1520,
        caddy 1521, honcho 1562, tududi 1564.
        - Rootless (authentik, n8n, o11y, honcho, tududi, caddy): linger and podman's user boot
          unit, every container `always`.
        - openbao and semaphore run rootful podman from legacy standalone directories, on
          `unless-stopped` or no policy. The Ubuntu 24.04 podman (4.9.3) cannot change a policy
          in place, so each gets `agent-cloud-boot-<service>.service` (a oneshot `podman start`
          by name, enabled, never started), which Verify reads back. Inventory declares
          `podman_rootful` and `compose_working_dir` for them, and `compose_working_dir` only
          for caddy (rootless, legacy directory).
        - honcho, n8n and tududi were found stopped (state `created`) and redeployed through
          `Deploy <service> (Dev)`: n8n 1507/1508, honcho 1557/1559, tududi 1558/1560.
          authentik's server was stopped too: 1469/1471.
        - Out of scope: Postiz (no containers in production), devlog (skipped). `grafanapodman`
          is retired.
        - OPEN: no boot path has been exercised by a real reboot (the registry's optional
          reboot test).
- [ ] 7.3 New executors: inventory lookup, address validation against pfSense ARP and NetBox,
      NetBox VM record, host instrumentation (after `inference-telemetry-production` lands the
      OTLP receiver)
      - 2026-09-23: `lookup-service-inventory.yml`, `validate-address-free.yml` (ARP + the
        NetBox VM record, `vm-recorder` token profile) done. Local: token mint task 1184 (two
        permissions read back exactly), lookup task 1185 records its refusal, collector 1186
        reports it. The ARP and Proxmox reads can only run against production (local-dev has
        neither), so they are proven by the evaluated BATS test and wait for a `(Dev)` dry run.
        OPEN: `instrument-host-o11y.yml`, blocked on the OTLP receiver.
      - 2026-10-03: blocker restated — the receiver exists in code:
        `platform/services/o11y/deployment/templates/config.alloy.j2:136`
        (`otelcol.receiver.otlp "traces"`) and `:202` (`"conformance"`). What remains open is
        `instrument-host-o11y.yml` itself (no such playbook exists yet).
      - 2026-10-05: `instrument-host-o11y.yml` built, template `Instrument Host Observability`
        (`(Dev)` variant), registry executor, OPA `o11y-agent` grant. It runs the receiver's own
        bounded node exporter on each host of the group (host network, declared
        `o11y_host_exporter_bind`), probes it from the receiver before writing
        `config/scrape.d/host-<group>.yml`, reloads Prometheus (restoring the previous file on a
        rejected reload) and requires `up` plus `node_memory_MemAvailable_bytes` per host. Host
        metrics only: the "Alloy on the host" half of the plan row (host logs over OTLP) is not
        built. Proven by `platform/tests/test_instrument_host_o11y.py` (real playbook, fake engine
        and Prometheus), not live. OPEN: the `(Dev)` check-mode run and an apply against one
        enrolled host; production enrollment also waits on the estate observability baseline
        (`estate-wide-observability-instrumentation` 1.3/1.4). Undo is `none`.
      - 2026-10-08: `lookup-service-inventory.yml` and `validate-address-free.yml` have run
        against production hosts: Lookup Service Inventory tasks 1741/1742 and Validate Address
        Free tasks 1749/1750, 2026-09-28, recorded in `production-internal-ca` task 1.2
        (`production-internal-ca/tasks.md:37-41`) for the DNS and CA hosts. Not re-read from
        Semaphore here. `instrument-host-o11y.yml` remains open (see 2026-10-05 above), so this
        task stays unticked
- [x] 7.4 Snapshot templates for service, firewall and access assessment; each verify-only,
      emitting one JSON document. 2026-09-22: all three pass on local tududi in normal and
      check mode (tasks 971-976); the document is recorded with set_stats under `snapshot`
- [ ] 7.5 Collector (scheduled), NetBox custom-field writes, Loki push, Grafana dashboard JSON,
      read-only report; single-writer test
      - Local, 2026-09-22: collector tasks 1023 (dry run) → 1035. Loki push, the Grafana
        dashboard (`service-conformance`, all four panel queries answered through Grafana)
        and the report (the collector's dry run) are proven on a real failure (`dns`
        secrets-approle, task 1034). OPEN: the NetBox write path has never run, because
        local NetBox holds no VM record (needs 7.3's NetBox VM record executor). The table
        transformations have been checked against Grafana 11.4 source but not viewed in a
        browser.
      - Changed after that run, not yet re-proven live (PRs #253, #258 and the grounding
        follow-up): each template's history is read from `/templates/{id}/tasks` (newest 1000,
        was `/tasks/last`, 200); the NetBox lookup runs BEFORE the aggregate, and each VM's
        stored status is merged under this run's results as `retained` (registry step ids
        only), so NetBox, the report and Loki agree; a full window marks the services it can
        hide `history_incomplete` (dashboard panel "History incomplete").
      - Local, 2026-09-25, on dev 90a99ea (local Semaphore v2.18.12): dry run task 1906
        (ok=29, failed=0) and real run task 1907 (ok=30, changed=1, failed=0). Order proven:
        NetBox lookup → sort → aggregate → write. All 13 inventoried services were looked up
        (`netbox_no_vm` lists all 13, none unreachable, none ambiguous); `history_window_full`
        empty. tududi's three snapshot results land in `inputs`, not conformance; Loki took
        the real run's push (`status` counts over 10 minutes: no_history 24, pass 4 — the
        snapshot passes no longer counted). The NetBox write had nothing to write there, since
        local NetBox holds no VM record for any service.
      - Production, 2026-09-25, on dev a12bb77 (PR #267: the collector and custom-fields
        templates gained `(Dev)` copies, the token template a Token profile survey field). Each
        step was a dry run first, through the Publish Semaphore Template Surveys (Dev) publisher
        and then the templates themselves:
        - survey update for Provision NetBox Automation Token (Dev): 1329, 1330;
        - scoped create of Provision NetBox Custom Fields (Dev): 1331, 1332;
        - scoped create of Collect Service Conformance (Dev): 1333, 1334;
        - custom fields: 1335, 1336, re-checked by 1337 (all three unchanged);
        - `workflow-collector` token minted into `collector_api_token`: 1338, 1339;
        - collector: dry run 1340, real run 1341 (ok=30, changed=1, failed=0).
        The contemporaneous task record describes PATCHing workflow custom fields on seven
        existing VM records (caddy, n8n, nemoclaw, netbox, nocodb, openbao, semaphore); it
        records no create or replacement. Seven services have no VM record; none was recorded
        unreachable or ambiguous. Re-read task 1341's raw report before relying on its
        `netbox_written` receipt. The record listed four failures from history:
        agentgateway and github-runner provision-vm, authentik oidc-config, postiz
        secrets-approle. OPEN:
        - the production Loki push: `collector_loki_url` is not set in the production
          inventory, so the push was skipped;
        - the source schedule was attached to the unsuffixed base while the generated (Dev)
          copy carried none; live Semaphore had only the (Dev) template and no schedule.
        - 2026-09-26: the collector has not run since task 1341, so NetBox and Loki do not yet
          carry the systemd-enablement and service-validate results recorded that day (7.2, 7.9).
      - Correction plan, 2026-09-29: the collector record for task 1341 says `loki: skipped`;
        its NetBox write summary needs raw-output read-back before it is treated as verified.
        The Grafana panels therefore show no recent samples, and the failure stat's `noValue: 0`
        can misstate missing telemetry as healthy. Source declared the
        schedule on the unsuffixed base, but live Semaphore has only the `Collect Service
        Conformance (Dev)` template (ID 234) and no schedule. Keep that exact name, bind it
        directly to `agent-cloud dev`, and attach its one schedule through the explicit,
        single-template Dev schedule opt-in on the controller publisher. Do not use the
        unavailable full-catalog publication path. Make missing Loki samples read as `No data`; in
        Service Overview use the minimum target health per service, list failed targets, and retain
        the existing Prometheus-derived selector scope for metric-enabled services. Initial
        private inventory read-back showed no controller-to-Loki ingress or collector URL.
        Implementation on `feature/conformance-otlp-ingress`: a dedicated Alloy OTLP/HTTP log
        receiver now listens on port 4318, separate from gateway OTLP/gRPC on 4317. The
        production collector requires its URL to exactly match the private `o11y_otlp_bind`
        and `/v1/logs`, posts only to Alloy, and fails visibly on delivery errors. Task and
        error details remain in the record body; Alloy applies `job`, `service`, `step`, and
        `status` as conformance-specific Loki labels, and the shared Loki writer also adds
        `cluster` and `environment`. Local development retains direct Loki push. The
        private site-config URL and controller-CIDR firewall rule are a separate companion
        change;
        live delivery and Loki read-back remain unverified until both changes are reviewed,
        synchronized into Semaphore inventory, and deployed.
      - OPEN (review of PR #195): the spec's collector reads Semaphore, Prometheus and
        NetBox; this collector reads Semaphore and NetBox only. The Prometheus read is not
        implemented because the two steps it would evidence (`instrument-host`,
        `instrument-service`, criteria "series present") have no executor yet (registry
        `executor: null`). It lands with those executors; until then those steps show no
        result, never a pass.
      - 2026-10-07: read-only Dev-bound Semaphore task 3351 returned data for the
        provisioned `Services tracked` and `Step status by service` queries over 24h.
        It did not prove the Grafana table renders correctly. Collector runs can overlap
        inside the dashboard's 16-minute lookback; the read model now selects the newest
        numeric state, while rendered-table validation remains open.
      - [ ] Validate the latest-state conformance read model after a non-destructive Dev-bound
        o11y update: confirm overlapping collector snapshots select the newest state and verify
        the rendered Grafana matrix.
      - [x] PR #476 review follow-up (2026-10-07): set `loki.format=raw` on the conformance
        pipeline and add a cross-file contract test tying the collector's JSON body, Alloy
        exporter format, and dashboard's `state_code` / `inventory_code` extraction together.
      - [x] Production acceptance: deploy the reviewed change through Dev-bound Semaphore and
        query both step and inventory records to prove top-level JSON extraction in Loki.
        2026-10-08: ticked (task ids as reported by the operator session; not re-read from
        Semaphore here). `Deploy o11y (Dev)` dry run 3527, real run 3528 at a2224685 reported
        "o11y healthy". `Verify o11y Dashboard Data (Dev)` task 3529, `service-conformance`, 1h:
        step records (`state_code`): "Latest step status by service" 29 series; inventory
        records (`inventory_code`): "Services not yet run" 5, "Services tracked" 1; also "Current
        failed step states" 1, "Recent failed task snapshots" returned data, and "History
        incomplete" was empty by its `== 2` filter (no history window full). Collector schedule
        live: tasks 3493, 3495, 3500, 3510. The "Validate the latest-state conformance read
        model" item above stays open: the rendered matrix needs an operator browser check.
- [ ] 7.6 **[skynet]** Role packs, `service_onboarding` graph built from the registry, proposer
      wiring with the three schemas, eval harness with thresholds in CI
- [x] 7.7 `agent-practices.md` for agentgateway
      - 2026-10-02: `platform/services/agentgateway/context/agent-practices.md`, the path the
        three snapshot playbooks read. Ports, both auth surfaces, what healthy means (deploy
        Phase 3), templates, what OPA lets each role launch, secrets boundary, undo path and
        known quirks, each citing its source file. Open for review: the access schema takes one
        `auth_mode` while the service has two surfaces (`api_key` on `/v1`, `oidc` on the UI).
      - 2026-10-03: landed in PR #395 (merged)
- [ ] 7.8 Validation gate: spec scenarios "Passing step records its evidence", "A failure with
      no result is still recorded", "Invalid proposal never executes", "Assessment sees
      earlier steps", "Failure appears within one interval", "NetBox outage does not block
      deployment", "Only the collector writes status", "Unreviewed step is visible" and
      "Regression blocks a prompt change" pass on local-dev
- [x] 7.9 Health contracts in the generated local inventory (`bootstrap-local-dev.yml`): a
      `service_url` + `health_path` per local service, each read from the service's own verify
      step and reachable from inside the local Semaphore container. Until then `Verify Service
      Health (Local)` fails closed with "no declared service_url + health_path" (PR 203 Codex
      review). Done for step-ca, Authentik, o11y and agentgateway as `health_url`, each
      verified 200 from inside the local Semaphore container; Caddy, OpenBao and the other
      local services remain undeclared and fail closed by name
      - Production counterpart, 2026-09-26 (PR #283, site-config #32). A host whose port is
        loopback-only or firewalled to the Caddy host sets `health_probe_on_host: true` with a
        loopback `health_url`, and the probe runs on the host. Verify Service Health (Dev) passes
        (HTTP 200) for authentik 1472, n8n 1509, o11y 1500, openbao 1522, semaphore 1523,
        honcho 1561 and tududi 1563. caddy declares no health path. Before the change, the
        executor's direct probe answered -1 for tududi, honcho and n8n however healthy they were
- [ ] 7.10 Connection identity is still settable from outside the run (open, recorded
      2026-10-07 from the PR #459 review; not implemented). `refuse-internal-extra-vars.yml`
      refuses `_` names and become/connection password variables (`ansible_password`,
      `ansible_*_pass`, `ansible_*_password`), but not the public
      connection variables: an extra var or a Semaphore environment JSON can set
      `ansible_host`, `ansible_port`, `ansible_user`, `ansible_connection` or
      `ansible_become_method`. Ansible host-key checking is off under Semaphore
      (`platform/playbooks/README.md`, `tasks/pin-ssh-host-key.yml` row), so a forged
      `ansible_host` sends the run, and the OpenBao-derived become password it hands to sudo,
      to a host the launcher chose. Proposed fix, to be designed: refuse those names as extra
      vars where the inventory is the only legitimate source, or pin the target's host key
      before the first connection that carries a credential, and test both with
      `forgeries.templated_forgeries`. Closed for the clean-service path (PR #470 and #473
      reviews): a forged `ansible_connection` can no longer move a teardown onto the controller
      or past Clean Deploy o11y's gates, and `local_monorepo_dir`, `local_mode`, `service_name`
      and `monorepo_deploy_path` can no longer choose what is deleted or run.
      `tasks/clean-service.yml` pins its inputs once, allows only an ssh connection (the
      collection prefix removed, so `ansible.builtin.local` and `ansible.legacy.local` do not
      pass) for a host that is not local-dev, takes the mode from host identity (every host
      of the play named `<service>-local`, read from `ansible_play_hosts_all`, which an
      extra var cannot set) instead of `local_mode`, and requires the host to be in
      `<service>_svc` with that service's own deploy path. The o11y gates are waived only for
      a local connection on such a host. `test_executor_internal_overrides.py` covers it with
      templated forgeries against a real run. Still open for the other executors: the
      connection-identity variables (`ansible_host`, `ansible_user`, `ansible_become_method`)
      and a connection forgery on any playbook that does not go through clean-service

## 8. Backfill agentgateway end to end

- [ ] 8.1 Local-dev run of the graph for agentgateway; record snapshots as eval cases
- [ ] 8.2 Production run when on the network; each finding becomes a pull request or a
      registry correction
- [ ] 8.3 Validation gate: spec scenarios "Declared state is right" and "Declared state is
      wrong" observed on the agentgateway run; dashboard green on every required step or a
      named finding per red step

## 9. Assessment sweep

- [ ] 9.1 Run the three reasoning steps in verify-only mode across every service with a deploy
      playbook; collector records; report generated
- [ ] 9.2 Triage findings into pull requests or recorded registry exceptions
- [ ] 9.3 Validation gate: spec scenario "Failure appears within one interval" holds across the
      estate and the report lists every service

## 10. Greenfield pilot and close-out

- [ ] 10.1 Operator picks the pilot service; all twenty-two steps pass, with its first
      configuration landing through pull requests the workflow opened
- [ ] 10.2 Update the diagram to the twenty-two steps, plan 15 status, root `AGENTS.md`
      workflow rows
- [ ] 10.3 On archive, retain the outcome (worked / dead end / corrected) into bank
      `agent-cloud-750a33b9`
- [ ] 10.4 Validation gate: `openspec validate service-deployment-workflow --store
      agent-cloud --strict` passes and spec scenario "Registry and catalog agree" holds for
      the pilot service's templates
