# Service deployment workflow: registry, agents, check mode, local-dev agent runtime

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Explored 2026-09-22 from
`docs/agent-cloud-service-deploy.svg` and the approved design
`plan/development/15-service-deployment-workflow-agents.md` (plan 15), then refined by seven
explore threads the same day (recorded in `design.md` §Threads).

Companion work: the private `uhstray-io/skynet` repository carries the four agent roles, the
workflow graph and the eval harness; the in-flight change `inference-gateway-agentgateway`
delivers the gateway this change builds on and must reach `dev` first.

## Why

The platform's target deployment flow exists only as a drawing. Of its twenty-two steps
(eighteen drawn, four the platform already requires), eleven have executors with no stated
pass criteria, seven have no executor at all, none of the 110 playbooks supports a dry run
through Ansible's own check mode, nothing records which step a service passed or failed, and
the agents meant to reason over three of the steps do not exist. Existing services drifted
from the flow without anyone being able to see it. Plan 15 fixed the design; this change
turns it into specified, testable behaviour and folds in the operator's 2026-09-22 decisions
on the inference edge, Ansible standards, the local-dev agent runtime, firewall access for
the orchestrator, check mode, the Semaphore environment model and local NetBox.

## What Changes

- **The workflow becomes code.** A step registry declares all twenty-two steps with owner
  role, executor template, snapshot template, undo template, pass criteria and a review
  date. It is the one definition read by skynet, the collector and the review checklist.
- **Every step reports a structured result** through Ansible's own `set_stats`, which runs
  in check mode and needs no custom output format. Proposals from the three reasoning steps
  use one envelope and three versioned schemas stored next to the step registry.
- **Every playbook supports a dry run and a verify run** through Ansible check mode
  (`--check`, `check_mode`, `ansible_check_mode`), replacing the three bespoke `dry_run`
  variables. Read-only probes are marked to run in check mode so verification never
  silently skips. **BREAKING** for launch arguments: `-e dry_run=true` on the three
  playbooks that accept it today is replaced by check mode; a compatibility shim maps the old
  variable for one release.
- **An Ansible standards reference** lands in `plan/architecture/` from the official
  documentation, and the automation documents are brought into line with it.
- **Four OPA identities** (`infra-agent`, `security-agent`, `o11y-agent`, `service-agent`)
  with per-role template allowlists and content rules over proposals. `netclaw` and
  `nemoclaw` are frozen as agent runtimes. A firewall proposal can never remove the
  orchestrator's SSH source and can never open SSH wider than the declared sources.
- **The inference edge and skynet are delineated.** agent-cloud's agentgateway owns
  `inference.uhstray.io`: keys, budgets, limits, telemetry and every route. skynet is a set of
  orchestration capabilities served under that hostname through the gateway, and its own
  model calls go through the gateway with one client identity per agent role. skynet drops
  its embedded Bifrost gateway for this path (skynet-repo task).
- **skynet runs on local-dev first**, deployed by the local Semaphore like any other local
  service, calling the DGX Spark vLLM through the local agentgateway. The upstream address
  lives only in the gitignored local inventory.
- **Semaphore environments are simplified** to one template catalog, with the branch as a
  launch parameter gated by OPA instead of a generated `(Dev)` twin per template, while the
  local-dev and production instances stay separate controllers.
- **NetBox runs on local-dev**, app tier under podman, with discovery scoped to local-dev
  targets only and a guard that refuses any production range.
- **Tracking MVP**: a scheduled collector writes per-step status to NetBox custom fields and
  Loki, rendered by a provisioned Grafana dashboard, plus a read-only failure report.
- **Rollout order**: agentgateway backfill end to end, then a read-only assessment sweep of
  every service, then a greenfield pilot.

## Capabilities

### New Capabilities
- `platform/service-onboarding-workflow`: the step registry, step-result and proposal
  contracts, reasoning-step behaviour, per-service conformance tracking and failure reports.
- `platform/automation-standards`: Ansible check mode and verify on every playbook, structured
  results via `set_stats`, conformance with the recorded Ansible standards.
- `platform/agent-orchestration`: the four agent identities and their OPA limits, the
  gateway-versus-skynet delineation, skynet's local-dev deployment and its inference path.
- `platform/semaphore-environments`: one template catalog, branch as a gated launch
  parameter, separate local and production controllers.
- `platform/netbox-local`: NetBox on local-dev with discovery confined to local-dev targets.

### Modified Capabilities
None in `openspec/specs/`. The in-flight `inference-gateway-agentgateway` change's
requirement "The skynet relationship is a recorded decision" is refined by
`platform/agent-orchestration`; the two are reconciled when that change archives.

## Impact

- **Playbooks**: all 110 under `platform/playbooks/` gain check-mode support; about 25 are
  registry executors and go first. New: inventory lookup, address validation, three snapshot
  templates, host instrumentation, collector, report, NetBox custom fields.
- **Tasks library**: a shared step-result task; a shared check-mode-safe probe pattern.
- **OPA**: `data.json` identities and `allowed_templates`; new rego rules and tests. Proposal
  schemas sit next to the step registry, NOT in the policy tree: OPA loads every JSON file
  there as data and rejected them with `merge error` (tested 2026-09-22).
- **Semaphore**: `templates.yml` loses generated `(Dev)` twins once branch-at-launch is
  proven; `setup-templates.yml` and the operating guide change accordingly.
- **Inventory**: local-dev gains `skynet_svc`, a DGX upstream for agentgateway and a NetBox
  host with discovery targets; production inventory is untouched until the backfill.
- **o11y**: Alloy OTLP receiver, a Grafana dashboard, Loki push from the collector.
- **Docs**: `plan/architecture/08-ansible-automation-standards.md` (new), plan 15 status,
  the diagram, playbook README, root `AGENTS.md`.
- **skynet repo**: role packs, graph, eval harness, Bifrost removal on the agent-cloud path,
  a durable checkpointer, a deployable image.
- **Dependencies**: `inference-gateway-agentgateway` merged to `dev`;
  `inference-telemetry-production` for the OTLP receiver; the NetBox automation token.

## Rollback Plan

Every piece is additive or flag-guarded, and each reverts independently:

- **Registry, schemas, collector, dashboard**: revert the commits; the collector is the only
  writer of the NetBox custom fields and Loki stream, so removing its schedule stops all
  writes, and the fields can be deleted by the same code that created them.
- **Check mode**: tasks marked `check_mode: false` or guarded by `ansible_check_mode` behave
  identically in a normal run, so reverting a playbook's check-mode edits changes nothing in
  normal execution. The `dry_run` shim keeps old launch arguments working during the
  transition.
- **OPA identities and rules**: the new identities are additive; reverting `data.json` and the
  rego restores the previous decisions. `netclaw` and `nemoclaw` entries are frozen, not
  removed, so nothing that uses them breaks.
- **Semaphore branch model**: the `(Dev)` twins are removed only after branch-at-launch is
  proven; until then both coexist, and re-running `setup-templates.yml` from the previous
  commit restores the twins.
- **skynet on local-dev and local NetBox**: local-only services removed with their clean-deploy
  templates; nothing in production depends on them.
