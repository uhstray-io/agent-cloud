# D10 review of existing executors (task 7.1)

Author: Joseph A. Wisneski IV — 2026-10-02

Design D10 (plan 15, "Tracking"): a step is trusted in the graph once its criteria are written
into the registry, an idempotent re-run is proven, its step result is recorded, its undo is
named (or `none`), and a test guards the emitter. This review applies those checks to every
executor step in `platform/workflows/service-onboarding/registry.yml`. Reasoning steps
(`service-assess`, `fw-assess`, `access-assess`) have no executor and are out of scope; the
two planned executors (`instrument-host`, `instrument-service`) do not exist yet.

Undo names were checked against the catalog by `test_named_templates_exist_and_planned_ones_do_not`;
the emitter guard is `test_review_stamp_requires_a_recorded_result_and_a_named_undo`: every
reviewed step, and every step whose `review_gap` records a passed review, must have a playbook
that includes `emit-step-result.yml` with its step id.

## Passing — all D10 checks hold

Per-host verdicts: lookup-inventory and validate-address run on `localhost` only; check-secrets
and verify-service-health refuse an empty target group in a preflight play and fold every
host's verdict into the one `run_once` result ("Collect the verdict across the target group").

| Step | Playbook | Re-run | Result emitted | Undo |
|------|----------|--------|----------------|------|
| lookup-inventory | lookup-service-inventory.yml | read-only: one NetBox GET (`check_mode: false`); BATS `test_address_steps.bats` refutes any write method | :106 | none |
| validate-address | validate-address-free.yml | the one write (NetBox VM POST) runs only when no record exists (`_vm_record \| length == 0`, :234) and never under `--check`; BATS "the one write is skipped under --check" | :246 | none (see note) |
| secrets-approle | check-secrets.yml | read-only: login POST `changed_when: false` (:96), then GETs | :190 | none |
| service-validate | verify-service-health.yml | read-only: one health GET | :86 | none |

Note: validate-address leaves a `planned` NetBox VM record behind with undo `none`. The
criterion allows it; a later step may want `Destroy VM` to retire the record too.

**Not stamped.** `reviewed` also drives OPA (`catalog.workflow_steps.<id>.reviewed` in
`platform/services/opa/deployment/policies/agentcloud/data.json`, kept equal by
`test_opa_step_map_matches_the_registry`), and a reviewed step may run from `main`. That file
is outside this change's scope, and three of the four executors (all but Check Secrets, per
`PREDATES_WORKFLOW` in the registry test) are new with the workflow, so `main` has no copy of
their playbooks until promotion. Each one carries a `review_gap` saying the stamp waits on that change.

## Failing

Except systemd-enablement, none of these playbooks include `tasks/emit-step-result.yml`, so
the collector cannot score them and they cannot report a failure. Each registry entry carries the gap as `review_gap`.

| Step | Executor | Gap |
|------|----------|-----|
| vm-template | Create VM Template (provision-template.yml) | no step result |
| provision-vm | Provision VM (provision-vm.yml) | no step result |
| cloud-init | Provision VM (provision-vm.yml) | no step result |
| ssh-keys | Distribute SSH Keys | no step result |
| ssh-key-backup | Back Up Service SSH Key | no step result; each run pushes a new branch |
| access-harden | Harden SSH | no step result |
| vm-rightsize | Resize VM | no step result |
| service-deploy | Deploy {service} | no per-service deploy emits; second-run no-change unproven |
| edge-route | Manage Caddy Sites | no step result |
| fw-harden | Apply Firewall | no step result |
| oidc-config | Deploy Authentik | no step result |
| systemd-enablement | Verify Service Persistence | emits at verify-service-persistence.yml:181, but no populated-group preflight (:22-23: an absent group exits 0 with no result), and per-host errors (:151-167) reach a `run_once` emitter (tasks/emit-step-result.yml:35,52), so a multi-host run records only the first host |
| credential-backup | Back Up Credentials to site-config | no step result; each run pushes a new branch |

Idempotency for the failing executors was not reviewed further: without a step result they fail D10 anyway.

Guard: `test_review_stamp_requires_a_recorded_result_and_a_named_undo` refuses a stamp on a
step no playbook emits for, a stamp beside a `review_gap`, and an unreviewed executor step
without one.

## Re-review — 2026-10-03

Author: Joseph A. Wisneski IV — 2026-10-03

