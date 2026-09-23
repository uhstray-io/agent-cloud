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

@test "o11y: webhook provisioning refuses missing private channel IDs before OpenBao access" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  cp "$REPO_ROOT/platform/inventory/local-dev.yml.example" "$BATS_TEST_TMPDIR/inventory.yml"
  run env ANSIBLE_LOCAL_TEMP="$BATS_TEST_TMPDIR/ansible" ansible-playbook \
    -i "$BATS_TEST_TMPDIR/inventory.yml" \
    "$REPO_ROOT/platform/playbooks/seed-o11y-alert-webhook.yml" \
    -e openbao_addr=http://127.0.0.1:8200
  [ "$status" -ne 0 ]
  [[ "$output" == *"Declare Discord guild and text-channel IDs in private o11y inventory."* ]]
}

@test "o11y: webhook credentials stay on the controller and out of task output" {
  python3 - "$REPO_ROOT/platform/playbooks/seed-o11y-alert-webhook.yml" "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import sys
import yaml

plays = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
tasks = plays[1]['tasks']
uri_tasks = [task for task in tasks if 'ansible.builtin.uri' in task]
assert uri_tasks
assert all(task.get('delegate_to') == 'localhost' and task.get('no_log') is True for task in uri_tasks)
for task in tasks:
    if task.get('ansible.builtin.set_fact') and any(
        key in task['ansible.builtin.set_fact'] for key in ('_matching', '_webhook', '_webhook_url')
    ):
        assert task.get('no_log') is True
merge, = (task for task in tasks if task.get('ansible.builtin.include_tasks') == 'tasks/bao-merge-keys.yml')
assert merge.get('no_log') is True
assert merge['vars']['_bm_on_missing'] == 'fail'
templates = yaml.safe_load(open(sys.argv[2], encoding='utf-8'))['templates']
seed, = (item for item in templates if item['name'] == 'Seed o11y Alert Webhook')
sha, = (item for item in seed['survey_vars'] if item['name'] == 'expected_repository_sha')
assert sha['required'] is True
PY
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
assert prod['GF_SERVER_ROOT_URL'] == 'https://o11y.uhstray.io/'
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

@test "o11y: production Dev template pins both controller and receiver revisions" {
  local playbook="$REPO_ROOT/platform/playbooks/deploy-o11y.yml"
  local templates="$REPO_ROOT/platform/semaphore/templates.yml"
  python3 - "$templates" <<'PY'
import sys
import yaml

items = yaml.safe_load(open(sys.argv[1]))['templates']
template, = (item for item in items if item['name'] == 'Deploy o11y (Dev)')
survey = {item['name']: item for item in template['survey_vars']}
assert template['repository'] == 'agent-cloud dev'
assert template['playbook'] == 'platform/playbooks/deploy-o11y.yml'
assert survey['service_branch']['default_value'] == 'dev'
assert survey['expected_repository_sha']['required'] is True
PY
  assert_precedes "$playbook" 'Read the placed revision when a candidate SHA is required' 'Manage secrets and template env file'
  assert_grep -qF 'that: _placed_revision.stdout == expected_repository_sha' "$playbook"
  assert_grep -qF 'argv: [git, status, --porcelain, --untracked-files=all]' "$playbook"
  python3 - "$playbook" <<'PY'
import sys
import yaml

plays = yaml.safe_load(open(sys.argv[1]))
tasks = {task['name']: task for play in plays for task in play.get('tasks', [])}
for name in (
    'Read the placed revision when a candidate SHA is required',
    'Refuse a receiver checkout that differs from the reviewed candidate',
):
    assert 'not (local_mode | default(false) | bool)' in tasks[name]['when']
PY
}

@test "o11y: an incorrect candidate SHA refuses before receiver placement" {
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
    "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" \
    -e expected_repository_sha=0000000000000000000000000000000000000000
  [ "$status" -ne 0 ]
  assert_contains "$output" "Semaphore checked out a different revision; no o11y files were placed."
  refute_contains "$output" "PLAY [Phase 1: Place repo + manage o11y secrets]"
}

@test "o11y: an empty receiver inventory refuses before placement" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  run ansible-playbook -i localhost, "$REPO_ROOT/platform/playbooks/deploy-o11y.yml"
  [ "$status" -ne 0 ]
  assert_contains "$output" "Inventory group 'o11y_svc' is absent or empty"
  refute_contains "$output" "PLAY [Phase 1: Place repo + manage o11y secrets]"
}

