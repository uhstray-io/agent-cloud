## Purpose

Define accurate, evidence-based behavior for the Service deployment conformance and Service Overview dashboards, including source freshness and intentional empty results.

## ADDED Requirements

### Requirement: Conformance coverage reflects inventoried services

The conformance dashboard SHALL count tracked services from valid inventory-marker records, independently of whether a service has completed a workflow step. It SHALL show the age or freshness state of the newest marker and SHALL distinguish unavailable source evidence from a healthy current source.

#### Scenario: Inventory markers identify the tracked estate

- **WHEN** the collector publishes valid inventory markers for tracked services
- **THEN** the tracked count equals the distinct services represented by those markers
- **AND** services without workflow history remain represented as tracked

#### Scenario: Collector source is stale or unavailable

- **WHEN** no valid marker arrives within the freshness boundary calibrated from observed collection intervals, task duration and delivery delay
- **THEN** the dashboard identifies the source as stale or unavailable
- **AND** it does not present missing conformance data as a healthy zero

### Requirement: Latest conformance state survives collection gaps

The dashboard SHALL show the latest recorded pass, fail or skip state after collection resumes, including state retained because older workflow history is no longer available. It SHALL keep incomplete history distinct from current step failure.

#### Scenario: Collector recovers after a gap

- **WHEN** the collector resumes and emits a retained last-known step state
- **THEN** the dashboard shows that state for the service and step
- **AND** it identifies incomplete history separately when the collector marks it incomplete

### Requirement: Historical conformance failures remain bounded and task-keyed

The historical failure panel SHALL read failed conformance records through Loki query-time JSON parsing and format each result with its service, step, task ID, and an error excerpt truncated to 180 characters. The full original record SHALL remain stored and queryable in Loki Explore. Grafana SHALL use exact deduplication for visual display in this panel only; it MUST NOT remove or deduplicate stored records. The panel SHALL retain its seven-day time scope and 100-line maximum. Query-time formatting and visual display deduplication MUST NOT change collector output, Loki ingestion, stored log bodies, or bounded Loki labels. Current workflow state MUST remain a separate view.

#### Scenario: Repeated historical failures are readable without losing task identity

- **WHEN** the seven-day history contains repeated snapshots of the same failed task and other failed tasks
- **THEN** each displayed line is keyed by service, step, and task ID and includes at most the first 180 characters of its error text
- **AND** the full original record remains stored and can be queried in Loki Explore
- **AND** Grafana exact deduplication affects visual display only; stored records remain unchanged
- **AND** distinct task IDs remain distinct in the display
- **AND** the query stays within the seven-day and 100-line bounds
- **AND** current failed step states remain independent of historical failures

### Requirement: Latest workflow result age and provenance are truthful

Each conformance step record from a current task SHALL carry the workflow completion time as a
numeric `completed_at_seconds` value and the numeric Semaphore `task_id` in its record body,
never as Loki labels. A completion time SHALL be derived only from a valid timezone-aware task
end value. When completion time is missing or invalid, `completed_at_seconds` SHALL be
`unknown`, while a known task ID remains numeric. When a state is retained without the
originating task, both fields SHALL be `unknown`. Dashboard age MUST use workflow completion
time rather than Loki event time. The dashboard MUST map missing or `unknown` completion
timestamps to numeric `-1` before reducing the latest record by service and step. It MUST filter
`-1` only after that reduction when computing the newest-known aggregate, and it MUST provide
a per-service/step age table that displays `-1` as neutral `Unknown`.

#### Scenario: Current workflow result has valid provenance

- **WHEN** the newest conformance result has a valid timezone-aware task end time and task ID
- **THEN** its record body contains a numeric `completed_at_seconds` and numeric `task_id`
- **AND** neither field is added to the bounded Loki label set

#### Scenario: Retained or missing workflow provenance is unavailable

- **WHEN** a conformance state is retained without its originating task or its task end value is missing or invalid
- **THEN** unavailable provenance fields are reported as `unknown`
- **AND** the dashboard does not calculate a current result age from collector emission time