PRs #413 (access executors), #416 (VM executors) and #417 (service executors) made the failing
executors record their step result. The same checks were re-run against `dev` at ce28cadf.
cloud-init was skipped because its step result is being changed in other work. service-deploy
was skipped because it waits on an open operator decision. Both keep their 2026-10-02 gap.

All undo names are unchanged. For every step below, `test_registry_evidence_keys_match_what_the_steps_emit`
pins the emitted evidence keys to the registry. Line numbers are on ce28cadf.

| Step | Playbook | Result emitted | Re-run | Group / failure handling | Guard | Verdict |
|------|----------|----------------|--------|--------------------------|-------|---------|
| vm-template | provision-template.yml | :245 | an existing template is adopted with no write (test `test_an_existing_template_is_adopted_without_a_write_and_proven`) | localhost; a dry run with no template records skip (:240, :250) | test_vm_lifecycle_step_results.py | pass |
| provision-vm | provision-vm.yml | :696, own play after the dry-run `end_host` (:687) | an existing VM that is ours is classified and not cloned again (:194-221); clone is `changed_when` on a real clone (:307) | localhost; a hard failure in the provisioning play is still recorded (test `test_a_hard_failure_in_the_provisioning_play_is_still_recorded`); undo `Destroy VM` passed to the emitter (:708) | test_vm_lifecycle_step_results.py | pass |
| ssh-keys | distribute-ssh-keys.yml | :339, controller play (:311) | `ansible.posix.authorized_key` (:152, :158) | the controller play folds every host's verdict (:322-336); an empty group or a host with no verdict fails (:343, :358) | test_access_executors_step_result.py | pass |
| ssh-key-backup | backup-service-ssh-key.yml | :228 | commits and pushes only a staged change (tasks/site-config-push.yml:26, :45), so a re-run with nothing new pushes no branch and evidence `branch` is empty (:233) | localhost; the rescue records a failure, with no_log details withheld (:216-222) | test_access_executors_step_result.py | pass |
| access-harden | harden-ssh.yml | :336, controller play (:308) | `copy`/`lineinfile` (:64, :71, :88); the sshd restart is skipped after a failed harden (:299) | the controller play folds every host's verdict (:319-333); an empty group fails (:340, :361) | test_access_executors_step_result.py | pass |
| vm-rightsize | resize-vm.yml | :558 | the config write is `changed_when: _cfg_changes \| length > 0` (:364); the verdict is judged on the re-read config (:539-555) | localhost; the rescue records a hard failure (:521) | test_vm_lifecycle_step_results.py | pass |
| edge-route | manage-caddy-sites.yml | :298 | restarts only when the block changed, a site was retired or the host is not yet served (:197-207) | **no populated-group preflight**: `hosts:` is the target group directly (:59), so an absent group exits 0 with no result. Per-host verdicts fold into the run_once result (:286-295) | test_service_executor_step_results.py | **gap** |
| fw-harden | apply-firewall.yml | :806, controller play (:783) | rules are added only when ufw reports added/updated (:503, :516) | an empty group is a recorded failure (:798); a host with no verdict fails (:791-794) | test_service_executor_step_results.py | pass |
| systemd-enablement | verify-service-persistence.yml | :214 | read-only (every probe is `changed_when: false`) | populated-group preflight (:26-41, tagged `verify`); per-host errors fold into the run_once result (:202-211), which closes the 2026-10-02 gap | test_persistence_group_verdict.py | pass |
| oidc-config | deploy-authentik.yml | :445 (verify failed), :510 (last) | **not no-change**: deploy.sh runs `compose up -d --force-recreate` on every run (services/authentik/deployment/deploy.sh:48), recorded as `changed_when: true` (:297) | preflight-target-group (:24-29); per-host verdicts fold (:433-442) | test_service_executor_step_results.py | **gap** |
| credential-backup | backup-credentials-to-site-config.yml | :245 | the same staged-only push as ssh-key-backup; evidence `branch` is empty when nothing was pushed (:251) | localhost; the rescue records a failure (:233-239) | test_access_executors_step_result.py | pass |

Two limits apply to every multi-host executor here, and the 2026-10-02 passing set had the
same ones. First, when every targeted host is unreachable or fails outside the rescued work,
Ansible ends the run before the result is recorded. The collector then records a failure
from the task output tail. Second, a `--limit` that excludes localhost skips the controller
result play.

**Not stamped.** As on 2026-10-02, `reviewed` stays null. Each passing entry's `review_gap`
says the stamp waits on the matching OPA `data.json` workflow_steps change.

Remaining gaps after this pass: edge-route (group preflight), oidc-config (second-run
no-change), and cloud-init and service-deploy (not re-reviewed).

