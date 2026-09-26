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

@test "o11y: webhook provisioning requires an exact reviewed revision before OpenBao access" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  cp "$REPO_ROOT/platform/inventory/local-dev.yml.example" "$BATS_TEST_TMPDIR/inventory.yml"
  run env ANSIBLE_LOCAL_TEMP="$BATS_TEST_TMPDIR/ansible" ansible-playbook \
    -i "$BATS_TEST_TMPDIR/inventory.yml" \
    "$REPO_ROOT/platform/playbooks/seed-o11y-alert-webhook.yml" \
    -e openbao_addr=http://127.0.0.1:8200
  [ "$status" -ne 0 ]
  [[ "$output" == *"Semaphore checked out a different revision; no webhook was provisioned."* ]]
}

@test "o11y: webhook credentials stay on the controller and out of task output" {
  python3 - "$REPO_ROOT/platform/playbooks/seed-o11y-alert-webhook.yml" "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import sys
import yaml

plays = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
revision = plays[0]['tasks']
assert all('when' not in task for task in revision if 'revision' in task['name'] or 'checkout changes' in task['name'] or 'clean candidate' in task['name'])
tasks = plays[1]['tasks']
assert next(task for task in tasks if task['name'] == 'Require the private Discord destination and OpenBao access') == tasks[0]
assert next(task for task in tasks if task['name'] == 'Authenticate to OpenBao') != tasks[0]
uri_tasks = [task for task in tasks if 'ansible.builtin.uri' in task]
assert uri_tasks
assert all(task.get('delegate_to') == 'localhost' and task.get('no_log') is True for task in uri_tasks)
assert all(task['ansible.builtin.uri']['headers']['User-Agent'].startswith('DiscordBot (')
           for task in uri_tasks if task['ansible.builtin.uri']['url'].startswith('https://discord.com/'))
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
assert 'O11Y_AUTHENTIK_EDGE_IP' not in local

prod = values(local_mode=False, o11y_zone='uhstray.io', _auth_edge_ip='192.0.2.10')
assert prod['GF_SERVER_ROOT_URL'] == 'https://o11y.uhstray.io/'
assert prod['GF_AUTH_GENERIC_OAUTH_AUTH_URL'] == 'https://auth.uhstray.io/application/o/authorize/'
assert prod['GF_AUTH_GENERIC_OAUTH_TOKEN_URL'] == 'https://auth.uhstray.io/application/o/token/'
assert prod['GF_AUTH_GENERIC_OAUTH_API_URL'] == 'https://auth.uhstray.io/application/o/userinfo/'
assert prod['O11Y_ZONE'] == 'uhstray.io'
assert prod['O11Y_AUTHENTIK_EDGE_IP'] == '192.0.2.10'
assert 'GF_AUTH_GENERIC_OAUTH_TLS_SKIP_VERIFY_INSECURE' not in prod
PY
}

@test "o11y: production Grafana resolves Authentik through declared Caddy IP" {
  python3 - "$DEPLOY_DIR/compose.prod.yml" "$DEPLOY_DIR/deploy.sh" <<'PY'
import sys
import yaml

overlay = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
assert overlay['services']['grafana']['extra_hosts'] == [
    'auth.${O11Y_ZONE:?}:${O11Y_AUTHENTIK_EDGE_IP:?}'
]
deploy = open(sys.argv[2], encoding='utf-8').read()
assert 'if [ "${LOCAL_MODE:-}" != "true" ]; then' in deploy
assert 'COMPOSE_OVERLAYS="compose.prod.yml ${COMPOSE_OVERLAYS:-}"' in deploy
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
  assert_contains "$output" "Production o11y needs its declared DNS zone and one Caddy origin IP"
  refute_contains "$output" "TASK [Place the monorepo"
}

@test "o11y: production refuses a missing Caddy origin before placement" {
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
    -e local_mode=false -e o11y_zone=example.invalid
  [ "$status" -ne 0 ]
  assert_contains "$output" "Production o11y needs its declared DNS zone and one Caddy origin IP"
  refute_contains "$output" "TASK [Place the monorepo"
}

