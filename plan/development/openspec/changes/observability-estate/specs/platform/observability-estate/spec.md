## Purpose

Provide a repeatable way for agent-cloud services to expose useful telemetry and receive actionable alerts without per-service monitoring configuration.

## ADDED Requirements

### Requirement: Declared metrics are discovered safely
The platform SHALL collect metrics from a service that declares a scrape endpoint, only when the endpoint is reachable from the collector through an approved network path. A service without that declaration SHALL not be scraped.

#### Scenario: Opted-in service is collected
- **WHEN** a deployed service declares its metrics endpoint and the collector can reach it
- **THEN** the service appears as an UP target without a hand-written per-service Prometheus job

#### Scenario: Unreachable endpoint fails visibly
- **WHEN** a declared endpoint cannot be reached from the collector
- **THEN** verification fails with the service and endpoint named rather than treating the service as covered

### Requirement: Signals share an identity
Metrics, logs, and traces for an instrumented container MUST carry one stable service identity derived from its declared container name. Telemetry labels MUST not contain credentials or personal data.

#### Scenario: Operator pivots between signals
- **WHEN** an operator selects a service in the generic dashboard
- **THEN** its metrics and logs use the same service identity and the selection can be reused for trace lookup when traces exist

### Requirement: Baseline views and alerts are provisioned
The platform SHALL provision a generic service view and baseline service-down and missing-telemetry alert rules from version-controlled files. Notification credentials MUST come from OpenBao.

#### Scenario: Rebuild restores views and rules
- **WHEN** o11y is redeployed from the same commit and inventory
- **THEN** the view and rules are present without manual Grafana edits

#### Scenario: Failure reaches an operator
- **WHEN** a verified pilot service stops reporting beyond the configured pending interval
- **THEN** the matching alert fires and reaches the configured notification destination

#### Scenario: Canary restores paused alerts
- **WHEN** the local or production delivery drill completes or fails after temporarily activating alerting
- **THEN** the provisioned rules return to paused state and the temporary contact point is removed before the drill reports completion
- **AND** persistent notification enablement remains a separate reviewed rollout

### Requirement: Onboarding verifies observability
The service onboarding workflow SHALL record whether logs, health, and applicable metrics are verified. It MUST not mark a service observability-complete on the basis of configuration files alone.

#### Scenario: New service has live evidence
- **WHEN** a service onboarding run claims observability complete
- **THEN** the run includes a successful live signal query for that service

### Requirement: Tracing follows the metrics and alert gate
The platform SHALL enable trace ingestion only after the metrics, alert delivery, retention, and cardinality gates pass. Trace data MUST remain self-hosted and expire within the declared retention period.

#### Scenario: Trace rollout gate
- **WHEN** the operator requests trace deployment before those gates are recorded as passed
- **THEN** the deployment refuses to enable the trace receiver and reports the missing gate

### Requirement: Local validation uses the proposed revision
The platform SHALL validate the exact proposed revision through an isolated local Semaphore repository/template binding without repointing the shared worktree binding. Grafana SHALL complete an Authentik sign-in through the chain-verified TLS route before local acceptance.

#### Scenario: Proposed revision is validated
- **WHEN** the local pilot deployment finishes
- **THEN** its Semaphore record identifies the proposed revision and live queries prove Grafana login, metrics, logs, and alert delivery

### Requirement: Production receiver proves external receipt
The production o11y deployment SHALL use Semaphore, OpenBao, and private site inventory for the receiver, retention, SSO, and approved network paths. A declared external collector SHALL not count as integrated until a named signal is queryable from the production receiver.

#### Scenario: DGX Spark and agentgateway telemetry arrives
- **WHEN** their approved collectors send telemetry to the production receiver
- **THEN** named DGX node and vLLM targets are UP, DGX logs are queryable in Loki, and agentgateway's agreed signal is queryable with stable service identity

#### Scenario: Optional GPU exporter is absent
- **WHEN** a DGX GPU exporter has not passed host and reachability validation
- **THEN** the GPU scrape job remains disabled and its absence is reported without claiming GPU coverage
