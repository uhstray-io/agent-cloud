## Purpose

Provide complete, evidence-backed logs, metrics, and tracing coverage for Agent Cloud's deployed services and infrastructure while keeping collection private, bounded, and repeatable.

## ADDED Requirements

### Requirement: Coverage inventory reflects deployed estate
The platform SHALL maintain a reviewable coverage declaration for every deployed Agent Cloud service, agent, VM, and managed infrastructure target. Each entry SHALL identify its owner, lifecycle state, signal applicability, collection method, expected identity, and last successful verification receipt. Planned, retired, unsupported, and intentionally excluded targets SHALL be distinguished from missing telemetry.

#### Scenario: Deployed target lacks a signal
- **WHEN** a deployed target has no verified required signal or its receipt is stale
- **THEN** the coverage report identifies that target and signal as incomplete without counting a healthy sibling as proof

#### Scenario: Scaffold is not counted as deployed
- **WHEN** a source directory exists without a deployed target declaration
- **THEN** the coverage report lists it as planned or unclassified and does not assert live telemetry

### Requirement: Required telemetry is collected and correlated
Every deployed target SHALL declare applicable logs, metrics, health, and traces with a verified source or a reviewed exclusion reason. Supported host and application logs SHALL be queryable; metrics-capable targets SHALL expose verified metrics; request-serving targets SHALL provide traces when their runtimes support safe instrumentation. The same declared service identity and inventory-derived environment SHALL join applicable logs, metrics, and traces, and verification SHALL use a fresh signal from the exact target.

#### Scenario: Service passes three-signal verification
- **WHEN** a service is declared to require logs, metrics, and traces and emits a controlled request
- **THEN** verification finds a fresh matching log, a healthy named metric target, and a retrievable trace with the declared service identity

#### Scenario: Instrumentation is unsupported
- **WHEN** automatic tracing is incompatible with a target runtime or would violate its security boundary
- **THEN** the declaration records the reason and an approved alternative signal or manual instrumentation plan, without claiming trace coverage

### Requirement: Telemetry is private and bounded
Collection SHALL use declared private network paths and source-scoped firewall rules, protect credentials and sensitive content, and enforce per-signal retention and ingestion/cardinality budgets before broadening the rollout. Remote logs and traces SHALL enter through receiver Alloy; remote metrics SHALL use declared private scrapes unless another reviewed path is established. The receiver SHALL surface dropped or refused telemetry and low disk headroom as observable failures.

#### Scenario: Ingestion budget exceeded
- **WHEN** a target exceeds a declared scrape, log, or trace budget
- **THEN** the collection path limits or rejects the excess, records the affected target, and alerts without exposing a secret or request body

#### Scenario: Unapproved sender attempts export
- **WHEN** a sender outside the declared private allowlist attempts to reach an ingestion endpoint from a known vantage host
- **THEN** the source-scoped firewall denies it and the source is not enrolled by discovery alone

### Requirement: Capacity gates each rollout wave
The platform SHALL compare observed and forecast ingestion, storage, CPU, and memory demand with the declared o11y VM capacity before enabling each wave. When the forecast lacks headroom, the wave SHALL wait for a reviewed, non-destructive capacity change and post-change readback.

#### Scenario: Wave exceeds capacity
- **WHEN** the forecasted wave exceeds a declared capacity or retention budget
- **THEN** rollout stops before enabling new collection and reports the limiting resource and proposed VM-spec change

#### Scenario: VM capacity increased
- **WHEN** a reviewed VM resource declaration is converged through Semaphore
- **THEN** the live CPU, memory, hypervisor disk, and guest filesystem configuration is read back; any disk growth used an idempotent guest-growth workflow, and the existing telemetry and alert baseline remains accessible before rollout resumes

### Requirement: Rollout and recovery are reproducible
Instrumentation and receiver changes SHALL be declared as code, applied through reviewed `dev` and Semaphore, and verified per wave. A failed wave SHALL be reversible by an operator-driven reviewed declaration revert and redeploy without deleting existing telemetry volumes.

#### Scenario: New instrumentation fails validation
- **WHEN** a rollout wave fails its exact-target verification or receiver-health gate
- **THEN** rollout stops; an operator reverts the declaration through the branch workflow and redeploys it through Semaphore, preserves stored telemetry, and records the failed target and validation receipt