### edge-route follow-up — 2026-10-03

The gap recorded in the table above (no populated-group preflight) is closed. The table row
stays as it was written.

- manage-caddy-sites.yml:63-87 adds a first play on `localhost`. It refuses a bare
  `target_service` group with no hosts. It records the refusal as a failed edge-route result
  (:73) and then fails the run (:84-87). Normal and check mode behave the same way.
- A compound pattern is not refused. rollback-inference-route.yml:455 passes
  `caddy_svc:!caddy_svc` on purpose in gateway-config mode so that the Caddy play is skipped.
  That importer already requires `caddy_svc` to have hosts (:85-89).
- Guard: `platform/tests/test_edge_route_group_preflight.py`. An absent group gives a non-zero
  exit code and exactly one edge-route `fail`, in normal and check mode. A populated group and
  the compound pattern pass the guard with no result. To mutation-check it, the group-size term
  was replaced with `false`: the two absent-group tests went red, and the file was restored
  byte-exact.

Verdict: edge-route **passes** all D10 checks. Its `review_gap` now records the pass. The
remaining gaps are oidc-config (second-run no-change, an operator decision), cloud-init and
service-deploy.

### Correction — 2026-10-03 (review of PR #421)

Two passes recorded above were wrong. The sections above stay as they were written. Both
steps are back to a dated gap in the registry.

- **access-harden does not pass.** The step's first criterion is "key-only access was proven
  before password authentication was withdrawn" (AGENTS.md Critical Deployment Rule 5).
  harden-ssh.yml turns off password authentication and restarts sshd (:70-99) before its
  first key-auth probe (:149-165). The executor therefore proves key access only after the
  password path is already gone. The proof made before that change lives in a separate
  template, Verify Host Access (verify-host-access.yml). Whether this executor must prove it
  itself, or may rely on that gate, is an operator decision. The first table missed this
  because its re-run column recorded idempotent writes and did not check the criteria's order.
- **edge-route does not pass.** The step's third criterion, "the Cloudflare plan is
  zero-diff", is delegated to Apply Cloudflare Tofu (manage-caddy-sites.yml:49-50) and is not
  in `evidence_keys`. The executor proves two of three criteria. The operator decides whether to
  split that criterion into its own step or to accept the delegation. The group preflight
  added in the follow-up stays.
- **Preflight fix.** The preflight had refused a bare target that names a host rather than a
  group, because a host name is a valid `hosts:` pattern but not a key of `groups`. It now
  resolves a bare name with the `ansible.builtin.inventory_hostnames` lookup. Compound
  patterns are still exempt, so the rollback's `caddy_svc:!caddy_svc` keeps skipping the play.
  A host-name case was added to the test. To mutation-check it, the lookup was put back to
  `groups.get`: the host case went red, and the file was restored byte-exact.

Remaining gaps: access-harden, edge-route and oidc-config (each an operator decision), and
cloud-init and service-deploy (not re-reviewed).

## Operator decisions and re-review — 2026-10-04

Author: Joseph A. Wisneski IV — 2026-10-04

The operator decided three things on 2026-10-04. Line numbers are on this change, based on
`dev` at 21891372.

1. **edge-route is split.** Its third criterion, "the Cloudflare plan is zero-diff", is now its own
   step, **edge-dns** (order 16, right after edge-route). The executor is Apply Cloudflare Tofu,
   the owner is service-agent, the undo is `none`, the policy is `required`, and the evidence is
   `plan_changes`. Steps 16 to 22 moved down to 17 to 23. OPA `data.json` has the matching
   `workflow_steps.edge-dns` entry and gives service-agent `Apply Cloudflare Tofu` in
   `allowed_templates`. Until the step is reviewed, OPA allows only the dev-bound variant.
2. **cloud-init is re-reviewed** against the verdict play that #419 added to provision-vm.yml.
3. **Stamp every passing step.** Not done: the hazard is below.

