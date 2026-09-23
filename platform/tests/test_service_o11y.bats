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

@test "o11y: retention defaults reach Prometheus and Loki" {
  grep -q "O11Y_PROM_RETENTION={{ o11y_prom_retention | default('15d') }}" "$DEPLOY_DIR/templates/env.j2"
  grep -q "O11Y_LOKI_RETENTION={{ o11y_loki_retention | default('7d') }}" "$DEPLOY_DIR/templates/env.j2"
  grep -q 'storage.tsdb.retention.time=${O11Y_PROM_RETENTION:-15d}' "$DEPLOY_DIR/compose.yml"
  grep -q -- '-config.expand-env=true' "$DEPLOY_DIR/compose.yml"
  grep -q 'retention_period: ${O11Y_LOKI_RETENTION:-7d}' "$DEPLOY_DIR/config/loki-config.yml"
}

@test "o11y: committed config-as-code present (prometheus/loki/alloy/grafana)" {
  # Prometheus self-scrape is the committed config-as-code. Per-target scrapes
  # (Caddy :2019, cAdvisor, ...) are deferred to Phase 2 in prometheus.yml
  # because Caddy's admin API is loopback-only and not yet cross-container
  # routable — so do NOT assert caddy:2019 here (O11Y-DEPLOYMENT.md Phase 2).
  grep -q 'job_name: prometheus' "$DEPLOY_DIR/config/prometheus.yml"
  grep -q 'schema: v13' "$DEPLOY_DIR/config/loki-config.yml"
  grep -q 'loki.write' "$DEPLOY_DIR/config/config.alloy"
  grep -qE 'url: http://prometheus:9090' "$DEPLOY_DIR/config/grafana/provisioning/datasources/datasources.yml"
  grep -qE 'url: http://loki:3100' "$DEPLOY_DIR/config/grafana/provisioning/datasources/datasources.yml"
}

@test "o11y: DGX scrape jobs render from inventory with GPU optional" {
  python3 - "$DEPLOY_DIR/templates/scrape-dgx-spark.yml.j2" <<'PY'
import json
import sys

import yaml
from jinja2 import Environment, StrictUndefined

env = Environment(undefined=StrictUndefined)
env.filters['to_json'] = json.dumps
env.filters['bool'] = bool
template = env.from_string(open(sys.argv[1], encoding='utf-8').read())
values = {
    'dgx_spark_nodes': [
        {'name': 'spark-1', 'address': '192.0.2.1'},
        {'name': 'spark-2', 'address': '192.0.2.2'},
    ],
    'dgx_spark_node_exporter_port': 9100,
    'dgx_spark_gpu_exporter_port': 9400,
    'dgx_spark_head_address': '192.0.2.1',
    'dgx_spark_head_name': 'spark-1',
    'dgx_spark_api_port': 8000,
}
for gpu in (False, True):
    jobs = yaml.safe_load(template.render(**values, dgx_spark_gpu_exporter_enabled=gpu))
    assert [job['job_name'] for job in jobs] == (
        ['dgx-spark-node', 'dgx-spark-gpu', 'dgx-spark-vllm']
        if gpu else ['dgx-spark-node', 'dgx-spark-vllm']
    )
    assert jobs[0]['static_configs'][1]['targets'] == ['192.0.2.2:9100']
    assert jobs[-1]['static_configs'][0]['labels']['node'] == 'spark-1'
PY
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
