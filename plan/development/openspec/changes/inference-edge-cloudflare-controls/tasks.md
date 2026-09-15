# Tasks: Cloudflare controls for the inference edge

## 0. Branch and ground truth
- [x] 0.1 Feature branch from `dev`: `feat/inference-edge-cloudflare-controls`; one `feat → dev`
      PR, then `dev → main`
- [x] 0.2 Read the zone plan tier and list existing `http_ratelimit` rules through the
      Cloudflare API from the Semaphore environment (read-only); record tier, rule count and
      rule limit in the header comment of `platform/infra/cloudflare/ratelimit.tf`
- [x] 0.3 Validation gate: the tier and existing-rule count are written down, and
      `openspec validate inference-edge-cloudflare-controls --store agent-cloud` passes;
      proves nothing yet in the spec, and blocks section 1 until the numbers are legal for the
      tier

## 1. Rate limiting rule
- [x] 1.1 `platform/infra/cloudflare/ratelimit.tf`: one `cloudflare_ruleset` in phase
      `http_ratelimit`, rule expression
      `http.host eq "inference.uhstray.io" and starts_with(http.request.uri.path, "/v1/")`,
      `ratelimit.characteristics = ["cf.colo.id", "ip.src"]`, `action = "block"`, period,
      count and `mitigation_timeout` per design decision 2, each with a comment deriving it
      from the tier and the measured ceiling. NO `logging` block: the API rejects
      `logging.enabled` on any non-skip rule (error 20018; first apply = Semaphore task 882),
      and `tofu validate` / `plan` do not catch it
- [x] 1.2 If the tier permits a second rule, add a `log` twin first and keep it for one review
      period; otherwise skip and note why in the file. STAGED: the tier (Pro) permits two rules,
      so the first apply enables ONLY `log`; the `block` rule is declared with
      `enabled = false` behind `local.inference_block_enabled`. Rule order alone does not
      defer a block action — both rules would fire on the first apply otherwise
- [x] 1.3 Update the pointer comment on the inference skip rule in `waf.tf` to name
      `ratelimit.tf`
