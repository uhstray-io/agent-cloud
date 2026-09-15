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
      from the tier and the measured ceiling; `logging.enabled = true`
- [x] 1.2 If the tier permits a second rule, add a `log` twin first and keep it for one review
      period; otherwise skip and note why in the file
- [x] 1.3 Update the pointer comment on the inference skip rule in `waf.tf` to name
      `ratelimit.tf`
- [ ] 1.4 **Apply Cloudflare Tofu** with `tofu_action=plan`: exactly one ruleset added, no
      other change; then `tofu_action=apply`
- [ ] 1.5 From a single source address, send more requests than the count inside one period;
      confirm the block response and the Security Events entry
- [ ] 1.6 Validation gate: 1.5 proves scenario "Burst from one address is blocked"; a second
      `plan` reporting no changes proves scenario "Rule is code"

## 2. Origin lockdown at Caddy
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
- [ ] 2.5 Redeploy Caddy through the Semaphore playbook; from a host that can reach the origin
      address, `curl -H 'Host: inference.uhstray.io'` to `/health` returns 404; through
      Cloudflare `/health` returns 200 and `/v1/models` with the key returns the list
- [ ] 2.6 Validation gate: 2.5 proves scenario "Direct request to the origin gets nothing" and
      scenario "Proxied request is served"; 2.4 and a rendered diff prove scenario "Template
      and production block agree"

## 3. Confirm the bypass still holds and normal traffic is unaffected
- [ ] 3.1 `curl -sI https://inference.uhstray.io/v1/models` (no key) returns 401 without a
      `cf-mitigated` header; `curl -sI https://inference.uhstray.io/metrics` returns a
      challenge
- [ ] 3.2 Run the dgx-spark `vllm.yml` public-path verify play (companion change, section 3)
      against the live edge; all probes pass under the new rule
- [ ] 3.3 Validation gate: 3.1 proves scenario "SDK request reaches Caddy" and scenario
      "Unlisted path stays challenged"; 3.2 proves scenario "Normal agentic use is unaffected"

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
