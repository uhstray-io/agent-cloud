# Design: Cloudflare controls for the inference edge

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

## Context

Verified 2026-09-14:

- `platform/infra/cloudflare/waf.tf`: one `cloudflare_ruleset` in phase
  `http_request_firewall_custom` with three block rules and three skip rules; the last,
  `inference-api-bypass-challenge`, skips SBFM, BIC and security level for
  `inference.uhstray.io` on `/v1/` and `/health`, with logging on. Its comment defers
  rate limiting to the `http_ratelimit` phase as a pending change.
- `platform/infra/cloudflare/dns.tf`: platform A-records are proxied and managed by a
  subdomain list; `inference` is not in that list, so the record is unmanaged today.
- `platform/infra/cloudflare/versions.tf`: provider `cloudflare/cloudflare ~> 5.0`, state
  in R2, token from OpenBao through the Semaphore environment; README: run through the
  **Apply Cloudflare Tofu** template, `plan` then `apply`, `-auto-approve` on apply.
- Cloudflare rate limiting documentation: rules deploy to the `http_ratelimit` phase
  entry-point ruleset; the `ratelimit` object carries `characteristics`, `period`,
  `requests_per_period`, `mitigation_timeout`, optional `counting_expression` and
  `requests_to_origin`; actions include `block`, `managed_challenge`, `log`. Plan bounds:
  Free 1 rule and a 10 s period only; Pro 2 rules, periods to 1 min; Business 5 rules,
  periods to 10 min; `ip.src` is available on every plan. Period API values are 10, 60,
  120, 300, 600, 3600; mitigation timeout values 0, 10, 60, 120, 300, 600, 3600, 86400.
  The documentation does not say how a long streamed request is counted; a request is
  counted on arrival as far as the documentation states.
- Cloudflare error 524 documentation: 125 s default Proxy Read Timeout, raisable to
  6,000 s on Enterprise only; a 30 s write timeout is fixed on every plan.
- Caddy matcher documentation: `remote_ip <ranges...>` matches the immediate peer by
  exact IP or CIDR; `client_ip` differs only when the `trusted_proxies` global option is
  set. Cloudflare publishes its ranges at `https://www.cloudflare.com/ips-v4` and
  `https://www.cloudflare.com/ips-v6` (both fetched, HTTP 200).
- `platform/services/caddy/deployment/templates/Caddyfile.local.j2`, `inference_api`
  branch: `@api path /v1/*`, `handle @api` with the Bearer `header_regexp` 401 and the
  streaming `reverse_proxy`, `handle /health`, bare `handle` returning 404, 16 MB body
  cap. `platform/tests/test_service_caddy.bats:74` asserts that shape line by line.
- site-config `inventory/production.yml:355`: the production block for
  `inference.uhstray.io` in the same shape, upstream set to the head node from inventory, comment stating
  that rate limiting belongs to Cloudflare and that `Caddyfile.local.j2` renders the same
  shape locally.
- dgx-spark `docs/FABLE-AUDIT-2026-09-13.md` finding 9: origin reachable without
  Cloudflare; ADR-0004 lists the rate-limiting rule as open follow-on work.
- Measured (dgx-spark `docs/LOAD-TESTING-2026-09-14.md`): about six useful concurrent
  requests; throughput flat past eight; a 48-request fan-out from one client.

Not verified in this session: the zone's Cloudflare plan tier. Every rate-limit parameter
depends on it, so reading it is the first task.

## Goals / Non-Goals

Goals: one client cannot consume the whole endpoint; a request that did not pass through
Cloudflare gets nothing from the origin; both controls are code in this repo, applied
through Semaphore, with a plan visible before apply; the timeout decision is written down
so it is not re-investigated.

Non-Goals: per-user keys or authentication changes (the shared key is an accepted
ADR-0004 tradeoff in dgx-spark); bringing the `inference` DNS record under `dns.tf`
(unrelated adoption, its own zero-diff import); raising the Cloudflare timeout; anything
on the vLLM host.

## Decisions

1. **Rate limit at Cloudflare, keyed on source IP, action block.** `ip.src` is the one
   characteristic every plan offers, and every team client today sits behind a small set
   of egress addresses, so a per-IP ceiling maps to "one machine cannot starve the rest".
   Alternative rejected: keying on the Authorization header, because there is one shared
   key, so it would be one bucket for everyone; header characteristics are also
   Enterprise-only per the documentation. Alternative rejected: `managed_challenge` as the
   action, because the clients are SDKs and curl, which cannot solve a challenge; on
   non-Enterprise plans a challenge action also forces `mitigation_timeout` to 0.
   Alternative rejected: enforcing at Caddy, because Caddy has no rate limiter without a
   third-party module and the dgx-spark decision already places this control at
   Cloudflare.

