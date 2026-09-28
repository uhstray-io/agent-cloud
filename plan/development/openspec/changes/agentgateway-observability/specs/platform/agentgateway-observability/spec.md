## Purpose

Carries the inference gateway's metrics, traces and access records into the platform's
Grafana with bounded labels, declared retention, an authenticated transport and no
request content, and keeps the uhstray.io team's prompt and completion content, for a
limited time, only in the gateway's own access-controlled store.

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
The gateway SHALL export one access record per request to the platform's Loki. Every
record MUST carry the HTTP status and the duration. A request that reaches inference
MUST also carry the identity, requested and served model, input, output and reasoning
token counts, and first-token latency when the response is streamed. A request the
gateway rejects before inference (a missing or invalid key, a rate-limit refusal or a
token-budget refusal) MUST carry its rejection reason; on such a record the served
model, the token counts and the first-token latency MAY be absent, the requested model
MAY be absent when the request was refused before its body was read, and the identity
MAY be absent only when authentication itself failed. Only `service`, `component`,
`signal` and `cluster` MAY be Loki labels on these records; identity, model and token
values MUST stay in the record body.

#### Scenario: A rejected request still leaves a record
- **WHEN** an enrolled identity's request is refused by the gateway's request rate
  limit, and separately a request carrying no key is refused
- **THEN** Loki holds one record for each: the refused identity's record carries that
  identity, HTTP status 429 and its rejection reason; the keyless record carries HTTP
  status 401 and its rejection reason; neither record is required to carry a served
  model or token counts

#### Scenario: A request is findable by identity
- **WHEN** an enrolled identity sends one chat completion through the gateway
- **THEN** a Loki query on `{service="agentgateway", signal="access-log"}` filtered by
  that identity returns the record with its model names and token counts

#### Scenario: Identity is not an index label
- **WHEN** the Loki label names for the gateway's access records are listed
- **THEN** the list contains no identity, model or token label

### Requirement: Team prompt content is kept only in the gateway's own store
When content logging is enabled, the gateway SHALL keep the prompt and completion
content of team requests in its own request-log database, readable only through the
gateway's authenticated operator interface by members of the platform admin group. No
metric label, span attribute, access-record field, exported record or Loki line MAY
contain prompt or completion text.

#### Scenario: A team request's content is stored
- **WHEN** content logging is enabled and a team identity sends a chat completion
  through the gateway
- **THEN** the gateway's request-log database holds a payload row for that request with
  its prompt and completion

#### Scenario: Loki holds no prompt text
- **WHEN** a team identity sends a chat completion containing a unique marker string
- **THEN** a Loki search for that marker string returns nothing and no span attribute in
  the trace store contains it

#### Scenario: Content is readable only by platform admins
- **WHEN** an unauthenticated browser, or a signed-in user outside the platform admin
  group, requests a stored log entry through the operator interface
- **THEN** the gateway does not return the entry's content

### Requirement: Content is kept only for uhstray.io team identities
Every identity enrolled at the gateway SHALL declare its team. While content logging is
enabled, the deployment MUST refuse to render any identity not declared as a uhstray.io
team member, and the gateway MUST refuse requests from any key not marked as a team
member. Users outside the team SHALL be served, when that feature exists, by a gateway
instance that does not keep content.

#### Scenario: A non-team identity is refused at render
- **WHEN** content logging is enabled and inventory enrols an identity whose team is not
  uhstray.io, or declares no team
- **THEN** the deployment fails before the gateway is restarted and names the identity

#### Scenario: A non-team key is refused at request time
- **WHEN** content logging is enabled and a request arrives with a valid key whose
  identity is not marked as a team member
- **THEN** the gateway refuses the request and no payload row is written for it

### Requirement: Stored content expires within the declared retention
The platform SHALL delete request-log rows, with their content, once they are older than
the declared retention period (90 days unless inventory declares otherwise), on a
schedule declared as code, and MUST detect a retention job that has stopped running.

#### Scenario: An expired row is removed
- **WHEN** the scheduled prune runs and a request-log row is older than the retention
  period
- **THEN** the row and its payload are deleted and a second run deletes nothing

#### Scenario: A stopped prune is detected
- **WHEN** the oldest request-log row is older than the retention period plus two days
- **THEN** the gateway deployment's verification fails and names the retention breach

### Requirement: No header, request body or key reaches any signal or store
The gateway MUST NOT place any request header value (including Authorization), any
request body expression or any API key material in any metric label, span attribute,
access-record field, exported record or request-log database attribute, and MUST NOT
reference prompt or completion content in any telemetry field expression. The deployment
MUST refuse to render a gateway configuration that would.

#### Scenario: Content-capturing configuration is refused
- **WHEN** a deployment is attempted with a telemetry field that reads prompt content,
  completion content, a request header, the request body or an API key
- **THEN** the deployment fails before the gateway is restarted and names the offending
  field

#### Scenario: Header and key are never stored
- **WHEN** a team identity sends a request through the gateway with content logging
  enabled
- **THEN** neither the caller's key nor its Authorization header value appears in the
  request-log database, in Loki or in the trace store

### Requirement: Gateway traces reach the platform trace store at a declared rate
The gateway SHALL export spans for a declared fraction of requests, over the
observability host's authenticated telemetry receiver, to the platform's Tempo ingest
endpoint named in inventory; that trace store is provided by separate work, and no
endpoint host SHALL be assumed. A request that arrives already carrying trace context
MUST NOT be traced on the caller's say-so. Trace export MUST NOT be enabled until the
Tempo endpoint is declared and the platform's trace rollout gate is recorded as passed.

#### Scenario: A sampled request is searchable in Grafana
- **WHEN** traces are enabled with a sampling fraction of one for a test window and one
  request is sent through the gateway
- **THEN** a search on the declared Tempo query endpoint for service `agentgateway`
  returns its trace, and the request's access record in Loki carries the same trace id

#### Scenario: Caller-supplied trace context does not force a trace
- **WHEN** traces are enabled with a sampling fraction of zero and a request carrying a
  `traceparent` header is sent through the gateway
- **THEN** no trace for that request appears in the trace store

#### Scenario: Trace enablement waits for the rollout gate
- **WHEN** gateway trace export is requested before the trace rollout gate is recorded as
  passed, or before a Tempo ingest endpoint is declared
- **THEN** the deployment refuses and reports what is missing

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

### Requirement: The gateway's client view is a separate provisioned dashboard
The observability stack SHALL provision, from committed configuration alone, a gateway
client-view dashboard of its own, not a row on another dashboard, showing p50 and p95
first-token latency, request duration, error ratio and per-identity request rate, and a
separate gateway operations dashboard showing token usage by identity and model,
rejections by reason, process health, the gateway's access records and, when a trace
store is available, a trace search.

#### Scenario: Dashboard survives a rebuild
- **WHEN** the observability stack is wiped and redeployed through Semaphore after one
  hour of gateway traffic has been collected
- **THEN** the gateway client-view dashboard loads without manual steps and renders
  first-token latency percentiles and per-identity request counts, and the operations
  dashboard loads alongside it

### Requirement: Local-dev collects the same signals by the same code
Local-dev SHALL deploy the gateway's telemetry from the same templates and playbooks as
production, differing only in inventory values and compose overlays.

#### Scenario: Local deploy shows metrics and access records
- **WHEN** the local gateway and local observability stack are deployed through the
  local Semaphore and one keyed request is sent through the gateway
- **THEN** the local Grafana's gateway dashboards show that request's identity in the
  metrics and in an access record