@test "o11y: local clean excludes production overlay and reports a failed Compose teardown" {
  python3 - "$REPO_ROOT/platform/playbooks/tasks/clean-service.yml" <<'PY'
import pathlib
import subprocess
import sys
import tempfile
import yaml

tasks = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
with tempfile.TemporaryDirectory() as temp:
    root = pathlib.Path(temp)
    deploy = root / 'deployment'
    deploy.mkdir()
    for name in ('compose.yml', 'compose.local.yml', 'compose.prod.yml'):
        (deploy / name).write_text('services: {}\n', encoding='utf-8')
    engine = root / 'mock-engine'
    engine.write_text('#!/bin/sh\ncase "$1" in compose) printf "%s\\n" "$@" > "$MOCK_ARGS"; exit 23;; ps) exit 0;; esac\n', encoding='utf-8')
    engine.chmod(0o755)
    for mode in ('local', 'prod'):
        task = next(t for t in tasks if t['name'].startswith(f'Stop and remove containers + volumes ({mode})'))
        shell = task['ansible.builtin.shell']
        shell = shell.replace('{{ _monorepo_dir }}', str(root))
        shell = shell.replace('{{ monorepo_deploy_path }}', 'deployment')
        shell = shell.replace('{{ container_engine | default(\'podman\') }}', str(engine))
        shell = shell.replace('{{ container_engine | default(\'docker\') }}', str(engine))
        shell = shell.replace('{{ service_name }}', 'o11y')
        shell = shell.replace("{{ '{{' }}.Names{{ '}}' }}", '.Names')
        args_path = root / f'{mode}-args'
        result = subprocess.run(['/bin/bash', '-c', shell], cwd=deploy,
                                env={'MOCK_ARGS': str(args_path), 'PATH': '/usr/bin:/bin'},
                                capture_output=True, text=True)
        assert result.returncode == 23, (mode, result.returncode, result.stderr)
        args = args_path.read_text(encoding='utf-8')
        assert ('compose.local.yml' in args) == (mode == 'local')
        assert ('compose.prod.yml' in args) == (mode == 'prod')
PY
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
  assert_precedes "$playbook" 'Read the placed revision when a candidate SHA is required' 'Configure o11y alert provisioning from OpenBao'
  assert_grep -qF 'ansible.builtin.include_tasks: tasks/o11y-alert-provision.yml' "$playbook"
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
  python3 - "$REPO_ROOT/platform/playbooks/drill-o11y-unreachable.yml" "$REPO_ROOT/platform/semaphore/templates.yml" "$REPO_ROOT/platform/playbooks/tasks/o11y-alert-probe.yml" "$REPO_ROOT/platform/playbooks/tasks/o11y-alert-delivery-preflight.yml" <<'PY'
import json, re, sys, yaml
from jinja2 import Environment, StrictUndefined

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
assert drill['tasks'][0]['ansible.builtin.include_tasks'] == 'tasks/assert-bao-transport.yml'
assert drill['tasks'][1]['ansible.builtin.include_tasks'] == 'tasks/o11y-alert-delivery-preflight.yml'
assert drill['tasks'][2]['ansible.builtin.include_tasks'] == 'tasks/o11y-alert-probe.yml'
probe_tasks = yaml.safe_load(open(sys.argv[3]))
preflight_tasks = yaml.safe_load(open(sys.argv[4]))
tasks = probe_tasks[-1]['block']
assert any(task['name'] == "Name this run's disposable probe" for task in probe_tasks)
assert "{{ _probe }}" in drill['vars']['expected_service']
assert all(task.get('delegate_to') == 'localhost' and task.get('no_log') is True
           for task in preflight_tasks if 'ansible.builtin.uri' in task)
assert all(task['ansible.builtin.uri']['headers']['User-Agent'].startswith('DiscordBot (')
           for task in preflight_tasks + tasks if 'ansible.builtin.uri' in task
           and task['ansible.builtin.uri']['url'].startswith('https://discord.com/'))
marker = next(task for task in preflight_tasks if task['name'] == 'Mark the last Discord message before the probe')
assert marker['ignore_errors'] is True
assert any(task['name'] == 'Require Discord message-history access before the probe' for task in preflight_tasks)
wait = next(t for t in tasks if t['name'] == "Wait for Grafana's service-down rule to fire for the probe")
rescue = next(t for t in tasks if t['name'] == 'Require the onboarding verifier to refuse the named endpoint')['rescue'][0]
env = Environment(undefined=StrictUndefined)
env.filters['from_json'] = json.loads
env.filters['to_json'] = json.dumps
env.tests['match'] = lambda value, pattern: re.match(pattern, value) is not None
env.tests['search'] = lambda value, pattern: re.search(pattern, value) is not None
matches = env.compile_expression(wait['until'])
for state, expected in [('Alerting', True), ('Alerting (Error)', False), ('firing', True), ('Normal', False)]:
    response = {'data': {'alerts': [{'labels': {'service': 'pilot'}, 'state': state}]}}
    assert bool(matches(_firing_alerts={'rc': 0, 'stdout': json.dumps(response)}, expected_service='pilot')) is expected
response = {'data': {'alerts': [
    {'state': 'Normal'},
    {'labels': {'team': 'other'}, 'state': 'Alerting'},
    {'labels': {'service': 'pilot'}, 'state': 'Alerting'},
]}}
assert matches(_firing_alerts={'rc': 0, 'stdout': json.dumps(response)}, expected_service='pilot')
response['data']['alerts'].pop()
assert not matches(_firing_alerts={'rc': 0, 'stdout': json.dumps(response)}, expected_service='pilot')
checks = rescue['ansible.builtin.assert']['that']
instance_check = env.compile_expression(checks[-1])
for msg, expected in [("pilot at probe:65535: failing instances=['probe:65535']; scrapes found=1.", True),
                      ("pilot at probe:65535: failing instances=[]; scrapes found=0.", False),
                      ("pilot at probe:65535: failing instances=['other']; scrapes found=1.", False),
                      ("pilot at probe:65535: failing instances=['other', 'probe:65535']; scrapes found=2.", True)]:
    assert bool(instance_check(ansible_failed_result={'msg': msg}, expected_instance='probe:65535')) is expected
receipt = next(t for t in tasks if t['name'] == 'Wait for the matching Discord webhook message')
assert receipt['delegate_to'] == 'localhost' and receipt['no_log'] is True and receipt['ignore_errors'] is True
probe = next(t for t in tasks if t['name'] == 'Start the opted-in probe with no metrics listener')
lifetime = int(re.search(r'sleep (\d+)', probe['ansible.builtin.command']['argv'][-1]).group(1))
scrape = next(t for t in tasks if t['name'] == 'Wait for the declared failed scrape')
assert lifetime > sum(t['retries'] * t['delay'] for t in (scrape, wait, receipt)) + 60
received = env.compile_expression(receipt['until'])
marker = 'o11y-delivery-status=firing service=o11y-fault-probe-new'
for webhook_id, body, expected in [('123', marker, True),
                                   ('456', marker, False),
                                   ('123', 'o11y-delivery-status=resolved service=o11y-fault-probe-new', False),
                                   ('123', 'o11y-delivery-status=firing service=o11y-fault-probe-old', False)]:
    messages = [{'webhook_id': webhook_id, 'content': body}]
    assert bool(received(_discord_messages={'json': messages},
                         _webhook_id='123', _delivery_marker=marker)) is expected
assert next(t for t in tasks if t['name'] == 'Require a readable firing receipt from the owned webhook')['ansible.builtin.assert']['fail_msg']
PY
}