| Step | Playbook | Result emitted | Re-run | Group / failure handling | Guard | Verdict |
|------|----------|----------------|--------|--------------------------|-------|---------|
| edge-route | manage-caddy-sites.yml | :75 (preflight refusal), :331 | unchanged from 2026-10-03 (restarts only on a changed or retired block) | the populated-group preflight (:63-87) and per-host verdicts folded into the run_once result (:318-339) | test_service_executor_step_results.py, test_edge_route_group_preflight.py | **pass**. Both remaining criteria are proven: `resolves` (:306) and an HTTP answer below 500 (`route_status`) |
| edge-dns | apply-cloudflare-tofu.yml | :259, and :150 as a `skip` when a dry run stops on a stale backend | a plan is read-only. An apply is followed by a fresh plan (:223-239), so it passes only if that plan shows no changes, and an already-converged zone applies nothing | localhost only. Both plans run `-detailed-exitcode` (:192, :227): 0 passes, 2 is a recorded fail with the parsed change count, and 1 fails the task (:201, :236). A tofu error therefore leaves no result, and the collector records the failure from the output tail, the same limit as the other executors. Only the credential tasks use `no_log` | test_edge_dns_step_result.py | **pass** |
| cloud-init | provision-vm.yml | :878 in the verdict play (:862), with undo `Destroy VM` | read-only: `cloud-init status --wait`, `changed_when: false` (:751-761) | the login (:739-744) and the cloud-init wait `ignore_errors`/`ignore_unreachable` and are classified (:813-823), so the verdict play still records a result after a failed login. A dry run or a VM that never reached post-boot is a `skip`, never a pass (:874-875). Address = the declared `vm_ip` (:96, :658); user = the `ciuser` that cloud-init was given (:465, :659) | test_vm_lifecycle_step_results.py (:400-421) | **pass** |

Limit on cloud-init: the login uses the orchestrator's SSH credential, which is kept in
Semaphore's key store. The repo does not record that this key is the one cloud-init
authorized (:857-861), so "bootstrap identity" is proven as the user and the connection, not
as a matching key fingerprint. The failure message names what to compare.

The edge-dns plan-only failure is recorded, but the run does not fail on it, because a
plan-only run is a preview. The step result carries the verdict.

**Not stamped: the hazard.** A stamp sets `workflow_steps.<id>.reviewed`, and OPA then lets the
**base** template run (agent_actions.rego:128-143). Base templates are bound to the
`agent-cloud` repository record, which is `main` (repositories.yml:33-36). Locally they are
bound the same way: setup-templates.yml:42-51 binds only templates-local.yml entries to the
working tree. On `origin/main` (11792d26), checked 2026-10-04:

- No step executor includes `tasks/emit-step-result.yml`.
- These playbooks are absent: lookup-service-inventory.yml, validate-address-free.yml,
  backup-service-ssh-key.yml, verify-service-health.yml, verify-service-persistence.yml and
  backup-credentials-to-site-config.yml.
- Every other executor playbook (provision-template, provision-vm, distribute-ssh-keys,
  resize-vm, check-secrets, apply-firewall, manage-caddy-sites, apply-cloudflare-tofu) differs
  from `dev`.

A stamped step would therefore run code that was not reviewed and records no step result.
For the six absent playbooks, the run would fail outright. This applies to every candidate,
so the stamp list is empty. Each passing `review_gap` now says the stamp waits on the
playbook reaching `main`. Once `dev` is promoted to `main`, the stamps can land with no
other change (`main` has no `workflow_steps` in `data.json` yet).

Remaining gaps: access-harden and oidc-config (other work in progress), and service-deploy.

### edge-dns owner — 2026-10-04

The operator decided the same day that `service-agent` must not launch Apply Cloudflare Tofu.
A new role-scoped OPA identity, `network-agent`, now owns edge-dns, and it is the only role
with that template on its `allowed_templates`. The review rules are unchanged: OPA allows only
the `(Dev)` variant until the step is stamped. The role has no principal bound to it. Agent
identity is the `agent` field of each OPA request, and no other mapping exists in this repo.
`netclaw` stays frozen (plan 15 D7).

### Correction — 2026-10-04 (review of PR #436)

The edge-dns pass recorded above had two defects. The sections above stay as they were written.

- **Credentials in visible tasks.** Every tofu command (init, plan, apply, and the new plan
  after apply) ran visibly with the R2 keys and the Cloudflare token in its environment. The
  test even required that no `no_log` be set. This was true of the existing tasks as well as
  the new one. Now every tofu command is its own `no_log` task with `failed_when: false`, and
  its environment comes from a `no_log` fact. A visible report shows only exit codes and
  change counts, and a visible assert fails the run on a tofu error. The plan text is no
  longer printed: it carries the declared origin address. Reviewing the diff before an apply
  now needs a run with access to that output. That is a trade-off for the operator.
