# imports.tf — reference for adopting MORE pre-existing Cloudflare resources.
#
# The WAF ruleset (waf.tf) and the platform DNS records (dns.tf) are already
# adopted (imported at zero-diff; state in R2). To bring another existing
# resource under management WITHOUT recreating it:
#
#   1. Add an import block (resource ids are env-specific — keep them in a
#      gitignored bootstrap-*.tf, not here):
#        import {
#          to = cloudflare_dns_record.platform["<label>"]   # or the new resource
#          id = "<zone_id>/<record_id>"                      # ruleset: zones/<zone_id>/<id>
#        }
#   2. tofu plan -generate-config-out=generated.tf   (or hand-write to match)
#   3. Move the generated/authored HCL into the right .tf; `tofu plan` MUST be
#      "No changes" before it counts as managed; then delete the import block.
#
# See README.md (bootstrap runbook) + plan/development/13-cloudflare-iac.md.
