# imports-inference.tf — adopt the pre-existing inference.uhstray.io A-record.
#
# The record was hand-created before the DNS phase landed, so WAF and rate-limit
# rules have governed a hostname whose proxied flag nothing in code owned. This
# brings it under dns.tf's platform_subdomains (A, proxied, TTL auto, content =
# the Caddy origin) at zero-diff: the plan must show "1 to import, 0 to add,
# 0 to change, 0 to destroy".
#
# Why the record id is committed, against the imports.tf note: the adoption
# runs through Semaphore (Apply Cloudflare Tofu), which checks out THIS repo,
# so a gitignored bootstrap file can never reach it. A DNS record id is an
# opaque handle, not a credential or an address; the zone id stays a variable.
# Read from the API 2026-09-15 (GET /zones/{zone}/dns_records?name=inference…).
#
# DELETE THIS FILE once the import has applied and a follow-up plan is
# "No changes" — it is a one-shot, exactly like the Phase 1-2 bootstrap blocks.

import {
  to = cloudflare_dns_record.platform["inference"]
  id = "${var.zone_id}/6d4baab8903230df8df9b9c1cfc59bec"
}