- **A dry run was not read-only.** `--check` still ran `tofu init -reconfigure`, which writes
  `.terraform/` into the tofu root. In check mode, tofu now gets a throwaway `TF_DATA_DIR` (a
  temporary directory that an `always` step removes). A fake-tofu test proves that init and
  plan used that directory, that it is gone afterwards, and that the tofu root is untouched.
  Limit: the committed `.terraform.lock.hcl` is still read from the root. Init rewrites it
  only when the providers or hashes differ from it, which this test does not exercise.
- Guards are in `test_edge_dns_step_result.py`. Three mutations each turned a test red, and
  the file was restored byte-exact each time:
  - `no_log` dropped from one plan;
  - `TF_DATA_DIR` not set;
  - the cleanup pointed at the wrong path.

Verdict: edge-dns still **passes** D10, with these fixes.

### Correction to the correction — 2026-10-04

The section above stays as it was written. One of its two fixes has been withdrawn.

- **The throwaway `TF_DATA_DIR` is withdrawn.** A `tempfile` create and a `file` removal forced
  to run in check mode, in a play that has `vars:`, are violations under the repository's
  check-mode contract (`platform/tests/test_check_mode_contract.py`, the runner-scratch class
  from plan/architecture/08). The `tempfile` users allowlist in `test_materialise_ssh_key.py`
  pins its population too. The full suite caught both after the previous commit. Widening that
  contract is not this change's call.
- **edge-dns is back to a gap.** A dry run still runs `tofu init`, and init writes `.terraform/`
  into the runner's checkout of the tofu root. The registry `review_gap` says so. The
  `no_log` fix stands. The plan after apply now carries `check_mode: false` like the other
  reads, but it does not run in a dry run, because its apply never ran.

Verdict: edge-dns **gap** (the dry run is not read-only). edge-route and cloud-init still pass.

### Plan visibility — 2026-10-04

