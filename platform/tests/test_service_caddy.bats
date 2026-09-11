#!/usr/bin/env bats
# Structural tests for the Caddy reverse proxy (platform/services/caddy/deployment).
# Verifies the composable conversion: env-parameterized compose, container-only
# deploy.sh, local Caddyfile template, and an overlay-safe local profile.
#
# Run: bats platform/tests/test_service_caddy.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/caddy/deployment"
}

@test "caddy: compose env-parameterizes image, ports, and the Caddyfile source" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{CADDY_IMAGE' "$f"
  grep -qE '\$\{CADDY_HTTP_PORT' "$f"
  grep -qE '\$\{CADDY_HTTPS_PORT' "$f"
  grep -qE '\$\{CADDYFILE' "$f"
}

@test "caddy: prod defaults are byte-identical (cloudflare image, 80/443, committed Caddyfile)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{CADDY_IMAGE:-iarekylew00t/caddy-cloudflare:2\.11\.4-alpine\}' "$f"
  grep -qE '\$\{CADDY_HTTP_PORT:-80\}:80' "$f"
  grep -qE '\$\{CADDY_HTTPS_PORT:-443\}:443' "$f"
  grep -qE '\$\{CADDYFILE:-\./Caddyfile\}' "$f"
}

@test "caddy: healthcheck uses the admin API on 127.0.0.1 (not localhost — IPv4-only bind)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -q 'healthcheck:' "$f"
  grep -q '127.0.0.1:2019/config/' "$f"
  ! grep -q 'localhost:2019' "$f"
}

@test "caddy: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
  ! grep -qE '\b(gen_secret|put_secret|get_secret|bao_)' "$f"
}

@test "caddy: local Caddyfile template serves the step-ca cert (with local_certs fallback), no real domain" {
  local f="$DEPLOY_DIR/templates/Caddyfile.local.j2"
  [ -f "$f" ]
  # step-ca-minted wildcard when caddy_tls_cert is set; Caddy's own internal CA
  # (local_certs) only as the fallback when it isn't.
  grep -q 'caddy_tls_cert' "$f"
  grep -qF 'tls {{ _cert }} {{ caddy_tls_key }}' "$f"
  grep -q 'local_certs' "$f"
  grep -q 'reverse_proxy' "$f"
  grep -q 'caddy_routes' "$f"
  ! grep -qE 'uhstray\.io' "$f"
}

@test "caddy: forward_auth route is opt-in per-route and strips client identity headers" {
  local f="$DEPLOY_DIR/templates/Caddyfile.local.j2"
  # Only routes with a forward_auth upstream get the gated route block.
  grep -qF "r.forward_auth" "$f"
  # The embedded-outpost endpoints must be proxied to Authentik, not the app.
  grep -qF '/outpost.goauthentik.io/*' "$f"
  grep -qF 'uri /outpost.goauthentik.io/auth/caddy' "$f"
  # Identity headers are copied from the outpost AND stripped from the client
  # request first (anti-spoofing) — both must be present.
  grep -qE 'copy_headers .*X-authentik-username' "$f"
  grep -qE 'request_header -X-authentik-username' "$f"
}

@test "caddy: inference_api route allowlists /v1 (Bearer required) + /health, 404s the rest, streams" {
  local f="$DEPLOY_DIR/templates/Caddyfile.local.j2"
  # Scope every assertion to the inference_api branch of the template, not the
  # whole file: the forward_auth branch also carries matchers and a reverse_proxy.
  sed -n '/r.inference_api/,/{% elif r.forward_auth/p' "$f" > "$BATS_TEST_TMPDIR/inference.j2"
  local b="$BATS_TEST_TMPDIR/inference.j2"
  [ -s "$b" ]
  # The API allowlist is a named matcher on /v1/* consumed by a handle block —
  # the 401 must sit INSIDE it (Caddy orders `handle` before a top-level `respond`).
  # [[:space:]] rather than \s, and $'\t' rather than '\t': neither escape is
  # portable ERE, and BSD grep on macOS reads '\t' as a literal t — which would
  # turn the refute below into one that can never match.
  assert_grep -qE '^[[:space:]]*@api path /v1/\*$' "$b"
  assert_grep -qE '^[[:space:]]*handle @api \{' "$b"
  # A regexp, not `header Authorization Bearer*`: the trailing * is a prefix
  # match, so `BearerX` would pass. The scheme, one space, a non-empty credential.
  assert_grep -qF '@noauth not header_regexp Authorization "^Bearer [^[:space:]]+$"' "$b"
  refute_grep -qE 'header Authorization Bearer\*' "$b"
  assert_grep -qE '^[[:space:]]*respond @noauth 401$' "$b"
  # ...and NOT at site level (exactly one leading tab in this template).
  refute_grep -qE $'^\trespond @noauth' "$b"
  # Liveness passes through; everything else is a 404 from the bare handle.
  assert_grep -qE '^[[:space:]]*handle /health \{' "$b"
  assert_grep -qE '^[[:space:]]*handle \{$' "$b"
  assert_grep -qE '^[[:space:]]*respond 404$' "$b"
  # Token streaming and the prompt-size cap.
  assert_grep -qE '^[[:space:]]*flush_interval -1$' "$b"
  assert_grep -qE '^[[:space:]]*max_size 16MB$' "$b"
  # The upstream comes from the route, never a literal.
  assert_grep -qF 'reverse_proxy {{ r.upstream }}' "$b"
  refute_grep -qE 'reverse_proxy [0-9]' "$b"
}

@test "caddy: env template prod defaults match the compose defaults" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  grep -qE "caddy_image \| default\('iarekylew00t/caddy-cloudflare:2\.11\.4-alpine'\)" "$f"
  grep -qE "caddy_file \| default\('\./Caddyfile'\)" "$f"
}

@test "caddy: local overlay adds caps/SELinux/network but does NOT republish ports" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'mem_limit:' "$f"
  grep -q 'label=disable' "$f"
  grep -q 'local-dev' "$f"
  # Ports/image/Caddyfile are env-param in the base — an overlay ports list
  # would APPEND (not replace), so it must not appear here.
  ! grep -qE '^[[:space:]]*ports:' "$f"
}