- [x] 1.4 **Apply Cloudflare Tofu** with `tofu_action=plan`: exactly one ruleset added, no
      other change; then `tofu_action=apply`. DONE 2026-09-15 03:12 UTC via the Semaphore API:
      task 884 plan `1 to add, 0 to change, 0 to destroy`; task 885 apply `1 added` (ruleset
      `8d991211…`); first attempt task 882 failed on `logging.enabled` (ledger §10.13, PR #175)
- [x] 1.5 From a single source address, send more requests than the count inside one period;
      during the log-only phase confirm the Security Events entry from the `log` rule and NO
      block response (every request still reaches Caddy). DONE 2026-09-15 03:12 UTC: 15 requests
      to `/v1/models` in one second from one address through colo EWR — all 15 answered 401 by
      Caddy, none carried `cf-mitigated`; Security Events (GraphQL `firewallEventsAdaptive`)
      shows 11 `skip` events from the bypass rule and 3 `log` events from the new ruleset on
      that host in the same second
- [x] 1.6 Validation gate: a second `plan` reporting no changes proves scenario "Rule is code"
      (DONE: task 886 `No changes. Your infrastructure matches the configuration.`). The
      threshold itself is NOT proven by 1.5 — 15 requests produced 3 log events, 12 concurrent
      streams produced none — so the paced 11 / 15 / 20-request measurements in 1.7 are
      pending before `block` is enabled
- [x] 1.7a MEASURED 2026-09-15 04:21–04:23 UTC (paced, one address, colo EWR, 45 s apart, all
      requests 401 at Caddy): 11 sequential requests → 1 rate-limit `log` event; 15 → 5; 20 → 9
      (19 skip events recorded for 20 requests, so one event of the twenty is missing from the
      feed). Sequential arrivals are counted as exact arithmetic (excess = n − 10). Still
      unexplained: the verify play's 12 concurrent streamed POSTs (03:28 UTC) produced ZERO
      rate-limit events. Before the flip, run that queue probe once more while watching
      Security Events; if it still records nothing, `block` will not trip it either; if it
      records 2, the probe must be paced or run from two addresses
- [ ] 1.7 After the review period (target 2026-09-28): reviewed PR sets
      `inference_block_enabled = true`; `plan` shows exactly one rule attribute change;
      `apply`; repeat 1.5 and confirm the block response — this proves scenario "Burst from
      one address is blocked". Then remove the `log` twin (5.3)

## 2. Origin lockdown at Caddy — WITHDRAWN 2026-09-15 (operator decision after 2.5 failed closed; code removed from the template, inventory, deploy assert, genesis INI and tests; site-config reverted via #14)
- [x] 2.1 Inventory variable `caddy_cloudflare_ranges` (list of CIDRs) with the fetch date and
      source URLs in a comment: default in `platform/inventory/local-dev.yml.example` and
      `local-dev.yml`; production value in site-config `inventory/production.yml`
- [x] 2.2 `Caddyfile.local.j2`, `inference_api` branch: a named matcher
      `@cf remote_ip {{ caddy_cloudflare_ranges | join(' ') }}` and wrap the `@api` and
      `/health` handlers so only `@cf` sources reach them; every other source falls through to
      the existing bare `handle` 404
- [x] 2.3 Mirror the same matcher into the production block in site-config, using the same
      variable, keeping the block's comment accurate
- [x] 2.4 `platform/tests/test_service_caddy.bats`: extend the inference test to assert the
      `remote_ip` matcher exists inside the `inference_api` branch, references the variable
      (never a literal range), and precedes the `reverse_proxy`; run `bats platform/tests`
- [ ] 2.5 PRE-CHECK: confirm the peer address the prod Caddy container sees for a Cloudflare
      request is inside the published ranges (podman docs: pasta, the default rootless mode,
      preserves the source IP on port forwarding; which backend the host runs is unverified) —
      otherwise the route fails CLOSED for everyone. Then redeploy Caddy through the Semaphore playbook; from a host that can reach the origin
      address, `curl -H 'Host: inference.uhstray.io'` to `/health` returns 404; through
      Cloudflare `/health` returns 200 and `/v1/models` with the key returns the list.
      ATTEMPTED 2026-09-15 03:53 UTC — DEAD END as deployed: site-config #13 merged, inventory
      synced, `Manage Caddy Sites` task 888 succeeded (Caddyfile validated, Caddy restarted),
      and the route FAILED CLOSED: proxied `/health` 404, `/v1/models` with key 404, direct
      404. The Caddy container does not see a Cloudflare address as its peer (the pre-check
      could not be run: no Semaphore-executable read of the container's network backend
      exists and workstation SSH is not a sanctioned path). Rolled back through the same
      path: site-config #14 reverts the two lockdown commits → sync → `Manage Caddy Sites`.
      Other hostnames on the host were unaffected throughout (matcher scoped to one block).
      NOT reopened: the operator withdrew the requirement. Kept for any FUTURE source-address
      control on this host (ledger §10.14): the precondition is a read-only Semaphore task that
      sends a known request through Cloudflare to the hostname and prints the matching
      access-log entry's peer address — the network mode or an arbitrary log line is not it
- [ ] 2.6 (withdrawn with the requirement) Validation gate: 2.5 proves scenario "Direct request to the origin gets nothing" and
      scenario "Proxied request is served"; 2.4 and a rendered diff prove scenario "Template
      and production block agree"

## 3. Confirm the bypass still holds and normal traffic is unaffected
- [x] 3.1 `curl -sI https://inference.uhstray.io/v1/models` (no key) returns 401 without a
      `cf-mitigated` header; `curl -sI https://inference.uhstray.io/metrics` returns a
      challenge. DONE 2026-09-15 03:17 UTC: `/v1/models` HTTP/2 401, no `cf-mitigated`;
      `/metrics` HTTP/2 403 `cf-mitigated: challenge`
- [x] 3.2 Run the dgx-spark `vllm.yml` public-path verify play (companion change, section 3)
      against the live edge; all probes pass under the new rule. DONE 2026-09-15 03:28–03:46 UTC
      from the companion worktree (`--start-at-task` on the public-path play; the two Spark
      plays ran no tasks): efforts 7/7 accepted; long stream 256.6 s to `[DONE]`; queue 12/12
      streams completed (longest 795.7 s, first token after 397 s with 26 keep-alives) — all
      through the log-only rate limit with no mitigation
- [x] 3.3 Validation gate: 3.1 proves scenario "SDK request reaches Caddy" and scenario
      "Unlisted path stays challenged"; 3.2 proves scenario "Normal agentic use is unaffected"
      (under the log-only phase). NOTE for 1.7: the queue probe starts 12 streams at once from
      ONE address, nominally above the 10 / 10 s threshold — yet Security Events recorded ZERO
      rate-limit `log` events for the whole verify window, while the earlier burst of 15 GETs
      in one second recorded 3 (not 5). The counter's behaviour at the threshold is not the
      exact arithmetic the design assumed; measure it (paced bursts of 11, 15, 20 from one
      address, count the log events) BEFORE enabling `block`, or a real client's fan-out and
      the verify probe may be treated differently than expected

## 4. Records
- [x] 4.1 `plan/development/13-cloudflare-iac.md`: status line for the rate-limit rule; the
      `http_ratelimit` phase joins the managed set
- [x] 4.2 `plan/architecture/` record: the 125 s Proxy Read Timeout is accepted; Enterprise and
      grey-cloud rejected with reasons; heartbeat implemented by dgx-spark
      `inference-endpoint-reliability`
- [x] 4.3 Cloudflare range refresh procedure documented next to the variable (re-fetch the two
      URLs, update the list and date, redeploy Caddy)
- [ ] 4.4 Validation gate: the architecture record exists and names both rejected alternatives,
      proving scenario "Decision is findable"; on archive, retain the outcome (worked / dead
      end / corrected) into bank `agent-cloud-750a33b9`

## 5. Deferred follow-ups (surfaced by the 2026-09-14 simplify pass; out of this change's scope)
- [ ] 5.1 DROPPED 2026-09-15 by the same operator decision (no Cloudflare-range lockdown of the
      origin in any form). Original: "Only Cloudflare reaches this origin" is a property of the Caddy HOST, not one
      route: every platform hostname is proxied, so the general mechanism is a host firewall
      rule (`apply-firewall.yml` already models port-from-source) for 443 from the Cloudflare
      ranges, covering every vhost. The per-route matcher is the correct first landing; do
      not build a per-route `allow_from` flag in its place
- [ ] 5.2 Bring the `inference` A-record under `dns.tf` (add to `platform_subdomains`, import
      at zero-diff): the whole control stack assumes `proxied = true` and nothing in code
      enforces it today
- [ ] 5.3 Remove the `log` twin in `ratelimit.tf` when its review period ends (target
      2026-09-28); it holds the zone's second and last Pro rule slot
