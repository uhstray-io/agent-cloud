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
