# Cloudflare controls for the inference edge: rate limiting and origin lockdown

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Explored 2026-09-14.

Companion change in dgx-spark: `inference-endpoint-reliability` (SSE keep-alive, effort
mapping, concurrency measurement, public-path verification). That change owns everything
on the vLLM host; this change owns everything at the Cloudflare edge and the Caddy route
that fronts it.

## Why

`inference.uhstray.io` went live on 2026-09-11 with a WAF skip rule so machine clients
reach Caddy (`platform/infra/cloudflare/waf.tf`, rule `inference-api-bypass-challenge`).
Two controls that the dgx-spark decision record (ADR-0004) assigned to Cloudflare were
left pending at that time, and load testing on 2026-09-13 and 2026-09-14 makes both
current:

1. **No rate limit on `/v1/*`.** The `waf.tf` comment on the inference rule says rate
   limiting "belongs in the http_ratelimit phase and is a separate, pending change"; the
   site-config Caddy block says the same. Load testing measured the endpoint at about six
   useful concurrent slots with throughput flat past eight; one client fanning out
   forty-eight requests degraded the endpoint for every other user of the shared key.
   With one key for the whole team, the edge is the only place a per-client ceiling can be
   applied.
2. **The origin is reachable without Cloudflare.** The dgx-spark audit of 2026-09-13
   (finding 9) records that the Caddy route has no source-address restriction and that
   ports 80 and 443 are forwarded to the Caddy host, so a client that learns the origin
   address bypasses the WAF, the skip rules and any rate limit, and reaches vLLM behind
   the shared bearer token alone.

A third finding from the load tests is recorded here as a decision, not a fix:
Cloudflare's 125-second Proxy Read Timeout severs non-streamed responses (HTTP 524) and
idle streams. It is adjustable only on the Enterprise plan. The origin-side heartbeat in
the companion change defeats it; nothing at the edge can.

## What Changes

- **Rate limiting rule for the inference API**, declared in the OpenTofu root
  `platform/infra/cloudflare/` as a ruleset in the `http_ratelimit` phase, scoped to
  `http.host eq "inference.uhstray.io"` and paths under `/v1/`, characteristic `ip.src`,
  action `block`. The period, request count and mitigation timeout are chosen after the
  zone's plan tier is read from the API (the tier bounds every parameter; see design) and
  from the concurrency ceiling the companion change measures. Applied through the
  existing Semaphore **Apply Cloudflare Tofu** template, plan first.
- **Origin lockdown on the inference route — WITHDRAWN 2026-09-15** (landed via site-config
  #13, failed closed in production, reverted via #14; operator decision: no Cloudflare-range
  lockdown of the origin in future, per route or host firewall). Original text follows.
  The Caddy `inference_api` route gains a
  source-address allowlist of Cloudflare's published IPv4 and IPv6 ranges, so a request
  that did not traverse Cloudflare receives no `/v1` or `/health` response from the
  origin. The template branch in `Caddyfile.local.j2` and its BATS test change in this
  repo; the production block in site-config's inventory is mirrored in the same shape,
  as the existing comment on that block requires.
- **Decision recorded: the 125 s read timeout stays.** The two ways to remove it, an
  Enterprise plan or grey-clouding the hostname, are both rejected in the design; the
  origin heartbeat is the fix.

No **BREAKING** changes for clients that already use the hostname through Cloudflare.
Direct-to-origin access, which was never a documented path, stops working by design.

## Capabilities

### New Capabilities
- `platform/inference-edge`: the Cloudflare and Caddy controls that stand in front of the
  DGX Spark inference API: challenge bypass for machine clients, per-source rate
  limiting, origin lockdown.

### Modified Capabilities
- none (no existing spec in the store covers the Cloudflare WAF or the Caddy routes).

## Impact

- Files: new `platform/infra/cloudflare/ratelimit.tf`; `waf.tf` comment on the inference
  rule updated to point at it; `platform/services/caddy/deployment/templates/Caddyfile.local.j2`
  (`inference_api` branch); `platform/tests/test_service_caddy.bats`;
  `plan/development/13-cloudflare-iac.md` status; `plan/architecture/` record.
- site-config: the `inference.uhstray.io` block in `inventory/production.yml` gains the
  same allowlist; the Cloudflare ranges are supplied as an inventory variable so the
  public template and the private block render the same matcher.
- Live: one tofu apply (new ruleset, no change to existing rules); one Caddy redeploy via
  the Semaphore playbook. Clients see no difference until they exceed the rate limit.
- Token scope: the Cloudflare API token used by the tofu root needs the ruleset edit
  permission it already has for `waf.tf`; whether the `http_ratelimit` phase needs any
  additional permission is checked at plan time (design, open questions).

## Rollback Plan

- Rate limit: remove `ratelimit.tf`, run **Apply Cloudflare Tofu** with `tofu_action=plan`
  to confirm the plan deletes only the new ruleset, then `apply`. Alternatively set the
  rule `enabled = false` and apply, which keeps the resource and stops enforcement.
- Origin lockdown: revert the template and inventory commits and redeploy Caddy through
  Semaphore; the route returns to accepting any source. The Cloudflare skip rule and the
  vLLM key are untouched by either direction.
- Nothing here changes DNS, the skip rules or the origin IP, so a rollback never
  re-imports or recreates a live object.
