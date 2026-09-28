## Purpose

Carries the inference gateway's metrics, access records and traces into the platform's
Grafana. Each signal has its own switch, labels stay bounded, retention is declared,
and no request content is exported. The uhstray.io team's prompt and completion content
is kept for a limited time, and only in the gateway's own access-controlled store.

## ADDED Requirements

### Requirement: Each gateway signal is switched independently
The gateway deployment SHALL treat the observability receiver's address as an address
only. It SHALL enable access-record export and trace export through separate declared
switches. Access-record export MUST NOT require the trace rollout receipts. Trace
export MUST require them. A deployment that declares a receiver address with neither
switch MUST be refused.

#### Scenario: Access records ship without trace receipts
- **WHEN** the gateway is deployed with access-record export on, trace export off, and
  a receiver that has recorded no trace rollout receipts
- **THEN** the deployment succeeds, and the rendered gateway configuration carries an
  access-record export and no trace export

#### Scenario: A receiver address alone is refused
- **WHEN** the gateway is deployed with a receiver address and neither switch enabled
- **THEN** the deployment fails before the gateway is restarted and names both switches

### Requirement: Gateway metrics are collected with the client identity
The platform SHALL collect the gateway's Prometheus metrics into the platform
Prometheus, labelled `service="agentgateway"`, and every request and token series SHALL
carry the calling identity as a label. The gateway's metrics listener MUST accept
connections only from the observability host.

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
When access-record export is on, the gateway SHALL export one access record per request
to the platform's Loki. Every record MUST carry the HTTP status and the duration. A
request that reaches inference MUST also carry the identity, the identity's team, the
requested and served model, input, output and reasoning token counts, and first-token
latency when the response is streamed. A request the gateway rejects before inference
MUST carry its rejection reason. On such a record, the served model, the token counts
and the first-token latency MAY be absent. The requested model MAY be absent when the
request was refused before its body was read, and the identity MAY be absent only when
authentication itself failed. Only `service`, `component`, `signal` and `cluster` MAY be
Loki labels on these records; identity, team, model and token values MUST stay in the
record body. The gateway deployment MUST prove that its own verification request's
record reached Loki.

#### Scenario: A request is findable by identity
- **WHEN** an enrolled identity sends a request through the gateway
- **THEN** a Loki query on `{service="agentgateway", signal="access-log"}` filtered by
  that identity returns its record, and the gateway deployment's verification finds its
  own request's record the same way

#### Scenario: Identity is not an index label
- **WHEN** the Loki label names for the gateway's access records are listed
- **THEN** the list contains no identity, team, model or token label

#### Scenario: A rejected request still leaves a record
- **WHEN** an enrolled identity's request is refused by the gateway's request rate
  limit, and separately a request carrying no key is refused
- **THEN** Loki holds one record for each: the refused identity's record carries that
  identity, HTTP status 429 and its rejection reason; the keyless record carries HTTP
  status 401 and its rejection reason

### Requirement: Team prompt content is kept only in the gateway's own store
When content logging is enabled, the gateway SHALL keep the prompt and completion
content of team requests in its own request-log database. Over the network, that
content SHALL be readable only through the gateway's authenticated operator interface,
by members of the platform admin group; the gateway's admin listener, which serves the
same log API without a login, MUST stay bound to the container loopback and unpublished.
The deploy MUST refuse to enable content logging unless the gateway environment makes
the UI's configuration store read-only. When content logging is disabled, the gateway SHALL store request
metadata without content. No metric label, span attribute, access-record field,
exported record or Loki line MAY contain prompt or completion text.

#### Scenario: A team request's content is stored
- **WHEN** content logging is enabled and a team identity sends a chat completion
  through the gateway
- **THEN** the gateway's request-log database holds a payload row for that request with
  its prompt and completion

#### Scenario: Loki holds no prompt text
- **WHEN** a team identity sends a chat completion containing a unique marker string
- **THEN** a Loki search for that marker string returns nothing, and no span attribute
  in the trace store contains it

#### Scenario: Content is readable only by platform admins
- **WHEN** an unauthenticated browser, or a signed-in user outside the platform admin
  group, requests a stored log entry through the operator interface
- **THEN** the gateway does not return the entry's content

#### Scenario: Content logging needs a read-only UI
- **WHEN** content logging is requested for a gateway whose environment does not set
  the UI's configuration store read-only
- **THEN** the deploy fails before rendering and names the missing setting

### Requirement: Content is kept only for uhstray.io team identities
Every identity enrolled at the gateway SHALL declare its team. While content logging is
enabled, the deployment MUST refuse to render any identity that is not declared as a
uhstray.io team member. When users outside the team become a feature, they SHALL be
served by a gateway instance that does not keep content.

#### Scenario: A non-team identity is refused at render
- **WHEN** content logging is enabled and inventory enrols an identity whose team is not
  uhstray.io, or that declares no team
- **THEN** the deployment fails before the gateway is restarted and names the identity