Operator decision: the visible output shows each changed resource's address and action, and
nothing else. The plan and the post-apply plan are each saved inside tofu's own `.terraform/`
(`-out`). `tofu show -json` reads each saved plan inside the `no_log` boundary. The visible
report takes only `resource_changes[].address` and `.change.actions`, skipping `["no-op"]`,
as `plan_actions` / `verify_actions` entries of the form `"<address>: <action>"`. The schema
is the OpenTofu JSON output format (https://opentofu.org/docs/internals/json-format/). No
before or after values reach the report, so neither does the origin address.

The saved plan files are removed in `always`. The removal is not forced under `--check`, as
the check-mode contract requires, so a dry run leaves them inside the `.terraform/` that the
edge-dns gap already records.

Guards are in `test_edge_dns_step_result.py`:
- a fixture plan JSON carrying values shows address and action only;
- the visible report has an exact set of keys.

Two mutations each turned the tests red, and the file was restored byte-exact:
- `change.after` appended to the entry;
- the no-op filter dropped.

The edge-dns verdict is unchanged: gap.

## Re-review — 2026-10-05

Author: Joseph A. Wisneski IV — 2026-10-05

Four steps re-checked on `dev` at 95a498a3, after #430, #436, #438, #439 and #444 merged. Line
numbers are on that commit. Semaphore task ids are production runs launched by the operator's
coordinating session.

| Step | Playbook | Result emitted | Re-run | Group / failure handling | Guard | Verdict |
|------|----------|----------------|--------|--------------------------|-------|---------|
| access-harden | harden-ssh.yml | :473, controller play (:403) | `copy`/`lineinfile` (:161 on); sshd restarts only through the handler (:199). A fresh key-only login is probed (:118) before sudoers or sshd_config is touched, and a failed probe refuses with nothing edited (#430). A dry run records `skip` with a would-change list, never `pass`, unless the host already meets every criterion (:446, #439) | the controller play folds every host's verdict; an empty group or a host with no verdict fails | test_harden_proves_key_first.py, test_harden_check_mode_verdict.py, test_access_executors_step_result.py | **pass** |
| oidc-config | deploy-authentik.yml | :473 (verify failed), :538 (last) | deploy.sh recreates only when an input or image changed (`compose_up_if_changed`, deploy.sh:47-50) and prints `DEPLOY_CHANGED`, which sets `changed` (:326). Active blueprints converge in place (#438). The operator lever `deploy_force_recreate` (:322, #444) accepts only the literal true | unchanged: preflight plus per-host verdicts folded into one result | test_service_executor_step_results.py | **pass** |
| edge-route | manage-caddy-sites.yml | unchanged since 2026-10-04 (the playbook has no commit since 21891372) | unchanged | unchanged | test_service_executor_step_results.py, test_edge_route_group_preflight.py | **pass** |
| edge-dns | apply-cloudflare-tofu.yml | unchanged | unchanged | unchanged | test_edge_dns_step_result.py | **gap**: a dry run still runs `tofu init` (:193-201), which writes `.terraform/` into the runner's checkout of the tofu root (the known limit at :176-179) |

Live evidence:

- oidc-config second run, no change: Deploy Authentik task 2896 forced a recreate
  (`FORCE_RECREATE`, verify OK, 14 blueprints); the next normal run, 2899, reported `changed=0`,
  `DEPLOY_CHANGED=false`, verify OK. The earlier tasks 2843 and 2846 belong to the stale-mount
  incident recorded in `docs/MISTAKES.md` 3.11 (separate PR).
- access-harden: the dry run 2859 against the o11y host recorded `skip`, `key_only_proven`
  true, and a would-change list. A real hardening run has not happened; it is the operator's.

Limit on access-harden: the criterion "sudo is passwordless for the management user" is
converged by the sudoers write (validated by `visudo`), but no evidence key records a
passwordless `sudo -n` probe. The criteria written for this step are what the evidence proves:
`key_only_proven` and `password_rejected`.

**Not stamped**, for the hazard recorded on 2026-10-04: a stamp lets the base template run
from `main`, which does not carry these playbooks yet. access-harden, oidc-config and
edge-route each carry the passing `review_gap`. Remaining gaps: edge-dns (dry run not
read-only) and service-deploy.

## edge-dns dry-run gap closed — 2026-10-05

Author: Joseph A. Wisneski IV — 2026-10-05

The sections above stay as written. The gap they record, a dry run whose `tofu init` writes
`.terraform/` into the runner's checkout of the tofu root, is closed in the executor without
widening the check-mode contract. Line numbers are on this change's commit.

- **Mechanism.** Under `--check`, init, plan and show run as ONE shell command
  (`apply-cloudflare-tofu.yml:206`) that makes its own `root=$(mktemp -d)`, removes it with
  `trap 'rm -rf "$root"' EXIT`, and points `TF_DATA_DIR` into it. OpenTofu keeps its
  per-working-directory data where `TF_DATA_DIR` says, and the value must hold for every
  command from init on (https://opentofu.org/docs/cli/config/environment-variables/#tf_data_dir),
  which is why the three run in one process instead of three tasks. That is the command shape
  `test_check_mode_contract.py` already classes as a read (`_sandboxed`, as used by the netplan
  validation); no Ansible `tempfile` or `file` write is forced into check mode, so the
  runner-scratch class is not widened.
- **The lock file.** The dry-run init takes `-lockfile=readonly`, which verifies checksums
  against the committed `.terraform.lock.hcl` and suppresses changes to it
  (https://opentofu.org/docs/cli/commands/init/).
- **No remote write.** The plan takes no state lock: the s3 backend locks only with
  `dynamodb_table` or `use_lockfile` (https://opentofu.org/docs/language/settings/backends/s3/),
  and neither `versions.tf` nor the rendered `backend.hcl` sets one.
- **Results.** The command prints one JSON object with what the real init, plan and show tasks
  would have registered; a `no_log` task unpacks it (:248) and the visible report reads it in
  check mode (:399). The report is unchanged: exit codes, counts, address and action only.
- **A real run is unchanged.** The real init now carries `when: not ansible_check_mode`
  (:266) instead of `check_mode: false`; plan, show, apply and the post-apply plan run as
  before, in the tofu root's own `.terraform/`.

Tests, with a fake `tofu` that writes into its data directory, rewrites the lock file unless
told `-lockfile=readonly`, and saves its `-out` file (`test_edge_dns_step_result.py:247-292`):
a dry run leaves the tofu root byte-identical and removes the throwaway root, records
`pass`/`plan_changes: 0`; a dry run with changes records the count and address/action only;
a dry run whose init fails fails the run and still writes nothing; a real plan still runs in
`.terraform/` and removes its saved plan. Five mutations each turned a test red and were
restored byte-exact: `TF_DATA_DIR` dropped, `-lockfile=readonly` dropped, the `trap` dropped,
the real init forced back into check mode, and the report reading the real init in check mode.

Not verified: a dry run against the real OpenTofu binary and the live R2 backend. The fake
binary proves the playbook's handling, not tofu's own behaviour with `-lockfile=readonly` on
the runner's platform.

Verdict: edge-dns **passes** D10 in the executor, pending that live dry run. Not stamped, for
the `main` hazard recorded on 2026-10-04.
