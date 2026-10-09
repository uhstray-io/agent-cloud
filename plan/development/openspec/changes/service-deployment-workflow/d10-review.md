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

Follow-up, same day (PR 454 Codex review): the exit code of `tofu show` was recorded nowhere,
so a failed show left `plan_actions` / `verify_actions` empty while the run still passed. The
report now carries `show_rc` and `verify_show_rc`, and the visible assert fails the run on a
non-zero value, in a dry run and a real run alike. Three tests prove it (plan show, post-apply
show, and a fake tofu whose show exits 1 under `--check` and for real); dropping either assert,
or reporting `show_rc` as none, turned them red.

## edge-dns live dry run — 2026-10-06

Author: Joseph A. Wisneski IV — 2026-10-06

The sections above stay as written. The 2026-10-05 verdict was "passes D10 in the executor,
pending that live dry run"; the run has happened. Apply Cloudflare Tofu (Dev) task 3006, a dry
run on `dev` after #454, against the real OpenTofu binary and the live R2 backend, recorded
`init_rc` 0, `plan_rc` 0, `show_rc` 0 and `plan_changes` 0, and an edge-dns step result of
`pass` with `check_mode` true. The task id and these values were reported by the coordinator;
the task output was not read for this note. A read-back that the runner's checkout of the tofu
root was left unchanged is not part of the reported evidence; the byte-identical root remains
proven by the fake-tofu tests only.

Verdict: edge-dns **passes** D10, with no pending proof. Not stamped, for the `main` hazard
recorded on 2026-10-04. Remaining gap: service-deploy, not yet re-reviewed.

## Stamped — 2026-10-08

Author: Joseph A. Wisneski IV — 2026-10-08

The sections above stay as written. Operator decision 2026-10-04, "Stamp every passing step",
was held only by the `main` hazard recorded the same day. `dev` was promoted to `main` on
2026-10-07 (PR #447, `origin/main` c81ada2a). This pass checks the hazard step by step on
`origin/dev` a2224685 against `origin/main` c81ada2a, then stamps.

Checks, per step. (a) The registry `review_gap` said the only remaining gap was the wait on
`main`, and no later section above records a new gap. (b) `git diff origin/main origin/dev --
<executor playbook>` is empty. (c) Every task file the playbook includes, followed through
`include_tasks`, `import_tasks` and `import_playbook`, is absent from the list of files that
differ between `main` and `dev`; so is every deploy script, template and infra file these
executors use. (d) The playbook includes `tasks/emit-step-result.yml` on `main`, and that task
file is identical on both branches. The files that do differ between the branches are the
agentgateway, o11y, clean-deploy and local-dev work, the collector library
(`lib/step_results.py`, not an executor), the Semaphore template files, tests and docs. In
`templates.yml` the changed entries are the Clean Deploy templates, Verify o11y Service and one
new `Verify agentgateway Runtime (Dev)`; none is an executor of these steps, and each executor
template (and Destroy VM, the undo) exists on `main` unchanged.

| Step | Executor | main == dev (diff empty) | Stamped |
|------|----------|--------------------------|---------|
| vm-template | provision-template.yml | yes | yes |
| lookup-inventory | lookup-service-inventory.yml | yes | yes |
| validate-address | validate-address-free.yml | yes | yes |
| provision-vm | provision-vm.yml | yes | yes |
| cloud-init | provision-vm.yml | yes | yes |
| ssh-keys | distribute-ssh-keys.yml | yes | yes |
| ssh-key-backup | backup-service-ssh-key.yml | yes | yes |
| access-harden | harden-ssh.yml | yes | yes |
| vm-rightsize | resize-vm.yml | yes | yes |
| secrets-approle | check-secrets.yml | yes | yes |
| service-validate | verify-service-health.yml | yes | yes |
| edge-route | manage-caddy-sites.yml | yes | yes |
| edge-dns | apply-cloudflare-tofu.yml | yes | yes, see note |
| fw-harden | apply-firewall.yml | yes | yes |
| systemd-enablement | verify-service-persistence.yml | yes | yes |
| oidc-config | deploy-authentik.yml | yes | yes |
| credential-backup | backup-credentials-to-site-config.yml | yes | yes |
| service-deploy | Deploy {service} | not checked | no: no per-service deploy emits, second-run no-change unproven, not re-reviewed |
| instrument-host | Instrument Host Observability | not checked | no: never run (registry `review_gap`) |
| instrument-service | none (planned) | n/a | no: no executor |
| service-assess, fw-assess, access-assess | none (reasoning) | n/a | no: out of scope |

edge-dns note: the registry `review_gap` still carried "a dry run against real tofu is not yet
verified", which the 2026-10-06 section above records as done (Apply Cloudflare Tofu (Dev) task
3006, reported by the coordinator; its output was not read for that note). That gap is resolved,
so the step is stamped. The 2026-10-06 limit stands: a read-back that the runner's checkout of
the tofu root was left unchanged is not in the reported evidence.

