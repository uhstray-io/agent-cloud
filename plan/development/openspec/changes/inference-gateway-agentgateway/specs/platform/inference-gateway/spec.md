# platform/inference-gateway

The authenticated, metered, observable inference edge in front of the DGX Spark vLLM
API, alongside skynet.

## ADDED Requirements

### Requirement: The gateway is a platform service on its own host
agentgateway SHALL run as an Infrastructure-tier platform service on a dedicated VM,
deployed and verified through Semaphore from committed compose and configuration
templates, with its admin interface reachable only on the container loopback and its
runtime credentials sourced from OpenBao.

#### Scenario: Deploy converges
- WHEN the gateway deploy template runs against the production inventory
- THEN the readiness endpoint answers, a second run reports no changes, and no
  credential appears in any committed file or container argument

#### Scenario: Admin interface is not exposed
- WHEN a LAN host connects to the gateway VM on the admin port
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
OpenBao, MUST apply a request-rate and a token-budget limit keyed on the client
identity, and MUST accept the legacy shared key as one identity only during a dated
grace period.

#### Scenario: Unknown key is rejected
- WHEN a request carries a key that is not enrolled
- THEN the gateway returns 401 and the request never reaches vLLM

#### Scenario: One client cannot exceed its share
- WHEN one identity exceeds its request-rate limit within the fill interval
- THEN its excess requests receive a 429 from the gateway while other identities are
  served

#### Scenario: Grace period ends
- WHEN the dated grace period has passed
- THEN the shared key is no longer enrolled at the gateway and has been rotated at vLLM

### Requirement: Caddy routes the inference hostname to the gateway
The Caddy `inference_api` route SHALL proxy to the gateway host and port taken from
inventory, keeping TLS termination, the path allowlist and the Bearer 401 at Caddy, and
MUST be revertible to the direct upstream by changing one inventory value. Caddy's Bearer
check SHALL remain a header-shape check so that every enrolled client key passes it and
identity is decided only at the gateway.

#### Scenario: Client key passes Caddy and is judged at the gateway
- WHEN a request with an enrolled client key arrives at the public hostname
- THEN Caddy forwards it unchanged and the gateway's access log attributes it to that
  identity; the vLLM key appears only in the gateway's upstream request

#### Scenario: Public path traverses the gateway
- WHEN a request arrives at `https://inference.uhstray.io/v1/models` with an enrolled key
- THEN the gateway's access log records it and the model list is returned

#### Scenario: Rollback is one value
- WHEN the route's upstream inventory value is set back to the head node and Caddy is
  redeployed
- THEN direct vLLM serves the hostname again with no other change

### Requirement: Gateway telemetry lands in the platform stack
The gateway SHALL export request metrics to Prometheus and traces over OTLP to the
o11y host's collector, and the inference dashboard MUST show client-view first-token
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
