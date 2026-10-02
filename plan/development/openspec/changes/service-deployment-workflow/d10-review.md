# D10 review of existing executors (task 7.1)

Author: Joseph A. Wisneski IV — 2026-10-02

Design D10 (plan 15, "Tracking"): a step is trusted in the graph once its criteria are written
into the registry, an idempotent re-run is proven, its step result is recorded, its undo is
named (or `none`), and a test guards the emitter. This review applies those checks to every
executor step in `platform/workflows/service-onboarding/registry.yml`. Reasoning steps
(`service-assess`, `fw-assess`, `access-assess`) have no executor and are out of scope; the
two planned executors (`instrument-host`, `instrument-service`) do not exist yet.

Undo names were checked against the catalog by `test_named_templates_exist_and_planned_ones_do_not`;
the emitter guard for every passing step is `test_registry_evidence_keys_match_what_the_steps_emit`.

## Passing — all D10 checks hold

| Step | Playbook | Re-run | Result emitted | Undo |
|------|----------|--------|----------------|------|
| lookup-inventory | lookup-service-inventory.yml | read-only: one NetBox GET (`check_mode: false`); BATS `test_address_steps.bats` refutes any write method | :106 | none |
| validate-address | validate-address-free.yml | the one write (NetBox VM POST) runs only when no record exists (`_vm_record \| length == 0`, :236) and never under `--check`; BATS "the one write is skipped under --check" | :246 | none (see note) |
| secrets-approle | check-secrets.yml | read-only: login POST `changed_when: false` (:96), then GETs | :190 | none |
| service-validate | verify-service-health.yml | read-only: one health GET | :86 | none |
| systemd-enablement | verify-service-persistence.yml | read-only: commands `changed_when: false`, `stat`/`getent`/`slurp` | :181 | none |

Note: validate-address leaves a `planned` NetBox VM record behind with undo `none`. The
criterion allows it; a later step may want `Destroy VM` to retire the record too.

**Not stamped.** `reviewed` also drives OPA (`catalog.workflow_steps.<id>.reviewed` in
`platform/services/opa/deployment/policies/agentcloud/data.json`, kept equal by
`test_opa_step_map_matches_the_registry`), and a reviewed step may run from `main`. That file
is outside this change's scope, and four of the five executors (all but Check Secrets, per
`PREDATES_WORKFLOW` in the registry test) are new with the workflow, so `main` has no copy of
their playbooks until promotion. Each one carries a `review_gap` saying the stamp waits on that change.

## Failing — no step result recorded

None of these playbooks include `tasks/emit-step-result.yml`, so the collector cannot score
them and they cannot report a failure. Each registry entry carries the gap as `review_gap`.

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
| credential-backup | Back Up Credentials to site-config | no step result; each run pushes a new branch |

Idempotency for the failing executors was not reviewed further: without a step result they fail D10 anyway.

Guard: `test_review_stamp_requires_a_recorded_result_and_a_named_undo` refuses a stamp on a
step no playbook emits for, a stamp beside a `review_gap`, and an unreviewed executor step
without one.