@test "o11y: delivery canary restores paused rules through the shared task" {
  python3 - "$REPO_ROOT/platform/playbooks/drill-o11y-alert-canary.yml" \
    "$REPO_ROOT/platform/playbooks/restore-o11y-alert-baseline.yml" \
    "$REPO_ROOT/platform/playbooks/tasks/o11y-restore-alert-baseline.yml" \
    "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import posixpath
import sys
import yaml

canary, recovery, restore, catalog = [yaml.safe_load(open(path)) for path in sys.argv[1:]]
assert canary[0]['ansible.builtin.import_playbook'] == 'preflight-target-group.yml'
assert canary[1]['name'] == 'Verify the reviewed Dev checkout before the canary'
assert canary[2]['name'] == 'Prove alert delivery and restore the paused baseline'
tasks = canary[2]['tasks']
assert next(i for i, task in enumerate(tasks) if task['name'] == 'Refuse an already active service-down rule') < next(
    i for i, task in enumerate(tasks) if 'block' in task)
delivery = next(i for i, task in enumerate(tasks) if task['name'] == 'Verify Discord delivery prerequisites before activation')
flight = next(task for task in tasks if 'block' in task)
assert delivery < tasks.index(flight)
assert flight['block'][0]['name'] == 'Block normal deploys until production restoration is verified'
assert flight['block'][1]['name'] == 'Render a production-only unreachable scrape target'
assert flight['block'][1]['when'] == 'not (local_mode | default(false) | bool)'
assert '127.0.0.1:65535' in flight['block'][1]['ansible.builtin.copy']['content']
assert 'service: "{{ _probe }}"' in flight['block'][1]['ansible.builtin.copy']['content']
provision = next(task for task in flight['block'] if task.get('ansible.builtin.include_tasks') == 'tasks/o11y-alert-provision.yml')
assert provision['vars']['o11y_alerts_enabled'] is True
assert flight['block'][-1]['ansible.builtin.include_tasks'] == 'tasks/o11y-alert-probe.yml'
assert flight['always'][0]['ansible.builtin.include_tasks'] == 'tasks/o11y-restore-alert-baseline.yml'
assert recovery[-1]['tasks'][-1]['ansible.builtin.include_tasks'] == 'tasks/o11y-restore-alert-baseline.yml'
assert 'not (o11y_alerts_enabled | default(false) | bool)' in recovery[-1]['tasks'][0]['ansible.builtin.assert']['that']
assert not any('manage-secrets.yml' in str(task) for task in restore)
directory = next(i for i, task in enumerate(restore) if task['name'] == 'Recreate the generated Grafana alert provisioning directory')
rules = next(i for i, task in enumerate(restore) if task['name'] == 'Render paused Grafana alert rules without OpenBao')
assert directory < rules
assert restore[directory]['ansible.builtin.file']['state'] == 'directory'
assert all(directory < i and posixpath.dirname(task['ansible.builtin.template']['dest']) == restore[directory]['ansible.builtin.file']['path']
           for i, task in enumerate(restore) if 'ansible.builtin.template' in task)
assert any(task['name'] == 'Remove the canary webhook from the existing runtime environment' for task in restore)
assert next(task for task in restore if task['name'] == 'Remove only the generated production canary scrape declaration')['ansible.builtin.file']['state'] == 'absent'
assert next(task for task in restore if task['name'] == 'Restart o11y with the paused baseline')['environment']['LOCAL_MODE'] != 'true'
assert any(task.get('vars', {}).get('o11y_alerts_enabled') is False for task in restore)
assert any(task['name'] == 'Require the service-down rule to be paused again' for task in restore)
assert any(task['name'] == 'Require the canary contact point to be absent again' for task in restore)
assert restore[-1]['name'] == 'Clear the production canary marker only after baseline verification'
templates = {item['name']: item for item in catalog['templates']}
for name in ('Drill o11y Alert Canary (Dev)', 'Restore o11y Alert Baseline (Dev)'):
    assert templates[name]['repository'] == 'agent-cloud dev'
    assert templates[name]['survey_vars'][0]['name'] == 'expected_repository_sha'
PY
  for name in observability.yml contact.yml; do
    grep -qxF "config/grafana/provisioning/alerting/$name" "$DEPLOY_DIR/.gitignore"
    grep -qF -- "--exclude platform/services/o11y/deployment/config/grafana/provisioning/alerting/$name" \
      "$REPO_ROOT/platform/playbooks/tasks/place-monorepo.yml"
  done
}

