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

The historical failure panel SHALL read failed conformance records through Loki query-time JSON parsing and format each result with its service, step, task ID, and an error excerpt truncated to 180 Unicode characters using Go `printf` rune precision. After formatting, the Loki pipeline SHALL keep only the original `job`, `service`, `step`, and `status` labels so extracted JSON fields such as the full `error` do not appear as result labels. The full original record SHALL remain stored and queryable in Loki Explore. Grafana SHALL use exact deduplication for visual display in this panel only; exact deduplication compares adjacent returned lines, can leave interleaved repeats, and does not replenish lines suppressed at the 100-line query cap. It MUST NOT remove or deduplicate stored records. The panel SHALL retain its seven-day time scope and 100-line maximum. Query-time formatting and visual display deduplication MUST NOT change collector output, Loki ingestion, stored log bodies, or bounded Loki labels. Current workflow state MUST remain a separate view.

#### Scenario: Repeated historical failures are readable without losing task identity

- **WHEN** the seven-day history contains repeated snapshots of the same failed task and other failed tasks
- **THEN** each displayed line is keyed by service, step, and task ID and includes at most the first 180 Unicode characters of its error text
- **AND** the 180-character boundary preserves a character after 179 ASCII characters, including a multibyte character such as `é`
- **AND** the query result retains only the original `job`, `service`, `step`, and `status` labels after formatting
- **AND** the full original record remains stored and can be queried in Loki Explore
- **AND** Grafana exact deduplication compares adjacent returned lines only, so interleaved repeats may remain and duplicates do not cause replacement lines to be fetched
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

### Requirement: Dashboard verification reports bounded current workflow failures

The existing read-only dashboard verifier SHALL report current failed service and workflow-step pairs only when the selected panel matches the exact provisioned latest-status query contract and the pair identities are present in the public component directories and the actual `steps` collection of the service-onboarding workflow registry. The controller SHALL parse and validate that canonical registry before invoking the receiver-side verifier; caller-supplied survey values MUST NOT replace it. The verifier SHALL accept exactly the `service` and `step` labels, validate the complete Loki vector sample shape and types before interpreting its timestamp or state code, recognize only the provisioned failure, pass, and skip state codes, sort and cap failure pairs deterministically, and count unknown or ambiguous series categorically without returning their labels or sample values. It SHALL include `window_seconds: 2700` and `scope: latest_recorded_state` for present, none, no_data, and unverified observations. Missing query data SHALL remain `no_data`; it MUST NOT be interpreted as a cleared failure. The report SHALL describe latest state within the query's 45-minute lookback and MUST NOT claim complete history, original task provenance, dashboard rendering, or absence of older failures outside that lookback.

The observation SHALL report fixed numeric counts for unknown label shapes, unregistered identities, malformed samples, and duplicate pairs, while preserving the total unknown-series count and `unverified` status. It MUST NOT echo unknown labels or sample values. When both the exact latest-status panel and the exact latest workflow completion-age panel are selected, the report SHALL attach a numeric completion age in seconds or null to each returned failed pair, along with known/unknown age counts. It SHALL validate the age panel's full existing query and datasource contract and exact public service/step labels. The 2700-second window bounds the observed collector snapshot; completion age derives from its workflow completion timestamp and MUST NOT be described as elapsed failure duration. Missing, duplicate, malformed, non-finite, boolean, negative, or explicit unknown ages SHALL be null and count as age-unknown; no zero fallback is permitted. When the completion-age panel is not selected, the report SHALL make no age query, set `completion_age_status` to `not_selected`, emit null pair ages, and count each returned failed pair as age-unknown. This age evidence SHALL NOT change the main workflow observation status or imply application health.

#### Scenario: Current failed workflow steps are identified safely

- **WHEN** the exact latest-status query returns a unique failed state for a registry-backed service and step
- **THEN** the verifier reports the validated pair in the bounded failure list
- **AND** newer pass or skip state is not reported as a current failure
- **AND** unknown labels, identities, states, and duplicate pairs make the observation `unverified` without echoing those values
- **AND** an empty result remains `no_data` rather than `none`
- **AND** malformed sample shapes, timestamps, or values make the observation `unverified`
- **AND** every result status reports its fixed 45-minute window and latest-recorded-state scope

#### Scenario: Failure uncertainty and completion age remain bounded

- **WHEN** the exact latest-status panel returns unknown rows, and both exact workflow panels are selected
- **THEN** the report splits unknown rows into fixed label-shape, identity, sample, and duplicate counters while retaining the total and `unverified` status
- **AND** the report never echoes unknown labels or sample values
- **AND** each returned failure pair includes a numeric completion age in seconds or null, with known and unknown age counts
- **AND** the verifier joins age only from one unambiguous non-hidden target matching the exact panel, reference, datasource UID and type, expression, and query mode
- **AND** missing, duplicate, malformed, non-finite, boolean, negative, and explicit unknown ages are null and counted as unknown
- **AND** when the completion-age panel is not selected, the verifier makes no age query, reports `completion_age_status: not_selected`, emits null ages, and counts each returned pair as age-unknown
- **AND** age evidence does not alter the main workflow observation status or assert application health

#### Scenario: Latest-status query or identity source drifts

- **WHEN** the dashboard query contract changes or the public identity sources cannot be read or parsed
- **THEN** the verifier marks the observation `unverified` and returns no service or step labels

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

The Service Overview SHALL preserve its dashboard and datasource identities, display existing service metrics and available container, access-log, span, conformance, and unclassified streams, and keep missing-source evidence distinct from service health. The unclassified source means the selected record has no nonempty `container` label and no recognized `signal`; it MUST NOT be described as access data, attributed to a producer, or treated as a health signal. Aggregate log-source counts MUST use disjoint selectors; an all-stream drill-down MUST NOT be included in those totals. New telemetry labels MUST remain bounded.

#### Scenario: Journal container records are visible

- **WHEN** the selected service has journal records labeled `signal="container"` but no `container` label
- **THEN** the Overview’s container-log view can query those records
- **AND** its aggregate log-source rate counts each record in one source only

#### Scenario: Records without source classification are visible

- **WHEN** the selected service has a record without a nonempty `container` label and without a recognized `signal`
- **THEN** the Overview's unclassified source view can query that record
- **AND** the source-rate and observed-record selectors exclude legacy containers, journal containers, access logs, spans, and conformance records
- **AND** the dashboard identifies the record only as unclassified and makes no service-health or access-record claim

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
