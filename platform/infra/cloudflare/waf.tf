# waf.tf — the http_request_firewall_custom entrypoint ruleset for the zone.
# Adopted from the live ruleset (imported at zero-diff). Rule order is
# significant. The honcho skip rule (last) exempts the honcho API/health paths
# from Super Bot Fight Mode + BIC + Security Level so non-browser clients work;
# the geo/AI block rules still apply to those paths.

resource "cloudflare_ruleset" "custom_firewall" {
  zone_id     = var.zone_id
  name        = "default"
  kind        = "zone"
  phase       = "http_request_firewall_custom"
  description = ""

  rules = [
    {
      ref         = "18ca3c2e79b845c59acb2148f295c07e"
      action      = "block"
      enabled     = true
      description = "Regional Firewall Blocks"
      expression  = "(ip.geoip.country eq \"CN\") or (ip.geoip.country eq \"BY\") or (ip.geoip.country eq \"KP\") or (ip.geoip.country eq \"RU\") or (ip.geoip.country eq \"SA\") or (ip.geoip.country eq \"AF\") or (ip.geoip.country eq \"IR\") or (ip.geoip.country eq \"SY\") or (ip.geoip.country eq \"BR\") or (ip.geoip.country eq \"CO\") or (ip.geoip.country eq \"SV\")"
    },
    {
      ref         = "882488a5afd543eebc0037d22fcb1754"
      action      = "block"
      enabled     = true
      description = "Block AI Scrapers and Crawlers rule"
      expression  = "(cf.verified_bot_category eq \"AI Crawler\") or (cf.verified_bot_category eq \"AI Assistant\") or (cf.verified_bot_category eq \"AI Search\")"
    },
    {
      ref         = "[CF AI Audit]"
      action      = "block"
      enabled     = true
      description = "AI Audit - Block AI bots by User Agent"
      expression  = "(http.user_agent contains \"bingbot\") or (http.user_agent contains \"ChatGPT-User\") or (http.user_agent contains \"Googlebot\") or (http.user_agent contains \"meta-externalfetcher\") or (http.user_agent contains \"MistralAI-User\") or (http.user_agent contains \"OAI-SearchBot\") or (http.user_agent contains \"Perplexity-User\") or (http.user_agent contains \"PerplexityBot\") or (http.user_agent contains \"ProRataInc\")"
    },
    {
      ref         = "48c8a0a15cc64245a27285d298db0f32"
      action      = "skip"
      enabled     = true
      description = "honcho API + health - bypass challenge for non-browser clients"
      expression  = "(http.host eq \"memory.uhstray.io\" and (starts_with(http.request.uri.path, \"/v3\") or http.request.uri.path eq \"/health\"))"
      action_parameters = {
        phases   = ["http_request_sbfm"]
        products = ["bic", "securityLevel"]
      }
      logging = {
        enabled = true
      }
    },
  ]
}
