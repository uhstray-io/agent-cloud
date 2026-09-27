## Purpose

Carries the inference gateway's metrics, traces and access records into the platform's
Grafana with bounded labels, declared retention, an authenticated transport and no
request content, in production and local-dev alike.

## ADDED Requirements

### Requirement: Gateway metrics are collected with the client identity
The platform SHALL collect the gateway's Prometheus metrics into the platform
Prometheus, labelled `service="agentgateway"`, with every request and token series
carrying the calling identity as a label, and the gateway's metrics listener MUST
accept connections only from the observability host.

#### Scenario: Metrics target is up and carries the identity
- **WHEN** the gateway and the observability stack are deployed and one enrolled
  identity completes a chat request through the gateway
- **THEN** Prometheus reports the gateway target `up == 1` and returns a request series
  and a token-usage series labelled with that identity's name

#### Scenario: Metrics listener refuses other hosts
- **WHEN** a LAN host other than the observability host connects to the gateway's
  metrics port
- **THEN** the connection is refused

### Requirement: Every gateway request produces an access record in Loki
The gateway SHALL export one access record per request to the platform's Loki, carrying
at least the identity, requested and served model, input, output and reasoning token
counts, first-token latency on streamed responses, HTTP status, rejection reason and
duration. Only `service`, `component`, `signal` and `cluster` MAY be Loki labels on
these records; identity, model and token values MUST stay in the record body.

#### Scenario: A request is findable by identity
- **WHEN** an enrolled identity sends one chat completion through the gateway
- **THEN** a Loki query on `{service="agentgateway", signal="access-log"}` filtered by
  that identity returns the record with its model names and token counts

#### Scenario: Identity is not an index label
- **WHEN** the Loki label names for the gateway's access records are listed
- **THEN** the list contains no identity, model or token label

### Requirement: No request content or credential leaves the gateway
The gateway MUST NOT place prompt text, completion text, any request header value
(including Authorization) or any API key material in any metric label, span attribute,
access-record field, exported record or request-log database row, and prompt and
completion logging SHALL remain disabled. The deployment MUST refuse to render a gateway
configuration that would capture such content.

#### Scenario: Content-capturing configuration is refused
- **WHEN** a deployment is attempted with a telemetry field that reads prompt content,
  completion content, a request header or an API key
- **THEN** the deployment fails before the gateway is restarted and names the offending
  field

#### Scenario: The request-log store holds no content
- **WHEN** requests with prompts flow through the gateway for an hour
- **THEN** the gateway's request-log payload table has no rows and no exported record or
  span contains the prompt or the caller's key

### Requirement: Gateway traces reach the platform trace store at a declared rate
The gateway SHALL export spans for a declared fraction of requests to a self-hosted trace
store that Grafana queries, with trace-to-log navigation to the matching access records.
A request that arrives already carrying trace context MUST NOT be traced on the caller's
say-so. Trace export MUST NOT be enabled until the platform's trace rollout gate is
recorded as passed, and stored traces MUST expire within the declared retention period.

#### Scenario: A sampled request is searchable in Grafana
- **WHEN** traces are enabled with a sampling fraction of one for a test window and one
  request is sent through the gateway
- **THEN** a Grafana trace search for service `agentgateway` returns its trace, and the
  trace links to the request's access record in Loki

#### Scenario: Caller-supplied trace context does not force a trace
- **WHEN** traces are enabled with a sampling fraction of zero and a request carrying a
  `traceparent` header is sent through the gateway
- **THEN** no trace for that request appears in the trace store

#### Scenario: Trace enablement waits for the rollout gate
- **WHEN** gateway trace export is requested before the trace rollout gate is recorded as
  passed
- **THEN** the deployment refuses and reports the missing gate

### Requirement: The telemetry push hop is mutually authenticated and encrypted
Outside local-dev, the gateway's export of access records and spans to the observability
host MUST use TLS with a certificate from the platform's internal CA on both ends, and
the receiver MUST reject a sender that presents no valid client certificate. A plaintext
export SHALL be permitted only in local-dev and only when explicitly declared.

#### Scenario: Plaintext export is refused in production
- **WHEN** a production deployment declares a plaintext telemetry export
- **THEN** the deployment fails before any container is restarted

#### Scenario: A sender without a client certificate is rejected
- **WHEN** a client connects to the observability host's telemetry receiver without a
  certificate issued by the internal CA
- **THEN** the TLS handshake fails and no record is ingested

### Requirement: A provisioned dashboard shows the gateway's client view
The observability stack SHALL provision, from committed configuration alone, a gateway
dashboard showing p50 and p95 first-token latency, request duration, error ratio and
per-identity request rate, token usage by identity and model, rejections by reason, the
gateway's access records and, when traces are enabled, a trace search.

#### Scenario: Dashboard survives a rebuild
- **WHEN** the observability stack is wiped and redeployed through Semaphore after one
  hour of gateway traffic has been collected
- **THEN** the gateway dashboard loads without manual steps and its client-view row
  renders first-token latency percentiles and per-identity request counts

### Requirement: Local-dev collects the same signals by the same code
Local-dev SHALL deploy the gateway's telemetry from the same templates and playbooks as
production, differing only in inventory values and compose overlays.

#### Scenario: Local deploy shows metrics and access records
- **WHEN** the local gateway and local observability stack are deployed through the
  local Semaphore and one keyed request is sent through the gateway
- **THEN** the local Grafana's gateway dashboard shows that request's identity in the
  metrics and in an access record