@test "o11y: fault drill accepts Grafana alert states and verifies the failing instance list" {
  python3 - "$REPO_ROOT/platform/playbooks/drill-o11y-unreachable.yml" "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import json, re, sys, yaml
from jinja2 import Environment

plays = yaml.safe_load(open(sys.argv[1]))
assert plays[0]['ansible.builtin.import_playbook'] == 'preflight-target-group.yml'
assert plays[0]['vars']['preflight_group'] == plays[0]['vars']['preflight_group_expected'] == 'o11y_svc'
revision = next(play for play in plays if play.get('name') == 'Verify the proposed revision before the fault drill')
assert any(task['name'] == 'Require a clean candidate checkout' for task in revision['tasks'])
templates = yaml.safe_load(open(sys.argv[2]))['templates']
template, = (item for item in templates if item['name'] == 'Drill o11y Unreachable')
assert template['dev_variant'] is True
survey = {item['name']: item for item in template['survey_vars']}
assert survey['expected_repository_sha']['required'] is True
assert survey['drill_expect_alert']['default_value'] == 'false'
drill = next(play for play in plays if play.get('name') == 'Prove a declared unreachable metrics endpoint fails visibly')
tasks = drill['tasks'][-1]['block']
wait = next(t for t in tasks if t['name'] == "Wait for Grafana's service-down rule to fire for the probe")
rescue = next(t for t in tasks if t['name'] == 'Require the onboarding verifier to refuse the named endpoint')['rescue'][0]
env = Environment()
env.filters['from_json'] = json.loads
env.filters['to_json'] = json.dumps
env.tests['match'] = lambda value, pattern: re.match(pattern, value) is not None
env.tests['search'] = lambda value, pattern: re.search(pattern, value) is not None
matches = env.compile_expression(wait['until'])
for state, expected in [('Alerting', True), ('Alerting (Error)', False), ('firing', True), ('Normal', False)]:
    response = {'data': {'alerts': [{'labels': {'service': 'pilot'}, 'state': state}]}}
    assert bool(matches(_firing_alerts={'rc': 0, 'stdout': json.dumps(response)}, expected_service='pilot')) is expected
checks = rescue['ansible.builtin.assert']['that']
instance_check = env.compile_expression(checks[-1])
for msg, expected in [("pilot at probe:65535: failing instances=['probe:65535']; scrapes found=1.", True),
                      ("pilot at probe:65535: failing instances=[]; scrapes found=0.", False),
                      ("pilot at probe:65535: failing instances=['other']; scrapes found=1.", False),
                      ("pilot at probe:65535: failing instances=['other', 'probe:65535']; scrapes found=2.", True)]:
    assert bool(instance_check(ansible_failed_result={'msg': msg}, expected_instance='probe:65535')) is expected
receipt = next(t for t in tasks if t['name'] == 'Wait for the matching Discord webhook message')
assert receipt['delegate_to'] == 'localhost' and receipt['no_log'] is True
received = env.compile_expression(receipt['until'])
for webhook_id, service, expected in [('123', 'o11y-fault-probe', True),
                                      ('456', 'o11y-fault-probe', False),
                                      ('123', 'other-service', False)]:
    messages = [{'webhook_id': webhook_id, 'embeds': [{'description': service}]}]
    assert bool(received(_discord_messages={'json': messages},
                         _webhook_id='123', expected_service='o11y-fault-probe')) is expected
PY
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
        assert 'up{service!=""}' == rules[0]['data'][0]['model']['expr']
        assert rules[0]['data'][1]['model']['conditions'][0]['evaluator'] == {'type': 'lt', 'params': [0.5]}
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