What the stamps change. The registry carries `reviewed: "2026-10-08"` and no `review_gap` for
the seventeen steps, and `catalog.workflow_steps.<id>.reviewed` in OPA `data.json` is `true`
for the same seventeen (the registry test pins the two together). OPA then allows the base,
`main`-bound template for these steps. The change reaches the running OPA only when OPA is
redeployed.

## Delta re-review at a2224685 — 2026-10-08

Author: Joseph A. Wisneski IV — 2026-10-08

The sections above stay as written. PR #486 review finding 1: the 2026-10-08 stamps rest on
verdicts given at older commits, and the executors changed afterwards. This pass takes each of
the seventeen stamped steps, finds the commit its latest pass verdict was given against, lists
every non-merge commit since that touched the executor playbook, any task file or playbook it
includes (followed through `include_tasks`, `import_tasks` and `import_playbook`), or the
service directory it deploys from, reads each diff, and re-checks the D10 criteria (idempotent
re-run, result emitted, group and failure handling, guard test, undo) only against what
changed. Line numbers are on `a2224685`; "result emitted" cites the `step_result_step:` line.

Verdict base per step, from the section that last passed it: lookup-inventory, validate-address,
secrets-approle, service-validate `8e97e452` (the parent of the 2026-10-02 review commit
`32e4a05a`; the emitted-result lines that review cites, :106, :246, :190 and :86, hold for
`step_result_step` at that commit); vm-template, provision-vm, ssh-keys, ssh-key-backup,
vm-rightsize, fw-harden, systemd-enablement, credential-backup `ce28cadf` (2026-10-03);
cloud-init and edge-route `21891372` (2026-10-04); access-harden and oidc-config `95a498a3`
(2026-10-05); edge-dns `9edcd9ec`, the merge of #454 (2026-10-05), which holds `c499cd34` (the
`tofu show` exit-code fix named in the finding); the live dry run 3006 ran at that revision (see the edge-dns note below).

### What the commits are

Every commit since a base that touches the files above falls in one of these classes. No commit
outside them touches them.

