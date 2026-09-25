## Purpose

Defines the platform's service deployment workflow as code: the ordered steps every service
passes through, the contracts each step and each reasoning agent emits, and how the platform
records and reports which steps each service passed or failed.

## ADDED Requirements

### Requirement: One registry defines the workflow
The platform SHALL declare every workflow step in one committed registry. Each entry MUST
name a stable step id, its order, its owner role, its executor template, its snapshot
template (reasoning steps only), its undo template or the literal `none`, whether it is
`required` or `advisory`, its pass criteria, its evidence keys, and the date its review
passed or null. The registry MUST contain the twenty-two steps of plan 15, and every executor
and snapshot template it names MUST exist in the Semaphore template catalog.

#### Scenario: Registry and catalog agree
- **WHEN** the registry test runs in CI
- **THEN** it fails if any named template is absent from the catalog, if any reasoning step
  lacks a snapshot template or schema, or if the step count or ids differ from the diagram

#### Scenario: Unreviewed step is visible
- **WHEN** a step's `reviewed` field is null
- **THEN** the failure report lists that step as unreviewed for every service

### Requirement: Every step emits one structured result
Every executor and snapshot template SHALL end by recording exactly one step result through
the platform's shared step-result task, with schema id, workflow id, service, step, status
(`pass`, `fail` or `skip`), evidence, error and undo. The result MUST be recorded in both
normal and check-mode runs.

#### Scenario: Passing step records its evidence
- **WHEN** an executor completes and its criteria hold
- **THEN** its task output carries one step result with status `pass` and the evidence keys
  the registry names

#### Scenario: A failure with no result is still recorded
- **WHEN** a template ends non-zero without recording a result
- **THEN** the collector records that step as `fail` with the last twenty output lines as the
  error context

### Requirement: Reasoning steps emit validated proposals
Each reasoning step SHALL take one read-only snapshot as its only model input and SHALL emit
one proposal in the shared envelope, whose body validates against the step's versioned
schema. The workflow id, proposing role, idempotency key and snapshot digest MUST be injected
by the orchestrator, never produced by the model. A proposal that fails validation MUST stop
the step with status `fail` and MUST NOT reach an executor.

#### Scenario: Invalid proposal never executes
- **WHEN** a model output does not validate against its schema
- **THEN** the step records `fail` with the validation error and no executor task is launched

### Requirement: Existing declared state is the default outcome
For a service already in the estate, a reasoning step SHALL either approve converging the
declared state, which runs the existing converge template, or record a `change-required`
finding carrying the proposed difference. A finding MUST NOT write declared state; the change
lands through the pull-request process. A new service's first configuration also lands
through a pull request.

#### Scenario: Declared state is right
- **WHEN** a firewall assessment finds the declared rules match every listening consumer
- **THEN** the proposal verdict is `converge`, the firewall template runs from inventory and no
  pull request is opened

#### Scenario: Declared state is wrong
- **WHEN** an assessment finds a declared value that must change
- **THEN** the step records `fail: change-required` with the difference and no executor
  writes that value

### Requirement: Agents read deployment state before acting
Every workflow node SHALL receive the service's prior step results in the current run and the
registry entries for its current and next step before it acts.

#### Scenario: Assessment sees earlier steps
- **WHEN** the access assessment runs for a service whose firewall step failed
- **THEN** its snapshot carries that failure and its registry context

### Requirement: Per-service conformance is tracked and reported
A scheduled collector SHALL read Semaphore task history and output, Prometheus and NetBox,
and SHALL be the only writer of per-step status to the service host's NetBox custom fields
and to the step-result Loki stream. A provisioned Grafana dashboard MUST show every service
by every step, and a read-only report MUST list, per service, each failed step with its
criteria not met, error context, undo availability and Semaphore task reference.

#### Scenario: Failure appears within one interval
- **WHEN** a step fails for a service
- **THEN** within one collector interval the dashboard and the report show that step as
  failed with its context

#### Scenario: NetBox outage does not block deployment
- **WHEN** NetBox is unreachable during a workflow run
- **THEN** the run proceeds and the collector records the NetBox write failure, retrying on its
  next interval

#### Scenario: Only the collector writes status
- **WHEN** the single-writer test runs in CI
- **THEN** it fails if any playbook other than the collector writes the conformance custom
  fields or pushes to the step-result stream
