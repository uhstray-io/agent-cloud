#!/usr/bin/env bats
# The Cloudflare half of the agentgateway MCP edge (change agentgateway-mcp, task 5.4):
# the `mcp` DNS record, the challenge skip and the one rate-limit rule, all scoped to the MCP
# host's exact /mcp path, and the inference rules left as they were.
#
# Run: bats platform/tests/test_cloudflare_mcp_edge.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  CF="$REPO_ROOT/platform/infra/cloudflare"
}

# The attributes of one rule, from its `ref` line to the rule's closing brace (4-space indent in
# both rulesets). $1 = file, $2 = ref.
_rule() {
  awk -v ref="$2" '
    index($0, "ref         = \"" ref "\"") { on = 1 }
    on { print }
    on && /^    \},?$/ { exit }
  ' "$1"
}

@test "cloudflare mcp: ONE definition names the MCP host and the exact /mcp path, and only those" {
  local e
  e=$(grep -E '^  mcp_endpoint = ' "$CF/ratelimit.tf")
  [ -n "$e" ]
  assert_contains "$e" 'http.host eq \"mcp.${var.zone_name}\"'
  assert_contains "$e" 'http.request.uri.path eq \"/mcp\"'
  # Exact match: no prefix operator that would also take /mcp/x, /sse or /v1.
  refute_contains "$e" 'starts_with'
  refute_contains "$e" 'contains'
  refute_contains "$e" 'inference'
}

@test "cloudflare mcp: the challenge skip is the MCP expression only, appended last" {
  _rule "$CF/waf.tf" mcp-api-bypass-challenge > "$BATS_TEST_TMPDIR/skip.tf"
  [ -s "$BATS_TEST_TMPDIR/skip.tf" ]
  assert_grep -qE '^[[:space:]]*action[[:space:]]+= "skip"$' "$BATS_TEST_TMPDIR/skip.tf"
  assert_grep -qE '^[[:space:]]*expression[[:space:]]+= local\.mcp_endpoint$' "$BATS_TEST_TMPDIR/skip.tf"
  assert_grep -qF 'phases   = ["http_request_sbfm"]' "$BATS_TEST_TMPDIR/skip.tf"
  # Last in the ruleset, so no existing rule moves (a list reorder is a plan diff).
  [ "$(grep -E '^      ref +=' "$CF/waf.tf" | tail -n 1 | cut -d'"' -f2)" = "mcp-api-bypass-challenge" ]
}

@test "cloudflare mcp: the inference skip rule and rate-limit rule are not widened to MCP" {
  _rule "$CF/waf.tf" inference-api-bypass-challenge > "$BATS_TEST_TMPDIR/inf-skip.tf"
  [ -s "$BATS_TEST_TMPDIR/inf-skip.tf" ]
  assert_grep -qF 'http.host eq \"inference.uhstray.io\" and (starts_with(http.request.uri.path, \"/v1/\") or http.request.uri.path eq \"/health\")' "$BATS_TEST_TMPDIR/inf-skip.tf"
  refute_grep -qiE 'mcp' "$BATS_TEST_TMPDIR/inf-skip.tf"
  _rule "$CF/ratelimit.tf" inference-api-ratelimit-block > "$BATS_TEST_TMPDIR/inf-rl.tf"
  [ -s "$BATS_TEST_TMPDIR/inf-rl.tf" ]
  assert_grep -qE '^[[:space:]]*expression[[:space:]]+= local\.inference_v1$' "$BATS_TEST_TMPDIR/inf-rl.tf"
  assert_grep -qE '^[[:space:]]*ratelimit[[:space:]]+= local\.inference_bucket$' "$BATS_TEST_TMPDIR/inf-rl.tf"
  refute_grep -qiE 'mcp' "$BATS_TEST_TMPDIR/inf-rl.tf"
  # The inference host set and its bucket, as locals, name no MCP host.
  grep -E '^  (inference_host|inference_admin_host|inference_v1) ' "$CF/ratelimit.tf" > "$BATS_TEST_TMPDIR/inf-locals.tf"
  [ "$(wc -l < "$BATS_TEST_TMPDIR/inf-locals.tf")" -eq 3 ]
  refute_grep -qiE 'mcp' "$BATS_TEST_TMPDIR/inf-locals.tf"
}

@test "cloudflare mcp: exactly one MCP rate-limit rule, log-first, its own bucket, two rules on the zone (Pro cap)" {
  _rule "$CF/ratelimit.tf" mcp-api-ratelimit-log > "$BATS_TEST_TMPDIR/mcp-rl.tf"
  [ -s "$BATS_TEST_TMPDIR/mcp-rl.tf" ]
  # Observe only until agentgateway-mcp task 6.6 measures a real handshake: the bucket is provisional.
  assert_grep -qE '^[[:space:]]*action[[:space:]]+= "log"$' "$BATS_TEST_TMPDIR/mcp-rl.tf"
  refute_grep -qE '"block"' "$BATS_TEST_TMPDIR/mcp-rl.tf"
  assert_grep -qE '^[[:space:]]*expression[[:space:]]+= local\.mcp_endpoint$' "$BATS_TEST_TMPDIR/mcp-rl.tf"
  assert_grep -qE '^[[:space:]]*ratelimit[[:space:]]+= local\.mcp_bucket$' "$BATS_TEST_TMPDIR/mcp-rl.tf"
  # The API accepts `logging` only on skip rules (error 20018); a rate-limit rule must not carry it.
  refute_grep -qE 'logging' "$BATS_TEST_TMPDIR/mcp-rl.tf"
  [ "$(grep -c 'ref *= "mcp-api-ratelimit' "$CF/ratelimit.tf")" -eq 1 ]
  # The zone is on the Pro tier: two http_ratelimit rules at most. A third would fail at apply.
  [ "$(grep -cE '^      ref +=' "$CF/ratelimit.tf")" -eq 2 ]
  # A bucket of its own, with the IP characteristic the plan allows and a legal period.
  sed -n '/^  mcp_bucket = {/,/^  }/p' "$CF/ratelimit.tf" > "$BATS_TEST_TMPDIR/bucket.tf"
  [ -s "$BATS_TEST_TMPDIR/bucket.tf" ]
  assert_grep -qF 'characteristics     = ["cf.colo.id", "ip.src"]' "$BATS_TEST_TMPDIR/bucket.tf"
  assert_grep -qE '^[[:space:]]*period[[:space:]]+= (10|60)$' "$BATS_TEST_TMPDIR/bucket.tf"
}

@test "cloudflare mcp: the mcp record is a proxied platform subdomain" {
  sed -n '/platform_subdomains = \[/,/^  \]/p' "$CF/dns.tf" > "$BATS_TEST_TMPDIR/subs.tf"
  [ -s "$BATS_TEST_TMPDIR/subs.tf" ]
  assert_grep -qE '^[[:space:]]*"mcp",' "$BATS_TEST_TMPDIR/subs.tf"
  # The record resource builds name = <label>.<zone> and proxies every platform name.
  sed -n '/resource "cloudflare_dns_record" "platform"/,/^}/p' "$CF/dns.tf" > "$BATS_TEST_TMPDIR/rec.tf"
  assert_grep -qE '^[[:space:]]*proxied = true$' "$BATS_TEST_TMPDIR/rec.tf"
  assert_grep -qF 'name    = "${each.key}.${var.zone_name}"' "$BATS_TEST_TMPDIR/rec.tf"
}

@test "cloudflare mcp: the HCL is formatted (tofu fmt -check)" {
  command -v tofu >/dev/null || skip "tofu not installed"
  tofu fmt -check "$CF"
}
