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
