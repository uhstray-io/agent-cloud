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

### Requirement: OIDC callback queries stay out of agentgateway telemetry
Agentgateway stdout access logs, OTLP access records, and sampled trace spans MUST omit OIDC callback query parameters. Their `http.path` attribute MUST remain query-free; applicable status, identity, model, usage, and trace-correlation fields MUST remain available on representative model requests. Configuration alone MUST NOT count as runtime proof.

#### Scenario: Callback query marker is absent from emitted telemetry
- **WHEN** a synthetic query marker is sent to the agentgateway OIDC callback and its callback trace is sampled, and a separate representative model request produces its own sampled trace
- **THEN** the marker is absent from stdout access logs, OTLP access records, and sampled trace-span attributes
- **AND** each emitted `http.path` contains only the callback path
- **AND** applicable status, identity, model, usage, and trace correlation remain available for the representative model request
- **AND** the verification records the pinned-image configuration acceptance and exact reviewed deployment revision

### Requirement: Callback-path proxy logging is assessed separately
The platform SHALL assess each declared proxy hop's access-log behavior for OIDC callback query exposure before claiming callback redaction covers the full request path. Missing source configuration alone MUST NOT be treated as proof of effective runtime behavior or as proof of a defect.

#### Scenario: Edge callback logging is evidence checked
- **WHEN** callback-query redaction is evaluated across the public route
- **THEN** the Caddy site fragment, effective runtime configuration, log sinks, and retention are reviewed
- **AND** any unavailable runtime evidence is recorded as unknown

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

#### Scenario: Agentgateway trace reaches Tempo
- **WHEN** the reviewed gateway sends a sampled request through its declared OTLP endpoint
- **THEN** Alloy forwards the span to Tempo and the access record to Loki with the same stable service identity
- **AND** Grafana can pivot from the trace to service logs and metrics, and the receiver is not considered ready before real metrics, logs, and a trace are read back

### Requirement: Service graphs and signal links are bounded and evidence based
Tempo-derived service-graph and span metrics SHALL remain disabled by default and MUST be bounded by finite dimensions and an active-series ceiling. Production enablement MUST require numeric Semaphore receipts for receiver capacity, backup/isolated restore, metrics, alert delivery, retention, and cardinality. Verified spans MUST underpin accepted graph edges. A graph node alone, especially an inferred peer node, MUST NOT prove that the named service emits traces; per-service trace coverage requires an exact-target receipt. Missing or unsupported signals MUST be recorded as such rather than inferred from configuration. Trace-to-profile links SHALL map only the declared Pyroscope service identity and require spans carrying the span-profile bridge attribute before a profile pivot is claimed.

#### Scenario: Derived metrics stay off until production gates pass
- **WHEN** the production receiver is deployed without an explicit derived-metrics enablement and numeric capacity, backup/restore, metrics, alert-delivery, retention, and cardinality Semaphore receipts
- **THEN** Tempo's metrics-generator processors remain disabled
- **AND** an attempted enablement with any receipt absent fails before receiver placement

#### Scenario: Graph contains only verified request-serving services
- **WHEN** a service coverage cohort is rolled out
- **THEN** verified spans underpin any accepted graph edge, and per-service trace coverage is recorded only after an exact-target receipt proves that service's request spans reached Tempo
- **AND** a graph node alone, including an inferred peer node, is not accepted as proof that the named service emits traces
- **AND** services without request traces or with unsupported signal paths are recorded as not applicable or unsupported

#### Scenario: Signal coverage is recorded per service
- **WHEN** a cohort rollout is reported complete
- **THEN** its coverage record states log, metric, trace, and profile applicability per service
- **AND** each applicable signal has an exact-target readback receipt
- **AND** profiles count as correlated only when profile data and the span-profile bridge are present

#### Scenario: Grafana major-version dependency stays gated
- **WHEN** trace-correlations UI from Grafana 12 is proposed while the service runs pinned Grafana 11.4
- **THEN** the upgrade remains a separate change with backup/restore and capacity gates
- **AND** no production version changes as part of this baseline

### Requirement: Imported observability dashboards have live sources
The platform SHALL provision an agentgateway dashboard from the standalone metric contract and SHALL migrate original o11y dashboard content only when its referenced backend and exporters are deployed. Provisioned dashboards MUST use stable data-source UIDs and retained queryable metric names.

#### Scenario: Agentgateway traffic is visible
- **WHEN** a production gateway request completes and its metrics are scraped
- **THEN** the provisioned dashboard displays request and LLM activity from that target, or names the missing required series during verification

#### Scenario: Legacy dashboard requires an absent backend
- **WHEN** a dashboard from the original o11y repository requires Mimir or an undeployed exporter
- **THEN** its migration is recorded as deferred rather than provisioning an empty or misleading dashboard

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