@test "o11y: production canary uses one static failed scrape and restores it before restart" {
  python3 - "$REPO_ROOT/platform/playbooks/drill-o11y-alert-canary.yml" \
    "$REPO_ROOT/platform/playbooks/tasks/o11y-alert-probe.yml" \
    "$REPO_ROOT/platform/playbooks/tasks/o11y-restore-alert-baseline.yml" \
    "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" \
    "$REPO_ROOT/platform/playbooks/tasks/o11y-alert-provision.yml" <<'PY'
import sys
import yaml
from jinja2 import Environment, StrictUndefined

canary, probe, restore, deploy, provision = [yaml.safe_load(open(path, encoding='utf-8')) for path in sys.argv[1:]]
tasks = canary[2]['tasks']
slot = next(i for i, task in enumerate(tasks) if task['name'] == 'Require a clean production canary slot')
flight = next(i for i, task in enumerate(tasks) if 'block' in task)
assert slot < flight
assert next(task for task in tasks if task['name'] == 'Place the reviewed checkout for local canary rendering')['when'] == 'local_mode | default(false) | bool'
assert next(task for task in tasks if task['name'] == 'Read tracked changes in the deployed production checkout')['ansible.builtin.command']['argv'] == ['git', 'status', '--porcelain', '--untracked-files=no']
assert next(task for task in tasks if task['name'] == 'Refuse a receiver not already deployed from the reviewed revision')['ansible.builtin.assert']['that'] == [
    '_deployed_revision.stdout == expected_repository_sha', '_deployed_changes.stdout | length == 0']
assert next(i for i, task in enumerate(tasks) if task['name'] == 'Require a cleared production canary marker') < flight
route = next(task for task in tasks if task['name'] == 'Require the production route values already deployed')
assert route['ansible.builtin.command']['argv'][1] == '-Fxq'
assert route['loop'] == ['O11Y_ZONE={{ o11y_zone }}', 'O11Y_AUTHENTIK_EDGE_IP={{ _auth_edge_ip }}']
block = tasks[flight]['block']
assert block[0]['ansible.builtin.copy']['dest'].endswith('/config/.o11y-alert-canary-active')
static = next(task for task in block if task['name'] == 'Render a production-only unreachable scrape target')
rendered = Environment(undefined=StrictUndefined).from_string(static['ansible.builtin.copy']['content']).render(
    _probe='o11y-fault-probe-abcdef123456')
job, = yaml.safe_load(rendered)['scrape_configs']
target, = job['static_configs']
assert target['targets'] == ['127.0.0.1:65535']
assert target['labels']['service'] == 'o11y-fault-probe-abcdef123456'
assert static['when'] == 'not (local_mode | default(false) | bool)'
canary_provision = next(task for task in block if task.get('ansible.builtin.include_tasks') == 'tasks/o11y-alert-provision.yml')
assert canary_provision['vars']['o11y_canary_preserve_env'] is True
append = next(task for task in provision if task['name'] == 'Add only the canary webhook to the existing runtime environment')
assert append['no_log'] is True and append['ansible.builtin.lineinfile']['mode'] == '0600'
assert next(task for task in provision if task.get('ansible.builtin.include_tasks') == 'manage-secrets.yml')['when'] == 'not (o11y_canary_preserve_env | default(false) | bool)'
assert next(task for task in probe[-1]['block'] if task['name'] == 'Start the opted-in probe with no metrics listener')['when'][-1] == 'local_mode | default(false) | bool'
names = [task['name'] for task in restore]
assert names.index('Remove only the generated production canary scrape declaration') < names.index('Restart o11y with the paused baseline')
assert names.index('Require the canary contact point to be absent again') < names.index('Clear the production canary marker only after baseline verification')
assert tasks[flight]['always'][0]['ansible.builtin.include_tasks'] == 'tasks/o11y-restore-alert-baseline.yml'
normal = next(play for play in deploy if play.get('name') == 'Phase 1: Place repo + manage o11y secrets')['tasks']
names = [task['name'] for task in normal]
assert names.index('Refuse a normal deploy while the production canary needs restoration') < names.index('Place the monorepo + ensure podman/compose')
assert len(next(task for task in normal if task['name'] == 'Refuse a normal deploy while the production canary needs restoration')['ansible.builtin.assert']['that']) == 2
PY
}

