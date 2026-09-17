# platform/inference-gateway

The authenticated, metered, observable inference edge in front of the DGX Spark vLLM
API, alongside skynet.

## ADDED Requirements

### Requirement: The gateway is a platform service on its own host
agentgateway SHALL run as an Infrastructure-tier platform service on a dedicated VM,
together with its own internal-only Postgres for budget usage, deployed and verified
through Semaphore from committed compose and configuration templates, with its admin
interface reachable only on the container loopback, its runtime credentials sourced from
OpenBao, and no credential in any rendered configuration file (environment references
and key hashes only).

#### Scenario: Deploy converges
- WHEN the gateway deploy template runs against the production inventory
- THEN the readiness endpoint answers, a second run reports no changes, and no
  credential appears in any committed file or container argument

#### Scenario: Admin interface is not exposed
- WHEN a LAN host connects to the gateway VM on the admin port
- THEN the connection is refused

### Requirement: The operator UI is reachable only through SSO
The gateway's built-in operator UI SHALL be served on its own listener, separate from
the admin interface (which stays on the container loopback), and MUST authenticate
browsers ITSELF with an OIDC policy against Authentik plus an authorization rule
requiring the platform admin group, so that the gateway reports the UI as
authenticated; Caddy SHALL be a plain TLS proxy in front, and the listener MUST NOT be
reachable from any host other than the Caddy host.

#### Scenario: UI requires an admin login
- WHEN an unauthenticated browser opens the gateway's UI hostname
- THEN the gateway redirects to Authentik's authorization endpoint, after an
  admin-group login the UI renders, and the UI shows no "exposed without
  authentication" warning

#### Scenario: Playground completes a request through the UI origin
- WHEN an admin, logged into the UI, sends a chat completion from the LLM Playground
  with an enrolled client key
- THEN the request reaches the gateway's `/v1` on the UI's own origin through Caddy,
  is authenticated by the API-key policy, and the completion is returned

#### Scenario: UI listener is not reachable around Caddy
- WHEN a LAN host that is not the Caddy host connects to the UI listener's port
- THEN the connection is refused

### Requirement: Client-visible contract is unchanged through the gateway
The gateway SHALL expose the vLLM served names at the same `/v1` paths the team uses,
and MUST pass chat completions, streaming, tool calls, `reasoning_effort`,
`chat_template_kwargs` and Responses API requests to vLLM without altering their
semantics or buffering the stream.

#### Scenario: Conformance against direct vLLM
- WHEN the same set of requests is sent to the gateway and directly to vLLM
- THEN the response bodies match apart from request identifiers and timing, and the
  gateway's first-token latency is within an agreed margin of the direct path

#### Scenario: Stream is not buffered
- WHEN a streamed chat completion with a long reasoning phase passes through the
  gateway
- THEN chunks and SSE keep-alive comments arrive at the client at the cadence vLLM
  emits them, and the stream completes past 130 seconds through the public hostname

### Requirement: Every client has its own identity and limit
The gateway SHALL authenticate `/v1` requests with per-client API keys stored in
OpenBao and enrolled as hashes, MUST apply a token budget per client identity and a
request-rate ceiling for the gateway as a whole (v1.5.0 offers no per-identity request
bucket without the verbose route shape; revisit when a release ships the bucket key),
and MUST accept the legacy shared key as one identity only during a dated grace period.

#### Scenario: Unknown key is rejected
- WHEN a request carries a key that is not enrolled
- THEN the gateway returns 401 and the request never reaches vLLM

#### Scenario: One client cannot exceed its share
- WHEN one identity exhausts its hourly token budget
- THEN its further requests are blocked by the gateway while other identities are
  served, and the budget window resets on the UTC hour

#### Scenario: Adding a client is an inventory change
- WHEN a name is added to the gateway's client list in inventory and the deploy runs
- THEN a key is generated once into OpenBao, enrolled as a hash, and reused unchanged on
  every later deploy

#### Scenario: Rotating a client key is explicit and never printed
- WHEN the key-management playbook runs with `action=rotate` for a declared client
- THEN a new value replaces the stored one, the gateway is re-rendered and reloaded, the
  old key gets 401, and the task output carries the field name only; the value reaches
  its owner through the site-config backup channel

#### Scenario: Revoking requires the inventory to forget the client first
- WHEN the playbook runs with `action=revoke` for a name still declared in inventory
- THEN it refuses; once the name is removed, revoke deletes the stored field and the
  reload drops the hash, so the key is rejected

#### Scenario: Grace period ends
- WHEN the dated grace period has passed
- THEN the shared key is no longer enrolled at the gateway and has been rotated at vLLM

### Requirement: Caddy routes the inference hostname to the gateway
The Caddy `inference_api` route SHALL proxy to the gateway host and port taken from
inventory, keeping TLS termination, the path allowlist and the Bearer 401 at Caddy.
During the shared key's grace period the route MUST be revertible to the direct upstream
by changing one inventory value; after the shared key is retired, rollback SHALL be the
`rollback-inference-route.yml` playbook (gateway-config, direct, restore modes), because
vLLM cannot authenticate gateway-issued client keys. Caddy's Bearer check SHALL remain a
header-shape check so that every enrolled client key passes it and identity is decided
only at the gateway.

#### Scenario: Client key passes Caddy and is judged at the gateway
- WHEN a request with an enrolled client key arrives at the public hostname
- THEN Caddy forwards it unchanged and the gateway's access log attributes it to that
  identity; the vLLM key appears only in the gateway's upstream request

#### Scenario: Public path traverses the gateway
- WHEN a request arrives at `https://inference.uhstray.io/v1/models` with an enrolled key
- THEN the gateway's access log records it and the model list is returned

#### Scenario: Rollback is one value during the grace period
- WHEN, before the shared key is retired, the route's upstream inventory value is set
  back to the head node and Caddy is redeployed
- THEN direct vLLM serves the hostname again with no other change

#### Scenario: Rollback after retirement is the playbook
- WHEN, after the shared key is retired, `rollback-inference-route.yml` runs in `direct`
  mode and then in `restore` mode
- THEN clients are served directly by vLLM with the key it publishes, and after `restore`
  the gateway is back in the path, the vLLM key is rotated and no published copy remains

### Requirement: Gateway telemetry lands in the platform stack
The gateway SHALL export request metrics to Prometheus (with the client identity as a
label) and traces over OTLP to the o11y host's collector, SHALL record the identity on every
access-log line (first-token latency is already a default field on streamed responses)
without ever logging prompt or completion content, and the inference dashboard MUST show client-view first-token
latency, request duration, failures and per-identity request counts from those signals.

#### Scenario: Client-view latency on the dashboard
- WHEN requests flow through the gateway for one hour
- THEN the inference dashboard's client-view row renders p50 and p95 first-token latency
  and per-identity request counts from gateway metrics

### Requirement: The skynet relationship is a recorded decision
The platform SHALL record in `plan/architecture/` that agentgateway is the inference
edge for the DGX Spark vLLM API and skynet is the platform's own orchestrating
model-serving gateway, two distinct authorities that do not replace each other, and
MUST amend plan 06 with a dated pointer rather than editing its text.

#### Scenario: Decision is findable and plan 06 is amended
- WHEN a reader searches `plan/architecture/` for the inference gateway
- THEN one record states both roles, the rejected alternatives and their reasons, and
  `plan/development/06-inference-skynet.md` carries a dated line pointing at it with
  its prior text unchanged
