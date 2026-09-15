# ratelimit.tf — the http_ratelimit entrypoint ruleset for the zone.
#
# One ceiling per source address on the inference API (vLLM on the DGX Spark
# pair). The team shares ONE bearer key, so the key cannot be the bucket;
# ip.src is the one characteristic every plan offers, and it maps to "one
# machine cannot starve the rest". Zone plan is Pro (read 2026-09-14): at most
# TWO rate-limit rules, IP characteristic only, period up to 60 s — both rules
# below use that whole budget. period and mitigation_timeout MUST be API fixed
# values (10/60/120/300/600/3600; 0/10/60/120/300/600/3600/86400); cf.colo.id
# is mandatory in characteristics, ip.src is the actual key.
#
# Threshold (design decision 2, fallback pending the dgx-spark A/B): about six
# useful concurrent slots were measured and one agentic client issues ONE
# request per turn, so ten arrivals from one address in ten seconds is a
# fan-out, not a conversation; a 10 s mitigation lets a tripped client recover
# within a turn. Deliberation + measurements: plan/development/openspec/
# changes/inference-edge-cloudflare-controls.
#
# Rule order is significant and the two rules share one definition so they
# cannot drift apart: the `log` twin runs first so Security Events keeps every
# excess request during the review period even while the block rule's
# mitigation window is open. Remove the twin when the review period ends
# (target 2026-09-28) — it holds the zone's second and last Pro rule slot.

locals {
  inference_host = "inference.${var.zone_name}"
  inference_v1   = "(http.host eq \"${local.inference_host}\" and starts_with(http.request.uri.path, \"/v1/\"))"
  inference_bucket = {
    characteristics     = ["cf.colo.id", "ip.src"]
    period              = 10
    requests_per_period = 10
    mitigation_timeout  = 10
  }
}

resource "cloudflare_ruleset" "ratelimit" {
  zone_id     = var.zone_id
  name        = "default"
  kind        = "zone"
  phase       = "http_ratelimit"
  description = "Per-source ceilings for machine APIs (inference)"

  # A LIST, not a map: a map iterates in key order and would put block before log.
  rules = [for a in ["log", "block"] : {
    ref         = "inference-api-ratelimit-${a}"
    action      = a
    enabled     = true
    description = "Inference API - ${a} a source exceeding 10 req / 10 s (10 s mitigation)"
    expression  = local.inference_v1
    ratelimit   = local.inference_bucket
    logging     = { enabled = true }
  }]
}