| Class | Commits | What changed | Test |
|-------|---------|--------------|------|
| A. Run-start guard | `5cb1faa0` (a first play in each executor), `4daf31f6`, `5da6f272`, `d2694c1f`, `eaa4bbcf` (the guard's own pattern and wording), `6abe7546` (first entry of every launched playbook, the `_extra_var_guard_nested` flag on imports such as `proxmox-validate.yml`, register renames) | A run whose extra vars set an underscore-prefixed name, or a become or connection password under any alias, is refused before any other play runs. Nothing else in an executor changes: the import is five lines, and the register renames in provision-vm, provision-template, resize-vm and proxmox-validate rename the register and every reader together | `test_executor_internal_overrides.py`, `test_extra_var_refusal_tests.py` |
| B. Shared result task | `3e089c47`, `1b96860e`, `29319e8c`, `b9e66719` | `tasks/emit-step-result.yml` still records the same result through the same include, now also appended to an aggregated `step_results` list (`3e089c47`), and it refuses the run when one of its seven input names reaches it from outside the executor (reserved names, by name, not by value) | `test_emit_step_result.py` |
| C. Proxmox token id from the store | `77c8a90c` | The literal fallback token id is gone. A required assert refuses an empty stored `token_id` before the first request: provision-vm `:169` and resize-vm `:169` inside the rescued block (a failure is recorded), provision-template `:65` inside its rescued block, validate-address `:86` with no rescue, like the vm-recorder assert above it at `:80`, and proxmox-validate | `test_pve_token_id_required.py` |
| D. Template play | `2548c033`, `29e29bca`, `210db130`, `f4787542`, `b8d44283` | provision-template reads its connection from OpenBao itself, refuses a cleartext endpoint, declares empty placeholder play vars, names the drive keys that hold a cloud-init volume in the mismatch message, and treats a `"data": null` read-back as empty. What passes is unchanged | `test_vm_lifecycle_step_results.py`, `test_pve_token_id_required.py` |
| E. SSH key helpers | `32b455bb` | `materialise-ssh-key.yml` and `pin-ssh-host-key.yml` refuse an extra var that would replace their internal names, through `refuse-var-overrides.yml`; an honest run is unaffected (distribute-ssh-keys only) | `test_materialise_ssh_key.py` |
| F. Firewall dry run | `eb874640` | In a dry run, the gaps a real run would converge (ufw inactive, default policy, declared rules not held, stale tagged rules) move from `errors` to `would_change`, and the step records `skip` instead of `fail`; a real run still enforces them, and a stale rule held back stays an error in both modes | `test_apply_firewall_convergence.py`, `test_service_executor_step_results.py` |
| G. Placement guard | `c02d2c5d` | `place-monorepo.yml` refuses placing `main` from a non-`main` checkout. Reached by oidc-config only: verify-service-persistence names `place-monorepo` in comments, includes no such task | `test_placement_branch_guard.py` |
| H. Comments | `38261cc6` (manage-caddy-sites header), `9ed75dd6` (authentik blueprint comments), `d2694c1f` (harden-ssh header: the become password comes from OpenBao, which the play has read since before `95a498a3`) | Text only | none needed |

### Per step

| Step | Verdict base | Commits since (class) | Criteria still met (file:line at a2224685) | Verdict |
|------|--------------|-----------------------|-------------------------------------------|---------|
| vm-template | `ce28cadf` | A: `5cb1faa0`, `6abe7546`, `4daf31f6`, `5da6f272`, `d2694c1f`, `eaa4bbcf`. B: `3e089c47`, `1b96860e`, `29319e8c`, `b9e66719`. C: `77c8a90c`. D: `2548c033`, `29e29bca`, `210db130`, `f4787542`, `b8d44283`. `32b455bb` is not on its path | Re-run: an existing template is adopted with no write (`_tmpl_present`, provision-template.yml:112). Result emitted :323, after a read-back that is skipped when the connection is refused (the step then records the refusal). Failure handling: the create block's rescue (:264) still turns every hard failure, the new asserts included, into a recorded `fail`. Guard: `test_vm_lifecycle_step_results.py`. Undo none | pass |
| lookup-inventory | `8e97e452` | A: `5cb1faa0`, `6abe7546`, `4daf31f6`, `5da6f272`, `d2694c1f`, `eaa4bbcf`. B: `3e089c47`, `1b96860e`, `29319e8c`, `b9e66719`. `32b455bb` is not on its path | Read-only (one NetBox GET, `check_mode: false` :83); result :111; BATS `test_address_steps.bats` unchanged in its assertions | pass |
| validate-address | `8e97e452` | A, B as above. C: `77c8a90c` | The one write still runs only when no record exists and never under `--check` (validate-address-free.yml:245-247); result :259; the new token-id assert (:86) is a refusal before any read, the same shape as the vm-recorder assert at :80 | pass |
| provision-vm | `ce28cadf` | A, B as above. C: `77c8a90c`. `3e089c47` also adds the cloud-init verdict play, which is the cloud-init step | Re-run: an existing VM that is ours is classified, not cloned again (:223); clone `changed_when` on a real clone (:322). Result emitted :714 in its own play after the dry-run `end_host` (:568); undo `Destroy VM` (:723). Failure handling: the provisioning block's rescue (:683) records a hard failure, token-id assert included (:169). Guard: `test_vm_lifecycle_step_results.py` | pass |
| cloud-init | `21891372` | A: `5cb1faa0`, `6abe7546`, `4daf31f6`, `5da6f272`, `d2694c1f`, `eaa4bbcf`. B: `1b96860e`, `29319e8c`, `b9e66719`. C: `77c8a90c`. `32b455bb` is not on its path | Result :899 in the verdict play, undo `Destroy VM` (:909); a dry run or a VM that never reached post-boot is a `skip`, never a pass (:895-908); the post-boot login and wait are unchanged. Guard: `test_vm_lifecycle_step_results.py` | pass |
| ssh-keys | `ce28cadf` | A, B as above. E: `32b455bb` | Re-run: `ansible.posix.authorized_key` (:157, :163). Result :347 from the controller play (:317), which folds every host's verdict (:327). Guard: `test_access_executors_step_result.py` | pass |
| ssh-key-backup | `ce28cadf` | A, B as above. `32b455bb` is not on its path | Staged-only push via `site-config-push.yml` (:204); rescue :221 records a failure; result :236. Guard: `test_access_executors_step_result.py` | pass |
| access-harden | `95a498a3` | A: `5cb1faa0`, `6abe7546`, `4daf31f6`, `5da6f272`, `d2694c1f`, `eaa4bbcf`. B: `1b96860e`, `29319e8c`, `b9e66719`. H: `d2694c1f` | The key-only proof still precedes any edit (:92-159, gate decided inside the probe task :159); controller play folds every host (:430); result :484. Guards: `test_harden_proves_key_first.py`, `test_harden_check_mode_verdict.py`, `test_access_executors_step_result.py` | pass |
| vm-rightsize | `ce28cadf` | A, B as above. C: `77c8a90c`. `6abe7546` also renames the hoisted `vm_*` declaration facts to `_hv_vm_*` | Config write `changed_when: _cfg_changes \| length > 0` (:379); rescue :536 records a hard failure, token-id assert inside the block (:169); result :576. Guard: `test_vm_lifecycle_step_results.py`, `test_resize_vm_agent.py` | pass |
| secrets-approle | `8e97e452` | A, B as above | Read-only (:101); verdict folded across the group (:183); result :195 | pass |
| service-validate | `8e97e452` | A, B as above | Read-only; verdict folded across the group (:79); result :91 | pass |
| edge-route | `21891372` | A: `5cb1faa0`, `6abe7546`, `4daf31f6`, `5da6f272`, `d2694c1f`, `eaa4bbcf`. B: `1b96860e`, `29319e8c`, `b9e66719`. H: `38261cc6`. `32b455bb` is not on its path | Populated-group preflight still the first play after the guard (:69-87); result :83 (refusal), :338; both criteria unchanged (`resolves`, `route_status` :342-343). Guards: `test_service_executor_step_results.py`, `test_edge_route_group_preflight.py` | pass |
| edge-dns | `9edcd9ec` | A: `5cb1faa0`, `6abe7546`, `4daf31f6`, `5da6f272`, `d2694c1f`, `eaa4bbcf`. B: `1b96860e`, `29319e8c`, `b9e66719`. `c499cd34` is inside the base | `-detailed-exitcode` plans (:223, :281, :330), `show_rc` asserts (:423-424), result :160 (stale backend `skip`) and :437. Guard: `test_edge_dns_step_result.py` | pass |
| fw-harden | `ce28cadf` | A, B as above. F: `eb874640` | A group error is still `fail` (:811-838); an empty group or a host with no verdict is still a recorded failure; only a dry run that would change something is `skip` (:838-839), and `skip` is never `pass`; result :835; rules still added only when ufw reports a change. Guards: `test_apply_firewall_convergence.py`, `test_service_executor_step_results.py` | pass |
| systemd-enablement | `ce28cadf` | A, B as above. G: `c02d2c5d` is not on its path | Read-only probes (`changed_when: false` :88-139); preflight and per-host errors fold into the run-once verdict (:207); result :221. Guard: `test_persistence_group_verdict.py` | pass |
| oidc-config | `95a498a3` | A: `5cb1faa0`, `6abe7546`, `4daf31f6`, `5da6f272`, `d2694c1f`, `eaa4bbcf`. B: `1b96860e`, `29319e8c`, `b9e66719`. G: `c02d2c5d`. H: `9ed75dd6` | Recreate only on a changed input or image (`DEPLOY_CHANGED` :331, operator lever :325-327); preflight (:30) and per-host verdicts folded; result :481 (verify failed), :546. The placement guard refuses only `main` from a non-`main` checkout, so the stamped `main`-bound template is not affected. Guard: `test_service_executor_step_results.py` | pass |
| credential-backup | `ce28cadf` | A, B as above. `32b455bb` is not on its path | Staged-only push; rescue :238 records a failure; evidence `branch` empty when nothing was pushed (:256); result :254. Guard: `test_access_executors_step_result.py` | pass |

No commit breaks a criterion for any of the seventeen steps, so every stamp stays. Every
change since a base is a guard import, a hardening with a test, or the firewall dry-run
classification (F); none removes a result, an undo or a failure path. The effects to hold on to:

- A run the new guards refuse (a forged internal name, a missing stored `token_id`, a
  forged result input) ends before the step result is recorded, and the collector records a
  failure from the task output tail. That is the limit already written for failures outside
  the rescued work (2026-10-03 re-review), not a new one. A `--limit` that excludes
  localhost skips the run-start guard, as `refuse-internal-extra-vars.yml` states.
- The Proxmox executors now need `token_id` in `secret/services/proxmox`. Unverified here:
  whether the live store holds it. Task 3195 passed the validation play before it failed
  the template read-back (tasks.md:106-110, a coordinator report), which implies it did.
- Live evidence after the guard landed is thin. Validate Proxmox Cluster (Dev) 3284 and Deploy
  agentgateway (Dev) 3285 succeeded at `dev` `8dc61584` (tasks.md:111-113, task output not read
  here); neither is one of the seventeen executors. No post-guard live run of the seventeen is
  recorded in the repository.
- Open, and not a commit: vm-template. Create VM Template (Dev) 3287 at `cf765af8` recorded
  `fail` for the live template 9000 (cloud-init drive on `ide0`, not `ide2`; tasks.md:111-120,
  a coordinator report), and the operator decision (rebuild the template, or accept any drive
  key) is pending. The executor reports the live state correctly, which is what D10 asks of
  it, so the stamp stands; the step will record `fail` against that template until the
  decision lands.

edge-dns: the stamp relies on the values task 3006 recorded at `d10-review.md:369-383`. That
section did not read the task output; this pass does, on the operator's coordinator's read of
Semaphore on 2026-10-08: Apply Cloudflare Tofu (Dev) task 3006, status success, created
2026-10-05T20:40Z, revision `9edcd9ec`; its step "Show the tofu result for action plan" gave
`plan_actions` empty and `plan_changes` "0", and "Record the edge-dns step result" ran after
it. The values are verified against the task output, and the run was at the base this pass
uses, so only the guard and shared-task commits above postdate it.

Re-run for this pass on this branch: `opa check --strict` and `opa test` (101/101), pytest over
the repository `testpaths` for `platform/tests` plus the registry, emit, guard, lifecycle,
access, service-executor, edge-dns, edge-route, harden, firewall, token-id, placement,
persistence, materialise and check-mode suites (1793 passed, Python 3.14 locally, CI pins
3.11), and `bats platform/tests/` (839 ok, 0 not ok). No guard was added or changed, so
there is nothing to mutation-check.
