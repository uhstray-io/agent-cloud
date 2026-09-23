#!/usr/bin/env bats
# Structural tests for the o11y stack (platform/services/o11y/deployment).
# Verifies the composable shape: env-parameterized 4-service compose, pinned
# images, healthchecks, container-only deploy.sh (no secret gen), committed
# config-as-code (Prometheus/Loki/Alloy/Grafana provisioning) + a valid
# dashboard, and an overlay-safe local profile.
#
# Run: bats platform/tests/test_service_o11y.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/o11y/deployment"
}

@test "o11y: webhook seed refuses a missing environment secret before OpenBao access" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  run env -u O11Y_ALERT_DISCORD_WEBHOOK_URL ansible-playbook \
    "$REPO_ROOT/platform/playbooks/seed-o11y-alert-webhook.yml" \
    -e openbao_addr=http://127.0.0.1:8200
  [ "$status" -ne 0 ]
  [[ "$output" == *"O11Y_ALERT_DISCORD_WEBHOOK_URL environment secret"* ]]
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

@test "o11y: Grafana OIDC uses local bridge only in local mode" {
  python3 - "$DEPLOY_DIR/templates/env.j2" <<'PY'
import sys
from jinja2 import Environment, StrictUndefined

env = Environment(undefined=StrictUndefined)
env.filters['bool'] = bool
template = env.from_string(open(sys.argv[1], encoding='utf-8').read())
secrets = {'grafana_admin_password': 'test-only', 'grafana_oidc_client_secret': 'test-only'}

def values(**kwargs):
    rendered = template.render(secrets=secrets, **kwargs)
    return dict(line.split('=', 1) for line in rendered.splitlines() if '=' in line and not line.startswith('#'))

local = values(local_mode=True)
assert local['GF_SERVER_ROOT_URL'] == 'https://grafana.agent-cloud.test:8443/'
assert local['GF_AUTH_GENERIC_OAUTH_AUTH_URL'] == 'https://auth.agent-cloud.test:8443/application/o/authorize/'
assert local['GF_AUTH_GENERIC_OAUTH_TOKEN_URL'] == 'http://authentik-server:9000/application/o/token/'
assert local['GF_AUTH_GENERIC_OAUTH_API_URL'] == 'http://authentik-server:9000/application/o/userinfo/'

prod = values(local_mode=False, o11y_zone='uhstray.io')
assert prod['GF_SERVER_ROOT_URL'] == 'https://grafana.uhstray.io/'
assert prod['GF_AUTH_GENERIC_OAUTH_AUTH_URL'] == 'https://auth.uhstray.io/application/o/authorize/'
assert prod['GF_AUTH_GENERIC_OAUTH_TOKEN_URL'] == 'https://auth.uhstray.io/application/o/token/'
assert prod['GF_AUTH_GENERIC_OAUTH_API_URL'] == 'https://auth.uhstray.io/application/o/userinfo/'
assert 'GF_AUTH_GENERIC_OAUTH_TLS_SKIP_VERIFY_INSECURE' not in prod
PY
}

@test "o11y: production refuses a missing DNS zone before placement" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  cat > "$BATS_TEST_TMPDIR/inventory.yml" <<'YAML'
all:
  children:
    o11y_svc:
      hosts:
        o11y-probe:
          ansible_connection: local
YAML
  run ansible-playbook -i "$BATS_TEST_TMPDIR/inventory.yml" \
    "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" -e local_mode=false
  [ "$status" -ne 0 ]
  assert_contains "$output" "Production o11y needs its declared DNS zone"
  refute_contains "$output" "TASK [Place the monorepo"
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
    config = yaml.safe_load(template.render(**values, dgx_spark_gpu_exporter_enabled=gpu))
    assert list(config) == ['scrape_configs']
    jobs = config['scrape_configs']
    assert [job['job_name'] for job in jobs] == (
        ['dgx-spark-node', 'dgx-spark-gpu', 'dgx-spark-vllm']
        if gpu else ['dgx-spark-node', 'dgx-spark-vllm']
    )
    assert jobs[0]['static_configs'][1]['targets'] == ['192.0.2.2:9100']
    assert jobs[-1]['static_configs'][0]['labels']['node'] == 'spark-1'
PY
}

@test "o11y: agentgateway scrape renders from a declared remote endpoint" {
  python3 - "$DEPLOY_DIR/templates/scrape-agentgateway.yml.j2" <<'PY'
import json
import sys

import yaml
from jinja2 import Environment, StrictUndefined

env = Environment(undefined=StrictUndefined)
env.filters['to_json'] = json.dumps
config = yaml.safe_load(env.from_string(open(sys.argv[1], encoding='utf-8').read()).render(
    agentgateway_metrics_address='gateway.example.test',
    agentgateway_metrics_port=19002,
))
job = config['scrape_configs'][0]
assert job['job_name'] == 'agentgateway'
assert job['metrics_path'] == '/metrics'
assert job['static_configs'] == [{
    'targets': ['gateway.example.test:19002'],
    'labels': {'service': 'agentgateway', 'component': 'gateway', 'env': 'prod'},
}]
PY
}

@test "o11y: alert rules and contact point render for both rollout states" {
  python3 - "$DEPLOY_DIR/templates/alerts.yml.j2" "$DEPLOY_DIR/templates/alert-contact.yml.j2" <<'PY'
import sys

import yaml
from jinja2 import Environment, StrictUndefined

env = Environment(undefined=StrictUndefined, trim_blocks=True)
env.filters['bool'] = bool
template = env.from_string(
    open(sys.argv[1], encoding='utf-8').read()
)
contact_template = env.from_string(open(sys.argv[2], encoding='utf-8').read())
targets = [{'uid': 'o11y_missing_caddy', 'service': 'caddy', 'instance': 'caddy:2021'}]
for enabled in (False, True):
    for declared in ([], targets):
        rules = yaml.safe_load(template.render(o11y_expected_metrics_targets=declared, local_mode=False, o11y_alerts_enabled=enabled))['groups'][0]['rules']
        assert [rule['uid'] for rule in rules] == ['o11y_service_down'] + [target['uid'] for target in declared]
        assert all(rule['isPaused'] is not enabled for rule in rules)
        assert all(rule['annotations']['dashboard_url'] == '/d/service-overview' for rule in rules)
        assert all(('notification_settings' in rule) is enabled for rule in rules)
        assert 'up{service!=""} == 0' == rules[0]['data'][0]['model']['expr']
        if declared:
            assert 'absent_over_time(up{service="caddy",instance="caddy:2021"}[5m])' == rules[1]['data'][0]['model']['expr']
    contact = yaml.safe_load(contact_template.render(o11y_alerts_enabled=enabled))
    if enabled:
        receiver = contact['contactPoints'][0]['receivers'][0]
        assert receiver['type'] == 'discord'
        assert receiver['settings']['url'] == '$O11Y_ALERT_DISCORD_WEBHOOK_URL'
    else:
        assert contact['deleteContactPoints'][0]['uid'] == 'o11y_ops_discord'
local_rules = yaml.safe_load(template.render(local_mode=True))['groups'][0]['rules']
assert local_rules[1]['uid'] == 'o11y_missing_caddy'
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
