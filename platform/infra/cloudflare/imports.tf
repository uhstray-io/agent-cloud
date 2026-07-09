# imports.tf — adoption of pre-existing Cloudflare resources.
#
# These resources already exist (created via the API / dashboard). We ADOPT them
# into OpenTofu state with `import` blocks + `tofu plan -generate-config-out`, so
# no resource is recreated and `tofu plan` ends at zero-diff. After the generated
# HCL is moved into waf.tf / dns.tf and `plan` is clean, DELETE the matching
# import block (import blocks are one-shot adoption aids, not steady state).
#
# Resource ids are environment-specific (not secrets, but not committed to this
# public repo): supply them at bootstrap via TF_VAR_* or an untracked
# import.auto.tfvars in the tofu working dir. The custom-firewall entrypoint
# ruleset id comes from:
#   GET /zones/{zone_id}/rulesets/phases/http_request_firewall_custom/entrypoint

variable "custom_firewall_ruleset_id" {
  type        = string
  description = "Existing http_request_firewall_custom entrypoint ruleset id (bootstrap import only)."
  default     = "" # set via TF_VAR_custom_firewall_ruleset_id at bootstrap
}

# Phase 1 — WAF custom firewall ruleset (geo/AI blocks + the honcho skip rule).
# Enable during bootstrap once the id var is set; the exact provider-v5 import id
# format is confirmed by `tofu plan -generate-config-out` in the tofu runtime.
# import {
#   to = cloudflare_ruleset.custom_firewall
#   id = "zones/${var.zone_id}/${var.custom_firewall_ruleset_id}"
# }

# Phase 2 — platform DNS A-records (Caddy origin). Add one import block per
# record (id = "zones/${var.zone_id}/${record_id}") and generate-config; keep
# external/email/ACME records OUT (see README ownership boundary).