@test "o11y: shared local placement preserves rendered alerts and copies committed rules" {
  command -v rsync >/dev/null 2>&1 || skip "rsync not available"
  python3 - "$REPO_ROOT/platform/playbooks/tasks/place-monorepo.yml" "$DEPLOY_DIR/.gitignore" <<'PY'
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml

placement = yaml.safe_load(Path(sys.argv[1]).read_text())
script = next(task['ansible.builtin.shell'] for task in placement
              if task['name'] == 'Copy working tree into place (local mode)')
assert '--delete-excluded' not in script
rel = Path('platform/services/o11y/deployment/config/grafana/provisioning/alerting')
with tempfile.TemporaryDirectory(prefix='o11y-placement-') as tmp:
    source, target = Path(tmp) / 'source', Path(tmp) / 'target'
    (source / rel).mkdir(parents=True)
    (source / 'platform/services/o11y/deployment/.gitignore').write_text(Path(sys.argv[2]).read_text())
    (source / rel / 'inference.yml').write_text('committed\n')
    (target / rel).mkdir(parents=True)
    for name in ('observability.yml', 'contact.yml'):
        (target / rel / name).write_text('rendered\n')
    rendered = script.replace('{{ _monorepo_dir }}', str(target)).replace(
        '{{ playbook_dir | dirname | dirname }}', str(source))
    subprocess.run(['bash', '-c', rendered], check=True, capture_output=True, text=True)
    assert all((target / rel / name).read_text() == 'rendered\n'
               for name in ('observability.yml', 'contact.yml'))
    assert (target / rel / 'inference.yml').read_text() == 'committed\n'
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

@test "o11y: DGX source probe is bounded and scraping remains opt-in" {
  python3 - "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" "$REPO_ROOT/platform/playbooks/probe-o11y-dgx-exporter.yml" "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import re
import sys

import yaml
from jinja2 import Environment, StrictUndefined

deploy = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
tasks = next(play['tasks'] for play in deploy if play.get('name') == 'Phase 1: Place repo + manage o11y secrets')
by_name = {task['name']: task for task in tasks}
gate = 'dgx_spark_scrape_enabled | default(false) | bool'
settings = by_name['Require complete DGX scrape settings when scraping is enabled']
assert settings['when'] == gate
assert 'dgx_spark_nodes | default([]) | length > 0' in settings['ansible.builtin.assert']['that']
assert by_name['Render DGX scrape targets only after source proof enables scraping']['when'] == gate
assert by_name['Remove DGX scrape targets while scraping is disabled']['when'] == f'not ({gate})'

probe = yaml.safe_load(open(sys.argv[2], encoding='utf-8'))
assert probe[0]['vars'] == {'preflight_group': 'o11y_svc', 'preflight_group_expected': 'o11y_svc'}
preflight = probe[1]['tasks']
assert any(task['name'] == 'Refuse a different controller revision' for task in preflight)
assert any(task['name'] == 'Refuse an altered controller checkout' for task in preflight)
node_play = probe[2]
assert node_play['hosts'] == 'o11y_svc'
env = Environment(undefined=StrictUndefined)
env.tests['search'] = lambda value, pattern: re.search(pattern, value) is not None
env.filters['bool'] = lambda value: value is True or str(value).lower() in ('true', 'yes', '1')
selector = env.compile_expression(node_play['vars']['_probe_nodes'].removeprefix('{{').removesuffix('}}').strip())
nodes = [{'name': 'spark-1', 'address': '192.0.2.1'}, {'name': 'spark-2', 'address': '192.0.2.2'}]
assert selector(dgx_spark_nodes=nodes, probe_node_name='spark-2') == [nodes[1]]
assert selector(dgx_spark_nodes=nodes, probe_node_name='unknown') == []
steps = {task['name']: task for task in node_play['tasks']}
route = steps['Read the receiver route and chosen source']
route_check = env.compile_expression(steps['Require a route with an explicit source address']['ansible.builtin.assert']['that'])
request = steps['Send one direct bounded metrics request from the receiver']
assert route['ansible.builtin.command']['argv'][-1] == '{{ _probe_node.address }}'
assert route_check(_probe_route={'stdout': '192.0.2.2 dev eth0 src 192.0.2.10'}) is True
assert route_check(_probe_route={'stdout': '192.0.2.2 dev eth0'}) is False
assert request['ansible.builtin.uri']['url'].startswith('http://{{ _probe_node.address }}:')
assert request['ansible.builtin.uri']['use_proxy'] is False
assert request['ansible.builtin.uri']['timeout'] == 5
assert request['when'] == 'not ansible_check_mode'
assert request['failed_when'] is False
status_guard = env.compile_expression(steps['Refuse a probe result without an HTTP status']['ansible.builtin.assert']['that'])
outcome = env.compile_expression(steps['Require the observed outcome to match the declared probe phase']['ansible.builtin.assert']['that'])
assert status_guard(_probe_http={'status': -1}) is True
assert status_guard(_probe_http={'msg': 'module failed'}) is False
for status, expected, valid in [(200, 'true', True), (-1, 'false', True), (200, 'false', False), (-1, 'true', False)]:
    assert bool(outcome(_probe_http={'status': status}, probe_expect_reachable=expected)) is valid
template = next(item for item in yaml.safe_load(open(sys.argv[3], encoding='utf-8'))['templates'] if item['name'] == 'Probe o11y DGX Exporter (Dev)')
assert template['repository'] == 'agent-cloud dev'
assert [item['name'] for item in template['survey_vars']] == ['expected_repository_sha', 'probe_node_name', 'probe_expect_reachable']
PY
}

@test "o11y: endpoint probe accepts only declared metrics targets in check mode" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  cat > "$BATS_TEST_TMPDIR/inventory.yml" <<'YAML'
all:
  children:
    o11y_svc:
      hosts:
        receiver:
          ansible_connection: local
          dgx_spark_nodes:
            - {name: spark-1, address: "192.0.2.1"}
          dgx_spark_head_name: spark-1
          dgx_spark_head_address: "192.0.2.1"
          dgx_spark_api_port: 8000
    agentgateway_svc:
      hosts:
        gateway:
          agw_stats_bind: "192.0.2.2"
          agw_stats_port: 19002
YAML
  local sha
  sha=$(git -C "$REPO_ROOT" rev-parse HEAD)
  python3 - "$REPO_ROOT/platform/playbooks/probe-o11y-metrics-endpoint.yml" "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import sys
import yaml
probe = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
steps = {task['name']: task for task in probe[2]['tasks']}
request = steps['Send one direct bounded metrics request from the receiver']
assert request['when'] == 'not ansible_check_mode'
assert request['ansible.builtin.uri']['url'] == 'http://{{ _probe_address }}:{{ _probe_port }}/metrics'
assert request['ansible.builtin.uri']['use_proxy'] is False
assert request['ansible.builtin.uri']['return_content'] is False
assert request['ansible.builtin.uri']['timeout'] == 5
template = next(item for item in yaml.safe_load(open(sys.argv[2], encoding='utf-8'))['templates'] if item['name'] == 'Probe o11y Metrics Endpoint (Dev)')
assert template['repository'] == 'agent-cloud dev'
assert [item['name'] for item in template['survey_vars']] == ['expected_repository_sha', 'probe_target']
PY
  for target in dgx-vllm agentgateway; do
    run env ANSIBLE_LOCAL_TEMP="$BATS_TEST_TMPDIR/ansible" ansible-playbook --check \
      -i "$BATS_TEST_TMPDIR/inventory.yml" \
      "$REPO_ROOT/platform/playbooks/probe-o11y-metrics-endpoint.yml" \
      -e expected_repository_sha="$sha" -e probe_target="$target"
    [ "$status" -eq 0 ]
    assert_contains "$output" "Refuse an unusable metrics address or port"
  done
  run env ANSIBLE_LOCAL_TEMP="$BATS_TEST_TMPDIR/ansible" ansible-playbook --check \
    -i "$BATS_TEST_TMPDIR/inventory.yml" \
    "$REPO_ROOT/platform/playbooks/probe-o11y-metrics-endpoint.yml" \
    -e expected_repository_sha="$sha" -e probe_target=arbitrary
  [ "$status" -ne 0 ]
  assert_contains "$output" "Select dgx-vllm or agentgateway"
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
        assert 'o11y-delivery-status=firing service=' in receiver['settings']['message']
        assert receiver['settings']['message'].index('o11y-delivery-status=firing service=') < receiver['settings']['message'].index('default.message')
    else:
        assert contact['deleteContactPoints'][0]['uid'] == 'o11y_ops_discord'
local_rules = yaml.safe_load(template.render(local_mode=True))['groups'][0]['rules']
assert local_rules[1]['uid'] == 'o11y_missing_caddy'
canary = 'o11y-fault-probe-' + 'a' * 12
canary_rules = yaml.safe_load(template.render(local_mode=True, o11y_alerts_enabled=True,
                                              o11y_alert_canary_service=canary))['groups'][0]['rules']
assert canary_rules[0]['data'][0]['model']['expr'] == f'up{{service="{canary}"}}'
assert canary_rules[0]['isPaused'] is False
assert all(rule['isPaused'] is True and 'notification_settings' not in rule for rule in canary_rules[1:])
PY
}

