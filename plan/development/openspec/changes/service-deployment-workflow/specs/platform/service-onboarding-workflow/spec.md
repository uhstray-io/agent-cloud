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

#### Scenario: Production conformance logs use the declared private OTLP receiver
- **WHEN** the production collector publishes conformance records
- **THEN** it sends OTLP/HTTP to the exact declared private Alloy bind on port `4318` at
  `/v1/logs`, delivery failure fails the collector visibly, and Alloy applies bounded
  conformance-specific `job`, `service`, `step`, and `status` labels. The shared Loki writer
  also adds `cluster` and `environment` labels; task and error details remain in the log body.
  The exporter preserves a JSON body as the Loki line so the dashboard can extract the
  top-level `state_code` and `inventory_code` fields without an exporter envelope.

#### Scenario: NetBox outage does not block deployment
- **WHEN** NetBox is unreachable during a workflow run
- **THEN** the run proceeds and the collector records the NetBox write failure, retrying on its
  next interval

#### Scenario: Only the collector writes status
- **WHEN** the single-writer test runs in CI
- **THEN** it fails if any playbook other than the collector writes the conformance custom
  fields or pushes to the step-result stream


#### Scenario: Collector completion is emitted only after required work
- **WHEN** a scheduled collector finishes its required reads and parsing and all configured required writes complete
- **THEN** after the successful required-work boundary and positive transport acknowledgement of the existing conformance data batch, the collector sends one separate final request containing only the fixed `job=agent-cloud-conformance,kind=run,status=success` receipt through the existing environment-specific transport
- **AND** the receipt proves successful required collection work only; it does not claim complete historical coverage, that every service passed, a green Semaphore task, or durable sender acknowledgement, and existing `history_incomplete`/`no_history` markers and per-step statuses remain unchanged
- **AND** interrupted or failed reads, check-mode/skipped writes, and failed or unknown required writes before that boundary prevent sending a success receipt
- **AND** the explicitly nonblocking NetBox outage behavior remains visible and does not block the workflow, while any failed required configured write before the boundary prevents sending
- **AND** the separate receipt request follows HTTP 204 for the local data batch or HTTP 200 with zero rejected records for the production data batch; when there are no conformance data records, no prior batch request is needed
- **AND** a lost response or timeout after possible remote acceptance is reported as failed/UNKNOWN publication; the record may already be queryable and may reset the silence clock, so no absence or non-publication claim is made
- **AND** failed or unknown acceptance of the prior data batch prevents the separate receipt request; a partial OTLP rejection cannot share a batch with and accidentally accept the completion receipt

#### Scenario: Completion distinguishes complete VM lookup outcomes from unknown reads
- **WHEN** NetBox is configured and each expected service lookup returns HTTP 200 with an integer `count` equal to the number of returned rows
- **THEN** zero matches and multiple matches remain visible nonblocking outcomes with no VM PATCH selected, while exactly one match selects that VM's required PATCH
- **AND** a failed, missing, duplicate, malformed, incomplete, or unknown configured lookup suppresses the completion receipt without turning into a known no-match result or failing the existing nonblocking collector path
- **AND** a failed, skipped, missing, duplicate, or unknown required PATCH suppresses the completion receipt; only one explicit HTTP 200 for every unique match completes the configured write boundary

#### Scenario: Collector silence alert observes delivered completion
- **WHEN** the collector's exact completion receipt is queryable in Loki and the job is declared in the existing scheduled-job silence configuration
- **THEN** the shared silence rule alerts after a reviewed integer-hour window selected from the verified effective schedule, normal run duration, and allowed missed runs
- **AND** receiver acceptance alone does not count as durable Loki ingestion; a known failure before the send boundary does not reset the clock, while ambiguous post-acceptance acknowledgement may have delivered a record that resets it, and the existing `renew-internal-certs` 36-hour default is preserved
- **AND** the live schedule, queryable success receipt, pre-boundary failure behavior, ambiguous-acknowledgement status/readback, rule expression/state, and existing notification delivery are separately verified before acceptance
