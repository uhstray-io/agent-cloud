#!/usr/bin/env bats
# Structural tests for the Caddy reverse proxy (platform/services/caddy/deployment).
# Verifies the composable conversion: env-parameterized compose, container-only
# deploy.sh, local Caddyfile template, and an overlay-safe local profile.
#
# Run: bats platform/tests/test_service_caddy.bats

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
  grep -qE '\$\{CADDY_IMAGE:-iarekylew00t/caddy-cloudflare:latest\}' "$f"
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

@test "caddy: local Caddyfile template uses internal CA + reverse_proxy, no real domain" {
  local f="$DEPLOY_DIR/templates/Caddyfile.local.j2"
  [ -f "$f" ]
  grep -q 'local_certs' "$f"
  grep -q 'reverse_proxy' "$f"
  grep -q 'caddy_routes' "$f"
  ! grep -qE 'uhstray\.io' "$f"
}

@test "caddy: env template prod defaults match the compose defaults" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  grep -qE "caddy_image \| default\('iarekylew00t/caddy-cloudflare:latest'\)" "$f"
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
