# ratelimit.tf — the http_ratelimit entrypoint ruleset for the zone.
#
# One ceiling per source address on the inference API (vLLM on the DGX Spark
# pair), and a second, separate one on the agentgateway MCP endpoint (its own
# host, path and bucket; the rule at the end of this file). The inference team
# shares ONE bearer key, so the key cannot be the bucket; ip.src is the one
# characteristic every plan offers, and it maps to "one machine cannot starve
# the rest". Zone plan is Pro (read 2026-09-14): at most
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
# changes/archive/2026-09-15-inference-edge-cloudflare-controls.
#
# Scope of the counter: Cloudflare keeps rate-limit counters PER DATA CENTER
# (cf.colo.id is mandatory for exactly that reason; only data centers sharing
# one geographic location share a counter — developers.cloudflare.com/waf/
# rate-limiting-rules/request-rate). So the ceiling is "10 per 10 s per source
# address per data center". One client's requests land at its nearest data
# center, which is the case this rule is for; a source spraying several
# regions would get several counters, and that is accepted — no
# provider-supported configuration gives a Pro zone a network-wide counter.
#
# ROLLOUT RECORD (design risk 1, log-first). First apply 2026-09-15 (Semaphore
# task 885) enabled a `log` twin only; a paced measurement the same night
# showed the counter exact for sequential arrivals (11/15/20 requests -> 1/5/9
# log events); the operator brought the block forward from the 2026-09-28
# target (task 906) and it was proven live: 15 requests in one second -> ten
# 401s from Caddy, five 429s from Cloudflare, served again after the 10 s
# window. The log twin was then removed to free the zone's second and last
# Pro rule slot. Block matches are visible in Security Events on their own.

locals {
  inference_host = "inference.${var.zone_name}"
  # The same /v1 API is also served on the agentgateway operator-UI host (its LLM
  # playground calls /v1 on its own origin), so the per-source ceiling covers both.
  inference_admin_host = "admin.inference.${var.zone_name}"
  inference_v1         = "(http.host in {\"${local.inference_host}\" \"${local.inference_admin_host}\"} and starts_with(http.request.uri.path, \"/v1/\"))"
  inference_bucket = {
    characteristics     = ["cf.colo.id", "ip.src"]
    period              = 10
    requests_per_period = 10
    mitigation_timeout  = 10
  }
  # The agentgateway MCP endpoint: its own host, the exact /mcp path the Caddy allowlist proxies.
  # ONE definition, used by this rule and by waf.tf's challenge skip, so the two cannot name
  # different traffic. The inference rule's host set above is not widened: MCP has its own bucket.
  mcp_endpoint = "(http.host eq \"mcp.${var.zone_name}\" and http.request.uri.path eq \"/mcp\")"
  # PROVISIONAL figures: spike finding 1.7 (agentgateway-mcp design.md) measured the protocol
  # minimum of a session, three requests (initialize, initialized notification, tools/list)
  # before any tool call, and set no ceiling, so this reuses the inference bucket's shape and
  # period: one source cannot exceed ten /mcp requests per ten seconds, which admits three full
  # handshakes, or one handshake plus seven tool calls, in a window. A real harness's handshake count and the
  # production behaviour are measured in tasks 6.6 and 7.7; adjust THEN, from the figures, not
  # from this guess. Cloudflare answers a block with 429, which a MCP client sees as a failed
  # HTTP request; the gateway's own refusal (a JSON-RPC -32003 inside HTTP 200) is a different,
  # later control.
  mcp_bucket = {
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

  rules = [
    {
      ref         = "inference-api-ratelimit-block"
      action      = "block"
      enabled     = true
      description = "Inference API - block a source exceeding 10 req / 10 s (10 s mitigation)"
      expression  = local.inference_v1
      ratelimit   = local.inference_bucket
      # No `logging` block: the API accepts logging.enabled ONLY on skip-action
      # rules (error 20018 "it can only be used with the skip action", first
      # apply = Semaphore task 882). Provider schema, `tofu validate` and `plan`
      # all accepted it; only the create call refused. Match visibility for these
      # rules is what the `log` action itself provides.
    },
    # The agentgateway MCP endpoint — the zone's second and LAST Pro rate-limit rule slot.
    # Source-address bucket like the inference rule (the only characteristic the plan offers).
    {
      ref         = "mcp-api-ratelimit-block"
      action      = "block"
      enabled     = true
      description = "MCP API - block a source exceeding 10 req / 10 s on /mcp (10 s mitigation; provisional)"
      expression  = local.mcp_endpoint
      ratelimit   = local.mcp_bucket
    },
  ]
}