@test "o11y: real alert-enabled deploy verifies live rule and contact state" {
  python3 - "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" <<'PY'
import json
import re
import sys

import yaml
from jinja2 import Environment, StrictUndefined

plays = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
verify = next(play for play in plays if play.get('name') == 'Phase 3: Verify o11y')
block = next(task for task in verify['tasks'] if task['name'] == 'Verify enabled Grafana alert provisioning after a real deploy')
assert block['when'] == ['o11y_alerts_enabled | default(false) | bool', 'not ansible_check_mode']
tasks = block['block']
rule_check = next(task for task in tasks if task['name'] == 'Require the service-down rule and every o11y rule to be active')
env = Environment(undefined=StrictUndefined)
env.tests['match'] = lambda value, pattern: re.match(pattern, value) is not None
env.filters['from_json'] = json.loads
compile_value = lambda value: env.compile_expression(value.removeprefix('{{').removesuffix('}}').strip())
select_rules = compile_value(rule_check['vars']['_o11y_rules'])
checks = [env.compile_expression(expr) for expr in rule_check['ansible.builtin.assert']['that']]
for rules, expected in [
    ([{'uid': 'o11y_service_down', 'isPaused': False}], True),
    ([{'uid': 'o11y_service_down', 'isPaused': False}, {'uid': 'unrelated', 'isPaused': True}], True),
    ([{'uid': 'o11y_service_down', 'isPaused': False}, {'uid': 'o11y_missing_caddy', 'isPaused': True}], False),
    ([{'uid': 'unrelated', 'isPaused': False}], False),
    ([{'uid': 'o11y_service_down'}], False),
]:
    scoped = select_rules(_active_rules={'stdout': json.dumps(rules)})
    assert all(bool(check(_o11y_rules=scoped)) for check in checks) is expected
contact = next(task for task in tasks if task['name'] == 'Read live Grafana contact points without displaying webhook settings')
count = next(task for task in tasks if task['name'] == 'Count only the intended contact point without its settings')
assert contact['no_log'] is True and count['no_log'] is True
count_contacts = compile_value(count['ansible.builtin.set_fact']['_o11y_contact_count'])
contact_check = env.compile_expression(tasks[-1]['ansible.builtin.assert']['that'])
for contacts, expected in [
    ([{'uid': 'o11y_ops_discord'}], True),
    ([{'uid': 'other'}], False),
    ([{'uid': 'o11y_ops_discord'}, {'uid': 'o11y_ops_discord'}], False),
]:
    selected = count_contacts(_active_contacts={'stdout': json.dumps(contacts)})
    assert bool(contact_check(_o11y_contact_count=selected)) is expected
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
