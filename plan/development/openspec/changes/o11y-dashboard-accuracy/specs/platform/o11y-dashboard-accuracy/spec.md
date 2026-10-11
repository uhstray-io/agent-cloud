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

### Requirement: Dashboard verification distinguishes expected empty panels

The existing read-only dashboard verifier SHALL allow a caller to name exact selected panels for which an empty query result is healthy. It MUST report those panels as expected empty, while query errors and empty required panels remain failures.

#### Scenario: An exception panel has no matching rows

- **WHEN** a named selected exception panel returns no data and its query succeeds
- **THEN** verification reports `expected_empty` for that panel and the run may pass if all required panels pass

#### Scenario: Required panel is empty or its query fails

- **WHEN** a required selected panel returns no data or its backend query fails
- **THEN** verification fails and reports the panel outcome without exposing backend error bodies or sample values

### Requirement: Service Overview presents existing signals without double counting

The Service Overview SHALL preserve its dashboard and datasource identities, display existing service metrics and available container, access-log, span and conformance streams, and keep missing-source evidence distinct from service health. Aggregate log-source counts MUST use disjoint selectors; an all-stream drill-down MUST NOT be included in those totals. New telemetry labels MUST remain bounded.

#### Scenario: Journal container records are visible

- **WHEN** the selected service has journal records labeled `signal="container"` but no `container` label
- **THEN** the Overview’s container-log view can query those records
- **AND** its aggregate log-source rate counts each record in one source only

#### Scenario: A selected signal has no live records

- **WHEN** a selected service has no matching records for a source
- **THEN** the Overview shows no source data rather than a healthy zero or a service-health claim

#### Scenario: Dashboard identity remains stable

- **WHEN** the dashboards are provisioned after the change
- **THEN** Grafana retains UIDs `service-conformance` and `service-overview` and datasource UIDs `prometheus` and `loki`
