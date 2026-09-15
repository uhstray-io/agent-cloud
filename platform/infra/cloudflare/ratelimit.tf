# ratelimit.tf — the http_ratelimit entrypoint ruleset for the zone.
#
# One ceiling per source address on the inference API (vLLM on the DGX Spark
# pair behind inference.uhstray.io). The team shares ONE bearer key, so the
# key cannot be the bucket; the source address is the only characteristic
# every plan offers, and it maps to "one machine cannot starve the rest".
# Decision + measurements: plan/development/openspec/changes/
# inference-edge-cloudflare-controls (design decisions 1-2).
#
# Ground truth read from the API on 2026-09-14 (GET /zones?name=uhstray.io,
# GET /zones/<id>/rulesets/phases/http_ratelimit/entrypoint):
#   plan tier ........ "Pro Website" (legacy_id "pro")
#   existing rules ... 0 — the http_ratelimit entrypoint did not exist (10003)
#   Pro bounds ....... 2 rules; characteristic IP only; period up to 60 s;
#                      mitigation_timeout up to 3600 s; no counting_expression
#   (developers.cloudflare.com/waf/rate-limiting-rules, availability table)
# Both rules below fit those bounds. There is no import: the entrypoint is
# created by the first apply, so the plan must show exactly one ruleset ADDED
# and nothing else touched.
#
# Parameters (design decision 2, fallback pending the dgx-spark A/B):
#   period 10 s, 10 requests, mitigation 10 s — six useful concurrent slots
#   were measured (dgx-spark docs/LOAD-TESTING-2026-09-14.md) and one agentic
#   client legitimately issues ONE request per turn, so ten arrivals from one
#   address inside ten seconds is a fan-out, not a conversation. A 10 s
#   mitigation lets a tripped client recover within a turn. Period and timeout
#   MUST be one of the API's fixed values (10/60/120/300/600/3600 and
#   0/10/60/120/300/600/3600/86400). cf.colo.id is mandatory in
#   characteristics per the parameters doc; ip.src is the actual key.
#
# Rule order is significant and both rules are counted independently. The
# `log` twin comes first (design: log-first for one review period — Pro
# permits the second rule), so Security Events carries every excess request
# even while the block rule's mitigation window is already open. Remove the
# twin after the review period, or re-tune both from what it recorded.

resource "cloudflare_ruleset" "ratelimit" {
  zone_id     = var.zone_id
  name        = "default"
  kind        = "zone"
  phase       = "http_ratelimit"
  description = "Per-source ceilings for machine APIs (inference)"

  rules = [
    {
      ref         = "inference-api-ratelimit-log"
      action      = "log"
      enabled     = true
      description = "Inference API - log sources exceeding 10 req / 10 s (review twin)"
      expression  = "(http.host eq \"inference.uhstray.io\" and starts_with(http.request.uri.path, \"/v1/\"))"
      ratelimit = {
        characteristics     = ["cf.colo.id", "ip.src"]
        period              = 10
        requests_per_period = 10
        mitigation_timeout  = 10
      }
      logging = {
        enabled = true
      }
    },
    {
      ref         = "inference-api-ratelimit-block"
      action      = "block"
      enabled     = true
      description = "Inference API - block a source exceeding 10 req / 10 s for 10 s"
      expression  = "(http.host eq \"inference.uhstray.io\" and starts_with(http.request.uri.path, \"/v1/\"))"
      ratelimit = {
        characteristics     = ["cf.colo.id", "ip.src"]
        period              = 10
        requests_per_period = 10
        mitigation_timeout  = 10
      }
      logging = {
        enabled = true
      }
    },
  ]
}
