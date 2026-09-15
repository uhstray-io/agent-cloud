# platform/inference-edge Specification

## Purpose
TBD - created by archiving change inference-edge-cloudflare-controls. Update Purpose after archive.
## Requirements
### Requirement: Machine clients bypass the managed challenge
Cloudflare SHALL skip Super Bot Fight Mode, Browser Integrity Check and the security
level for requests to `inference.uhstray.io` whose path starts with `/v1/` or equals
`/health`, and MUST leave every other path on that host under the challenge.

#### Scenario: SDK request reaches Caddy
- WHEN a non-browser client sends a request to `https://inference.uhstray.io/v1/models`
- THEN the response comes from Caddy or vLLM (200 or 401), never a Cloudflare challenge
  page with a `cf-mitigated` header

#### Scenario: Unlisted path stays challenged
- WHEN a non-browser client requests `https://inference.uhstray.io/metrics`
- THEN Cloudflare answers with a challenge before the request reaches Caddy

### Requirement: One source address cannot consume the endpoint
Cloudflare SHALL enforce a rate limiting rule in the `http_ratelimit` phase on
`inference.uhstray.io` for paths under `/v1/`, keyed on the client source address (counted
per Cloudflare data center, the provider's mandatory counter scope), with a
period, request count and mitigation timeout that the zone's plan tier permits and that
are derived from the measured concurrency ceiling of the endpoint, and the rule MUST be
declared in the OpenTofu root and applied through Semaphore.

#### Scenario: Burst from one address is blocked
- WHEN one source address sends more requests to `/v1/chat/completions` through one
  Cloudflare data center within the configured period than the configured count, and the
  block rule is enabled (after the log-only review period)
- THEN the excess requests receive a Cloudflare block response for the mitigation
  timeout and the match is visible in Security Events

#### Scenario: Normal agentic use is unaffected
- WHEN one source address sends one streamed request per turn at the measured
  concurrency ceiling or below
- THEN no request from that address is blocked

#### Scenario: Rule is code
- WHEN `tofu plan` runs in `platform/infra/cloudflare/` after apply
- THEN it reports no changes, and the rate limiting ruleset appears in state

### Requirement: The origin answers only Cloudflare — WITHDRAWN 2026-09-15
Withdrawn by operator decision after the first production landing failed closed (the
Caddy container does not see a Cloudflare peer address for proxied traffic, so a range
allowlist rejected every request until it was reverted). The platform SHALL NOT restrict
the Caddy origin to Cloudflare's address ranges, per route or at the host firewall;
authentication at Caddy (Bearer required on `/v1/*`) and at vLLM (`--api-key`) is the
accepted control for a caller that reaches the origin directly. The scenarios below are
kept as the record of what was specified and disproven; none is a requirement.

Former text: the Caddy route for `inference.uhstray.io` SHALL serve `/v1/*` and `/health`
only to requests whose immediate peer address is within Cloudflare's published IPv4 or
IPv6 ranges, MUST answer every other source with the route's existing 404, and the ranges
MUST be supplied as an inventory variable shared by the local template and the
production block.

#### Scenario: Direct request to the origin gets nothing
- WHEN a client connects to the origin address directly with `Host: inference.uhstray.io`
  and requests `/health`
- THEN the response is 404 and the request never reaches vLLM

#### Scenario: Proxied request is served
- WHEN the same request arrives through Cloudflare
- THEN `/health` returns 200 and `/v1/models` with a valid Bearer credential returns the
  model list

#### Scenario: Template and production block agree
- WHEN the `inference_api` branch of `Caddyfile.local.j2` is rendered with the inventory
  variable and compared with the production block in site-config
- THEN both contain the same source-address matcher over the same variable and the BATS
  test asserts the matcher's presence and its position before the `reverse_proxy`

### Requirement: The proxy read timeout is a recorded decision
The platform SHALL record that Cloudflare's 125-second Proxy Read Timeout on
`inference.uhstray.io` is accepted, that raising it requires the Enterprise plan, that
grey-clouding the hostname is rejected, and that the origin-side SSE heartbeat is the
fix, in a `plan/architecture/` record.

#### Scenario: Decision is findable
- WHEN an operator searches `plan/architecture/` for the inference timeout
- THEN one record states the accepted timeout, the two rejected alternatives with their
  reasons, and points at the dgx-spark change that implements the heartbeat
