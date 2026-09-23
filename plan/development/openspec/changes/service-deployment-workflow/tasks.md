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
      `platform/tests/test_emit_step_result.py` in normal and check mode (mutated once: red)
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
- [x] 2.3 Wave 2, the remaining playbooks, grouped by service; same patterns and runs.
      2026-09-22: 67 files (161 reads, 27 logins, 125 skips) applied from the guard's own
      findings; normal runs are unchanged by construction (every guard is inert without
      --check), BATS 0 failures, pytest 339, ansible-lint and syntax-check clean. Check-mode
      RUNS of wave 2 are not done: each is proven when its service is next dry-run
- [x] 2.4 Allowlist in 1.4 is empty; the guard now fails on any new violation
- [ ] 2.5 Validation gate: spec scenarios "Dry run changes nothing", "Read-only probe is not
      skipped", "Legacy dry-run argument still works" and "Verify-only run" pass

## 3. Local-dev agent runtime (skynet)

- [ ] 3.0 Entry gate: `git ls-tree origin/dev platform/services/agentgateway` lists the service
      (the gateway implementation has merged to `dev`); rebase this branch onto `dev`
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

- [ ] 5.1 PENDING one live launch of a `dev` task by branch on a base template. On v2.19.11 the
      template must set `allow_override_branch_in_task` (design Context). Then: set it in
      `setup-templates.yml`, remove `dev_variant` generation, launch on `dev` by branch; update the operating guide. If not:
      record the result and keep the twins (design risk entry)
- [x] 5.2 Test that no `templates-local.yml` entry reaches the production catalog
      (`platform/tests/test_local_templates_isolation.py`, mutated once: red)
- [ ] 5.3 Validation gate: spec scenarios "Integration run without a twin" and "Local template
      cannot reach production" pass (the first is marked not-applicable if 5.1 kept the twins)

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
- [x] 7.2 `provision-vm.yml` sets `onboot`; restart-policy check beside `enable-linger`.
      2026-09-22: onboot with per-host opt-out; `verify-service-persistence.yml` (step
      systemd-enablement) passes on local tududi, normal and check mode (tasks 977, 978)
- [ ] 7.3 New executors: inventory lookup, address validation against pfSense ARP and NetBox,
      NetBox VM record, host instrumentation (after `inference-telemetry-production` lands the
      OTLP receiver)
      - 2026-09-23: `lookup-service-inventory.yml`, `validate-address-free.yml` (ARP + the
        NetBox VM record, `vm-recorder` token profile) done. Local: token mint task 1184 (two
        permissions read back exactly), lookup task 1185 records its refusal, collector 1186
        reports it. The ARP and Proxmox reads can only run against production (local-dev has
        neither), so they are proven by the evaluated BATS test and wait for a `(Dev)` dry run.
        OPEN: `instrument-host-o11y.yml`, blocked on the OTLP receiver.
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
- [ ] 7.6 **[skynet]** Role packs, `service_onboarding` graph built from the registry, proposer
      wiring with the three schemas, eval harness with thresholds in CI
- [ ] 7.7 `agent-practices.md` for agentgateway
- [ ] 7.8 Validation gate: spec scenarios "Passing step records its evidence", "A failure with
      no result is still recorded", "Invalid proposal never executes", "Assessment sees
      earlier steps", "Failure appears within one interval", "NetBox outage does not block
      deployment", "Only the collector writes status", "Unreviewed step is visible" and
      "Regression blocks a prompt change" pass on local-dev

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
