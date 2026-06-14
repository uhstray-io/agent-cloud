#!/usr/bin/env bats
# Structural tests for the o11y stack (platform/services/o11y/deployment).
# Verifies the composable shape: env-parameterized 4-service compose, pinned
# images, healthchecks, container-only deploy.sh (no secret gen), committed
# config-as-code (Prometheus/Loki/Alloy/Grafana provisioning) + a valid
# dashboard, and an overlay-safe local profile.
#
# Run: bats platform/tests/test_service_o11y.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/o11y/deployment"
}

@test "o11y: compose env-parameterizes all four images + grafana bind/port" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{O11Y_GRAFANA_IMAGE' "$f"
  grep -qE '\$\{O11Y_PROM_IMAGE' "$f"
  grep -qE '\$\{O11Y_LOKI_IMAGE' "$f"
  grep -qE '\$\{O11Y_ALLOY_IMAGE' "$f"
  grep -qE '\$\{O11Y_GRAFANA_PORT' "$f"
}

@test "o11y: prod default images are pinned upstream tags (no :latest drift)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{O11Y_GRAFANA_IMAGE:-docker\.io/grafana/grafana:[0-9.]+\}' "$f"
  grep -qE '\$\{O11Y_PROM_IMAGE:-docker\.io/prom/prometheus:v[0-9.]+\}' "$f"
  grep -qE '\$\{O11Y_LOKI_IMAGE:-docker\.io/grafana/loki:[0-9.]+\}' "$f"
  grep -qE '\$\{O11Y_ALLOY_IMAGE:-docker\.io/grafana/alloy:v[0-9.]+\}' "$f"
}

@test "o11y: four-service stack with healthchecks on the queryable services" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '^\s+grafana:' "$f"
  grep -qE '^\s+prometheus:' "$f"
  grep -qE '^\s+loki:' "$f"
  grep -qE '^\s+alloy:' "$f"
  grep -q '/api/health' "$f"   # grafana
  grep -q '/-/ready' "$f"      # prometheus
  grep -q '/ready' "$f"        # loki
}

@test "o11y: deploy.sh is executable, bash, sources common.sh, uses compose, no secrets" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qE '^#!/usr/bin/env bash'
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
  ! grep -qE '\b(gen_secret|put_secret|get_secret|bao_)' "$f"
}

@test "o11y: env template default images match compose + grafana pw from OpenBao" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  grep -qF 'GF_SECURITY_ADMIN_PASSWORD={{ secrets.grafana_admin_password }}' "$f"
  grep -qE "o11y_grafana_image \| default\('docker\.io/grafana/grafana:[0-9.]+'\)" "$f"
}

@test "o11y: committed config-as-code present (prometheus/loki/alloy/grafana)" {
  grep -q 'caddy:2019' "$DEPLOY_DIR/config/prometheus.yml"
  grep -q 'schema: v13' "$DEPLOY_DIR/config/loki-config.yml"
  grep -q 'loki.write' "$DEPLOY_DIR/config/config.alloy"
  grep -qE 'url: http://prometheus:9090' "$DEPLOY_DIR/config/grafana/provisioning/datasources/datasources.yml"
  grep -qE 'url: http://loki:3100' "$DEPLOY_DIR/config/grafana/provisioning/datasources/datasources.yml"
}

@test "o11y: starter dashboard is valid JSON with the expected uid" {
  local f="$DEPLOY_DIR/config/grafana/dashboards/agent-cloud-overview.json"
  [ -f "$f" ]
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['uid']=='agent-cloud-overview'" "$f"
}

@test "o11y: local overlay adds caps/SELinux/local-dev + the alloy socket, no ports republish" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'mem_limit:' "$f"
  grep -q 'label=disable' "$f"
  grep -q 'local-dev' "$f"
  grep -q 'podman.sock' "$f"
  ! grep -qE '^[[:space:]]*ports:' "$f"
}