#### Scenario: Newer unknown completion replaces older known completion

- **WHEN** a newer step snapshot has missing or `unknown` completion time while an older known completion remains in the 45-minute window
- **THEN** the dashboard reduces the `-1` unknown value as that service and step's newest record
- **AND** the per-step age table displays `Unknown` instead of falling back to the older known age
- **AND** the newest-known aggregate excludes `-1` only after each service and step has been reduced

#### Scenario: Per-step completion ages align with latest state

- **WHEN** workflow step snapshots are available in the selected window
- **THEN** the dashboard shows one latest completion age per service and step
- **AND** operators can match each row to the latest step state by service and step
- **AND** unknown ages use a neutral display while inventory marker age remains separate

#### Scenario: A later pass replaces an earlier failure

- **WHEN** a newer completed workflow result passes after an older result failed
- **THEN** the latest state, task ID, and completion time all come from the newer passing result
- **AND** the prior failure does not remain the current result

### Requirement: Dashboard verification distinguishes expected empty panels

The existing read-only dashboard verifier SHALL allow a caller to name exact selected panels for which an empty query result is healthy. It MUST report those panels as expected empty, while query errors and empty required panels remain failures.

#### Scenario: An exception panel has no matching rows

- **WHEN** a named selected exception panel returns no data and its query succeeds
- **THEN** verification reports `expected_empty` for that panel and the run may pass if all required panels pass

#### Scenario: Required panel is empty or its query fails

- **WHEN** a required selected panel returns no data or its backend query fails
- **THEN** verification fails and reports the panel outcome without exposing backend error bodies or sample values

### Requirement: Dashboard verification reproduces bounded panel-relative time

The dashboard verifier SHALL accept `timeFrom` only as one positive Prometheus duration unit. It SHALL use the resulting per-panel lookback for range-query starts, range steps, and Grafana range built-ins. Instant queries SHALL remain evaluated at the current end time. Other unsupported panel and target settings SHALL remain refused.

#### Scenario: Selected panel has a supported relative range
- **WHEN** an evaluated panel declares `timeFrom` as one positive duration such as `7d` or `45m`
- **THEN** its range query starts at now minus that panel duration
- **AND** its step and interpolated Grafana range built-ins use that same duration

#### Scenario: Selected panel has an unsupported relative range
- **WHEN** an evaluated panel declares a malformed or compound `timeFrom`
- **THEN** verification refuses the panel before sending backend requests
- **AND** a valid `timeFrom` does not permit other unsupported panel settings

### Requirement: Service Overview presents existing signals without double counting

The Service Overview SHALL preserve its dashboard and datasource identities, display existing service metrics and available container, access-log, span and conformance streams, and keep missing-source evidence distinct from service health. Aggregate log-source counts MUST use disjoint selectors; an all-stream drill-down MUST NOT be included in those totals. New telemetry labels MUST remain bounded.

#### Scenario: Journal container records are visible

- **WHEN** the selected service has journal records labeled `signal="container"` but no `container` label
- **THEN** the Overview’s container-log view can query those records
- **AND** its aggregate log-source rate counts each record in one source only

#### Scenario: A selected signal has no live records

- **WHEN** a selected service has no matching records for a source
- **THEN** the Overview reports an explicit no-data observation for that source rather than a healthy zero or a service-health claim
- **AND** the source's ingestion cause remains unverified unless separate evidence establishes it

#### Scenario: Dashboard identity remains stable

- **WHEN** the dashboards are provisioned after the change
- **THEN** Grafana retains UIDs `service-conformance` and `service-overview` and datasource UIDs `prometheus` and `loki`

#### Scenario: Completion age table preserves row identity
- **WHEN** Loki returns separate frames for service and step completion ages
- **THEN** the table converts labels to columns, merges the frames, removes the time column, and renames the value column to Age
- **AND** service, step, and the `-1` to neutral `Unknown` mapping remain visible