### Requirement: Stored content expires within the declared retention
The platform SHALL delete request-log rows, together with their content, once they are
older than the declared retention period (90 days unless inventory declares otherwise),
on a schedule declared as code. It MUST raise an alert when that job stops running. A
stale retention job MUST NOT block a gateway deployment.

#### Scenario: An expired row is removed
- **WHEN** the scheduled prune runs and a request-log row is older than the retention
  period
- **THEN** the row and its payload are deleted, and a second run deletes nothing

#### Scenario: A stopped prune is detected
- **WHEN** no prune result has arrived within the alert window, or the oldest
  request-log row is older than the retention period plus two days
- **THEN** a Grafana alert fires and names the retention breach

#### Scenario: A stale prune never blocks a gateway deploy
- **WHEN** the prune alert is firing and a gateway deployment runs, for example to
  revoke a key
- **THEN** the gateway deployment is not refused on account of the prune

### Requirement: No header, request body or key reaches any signal or store
The gateway MUST NOT place any request header value (Authorization included), any
request body expression or any API key material in any metric label, span attribute,
access-record field, exported record or request-log database attribute. It MUST NOT
reference prompt or completion content in any telemetry field expression. The
repository's test suite MUST fail on a gateway configuration template that would.

#### Scenario: Content-capturing configuration is refused
- **WHEN** the gateway configuration template gains a telemetry field that reads prompt
  content, completion content, a request header, the request body, or an API key member
  other than its name and team
- **THEN** the test suite fails and names the offending reference

#### Scenario: Header and key are never stored
- **WHEN** a team identity sends a request through the gateway with content logging
  enabled
- **THEN** neither the caller's key nor its Authorization header value appears in the
  request-log database, in Loki or in the trace store

### Requirement: Gateway traces reach the platform trace store at a declared rate
When trace export is on, the gateway SHALL export spans for a declared fraction of new
requests, at most one in ten and 10 percent unless inventory declares a lower fraction,
through the observability host's receiver to the platform's Tempo. A request that
arrives already carrying trace context MUST NOT be traced on the caller's say-so. Trace
export MUST NOT be enabled until the platform's trace rollout gate is recorded as
passed. The platform SHALL raise an alert when gateway requests keep arriving but no
spans are received.

#### Scenario: A sampled request is searchable in Grafana
- **WHEN** trace export is on and the platform's trace canary sends its burst of
  anonymous requests to the gateway
- **THEN** a Tempo search for service `agentgateway` from the canary's start returns a
  trace

#### Scenario: Caller-supplied trace context does not force a trace
- **WHEN** trace export is on and a batch of requests is sent, each carrying a distinct
  caller-supplied trace context marked as sampled
- **THEN** the trace store does not hold a trace for every one of those trace ids

#### Scenario: Trace enablement waits for the rollout gate
- **WHEN** gateway trace export is requested before the trace rollout gate is recorded
  as passed
- **THEN** the deployment refuses and reports what is missing

### Requirement: The telemetry push hop moves to mutual TLS
Outside local-dev, until the platform's internal CA can issue both leaves, the gateway's
export to the observability host MAY be plaintext only to the receiver's declared
private, non-loopback address, behind a host firewall that admits only the gateway.
Once the receiver requires TLS, the gateway MUST export with a client certificate from
the internal CA and verify the receiver against that CA, and the receiver MUST reject a
sender that presents no valid client certificate.

#### Scenario: Plaintext export is refused once the receiver requires TLS
- **WHEN** the receiver declares TLS and the gateway deployment renders an export with
  no client certificate
- **THEN** the deployment fails before the gateway is restarted

#### Scenario: A sender without a client certificate is rejected
- **WHEN** a client connects to the observability host's telemetry receiver, which
  requires TLS, without a certificate issued by the internal CA
- **THEN** the TLS handshake fails and no record is ingested

### Requirement: The gateway has an operations dashboard and a separate client-view dashboard
The observability stack SHALL provision, from committed configuration alone, two gateway
dashboards. The client-view dashboard stands on its own, not as a row on another
dashboard, and shows p50 and p95 first-token latency, request duration, error ratio and
per-identity request rate. The operations dashboard shows token usage by identity and
model, rejections by reason, process health, the gateway's access records and a trace
search.

#### Scenario: Dashboard survives a rebuild
- **WHEN** the observability stack is wiped and redeployed through Semaphore after one
  hour of gateway traffic has been collected
- **THEN** the client-view dashboard loads without manual steps and renders first-token
  latency percentiles and per-identity request counts, and the operations dashboard
  loads alongside it

### Requirement: Local-dev collects the same signals by the same code
Local-dev SHALL deploy the gateway's telemetry from the same templates and playbooks as
production, differing only in inventory values and compose overlays.

#### Scenario: Local deploy shows metrics and access records
- **WHEN** the local gateway and local observability stack are deployed through the
  local Semaphore and one keyed request is sent through the gateway
- **THEN** the local Grafana's gateway dashboards show that request's identity in the
  metrics and in an access record
