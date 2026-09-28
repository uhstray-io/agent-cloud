## Purpose

Define bounded signal identity, service visibility, continuous profiling, and actionable alert delivery for the self-hosted observability estate.

## ADDED Requirements

### Requirement: Bounded cross-signal identity

The observability configuration SHALL use a stable declared service identity across metrics, logs, traces, profiles, and alerts. Indexed labels SHALL be bounded by inventory or a controlled enumeration; request identifiers, user identifiers, raw paths, addresses, timestamps, and secrets MUST NOT become metric or Loki stream labels.

#### Scenario: A new service is instrumented

- **WHEN** a service is added to the instrumentation inventory
- **THEN** its signal declarations name the same service identity and cluster or environment context, and document the finite value set of every indexed label

#### Scenario: A high-cardinality attribute is received

- **WHEN** a request, user, trace, or raw-path attribute reaches the collector
- **THEN** the attribute remains in the log body or structured metadata, or is omitted, and does not become a metric or Loki stream label

### Requirement: Honest service visibility

Grafana SHALL show declared service health and signal freshness across local and remote targets without equating an absent signal to a healthy service. Dashboards SHALL preserve links from logs to traces and expose cardinality or ingestion-volume context for operators.

#### Scenario: A declared remote target stops reporting

- **WHEN** a declared remote service has no recent metrics
- **THEN** the service view or alert indicates missing telemetry with service and cluster context instead of silently removing the service from the view

### Requirement: Private, optional continuous profiling

The stack SHALL provision Pyroscope with persistent storage and no public listener. Profile collection SHALL require a declared producer and a measured resource and privacy budget; disabling collection MUST preserve stored profiles and existing telemetry volumes.

#### Scenario: Profile pilot is disabled

- **WHEN** the profile pilot is not enabled in inventory
- **THEN** Alloy does not scrape profiling targets and the normal deploy does not delete profile or other observability volumes

#### Scenario: Profile pilot is enabled

- **WHEN** the declared pprof endpoint is reachable and the pilot is enabled
- **THEN** Alloy forwards samples to Pyroscope under the service identity and Grafana can query that profile series

### Requirement: Actionable and bounded alerts

Grafana-managed alerts SHALL carry severity, service, owner, environment, and cluster context. Alert grouping SHALL use stable bounded labels and deliberate notification timings on the existing contact point. Configuration MUST NOT replace the shared notification policy tree.

#### Scenario: Several target failures occur in one service

- **WHEN** multiple related target alerts fire in the same service and cluster
- **THEN** the configured notification grouping delivers a bounded incident summary with a useful runbook or dashboard link, while retaining individual alert details

#### Scenario: A notification change is deployed

- **WHEN** a reviewed configuration is applied through Semaphore
- **THEN** deployment verifies the provisioned rule labels and timing configuration, and a delivery drill confirms the Discord destination before acceptance

### Requirement: Repeatable rollout evidence

The implementation SHALL be reviewed on an isolated branch, promoted through `dev`, and applied via a non-destructive Semaphore workflow. The estate-wide plan SHALL record applicability, label budget, owner, signal proof, and resource headroom for each onboarding wave.

#### Scenario: Configuration is promoted

- **WHEN** a new o11y configuration is deployed
- **THEN** normal deployment preserves existing volumes and produces source, CI, Semaphore, and runtime evidence separately