2. **Parameters come from the plan tier and the measured ceiling, in that order.** The
   tier bounds the period (Free: 10 s only) and the rule count (Free: 1, and the zone may
   already have one). The threshold is derived from the companion change's measured
   concurrency: with about six useful slots and request durations of 20 to 120 s, a
   sensible per-IP count is on the order of a few requests per 10 s window, since a
   single agentic client legitimately issues one request per turn. Task 1 reads the tier
   and any existing `http_ratelimit` rules; task 2 fixes the numbers in `ratelimit.tf`
   with a comment deriving them. Alternative rejected: pick numbers now, because a value
   the plan does not permit fails at apply, and a value not tied to the measurement is a
   guess.

3. **Origin lockdown as a Caddy source-address matcher, not a host firewall rule.** The
   Caddy host serves other hostnames whose reachability this change must not alter, and
   the matcher is scoped to the one site block, rendered from the same template and test
   that already govern the route. The ranges are an inventory variable
   (`caddy_cloudflare_ranges`), not literals in the template, so the public template and
   the private production block share one source and a range refresh is a variable
   change. Alternative rejected: `ufw` rules for 80 and 443 on the Caddy host, because
   Caddy's own ACME HTTP-01 and every other site would be affected. Alternative rejected:
   the `cloudflare_ip_ranges` provider data source for automatic refresh, because the
   ranges are consumed by Caddy through Ansible, not by tofu, and a second path for the
   same list is a second source of truth.

4. **`remote_ip`, not `client_ip`.** The template does not set `trusted_proxies`, so the
   two matchers behave identically today; `remote_ip` says what is meant (the peer is
   Cloudflare) and does not change meaning if `trusted_proxies` is added later for
   logging. Requests failing the matcher fall through to the bare `handle` and receive
   the existing 404, so nothing new is exposed and the response is identical to an
   unknown path.

5. **The 125 s read timeout is accepted, and recorded as such.** Enterprise is not a
   plan this project is on. Grey-clouding the hostname (DNS-only) removes the timer, and
   with it the challenge bypass, the block rules and the new rate limit, and exposes the
   origin address in DNS. The companion change's SSE heartbeat resets the timer from the
   origin side and is the correct fix. This is written into `plan/architecture/` so the
   next load test does not reopen it.

## Risks / Trade-offs

- [Rate limit trips a legitimate agentic client] → the threshold is derived from the
  measured ceiling with headroom and starts with `action = "log"` for one review period
  if the plan permits a second rule, otherwise `block` with a short `mitigation_timeout`
  (10 s) so a tripped client recovers within one turn. Matches are logged to Security
  Events for review.
- [Cloudflare ranges change] → the ranges are an inventory variable with the fetch date
  in a comment; a stale list fails closed (a new Cloudflare edge address gets a 404),
  which is visible immediately in the companion change's public-path verify play.
  Refresh procedure is a task in this change's docs.
- [Lockdown breaks ACME] → the production block uses `tls { dns cloudflare ... }`
  (DNS-01), which never needs an inbound HTTP request; the matcher affects only the
  routed handlers. The local template uses `local_certs` or a supplied certificate, so
  no ACME path exists there either.
- [Plan tier forbids the second rule or the chosen period] → decision 2 makes the tier
  the first read; if only one rule is possible and one exists, the choice between them
  is escalated, not made silently.
- [Streamed requests counted differently than assumed] → unverified in the
  documentation; the log-first period (or the verify play's 12-stream probe under the
  block rule) shows whether a single client's burst is counted on arrival.

## Migration Plan

1. Read the zone plan tier and any existing `http_ratelimit` rules through the API from
   the Semaphore environment (read-only); record both in `ratelimit.tf`'s header.
2. Land `ratelimit.tf`; **Apply Cloudflare Tofu** `plan`; confirm it adds one ruleset and
   changes nothing else; `apply`.
3. Land the template, variable and BATS change; add the variable and matcher to the
   site-config production block; redeploy Caddy through Semaphore; confirm a direct
   request to the origin address gets 404 on `/health` and a proxied one gets 200.
4. Run the companion change's public-path verify play from dgx-spark to confirm normal
   traffic is unaffected.
5. Update `plan/development/13-cloudflare-iac.md` status and `waf.tf`'s pointer comment;
   write the `plan/architecture/` record; archive; retain the outcome into bank
   `agent-cloud-750a33b9`.

## Open Questions

- Zone plan tier: unknown until read. It decides whether a log-first rule is possible and
  which periods are legal.
- Does the existing tofu token carry permission for the `http_ratelimit` phase? The
  documentation groups rate limiting under the WAF rulesets permission the token already
  has; confirmed only when `plan` succeeds.
- Threshold numbers: fixed after the dgx-spark A/B (companion tasks 4.x) is on file.
  Default if the A/B is delayed: 10 requests per 10 s per IP, `mitigation_timeout` 10,
  derived from six slots and one request per turn per client, revisited when the
  measurement lands.
