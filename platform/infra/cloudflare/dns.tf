# dns.tf — platform service DNS records (the *.uhstray.io subdomains that resolve
# to the central Caddy origin). Uniform A + proxied records, so modeled with a
# for_each keyed by subdomain; content is the parameterized origin IP.
#
# Ownership boundary (see plan/development/13-cloudflare-iac.md + README): only
# platform Caddy-origin records live here. Email/vendor records and the
# Caddy-managed _acme-challenge TXT records are intentionally NOT managed.

locals {
  # subdomain (label) => the platform service behind it, all proxied A-records
  # pointing at the Caddy origin. Add a service here to bring its record under
  # management (then import it).
  platform_subdomains = [
    "auth",   # Authentik IdP
    "canvas", # OpenHands
    "devlog",
    "memory", # honcho
    "mixpost",
    "n8n",
    "netbox",
    "nocodb",
    "o11y", # Grafana
    "postiz",
    "pve", # Proxmox
    "semaphore",
    "todo", # tududi
    "wisbot",
  ]
}

resource "cloudflare_dns_record" "platform" {
  for_each = toset(local.platform_subdomains)

  zone_id = var.zone_id
  name    = "${each.key}.${var.zone_name}"
  type    = "A"
  content = var.caddy_origin_ip
  proxied = true
  ttl     = 1 # 1 = automatic (required by Cloudflare when proxied)
}
