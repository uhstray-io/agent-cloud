#!/usr/bin/env bats
# Structural tests for the o11y stack (platform/services/o11y/deployment).
# Verifies the composable shape: env-parameterized compose, pinned
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

@test "o11y: shared local placement preserves rendered configs and alerts and copies committed rules" {
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
generated = Path('platform/services/o11y/deployment/config')
with tempfile.TemporaryDirectory(prefix='o11y-placement-') as tmp:
    source, target = Path(tmp) / 'source', Path(tmp) / 'target'
    (source / rel).mkdir(parents=True)
    (source / 'platform/services/o11y/deployment/.gitignore').write_text(Path(sys.argv[2]).read_text())
    (source / rel / 'inference.yml').write_text('committed\n')
    (source / generated).mkdir(parents=True, exist_ok=True)
    (source / generated / 'loki-config.yml').write_text('committed config\n')
    (target / generated).mkdir(parents=True)
    for name in ('config.alloy', 'prometheus.yml'):
        (target / generated / name).write_text('rendered config\n')
    (target / rel).mkdir(parents=True)
    for name in ('observability.yml', 'contact.yml'):
        (target / rel / name).write_text('rendered\n')
    rendered = script.replace('{{ _monorepo_dir }}', str(target)).replace(
        '{{ playbook_dir | dirname | dirname }}', str(source))
    subprocess.run(['bash', '-c', rendered], check=True, capture_output=True, text=True)
    assert all((target / rel / name).read_text() == 'rendered\n'
               for name in ('observability.yml', 'contact.yml'))
    assert all((target / generated / name).read_text() == 'rendered config\n'
               for name in ('config.alloy', 'prometheus.yml'))
    assert (target / generated / 'loki-config.yml').read_text() == 'committed config\n'
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
  # (Caddy :2019, cAdvisor, ...) are deferred to Phase 2 in the Prometheus template
  # because Caddy's admin API is loopback-only and not yet cross-container
  # routable — so do NOT assert caddy:2019 here (O11Y-DEPLOYMENT.md Phase 2).
  grep -q 'job_name: prometheus' "$DEPLOY_DIR/templates/prometheus.yml.j2"
  grep -q 'schema: v13' "$DEPLOY_DIR/config/loki-config.yml"
  grep -q 'loki.write' "$DEPLOY_DIR/templates/config.alloy.j2"
  grep -qE 'url: http://prometheus:9090' "$DEPLOY_DIR/config/grafana/provisioning/datasources/datasources.yml"
  grep -qE 'url: http://loki:3100' "$DEPLOY_DIR/config/grafana/provisioning/datasources/datasources.yml"
}

@test "o11y: DGX scrape jobs render from inventory with GPU optional" {
  python3 - "$DEPLOY_DIR/templates/scrape-dgx-spark.yml.j2" "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" <<'PY'
import json
import sys

import yaml
from jinja2 import Environment, StrictUndefined

env = Environment(undefined=StrictUndefined)
env.filters['to_json'] = json.dumps
env.filters['bool'] = bool
plays = yaml.safe_load(open(sys.argv[2], encoding='utf-8'))
phase_one = next(play for play in plays if play.get('name') == 'Phase 1: Place repo + manage o11y secrets')
policy = phase_one['vars']['_o11y_forbidden_metric_label_names_regex']
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
    '_o11y_forbidden_metric_label_names_regex': policy,
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
    assert all(job['metric_relabel_configs'][0]['regex'] == policy for job in jobs)
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
assert request['ansible.builtin.uri']['follow_redirects'] == 'none'
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
          monorepo_deploy_path: platform/services/o11y/deployment
          o11y_zone: example.invalid
          agentgateway_metrics_address: "192.0.2.9"
          agentgateway_metrics_port: 19002
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
    caddy_svc:
      hosts:
        caddy:
          ansible_host: "192.0.2.3"
YAML
  # The probe refuses a checkout that differs from the reviewed commit or has local changes
  # (its "Refuse an altered controller checkout" gate), and it reads the repo it lives in. Run
  # it from a scratch repo holding this working tree's playbooks, so the test covers uncommitted
  # edits and passes in a developer checkout with untracked files (PR 281 review). Git exports
  # GIT_DIR and friends to hooks; clear them before touching any repo (2026-09-23 incident).
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX
  local probe_repo="$BATS_TEST_TMPDIR/probe-repo"
  mkdir -p "$probe_repo/platform"
  cp -R "$REPO_ROOT/platform/playbooks" "$probe_repo/platform/"
  # The repo's ignore rules, so interpreter caches Ansible writes (filter_plugins/__pycache__)
  # stay ignored exactly as in a real checkout.
  cp "$REPO_ROOT/.gitignore" "$probe_repo/"
  git -C "$probe_repo" init -q
  git -C "$probe_repo" add -A
  git -C "$probe_repo" -c user.email=t@example.invalid -c user.name=t -c core.hooksPath=/dev/null \
    -c commit.gpgsign=false commit -q -m scratch
  local sha probe="$probe_repo/platform/playbooks/probe-o11y-metrics-endpoint.yml"
  sha=$(git -C "$probe_repo" rev-parse HEAD)
  python3 - "$REPO_ROOT/platform/playbooks/probe-o11y-metrics-endpoint.yml" "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import sys
import yaml
probe = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
steps = {task['name']: task for task in probe[2]['tasks']}
request = steps['Send one direct bounded metrics request from the receiver']
assert request['when'] == 'not ansible_check_mode'
assert request['ansible.builtin.uri']['url'] == 'http://{{ _probe_address }}:{{ _probe_port }}/metrics'
assert request['ansible.builtin.uri']['use_proxy'] is False
assert request['ansible.builtin.uri']['follow_redirects'] == 'none'
assert request['ansible.builtin.uri']['return_content'] is False
assert request['ansible.builtin.uri']['timeout'] == 5
assert request['timeout'] == 15
template = next(item for item in yaml.safe_load(open(sys.argv[2], encoding='utf-8'))['templates'] if item['name'] == 'Probe o11y Metrics Endpoint (Dev)')
assert template['repository'] == 'agent-cloud dev'
assert [item['name'] for item in template['survey_vars']] == ['expected_repository_sha', 'probe_target']
assert template['survey_vars'][1]['type'] == 'enum'
PY
  for target in dgx-vllm agentgateway; do
    run env ANSIBLE_LOCAL_TEMP="$BATS_TEST_TMPDIR/ansible" ansible-playbook --check \
      -i "$BATS_TEST_TMPDIR/inventory.yml" \
      "$probe" \
      -e expected_repository_sha="$sha" -e probe_target="$target"
    [ "$status" -eq 0 ]
    assert_contains "$output" "Refuse an unusable metrics address or port"
  done
  run env ANSIBLE_LOCAL_TEMP="$BATS_TEST_TMPDIR/ansible" ansible-playbook --check \
    -i "$BATS_TEST_TMPDIR/inventory.yml" \
    "$probe" \
    -e expected_repository_sha="$sha" -e probe_target=arbitrary
  [ "$status" -ne 0 ]
  assert_contains "$output" "Select dgx-vllm or agentgateway"
  python3 - "$BATS_TEST_TMPDIR/inventory.yml" "$BATS_TEST_TMPDIR/missing-gateway.yml" "$BATS_TEST_TMPDIR/mismatched-head.yml" <<'PY'
import copy
import sys
import yaml
source = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
missing = copy.deepcopy(source)
del missing['all']['children']['agentgateway_svc']['hosts']['gateway']['agw_stats_bind']
with open(sys.argv[2], 'w', encoding='utf-8') as stream:
    yaml.safe_dump(missing, stream)
mismatched = copy.deepcopy(source)
mismatched['all']['children']['o11y_svc']['hosts']['receiver']['dgx_spark_head_address'] = '192.0.2.8'
with open(sys.argv[3], 'w', encoding='utf-8') as stream:
    yaml.safe_dump(mismatched, stream)
PY
  run env ANSIBLE_LOCAL_TEMP="$BATS_TEST_TMPDIR/ansible" ansible-playbook --check \
    -i "$BATS_TEST_TMPDIR/missing-gateway.yml" \
    "$probe" \
    -e expected_repository_sha="$sha" -e probe_target=agentgateway
  [ "$status" -ne 0 ]
  assert_contains "$output" "Select dgx-vllm or agentgateway"
  run env ANSIBLE_LOCAL_TEMP="$BATS_TEST_TMPDIR/ansible" ansible-playbook --check \
    -i "$BATS_TEST_TMPDIR/mismatched-head.yml" \
    "$probe" \
    -e expected_repository_sha="$sha" -e probe_target=dgx-vllm
  [ "$status" -ne 0 ]
  assert_contains "$output" "Select dgx-vllm or agentgateway"
  run env ANSIBLE_LOCAL_TEMP="$BATS_TEST_TMPDIR/ansible" ansible-playbook --check \
    -i "$BATS_TEST_TMPDIR/inventory.yml" \
    "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" -e local_mode=false
  [ "$status" -ne 0 ]
  assert_contains "$output" "receiver's gateway scrape target differs"
  refute_contains "$output" "TASK [Place the monorepo"
}

@test "o11y: agentgateway scrape renders from a declared remote endpoint" {
  python3 - "$DEPLOY_DIR/templates/scrape-agentgateway.yml.j2" "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" <<'PY'
import json
import sys

import yaml
from jinja2 import Environment, StrictUndefined

env = Environment(undefined=StrictUndefined)
env.filters['to_json'] = json.dumps
env.filters['bool'] = bool
plays = yaml.safe_load(open(sys.argv[2], encoding='utf-8'))
phase_one = next(play for play in plays if play.get('name') == 'Phase 1: Place repo + manage o11y secrets')
policy = phase_one['vars']['_o11y_forbidden_metric_label_names_regex']
config = yaml.safe_load(env.from_string(open(sys.argv[1], encoding='utf-8').read()).render(
    agentgateway_metrics_address='gateway.example.test',
    agentgateway_metrics_port=19002,
    o11y_cluster='test-cluster',
    o11y_environment='prod',
    local_mode=False,
    _o11y_forbidden_metric_label_names_regex=policy,
))
job = config['scrape_configs'][0]
assert job['job_name'] == 'agentgateway'
assert job['metrics_path'] == '/metrics'
assert job['static_configs'] == [{
    'targets': ['gateway.example.test:19002'],
    'labels': {'service': 'agentgateway', 'component': 'gateway', 'cluster': 'test-cluster', 'environment': 'prod'},
}]
assert job['metric_relabel_configs'][0]['regex'] == policy
PY
}

@test "o11y: alert rules and contact point render for both rollout states" {
  python3 - "$DEPLOY_DIR/templates/alerts.yml.j2" "$DEPLOY_DIR/templates/alert-contact.yml.j2" <<'PY'
import json
import sys

import yaml
from jinja2 import Environment, StrictUndefined

env = Environment(undefined=StrictUndefined, trim_blocks=True)
env.filters['bool'] = bool
env.filters['to_json'] = json.dumps
template = env.from_string(
    open(sys.argv[1], encoding='utf-8').read()
)
contact_template = env.from_string(open(sys.argv[2], encoding='utf-8').read())
targets = [{'uid': 'o11y_missing_caddy', 'service': 'caddy-reverse-proxy/caddy', 'instance': 'caddy:2021'}]
for enabled in (False, True):
    for declared in ([], targets):
        rules = yaml.safe_load(template.render(o11y_expected_metrics_targets=declared, local_mode=False, o11y_alerts_enabled=enabled))['groups'][0]['rules']
        assert [rule['uid'] for rule in rules] == ['o11y_service_down', 'o11y_receiver_root_disk_low'] + [target['uid'] for target in declared]
        assert all(rule['isPaused'] is not enabled for rule in rules)
        assert all(('notification_settings' in rule) is enabled for rule in rules)
        assert rules[0]['annotations']['dashboard_url'] == '/d/service-overview?var-service={{ $labels.service }}'
        assert rules[0]['labels']['owner'] == 'platform-operations'
        assert rules[0]['labels']['environment'] == 'prod'
        disk_rule = rules[1]
        assert disk_rule['for'] == '15m'
        assert disk_rule['labels']['severity'] == 'warning'
        assert disk_rule['labels']['service'] == 'o11y/receiver-host'
        assert disk_rule['labels']['owner'] == 'platform-operations'
        assert disk_rule['data'][0]['model']['expr'] == (
            '100 * node_filesystem_avail_bytes{job="receiver-host",service="o11y/receiver-host",mountpoint="/"} '
            '/ node_filesystem_size_bytes{job="receiver-host",service="o11y/receiver-host",mountpoint="/"}'
        )
        assert disk_rule['data'][1]['model']['conditions'][0]['evaluator'] == {'type': 'lt', 'params': [10]}
        assert disk_rule['noDataState'] == 'Alerting'
        if declared:
            assert rules[2]['annotations']['dashboard_url'] == '/d/service-overview?var-service=caddy-reverse-proxy/caddy'
            assert rules[2]['labels']['service'] == 'caddy-reverse-proxy/caddy'
            assert rules[2]['labels']['owner'] == 'platform-operations'
        if enabled:
            for rule in rules:
                settings = rule['notification_settings']
                assert settings['group_by'] == ['service', 'environment', 'cluster', 'alertname']
                assert settings['group_wait'] == '30s'
                assert settings['group_interval'] == '5m'
                assert settings['repeat_interval'] == '4h'
        assert 'up{service!=""}' == rules[0]['data'][0]['model']['expr']
        assert rules[0]['data'][1]['model']['conditions'][0]['evaluator'] == {'type': 'lt', 'params': [0.5]}
        if declared:
            assert 'absent_over_time(up{service="caddy-reverse-proxy/caddy",instance="caddy:2021"}[5m])' == rules[2]['data'][0]['model']['expr']
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
assert local_rules[1]['uid'] == 'o11y_receiver_root_disk_low'
assert local_rules[1]['isPaused'] is True
assert local_rules[2]['uid'] == 'o11y_missing_caddy'
canary = 'o11y-fault-probe-' + 'a' * 12
canary_rules = yaml.safe_load(template.render(local_mode=True, o11y_alerts_enabled=True,
                                              o11y_alert_canary_service=canary))['groups'][0]['rules']
assert canary_rules[0]['data'][0]['model']['expr'] == f'up{{service="{canary}"}}'
assert canary_rules[0]['isPaused'] is False
assert all(rule['isPaused'] is True and 'notification_settings' not in rule for rule in canary_rules[1:])
PY
}

@test "o11y: backup readiness survey is reviewed, credential-safe, read-only, and sanitized" {
  python3 - "$REPO_ROOT/platform/playbooks/survey-o11y-backup-readiness.yml" "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import sys

import yaml

playbook = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
survey = next(play for play in playbook if play.get('name') == 'Survey Proxmox backup readiness')
tasks = survey['tasks']
api_reads = [task for task in tasks if 'ansible.builtin.uri' in task]
assert len(api_reads) == 3
assert all(task['ansible.builtin.uri']['method'] == 'GET' for task in api_reads)
assert all(task.get('no_log') is True for task in api_reads)
assert all(task.get('check_mode') is False for task in api_reads)
uri_defaults = survey['module_defaults']['ansible.builtin.uri']
assert uri_defaults['follow_redirects'] == 'none'
assert 'headers' not in uri_defaults
credential_tasks = [
    task for task in tasks
    if task['name'] in ('Read Proxmox API credentials from OpenBao', 'Derive Proxmox API connection values')
]
assert len(credential_tasks) == 2
assert all(task.get('no_log') is True for task in credential_tasks)
derive_index = tasks.index(credential_tasks[1])
assert all(tasks.index(task) > derive_index for task in api_reads)
expected_headers = {'Authorization': 'PVEAPIToken={{ _pve_token_id }}={{ _pve_secret }}'}
assert all(task['ansible.builtin.uri']['headers'] == expected_headers for task in api_reads)
summarize = next(task for task in tasks if task['name'] == 'Summarize backup listing without exposing storage or artifact details')
assert summarize.get('no_log') is True
summary_template = summarize['ansible.builtin.set_fact']['_backup_summary']
from jinja2 import Environment, StrictUndefined
jinja = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
def summarize_results(results, storages=None):
    rendered = jinja.from_string(summary_template).render(
        _backup_content_reads={'results': results},
        _backup_storages=[] if storages is None else storages,
    )
    return yaml.safe_load(rendered)
assert summarize_results([{'status': 403}])['listing_complete'] is False
assert summarize_results([{'status': 200, 'json': {'data': {'unexpected': 'mapping'}}}])['listing_complete'] is False
sanitized = summarize_results(
    [{'status': 200, 'json': {'data': [{'volid': 'hidden-candidate'}]}}],
    [
        {'storage': 'hidden-pbs', 'type': 'pbs', 'path': '/private/pbs'},
        {'storage': 'hidden-dir', 'type': 'dir', 'path': '/private/dir'},
        {'storage': 'hidden-nfs', 'type': 'nfs', 'server': 'private.example'},
    ],
)
assert sanitized == {
    'listing_complete': True,
    'backend_types_complete': True,
    'pbs_storage_count': 1,
    'non_pbs_storage_count': 2,
    'candidate_artifact_count': 1,
}
assert not any(value in str(sanitized) for value in ('hidden-', '/private', 'private.example'))
assert summarize_results([], [{'storage': 'hidden-untyped'}])['backend_types_complete'] is False
assert summarize_results([], [{'storage': 'hidden-empty-type', 'type': ''}])['backend_types_complete'] is False
assert summarize_results([], [{'storage': 'hidden-blank-type', 'type': '   '}])['backend_types_complete'] is False
assert any(task.get('ansible.builtin.include_tasks') == 'tasks/assert-bao-transport.yml' for task in tasks)
assert any(task.get('ansible.builtin.assert', {}).get('that') == "_pve_host is match('^https://')" for task in tasks)
summary = next(task for task in tasks if task['name'] == 'Report sanitized backup and restore prerequisites')
fields = summary['ansible.builtin.debug']['msg']
assert set(fields) == {
    'survey', 'target_vm_verified', 'backup_capable_storage_count',
    'backup_capable_pbs_storage_count', 'backup_capable_non_pbs_storage_count',
    'candidate_backup_artifact_count', 'artifact_immutability_verified',
    'isolated_restore_target_verified', 'restore_test_verified', 'next_gate',
}
assert fields['artifact_immutability_verified'] is False
assert fields['isolated_restore_target_verified'] is False
assert fields['restore_test_verified'] is False
readiness_guard = next(task for task in tasks if task.get('name') == 'Require complete read-only backup listing')
assert readiness_guard['ansible.builtin.assert']['that'] == [
    '_backup_summary.listing_complete | bool',
    '_backup_summary.backend_types_complete | bool',
]
assert tasks.index(readiness_guard) < tasks.index(summary)
templates = yaml.safe_load(open(sys.argv[2], encoding='utf-8'))['templates']
item, = (item for item in templates if item['name'] == 'Survey o11y Backup Readiness (Dev)')
assert item['playbook'] == 'platform/playbooks/survey-o11y-backup-readiness.yml'
assert item['repository'] == 'agent-cloud dev'
sha, = (field for field in item['survey_vars'] if field['name'] == 'expected_repository_sha')
assert sha['required'] is True
assert len(item['survey_vars']) == 1
PY
}

@test "o11y: snapshot workflow attaches Proxmox credentials after connection freeze" {
  python3 - "$REPO_ROOT/platform/playbooks/snapshot-vm.yml" <<'PY'
import sys

import yaml

playbook, = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
defaults = playbook['module_defaults']['ansible.builtin.uri']
assert defaults['validate_certs'] is False
assert defaults['follow_redirects'] == 'none'
assert 'headers' not in defaults
tasks = playbook['tasks']
freeze_index = next(
    index for index, task in enumerate(tasks)
    if task['name'] == 'Resolve + freeze the Proxmox connection'
)
requests = [task for task in tasks if 'ansible.builtin.uri' in task]
assert len(requests) == 4
expected = {'Authorization': 'PVEAPIToken={{ _pve_tid }}={{ _pve_sec }}'}
assert all(task['ansible.builtin.uri']['headers'] == expected for task in requests)
assert all(tasks.index(task) > freeze_index for task in requests)
assert all(task.get('no_log') is True for task in requests)
PY
}

@test "o11y: real alert-enabled deploy verifies live rule and contact state" {
  python3 - "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" "$DEPLOY_DIR/templates/alerts.yml.j2" <<'PY'
import base64
import json
import re
import sys

import yaml
from jinja2 import Environment, StrictUndefined

plays = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
alert_template = open(sys.argv[2], encoding='utf-8').read()
verify = next(play for play in plays if play.get('name') == 'Phase 3: Verify o11y')
block = next(task for task in verify['tasks'] if task['name'] == 'Verify enabled Grafana alert provisioning after a real deploy')
assert block['when'] == [
    'o11y_alerts_enabled | default(false) | bool',
    'not ansible_check_mode',
]
env = Environment(undefined=StrictUndefined)
env.filters['bool'] = bool
env.filters['to_json'] = json.dumps
should_verify = [env.compile_expression(condition) for condition in block['when']]
assert all(check(local_mode=False, o11y_alerts_enabled=True, ansible_check_mode=False) for check in should_verify)
assert all(check(local_mode=True, o11y_alerts_enabled=True, ansible_check_mode=False) for check in should_verify)
tasks = block['block']
rule_check = next(task for task in tasks if task['name'] == 'Require expected o11y rules and active routed rules')
env.tests['match'] = lambda value, pattern: re.match(pattern, value) is not None
env.filters['from_json'] = json.loads
env.filters['b64decode'] = lambda value: base64.b64decode(value).decode()
env.filters['from_yaml'] = yaml.safe_load
env.filters['flatten'] = lambda value: [item for sub in value for item in sub]
compile_value = lambda value: env.compile_expression(value.removeprefix('{{').removesuffix('}}').strip())
select_rules = compile_value(rule_check['vars']['_o11y_rules'])
rules_for_active_routing = compile_value(rule_check['vars']['_o11y_rules_for_active_routing'])
provisioned_doc = compile_value(rule_check['vars']['_o11y_provisioned'])
expected_uids = compile_value(rule_check['vars']['_o11y_expected_uids'])
withdrawn_uids = compile_value(rule_check['vars']['_o11y_withdrawn_uids'])
checks = [env.compile_expression(expr) for expr in rule_check['ansible.builtin.assert']['that']]
slurp = next(task for task in tasks if task['name'] == 'Read the provisioned alert rules file')
assert tasks.index(slurp) < tasks.index(rule_check)
assert slurp['register'] == '_provisioned_rules_file' and 'no_log' not in slurp
assert slurp['ansible.builtin.slurp']['src'].endswith('/config/grafana/provisioning/alerting/observability.yml')
LEGACY_FILE = {'groups': [{'rules': [{'uid': 'o11y_service_down'}, {'uid': 'o11y_receiver_root_disk_low'}]}]}
settings = {
    'group_by': ['service', 'environment', 'cluster', 'alertname'],
    'group_wait': '30s', 'group_interval': '5m', 'repeat_interval': '4h',
}
healthy = {'uid': 'o11y_service_down', 'isPaused': False, 'notification_settings': settings}
healthy_disk = {'uid': 'o11y_receiver_root_disk_low', 'isPaused': False, 'notification_settings': settings}
paused_disk = {'uid': 'o11y_receiver_root_disk_low', 'isPaused': True}
wrong_group = {'uid': 'o11y_service_down', 'isPaused': False,
               'notification_settings': settings | {'group_by': ['instance']}}
def verify_rules(rules, local_mode, expected, provisioned=LEGACY_FILE):
    active = {'stdout': json.dumps(rules)}
    scoped = select_rules(_active_rules=active)
    active_routing = rules_for_active_routing(_o11y_rules=scoped, local_mode=local_mode)
    doc = provisioned_doc(_provisioned_rules_file={'content': base64.b64encode(yaml.safe_dump(provisioned).encode()).decode()})
    names = dict(_o11y_rules=scoped, _o11y_rules_for_active_routing=active_routing, _active_rules=active,
                 _o11y_expected_uids=expected_uids(_o11y_provisioned=doc),
                 _o11y_withdrawn_uids=withdrawn_uids(_o11y_provisioned=doc))
    assert all(bool(check(**names)) for check in checks) is expected

for rules, expected in [
    ([healthy, healthy_disk], True),
    ([healthy, healthy_disk, {'uid': 'unrelated', 'isPaused': True}], True),
    ([healthy, healthy_disk, {'uid': 'o11y_missing_caddy', 'isPaused': True}], False),
    ([healthy], False),
    ([{'uid': 'unrelated', 'isPaused': False}], False),
    ([{'uid': 'o11y_service_down'}, healthy_disk], False),
    ([healthy, {'uid': 'o11y_receiver_root_disk_low', 'isPaused': False}], False),
    ([wrong_group], False),
]:
    verify_rules(rules, local_mode=False, expected=expected)

# Exercise actual rendered local and production rule sets. Only the intentionally
# paused local disk rule is excluded from active/routing checks.
# The live set must equal the provisioned file's o11y_ set, and no withdrawn uid may
# survive, whatever its prefix.
render_alerts = env.from_string(alert_template)
local_doc = yaml.safe_load(render_alerts.render(local_mode=True, o11y_alerts_enabled=True))
prod_doc = yaml.safe_load(render_alerts.render(local_mode=False, o11y_alerts_enabled=True))
all_rules = lambda doc: [rule for group in doc['groups'] for rule in group['rules']]
local_rules, prod_rules = all_rules(local_doc), all_rules(prod_doc)
assert len(prod_rules) > 2, 'the rendered file carries more than the two legacy rules'
assert next(rule for rule in local_rules if rule['uid'] == 'o11y_receiver_root_disk_low')['isPaused'] is True
assert next(rule for rule in prod_rules if rule['uid'] == 'o11y_receiver_root_disk_low')['isPaused'] is False
verify_rules(local_rules, local_mode=True, expected=True, provisioned=local_doc)
verify_rules(prod_rules, local_mode=False, expected=True, provisioned=prod_doc)
verify_rules(prod_rules[:-1], local_mode=False, expected=False, provisioned=prod_doc)  # a rendered rule is missing
verify_rules(prod_rules + [dict(prod_rules[0], uid='o11y_stray')], local_mode=False, expected=False,
             provisioned=prod_doc)  # a rule the file does not carry
withdrawn = prod_doc['deleteRules'][0]['uid']
verify_rules(prod_rules + [{'uid': withdrawn, 'isPaused': True}], local_mode=False, expected=False,
             provisioned=prod_doc)  # a withdrawn rule survived
local_service_down = next(rule for rule in local_rules if rule['uid'] == 'o11y_service_down')
local_service_down['isPaused'] = True
verify_rules(local_rules, local_mode=True, expected=False, provisioned=local_doc)
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

@test "o11y: self-monitoring scrapes feed the provisioned dashboard" {
  python3 - "$DEPLOY_DIR" "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" <<'PY'
import json
import pathlib
import re
import sys
import yaml
from jinja2 import Environment, StrictUndefined

deploy = pathlib.Path(sys.argv[1])
env = Environment(undefined=StrictUndefined)
env.filters['to_json'] = json.dumps
env.filters['bool'] = bool
plays = yaml.safe_load(pathlib.Path(sys.argv[2]).read_text())
phase_one = next(play for play in plays if play.get('name') == 'Phase 1: Place repo + manage o11y secrets')
policy = phase_one['vars']['_o11y_forbidden_metric_label_names_regex']
prometheus_template = (deploy / 'templates/prometheus.yml.j2').read_text()
rendered_prometheus = env.from_string(prometheus_template).render(
    o11y_cluster='test-cluster', o11y_environment='prod', local_mode=False,
    _o11y_forbidden_metric_label_names_regex=policy,
)
prometheus_config = yaml.safe_load(rendered_prometheus)
assert prometheus_config['global']['external_labels']['cluster'] == 'test-cluster'
scrapes = prometheus_config['scrape_configs']
jobs = {job['job_name']: job['static_configs'][0]['targets'] for job in scrapes}
assert {key: jobs[key] for key in ('prometheus', 'grafana', 'loki', 'alloy', 'tempo', 'pyroscope', 'receiver-host')} == {
    'prometheus': ['localhost:9090'],
    'grafana': ['grafana:3000'],
    'loki': ['loki:3100'],
    'alloy': ['alloy:12345'],
    'tempo': ['tempo:3200'],
    'pyroscope': ['pyroscope:4040'],
    'receiver-host': ['node-exporter:9100'],
}
for job in scrapes:
    labels = job['static_configs'][0]['labels']
    assert labels['cluster'] == 'test-cluster'
    assert labels['environment'] == 'prod'
    assert job['metric_relabel_configs'][0]['regex'] == policy
compose = yaml.safe_load((deploy / 'compose.yml').read_text())
local_overlay = yaml.safe_load((deploy / 'compose.local.yml').read_text())
assert compose['services']['grafana']['environment']['GF_METRICS_ENABLED'] == 'true'
pyroscope = compose['services']['pyroscope']
assert pyroscope['image'].endswith('grafana/pyroscope:2.2.0}')
assert './config/pyroscope-config.yml:/etc/pyroscope/config.yml:ro' in pyroscope['volumes']
assert 'pyroscope-data:/data' in pyroscope['volumes']
assert not pyroscope.get('ports')
assert {'prometheus-data', 'loki-data', 'grafana-data', 'tempo-data', 'pyroscope-data'} <= set(compose['volumes'])
pyroscope_config = yaml.safe_load((deploy / 'config/pyroscope-config.yml').read_text())
assert pyroscope_config['architecture_storage'] == 'v2'
assert pyroscope_config['storage']['backend'] == 'filesystem'
assert pyroscope_config['storage']['filesystem']['dir'] == '/data/storage'
assert pyroscope_config['limits']['retention_period'] == '168h'
assert pyroscope_config['metastore']['index']['cleanup_interval'] == '15m'
assert pyroscope_config['self_profiling']['disable_push'] is True
datasources = yaml.safe_load((deploy / 'config/grafana/provisioning/datasources/datasources.yml').read_text())['datasources']
assert any(source['uid'] == 'pyroscope' and source['url'] == 'http://pyroscope:4040' for source in datasources)
dashboard = json.loads((deploy / 'config/grafana/dashboards/o11y-self-monitoring.json').read_text())
assert dashboard['uid'] == 'o11y-self-monitoring'
assert len(dashboard['panels']) >= 16
component = next(panel for panel in dashboard['panels'] if panel['title'] == 'O11y components up (expected 7)')
assert 'tempo' in component['targets'][0]['expr']
assert 'receiver-host' in component['targets'][0]['expr']
assert 'max": 7' in json.dumps(component['fieldConfig'])
assert all(panel['datasource']['uid'] == 'prometheus' for panel in dashboard['panels'])
panels = {panel['id']: panel for panel in dashboard['panels']}
assert all(panels[panel_id]['options']['colorMode'] == 'none' for panel_id in (2, 3))
expressions = '\n'.join(target['expr'] for panel in dashboard['panels'] for target in panel['targets'])
for metric in ('up{', 'prometheus_tsdb_head_series', 'scrape_samples_scraped',
               'loki_distributor_bytes_received_total', 'loki_distributor_lines_received_total',
               'alloy_component_controller_running_components',
               'tempo_distributor_spans_received_total', 'tempo_distributor_bytes_received_total',
               'process_start_time_seconds', 'pyroscope_write_sent_profiles_total'):
    assert metric in expressions, metric
for metric in ('node_cpu_seconds_total', 'node_memory_MemAvailable_bytes',
               'node_memory_MemTotal_bytes', 'node_filesystem_avail_bytes', 'node_load1'):
    assert metric in expressions, metric
assert expressions.count('or vector(0)') == 4
plays = yaml.safe_load(pathlib.Path(sys.argv[2]).read_text())
verify = next(play for play in plays if play.get('name') == 'Phase 3: Verify o11y')
names = {task['name'] for task in verify['tasks']}
assert 'Verify Grafana can query its provisioned data sources' in names
assert 'Require the committed self-monitoring dashboard to be active' in names
for task_name in ('Read the provisioned Pyroscope datasource', 'Verify Grafana can proxy the private Pyroscope datasource'):
    task = next(task for task in verify['tasks'] if task['name'] == task_name)
    assert task['no_log'] is True
readback = next(task for task in verify['tasks'] if task['name'] == 'Require the committed self-monitoring dashboard to be active')
assert any('O11y components up (expected 7)' in condition for condition in readback['ansible.builtin.assert']['that'])
assert any('Profile samples written / sec' in condition for condition in readback['ansible.builtin.assert']['that'])
assert 'Pyroscope ready (/ready) through the private compose network' in names
assert 'Read the receiver-host exporter runtime boundary' in names
assert 'Require the receiver-host exporter to remain private and read-only' in names
assert 'Verify receiver-host metrics and host-versus-guest root filesystem' in names
runtime_check = next(task for task in verify['tasks'] if task['name'] == 'Require the receiver-host exporter to remain private and read-only')
runtime_conditions = runtime_check['ansible.builtin.assert']['that']
assert "(_receiver_host_runtime.stdout | from_json).pid_mode in [none, '', 'private']" in runtime_conditions
assert any('networks.keys()' in condition and 'o11y' in condition for condition in runtime_conditions)
assert any("'/'" in condition and "'/host'" in condition and 'RW' in condition for condition in runtime_conditions)
metrics_check = next(task for task in verify['tasks'] if task['name'] == 'Verify receiver-host metrics and host-versus-guest root filesystem')
assert metrics_check['delegate_to'] == 'localhost'
assert metrics_check['failed_when'] == '_receiver_host_metrics_check.rc != 0'
assert 'to_json' in metrics_check['ansible.builtin.command']['stdin']
local_runtime_guard = ['not ansible_check_mode', 'not (local_mode | default(false) | bool)']
for task_name in (
    'Read the receiver-host exporter runtime boundary',
    'Require the receiver-host exporter to remain private and read-only',
    'Read receiver-host metrics from the private Prometheus API',
    'Read guest-visible root filesystem size and free bytes',
    'Verify receiver-host metrics and host-versus-guest root filesystem',
):
    task = next(task for task in verify['tasks'] if task['name'] == task_name)
    assert task['when'] == local_runtime_guard
node_exporter = compose['services']['node-exporter']
assert 'pid' not in node_exporter
assert not node_exporter.get('ports')
assert node_exporter['networks'] == ['o11y']
assert '/:/host:ro,rslave' in node_exporter['volumes']
assert 'label=disable' in local_overlay['services']['node-exporter']['security_opt']
assert {arg for arg in node_exporter['command'] if arg.startswith('--collector.')} == {
    '--collector.disable-defaults', '--collector.cpu', '--collector.meminfo',
    '--collector.filesystem', '--collector.filesystem.mount-points-include=^/$',
    '--collector.loadavg',
}
assert not any('/host' in volume for volume in compose['services']['alloy'].get('volumes', []))
ready = next(task for task in verify['tasks'] if task['name'] == 'Pyroscope ready (/ready) through the private compose network')
assert ready['when'] == 'not ansible_check_mode'
config = next(task for task in verify['tasks'] if task['name'] == 'Read the effective Pyroscope storage and retention configuration')
assert '/api/v1/status/config' in ' '.join(config['ansible.builtin.command']['argv'])
assert 'Require private persistent Pyroscope v2 storage and bounded retention' in names
assert any('tempo_distributor_spans_received_total' in condition for condition in readback['ansible.builtin.assert']['that'])
assert any('tempo_distributor_bytes_received_total' in condition for condition in readback['ansible.builtin.assert']['that'])
tempo_config = next(task for task in verify['tasks'] if task['name'] == 'Read the live Tempo datasource correlation settings')
assert '/api/datasources/uid/tempo' in tempo_config['ansible.builtin.command']['argv'][-1]
correlation = next(task for task in verify['tasks'] if task['name'] == 'Require live Tempo trace-to-metric and trace-to-log mappings')
assert any('tracesToMetrics.datasourceUid' in condition for condition in correlation['ansible.builtin.assert']['that'])
assert any('tracesToLogsV2.datasourceUid' in condition for condition in correlation['ansible.builtin.assert']['that'])
loki = yaml.safe_load((deploy / 'config/grafana/provisioning/datasources/datasources.yml').read_text())
loki = next(source for source in loki['datasources'] if source['uid'] == 'loki')
trace_link = next(field for field in loki['jsonData']['derivedFields'] if field['name'] == 'TraceID')
assert trace_link['datasourceUid'] == 'tempo'
assert re.search(trace_link['matcherRegex'], '{"traceid":"' + 'a' * 32 + '"}').group(1) == 'a' * 32
assert not re.search(trace_link['matcherRegex'], '{"traceid":"short"}')
assert trace_link['url'] == '$${__value.raw}'
assert any(task['name'] == 'Require the live Loki log-to-trace link' for task in verify['tasks'])
health = next(task for task in verify['tasks'] if task['name'] == 'Verify Grafana can query its provisioned data sources')
assert 'curl -sS --config -' in health['ansible.builtin.command']['argv'][5]
PY
}

@test "o11y: agentgateway operations and client dashboards use declared sources and metrics" {
  python3 - "$DEPLOY_DIR" "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" <<'PY'
import json
import pathlib
import sys
import yaml

deploy = pathlib.Path(sys.argv[1])
dashboard = json.loads((deploy / 'config/grafana/dashboards/agentgateway-traffic.json').read_text())
assert dashboard['uid'] == 'agentgateway-traffic'
assert dashboard['title'] == 'Agentgateway operations'
assert len(dashboard['panels']) == 14
assert {panel['datasource']['uid'] for panel in dashboard['panels']} == {'prometheus', 'loki', 'tempo'}
operation_identity = next(variable for variable in dashboard['templating']['list'] if variable['name'] == 'identity')
assert 'label_values(agentgateway_requests_total' in operation_identity['query']['query']
queries = '\n'.join(target.get('expr', '') for panel in dashboard['panels'] for target in panel['targets'])
for metric in ('up{job="agentgateway"}', 'agentgateway_config_synchronized',
               'agentgateway_requests_total', 'agentgateway_request_duration_seconds_bucket',
               'agentgateway_gen_ai_client_token_usage_sum',
               'agentgateway_gen_ai_server_time_to_first_token_bucket',
               'identity, gen_ai_request_model, gen_ai_token_type',
               'status="429"', 'agentgateway_build_info'):
    assert metric in queries, metric
assert 'or vector(0)' not in queries
rejections = next(panel for panel in dashboard['panels'] if panel['title'] == 'Rejected access records (reason pending sample)')
assert rejections['type'] == 'logs'
assert rejections['datasource']['uid'] == 'loki'
assert '4xx access records' in rejections['description']
assert 'Grouping by rejection reason awaits a captured access record' in rejections['description']
assert rejections['targets'][0]['expr'].startswith('{service="agentgateway", signal="access-log"} |~ `')
assert 'http[.]status' in rejections['targets'][0]['expr']
assert '4[0-9]{2}' in rejections['targets'][0]['expr']
assert 'agentgateway_requests_total' not in rejections['targets'][0]['expr']
access = next(panel for panel in dashboard['panels'] if panel['title'] == 'Recent access records')
assert access['datasource']['uid'] == 'loki'
assert access['targets'][0]['expr'] == '{service="agentgateway", signal="access-log"}'
traces = next(panel for panel in dashboard['panels'] if panel['title'] == 'Recent agentgateway traces')
assert traces['datasource']['uid'] == 'tempo'
assert traces['type'] == 'table'
assert 'agentgateway' in traces['targets'][0]['query']
assert traces['targets'][0]['query'] == '{ resource.service.name = "agentgateway" }'
rate_limited = next(panel for panel in dashboard['panels'] if panel['title'] == 'Rate-limited requests (429)')
assert 'agentgateway_requests_total' in rate_limited['targets'][0]['expr']
assert 'status="429"' in rate_limited['targets'][0]['expr']
assert 'sum(increase(' in rate_limited['targets'][0]['expr']
assert 'identity=~"$identity"' in rate_limited['targets'][0]['expr']

client = json.loads((deploy / 'config/grafana/dashboards/agentgateway-client-view.json').read_text())
assert client['uid'] == 'agentgateway-client-view'
assert client['title'] == 'Agentgateway client view'
assert len(client['panels']) == 6
assert all(panel['datasource']['uid'] == 'prometheus' for panel in client['panels'])
identity = next(variable for variable in client['templating']['list'] if variable['name'] == 'identity')
assert 'label_values(agentgateway_requests_total' in identity['query']['query']
client_queries = '\n'.join(target['expr'] for panel in client['panels'] for target in panel['targets'])
assert 'histogram_quantile(0.50' in client_queries
assert 'histogram_quantile(0.95' in client_queries
assert 'agentgateway_request_duration_seconds_bucket' in client_queries
assert 'status=~"4.."' in client_queries and 'status=~"5.."' in client_queries
assert 'sum by (identity)' in client_queries and 'identity=~"$identity"' in client_queries
plays = yaml.safe_load(pathlib.Path(sys.argv[2]).read_text())
verify = next(play for play in plays if play.get('name') == 'Phase 3: Verify o11y')
names = {task['name'] for task in verify['tasks']}
assert 'Read the provisioned agentgateway traffic dashboard' in names
assert 'Require the committed agentgateway traffic dashboard to be active' in names
assert 'Read the provisioned agentgateway client-view dashboard' in names
assert 'Require the committed agentgateway client-view dashboard to be active' in names
PY
}

@test "o11y: private Alloy OTLP input has a bounded persistent Tempo consumer" {
  python3 - "$DEPLOY_DIR" "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" <<'PY'
import json
import pathlib
import sys
import yaml
from jinja2 import Environment, StrictUndefined

deploy = pathlib.Path(sys.argv[1])
compose = yaml.safe_load((deploy / 'compose.yml').read_text())
assert compose['services']['tempo']['image'].endswith('grafana/tempo:2.10.8}')
assert 'tempo-data:/var/tempo' in compose['services']['tempo']['volumes']
assert not compose['services']['tempo'].get('ports')
assert list(compose['services']['tempo']['environment']) == ['O11Y_TEMPO_RETENTION', 'O11Y_TEMPO_METRIC_PROCESSORS']
alloy_ports = compose['services']['alloy']['ports']
assert len(alloy_ports) == 2
assert '${O11Y_OTLP_BIND:-127.0.0.1}' in alloy_ports[0] and alloy_ports[0].endswith('}:4317')
assert '${O11Y_OTLP_BIND:-127.0.0.1}' in alloy_ports[1] and alloy_ports[1].endswith(':4318:4318')
tempo = yaml.safe_load((deploy / 'config/tempo-config.yml').read_text())
assert tempo['storage']['trace']['backend'] == 'local'
assert tempo['compactor']['compaction']['block_retention'] == '${O11Y_TEMPO_RETENTION:-168h}'
alloy_template = (deploy / 'templates/config.alloy.j2').read_text()
env = Environment(undefined=StrictUndefined)
env.filters['to_json'] = json.dumps
env.filters['bool'] = bool
alloy = env.from_string(alloy_template).render(
    o11y_cluster='test-cluster',
    _o11y_forbidden_metric_label_names_regex=r'(?i)(request|user|client|session|trace|email|api[_-]?key)',
)
assert 'pyroscope.scrape' not in alloy
profiled_alloy = env.from_string(alloy_template).render(
    o11y_cluster='test-cluster',
    _o11y_forbidden_metric_label_names_regex=r'(?i)(request|user|client|session|trace|email|api[_-]?key)',
    o11y_alloy_profile_pilot_enabled=True,
)
assert 'pyroscope.scrape "alloy_self_profile"' in profiled_alloy
assert '"__address__" = "alloy:12345", "service_name" = "o11y/alloy"' in profiled_alloy
assert 'scrape_interval = "60s"' in profiled_alloy
assert 'pyroscope.write.private.receiver' in profiled_alloy
assert 'otelcol.receiver.otlp "traces"' in alloy
assert 'grpc {\n    endpoint = "0.0.0.0:4317"' in alloy
assert 'otelcol.processor.attributes.gateway_logs.input' in alloy
assert 'otelcol.exporter.loki "gateway"' in alloy
assert 'loki.attribute.labels' in alloy
assert 'value  = "service,signal"' in alloy
assert 'cluster = "test-cluster"' in alloy
assert 'otelcol.processor.batch.traces.input' in alloy
assert 'otelcol.exporter.otlp.tempo.input' in alloy
assert 'endpoint = "tempo:4317"' in alloy
assert 'otelcol.receiver.otlp "conformance"' in alloy
assert 'http {\n    endpoint = "0.0.0.0:4318"' in alloy
assert 'otelcol.processor.attributes.conformance_logs.input' in alloy
assert 'otelcol.exporter.loki "conformance"' in alloy
assert 'action = "upsert"' in alloy
assert 'value  = "job,service,step,status"' in alloy
datasources = yaml.safe_load((deploy / 'config/grafana/provisioning/datasources/datasources.yml').read_text())['datasources']
assert {d['uid'] for d in datasources} == {'prometheus', 'loki', 'tempo', 'pyroscope'}
assert any(d['uid'] == 'tempo' and d['url'] == 'http://tempo:3200' for d in datasources)
assert any(d['uid'] == 'pyroscope' and d['url'] == 'http://pyroscope:4040' for d in datasources)
plays = yaml.safe_load(pathlib.Path(sys.argv[2]).read_text())
phase_one = next(p for p in plays if p.get('name') == 'Phase 1: Place repo + manage o11y secrets')
cluster_guard = next(t for t in phase_one['tasks'] if t['name'] == 'Require the observability cluster label')
assert any('o11y_cluster is defined' in item for item in cluster_guard['ansible.builtin.assert']['that'])
assert any("local_mode | default(false) | bool" in item for item in cluster_guard['ansible.builtin.assert']['that'])
http_guard = next(t for t in phase_one['tasks'] if t['name'] == 'Require a private bind for the fixed conformance OTLP/HTTP listener')
assert any('o11y_otlp_bind' in item for item in http_guard['ansible.builtin.assert']['that'])
assert any('172\\\\.' in item for item in http_guard['ansible.builtin.assert']['that'])
for name, src, dest in (
    ('Render Alloy config with the inventory cluster label', 'templates/config.alloy.j2', 'config/config.alloy'),
    ('Render Prometheus config with the inventory cluster label', 'templates/prometheus.yml.j2', 'config/prometheus.yml'),
):
    task = next(t for t in phase_one['tasks'] if t['name'] == name)
    assert task['ansible.builtin.template']['src'].endswith(src)
    assert task['ansible.builtin.template']['dest'].endswith(dest)
verify = next(p for p in plays if p.get('name') == 'Phase 3: Verify o11y')
assert any(t['name'] == 'Tempo ready (/ready) through the private compose network' for t in verify['tasks'])
health = next(t for t in verify['tasks'] if t['name'] == 'Verify Grafana can query its provisioned data sources')
assert health['loop'] == ['prometheus', 'loki']
proxy = next(t for t in verify['tasks'] if t['name'] == 'Verify Grafana can proxy the Tempo datasource')
assert '/api/datasources/proxy/uid/tempo/ready' in proxy['ansible.builtin.command']['argv'][-1]
assert 'curl -fsS' in proxy['ansible.builtin.command']['argv'][-1]
PY
}

@test "o11y: service receipt can require a real Tempo trace without assuming remote logs" {
  python3 - "$REPO_ROOT/platform/playbooks/verify-o11y-service.yml" "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import sys
import yaml
plays = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
inputs = plays[0]['tasks'][-1]['ansible.builtin.assert']['that']
assert any('expect_logs' in item for item in inputs)
assert any('expect_traces' in item for item in inputs)
assert any('emit_agentgateway_canary' in item for item in inputs)
canary = next(p for p in plays if p.get('name') == 'Emit an optional gateway trace canary')
mark, traffic = canary['tasks']
assert mark['delegate_to'] == "{{ groups['o11y_svc'][0] }}"
assert traffic['ansible.builtin.uri']['status_code'] == 401
assert traffic['ansible.builtin.uri']['use_netrc'] is False
assert traffic['ansible.builtin.uri']['use_proxy'] is False
assert '_canary_bind' in traffic['ansible.builtin.uri']['url']
assert "['0.0.0.0', '::', '']" in canary['vars']['_canary_bind']
assert traffic['loop'] == '{{ range(100) | list }}'
assert 'headers' not in traffic['ansible.builtin.uri']
tasks = next(p for p in plays if p.get('name') == 'Verify the named service is collected')['tasks']
logs = next(t for t in tasks if t['name'] == 'Query recent Loki logs for the service')
traces = next(t for t in tasks if t['name'] == 'Search recent Tempo traces for the service')
require = next(t for t in tasks if t['name'] == 'Require a recent trace returned by Tempo')
assert logs['when'] == "expect_logs | default('true') | bool"
assert traces['when'] == "expect_traces | default('false') | bool"
assert 'service.name=' in traces['ansible.builtin.command']['argv'][-1]
assert 'tempo:3200/api/search' in traces['ansible.builtin.command']['argv'][-1]
assert '_canary_started.stdout' in traces['ansible.builtin.command']['argv'][-1]
assert ' - 60' in traces['ansible.builtin.command']['argv'][-1]
assert ' + 60' in traces['ansible.builtin.command']['argv'][-1]
assert traces['retries'] == 12
assert traces['ignore_errors'] is True
assert require['when'] == "expect_traces | default('false') | bool"
templates = yaml.safe_load(open(sys.argv[2], encoding='utf-8'))['templates']
service = next(t for t in templates if t['name'] == 'Verify o11y Service')
assert {v['name'] for v in service['survey_vars']} >= {'expect_logs', 'expect_traces', 'emit_agentgateway_canary'}
PY
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

@test "o11y: active alert drill preserves rules and has separate recovery" {
  python3 - "$REPO_ROOT/platform/playbooks/drill-o11y-active-alert-delivery.yml" \
    "$REPO_ROOT/platform/playbooks/tasks/o11y-alert-probe.yml" \
    "$REPO_ROOT/platform/playbooks/tasks/o11y-recover-active-alert-drill.yml" \
    "$REPO_ROOT/platform/playbooks/recover-o11y-active-alert-drill.yml" \
    "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" \
    "$REPO_ROOT/platform/semaphore/templates.yml" <<'PY'
import sys
import yaml

drill, probe, recover, recovery, deploy, catalog = [yaml.safe_load(open(p, encoding='utf-8')) for p in sys.argv[1:]]
assert drill[0]['ansible.builtin.import_playbook'] == 'preflight-target-group.yml'
tasks = drill[2]['tasks']
names = [task['name'] for task in tasks]
assert names.index('Require exactly one active service-down rule') < names.index('Verify Discord receipt credentials and history access')
assert names.index('Verify Discord receipt credentials and history access') < names.index('Atomically claim the active-drill interruption marker')
flight = next(task for task in tasks if 'block' in task)
target = flight['block'][0]
assert target['ansible.builtin.copy']['dest'].endswith('/config/scrape.d/o11y-prod-alert-drill.yml')
assert 'o11y-production-active-alert-drill' in target['ansible.builtin.copy']['content']
assert '127.0.0.1:65535' in target['ansible.builtin.copy']['content']
assert 'service: "{{ _probe }}"' in target['ansible.builtin.copy']['content']
assert flight['block'][-1]['ansible.builtin.include_tasks'] == 'tasks/o11y-alert-probe.yml'
assert flight['block'][-1]['vars']['drill_expect_alert'] is True
assert flight['always'][0]['ansible.builtin.include_tasks'] == 'tasks/o11y-recover-active-alert-drill.yml'
recover_names = [task['name'] for task in recover]
for before in ('Require the drill scrape job to be absent', 'Require every o11y alert rule to remain active', 'Require the production Discord contact point to remain present once'):
    assert recover_names.index(before) < recover_names.index('Clear the active-drill marker after recovery checks pass')
assert recovery[-1]['tasks'][-2]['ansible.builtin.include_tasks'] == 'tasks/o11y-recover-active-alert-drill.yml'
normal = next(p for p in deploy if p.get('name') == 'Phase 1: Place repo + manage o11y secrets')['tasks']
normal_names = [task['name'] for task in normal]
assert normal_names.index('Require the active alert drill to be recovered before deploy') < normal_names.index('Place the monorepo + ensure podman/compose')
templates = {item['name']: item for item in catalog['templates']}
for name in ('Drill o11y Active Alert Delivery (Dev)', 'Recover o11y Active Alert Drill (Dev)'):
    assert templates[name]['repository'] == 'agent-cloud dev'
    assert [var['name'] for var in templates[name]['survey_vars']] == ['expected_repository_sha', 'expected_receiver_sha']
assert "_deployed_revision.stdout == expected_receiver_sha" in str(drill)
assert "SEMAPHORE_TASK_ID" not in str(drill)
assert "_deployed_revision.stdout == expected_receiver_sha" in str(recovery)
PY
}

@test "o11y: shared trace gate refuses missing receipts and accepts complete inventory" {
  python3 - "$REPO_ROOT/platform/playbooks/tasks/assert-o11y-trace-rollout.yml" \
    "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" \
    "$REPO_ROOT/platform/playbooks/deploy-agentgateway.yml" <<'PY'
import sys
import yaml

gate, receiver, gateway = [yaml.safe_load(open(path, encoding='utf-8')) for path in sys.argv[1:]]
assert gate[0]['ansible.builtin.assert']['that'] == "groups.get('o11y_svc', []) | length == 1"
checks = gate[1]['ansible.builtin.assert']['that']
required = ('o11y_trace_rollout_enabled', 'o11y_alerts_enabled',
    'o11y_metrics_receipt_id', 'o11y_alert_delivery_receipt_id',
    'o11y_retention_receipt_id', 'o11y_cardinality_receipt_id',
    'o11y_prom_retention', 'o11y_prom_retention_size', 'o11y_loki_retention',
    'o11y_tempo_retention', 'o11y_scrape_sample_limit', 'agentgateway_metrics_address')
for name in required:
    assert any(name in check for check in checks), name
assert 'Trace rollout is blocked' in gate[1]['ansible.builtin.assert']['fail_msg']
o11y_tasks = next(play for play in receiver if 'manage o11y secrets' in play.get('name', ''))['tasks']
gateway_tasks = next(play for play in gateway if 'manage agentgateway secrets' in play.get('name', ''))['tasks']
receiver_gate = next(task for task in o11y_tasks if task.get('ansible.builtin.include_tasks') == 'tasks/assert-o11y-trace-rollout.yml')
assert 'agw_otlp_host' in receiver_gate['when']
assert 'o11y_trace_rollout_enabled' in receiver_gate['when']
assert any(task.get('ansible.builtin.include_tasks') == 'tasks/assert-o11y-trace-rollout.yml' for task in gateway_tasks)
PY
}

@test "o11y: production budget receipt is read-only and emits bounded fields" {
  python3 - "$REPO_ROOT/platform/playbooks/verify-o11y-production-budgets.yml" \
    "$REPO_ROOT/platform/semaphore/templates.yml" \
    "$REPO_ROOT/platform/playbooks/files/compare-o11y-budgets.py" \
    "$REPO_ROOT/platform/playbooks/files/diagnose-o11y-budget-readback.py" <<'PY'
import json
import subprocess
import sys
import yaml

playbook, catalog = [yaml.safe_load(open(p, encoding='utf-8')) for p in sys.argv[1:3]]
comparator, diagnostic = sys.argv[3:5]
assert playbook[0]['ansible.builtin.import_playbook'] == 'preflight-target-group.yml'
assert "SEMAPHORE_TASK_ID" not in str(playbook)
assert playbook[1]['tasks'][-2]['ansible.builtin.command']['argv'] == ['git', 'status', '--porcelain', '--untracked-files=all']
assert playbook[1]['tasks'][-1]['ansible.builtin.assert']['that'] == '_controller_changes.stdout | length == 0'
tasks = playbook[2]['tasks']
declaration_task = next(task for task in tasks
                       if task.get('name') == 'Require explicit production budget declarations')
assert declaration_task['ansible.builtin.assert']['that']
names = {task['name'] for task in tasks}
assert {'Read Prometheus runtime retention flags', 'Read Loki runtime configuration',
        'Read Tempo runtime configuration', 'Read the live Alloy sample limit',
        'Read current Prometheus head series count',
        'Read current guest root filesystem capacity',
        'Read current guest memory headroom',
        'Compare equivalent live and declared retention units'} <= names
diagnostic_index = next(i for i, task in enumerate(tasks)
                        if task.get('name') == 'Calculate sanitized sample-limit and series diagnostics')
report_index = next(i for i, task in enumerate(tasks)
                    if task.get('name') == 'Report sanitized sample-limit and series diagnostics')
assert diagnostic_index < report_index < next(
    i for i, task in enumerate(tasks) if task.get('name') == 'Require matching live budgets and measurable cardinality')
diagnostic_task = tasks[diagnostic_index]
assert diagnostic_task['ansible.builtin.command']['argv'] == [
    'python3', '{{ playbook_dir }}/files/diagnose-o11y-budget-readback.py']
assert diagnostic_task['delegate_to'] == 'localhost'
assert diagnostic_task['changed_when'] is False and diagnostic_task['check_mode'] is False
report_task = tasks[report_index]
assert 'no_log' not in report_task and report_task['ansible.builtin.debug']['msg'] == \
    '{{ _budget_readback_diagnostics.stdout | from_json }}'
gate = next(task for task in tasks if task.get('name') == 'Require matching live budgets and measurable cardinality')
assert gate['ansible.builtin.assert']['that'] == [
    '_retention_comparison.rc == 0',
    '(_budget_readback_diagnostics.stdout | from_json).sample_limit_matches',
    '(_budget_readback_diagnostics.stdout | from_json).head_series_count_matches',
    '(_budget_readback_diagnostics.stdout | from_json).positive_head_series',
]
assert set(json.loads(subprocess.run(
    [sys.executable, diagnostic], input=json.dumps({'sample_limit': '2000',
        'expected_sample_limit': 2000, 'head_series': json.dumps({'data': {'result': [
            {'metric': {'instance': 'https://private.example/?token=secret'}, 'value': [1, '109']}]}})}),
    text=True, capture_output=True, check=True).stdout)) == {
        'sample_limit_matches', 'sample_limit_expected', 'sample_limit_observed',
        'head_series_count_matches', 'head_series_count_observed',
        'positive_head_series', 'head_series_observed'}
def diagnose(sample, expected, response):
    return json.loads(subprocess.run([sys.executable, diagnostic], input=json.dumps({
        'sample_limit': sample, 'expected_sample_limit': expected,
        'head_series': response}), text=True, capture_output=True, check=True).stdout)
one = json.dumps({'data': {'result': [{'value': [1, '109']}]}})
good = diagnose('2000\n', 2000, one)
assert good['sample_limit_matches'] and good['head_series_count_matches']
assert good['positive_head_series'] and good['head_series_observed'] == 109
mismatch = diagnose('1500', 2000, one)
assert not mismatch['sample_limit_matches'] and mismatch['sample_limit_observed'] == 1500
assert not diagnose('not-a-number', 2000, one)['sample_limit_matches']
assert not diagnose('+2000', 2000, one)['sample_limit_matches']
assert not diagnose('2000', 2000, '{malformed')['head_series_count_matches']
assert not diagnose('2000', 2000, json.dumps({'data': {'result': []}}))['positive_head_series']
assert not diagnose('2000', 2000, json.dumps({'data': 'unexpected'}))['head_series_count_matches']
assert not diagnose('2000', 2000, json.dumps({'data': {'result': 'unexpected'}}))['head_series_count_matches']
multiple = json.dumps({'data': {'result': [{'value': [1, '1']}, {'value': [1, '2']}]}})
assert diagnose('2000', 2000, multiple)['head_series_count_observed'] == 2
assert not diagnose('2000', 2000, multiple)['positive_head_series']
zero = json.dumps({'data': {'result': [{'value': [1, '0']}]}})
assert not diagnose('2000', 2000, zero)['positive_head_series']
private_response = json.dumps({'data': {'result': [{'metric': {
    'instance': 'https://private.example/?token=secret'}, 'value': [1, '9']}]}})
sanitized = subprocess.run([sys.executable, diagnostic], input=json.dumps({
    'sample_limit': '2000', 'expected_sample_limit': 2000,
    'head_series': private_response}), text=True, capture_output=True, check=True).stdout
assert 'private.example' not in sanitized and 'token=secret' not in sanitized and 'https://' not in sanitized
assert all('no_log' not in task for task in tasks if task['name'].startswith('Read ') and 'configuration' in task['name'])
summary = tasks[-1]['ansible.builtin.debug']['msg']
assert set(summary) == {'status', 'receipt_instruction', 'prometheus_retention',
    'prometheus_retention_size', 'loki_retention', 'tempo_retention',
    'scrape_sample_limit', 'active_prometheus_series',
    'guest_root_filesystem_total_bytes', 'guest_root_filesystem_available_bytes',
    'guest_memory_headroom_percent', 'o11y_volume_capacity'}
assert 'http://' not in str(summary)
assert not any(any(key in task for key in ('ansible.builtin.file', 'ansible.builtin.copy',
    'ansible.builtin.template', 'ansible.builtin.uri')) for task in tasks)
template = next(t for t in catalog['templates'] if t['name'] == 'Verify o11y Production Budgets (Dev)')
assert template['repository'] == 'agent-cloud dev'
assert [var['name'] for var in template['survey_vars']] == ['expected_repository_sha', 'expected_receiver_sha']
payload = {'declared': {'prom_time': '15d', 'prom_size': '1GB', 'loki_time': '7d', 'tempo_time': '168h'},
           'live': {'prom_time': '360h0m0s', 'prom_size': '1073741824B',
                    'loki_time': '168h0m0s', 'tempo_time': '7d'}}
result = subprocess.run([sys.executable, comparator], input=json.dumps(payload), text=True, capture_output=True)
assert result.returncode == 0, result.stderr
payload['declared']['prom_size'] = '0B'
payload['live']['prom_size'] = '0'
result = subprocess.run([sys.executable, comparator], input=json.dumps(payload), text=True, capture_output=True)
assert result.returncode == 0, result.stderr
payload['live']['tempo_time'] = '6d'
result = subprocess.run([sys.executable, comparator], input=json.dumps(payload), text=True, capture_output=True)
assert result.returncode != 0 and 'tempo_time' in result.stderr

PY
}

@test "o11y: changed production retention refuses missing capacity receipt before deploy writes" {
  python3 - "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" <<'PY'
import pathlib
import re
import sys
import yaml
from jinja2 import Environment

plays = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())
phase = next(play for play in plays if play.get('name') == 'Phase 1: Place repo + manage o11y secrets')
tasks = phase['tasks']
gate = next(task for task in tasks if task.get('name') == 'Require a capacity receipt and nonzero Prometheus cap for retention expansion')
gate_index = tasks.index(gate)
write_index = next(index for index, task in enumerate(tasks)
                   if task.get('ansible.builtin.include_tasks') == 'tasks/place-monorepo.yml'
                   or 'ansible.builtin.template' in task)
assert gate_index < write_index
assert '_o11y_retention_expansion' in gate['when']
assert 'local_mode' in gate['when']
message = gate['ansible.builtin.assert']['fail_msg'].lower()
assert 'o11y_capacity_receipt_id' in message
assert 'seven-day backend growth' in message
assert 'backup' in message
assert 'separately reviewed successful capacity receipt' in message
env = Environment()
env.tests['match'] = lambda value, pattern: re.match(pattern, str(value)) is not None
env.filters['bool'] = lambda value: value is True or str(value).lower() in ('true', 'yes', '1')
def evaluate(expression, **context):
    return env.compile_expression(expression)(**context)

changed = {'local_mode': False, 'o11y_prom_retention': '90d',
           'o11y_loki_retention': '45d', 'o11y_tempo_retention': '1080h'}
baseline = {'local_mode': False, 'o11y_prom_retention': '15d',
            'o11y_loki_retention': '7d', 'o11y_tempo_retention': '168h'}
expansion_expression = phase['vars']['_o11y_retention_expansion'].removeprefix('{{').removesuffix('}}').strip()
assert '_o11y_retention_expansion' in gate['when']
assert evaluate(expansion_expression, **changed)
assert evaluate(expansion_expression, **{**baseline, 'o11y_prom_retention': '91d'})
assert evaluate(expansion_expression, **{**baseline, 'o11y_tempo_retention': '1090h'})
assert not evaluate(expansion_expression, **baseline)
assert evaluate(gate['when'], _o11y_retention_expansion=True, local_mode=False)
assert not evaluate(gate['when'], _o11y_retention_expansion=False, local_mode=False)
assert not evaluate(gate['when'], _o11y_retention_expansion=True, local_mode=True)
requirements = gate['ansible.builtin.assert']['that']
assert len(requirements) == 2
assert not evaluate(requirements[0], o11y_prom_retention_size='0B')
assert not evaluate(requirements[1], o11y_capacity_receipt_id='')
assert evaluate(requirements[0], o11y_prom_retention_size='100GB')
assert evaluate(requirements[1], o11y_capacity_receipt_id='1776')
PY
}

@test "o11y: service identity and forbidden dimensions are bounded across signals" {
  python3 - "$DEPLOY_DIR" "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" <<'PY'
import json
import pathlib
import re
import sys
import yaml
from jinja2 import Environment, StrictUndefined

deploy = pathlib.Path(sys.argv[1])
plays = yaml.safe_load(pathlib.Path(sys.argv[2]).read_text())
phase_one = next(play for play in plays if play.get('name') == 'Phase 1: Place repo + manage o11y secrets')
policy = phase_one['vars']['_o11y_forbidden_metric_label_names_regex']
forbidden = re.compile(policy)
for label in ('request_id', 'user_id', 'client_id', 'session_id', 'trace_id', 'email', 'api_key',
              'prompt', 'raw_path', 'remote_addr', 'timestamp', 'token_value',
              'http_request_id', 'x_user_id', 'user_email', 'request_path', 'http_url',
              'client_ip_address', 'gen_ai_prompt', 'url_path'):
    assert forbidden.fullmatch(label), label
for label in ('service', 'cluster', 'environment', 'component', 'identity', 'status', 'model_name',
              'gen_ai_request_model', 'gen_ai_token_type', 'gpu', 'device', 'node'):
    assert not forbidden.fullmatch(label), label
repo = deploy.parents[3]
caddy = yaml.safe_load((repo / 'platform/services/caddy/deployment/compose.yml').read_text())
local_inventory = yaml.safe_load((repo / 'platform/inventory/local-dev.yml.example').read_text())
caddy_target = local_inventory['all']['children']['o11y_svc']['hosts']['o11y-local']['o11y_expected_metrics_targets'][0]
assert caddy_target['service'] == f"{caddy['name']}/caddy"
for verifier in ('verify-o11y-service.yml', 'verify-o11y-metrics-target.yml'):
    assert "(/[a-z][a-z0-9_-]*)?" in (repo / 'platform/playbooks' / verifier).read_text()
overview = json.loads((deploy / 'config/grafana/dashboards/service-overview.json').read_text())
service = next(variable for variable in overview['templating']['list'] if variable['name'] == 'service')
assert service['datasource']['uid'] == 'prometheus'
assert service['query'] == 'label_values(up, service)'
queries = [target['expr'] for panel in overview['panels'] for target in panel['targets']]
assert all('{service=~"$service"}' in query or 'service=~"$service"' in query for query in queries)
assert all('container=' not in query for query in queries)
log_rate = next(panel for panel in overview['panels'] if panel['title'] == 'Service log lines per second')
assert log_rate['targets'][0]['expr'] == 'sum by (service) (rate({service=~"$service"}[5m]))'

alloy_template = (deploy / 'templates/config.alloy.j2').read_text()
env = Environment(undefined=StrictUndefined)
env.filters['to_json'] = json.dumps
env.filters['bool'] = bool
alloy = env.from_string(alloy_template).render(
    o11y_cluster='test-cluster', _o11y_forbidden_metric_label_names_regex=policy,
)
assert 'target_label  = "service"' in alloy
assert '__meta_docker_container_label_com_docker_compose_service' in alloy
assert '__meta_docker_container_label_com_docker_compose_project' in alloy
assert 'separator     = "/"' in alloy
assert 'value  = "service,signal"' in alloy
assert 'prometheus.relabel "bounded_labels"' in alloy

profile_gate = next(task for task in phase_one['tasks']
                    if task.get('name') == 'Require prior config, privacy, and resource receipts before profile collection')
receipt_checks = profile_gate['ansible.builtin.assert']['that']
assert len(receipt_checks) == 3
env.tests['match'] = lambda value, pattern: re.fullmatch(pattern, str(value)) is not None
for check in receipt_checks:
    assert not env.compile_expression(check)(o11y_profile_pilot_config_receipt_id='0',
        o11y_profile_pilot_privacy_receipt_id='0', o11y_profile_pilot_resource_receipt_id='0')
    assert env.compile_expression(check)(o11y_profile_pilot_config_receipt_id='17',
        o11y_profile_pilot_privacy_receipt_id='19', o11y_profile_pilot_resource_receipt_id='23')
assert f'regex  = {json.dumps(policy)}' in alloy
assert 'loki.attribute.labels' in alloy

for name in ('scrape-agentgateway.yml.j2', 'scrape-dgx-spark.yml.j2'):
    template = (deploy / 'templates' / name).read_text()
    assert 'metric_relabel_configs:' in template
    assert 'action: labeldrop' in template
    assert '_o11y_forbidden_metric_label_names_regex | to_json' in template

alerts = (deploy / 'templates/alerts.yml.j2').read_text()
assert f"'service': '{caddy_target['service']}'" in alerts
assert 'absent_over_time(up{service="{{ target.service }}",instance="{{ target.instance }}"}[5m])' in alerts
assert 'service: \'{{ target.service }}\'' in alerts
PY
}

@test "o11y: receiver-host readback compares exact root size with bounded free-space tolerance" {
  python3 - "$REPO_ROOT/platform/playbooks/files/verify-o11y-receiver-host-metrics.py" <<'PY'
import json
import subprocess
import sys

script = sys.argv[1]
labels = {'job': 'receiver-host', 'service': 'o11y/receiver-host', 'cluster': 'test', 'environment': 'prod'}
metrics = []
for name, value, extra in (
    ('node_cpu_seconds_total', '12', {'mode': 'idle'}),
    ('node_memory_MemAvailable_bytes', '700', {}),
    ('node_memory_MemTotal_bytes', '1000', {}),
    ('node_load1', '0', {}),
    ('node_filesystem_size_bytes', '10000000000', {'mountpoint': '/'}),
    ('node_filesystem_avail_bytes', '5000000000', {'mountpoint': '/'}),
):
    metrics.append({'metric': {'__name__': name, **labels, **extra}, 'value': [1, value]})

def run(series, guest_df='Size Avail\n10000000000 5000000000'):
    payload = {
        'prometheus': json.dumps({'status': 'success', 'data': {'resultType': 'vector', 'result': series}}),
        'guest_df': guest_df,
    }
    return subprocess.run([sys.executable, script], input=json.dumps(payload), text=True, capture_output=True)

valid = run(metrics)
assert valid.returncode == 0, valid.stdout
# A 90 MiB scrape/write race is tolerated on a 10 GB filesystem.
within_tolerance = [dict(item) for item in metrics]
within_tolerance[-1] = {**metrics[-1], 'value': [1, '4900000000']}
assert run(within_tolerance).returncode == 0
mismatch = [dict(item) for item in metrics]
mismatch[-1] = {**metrics[-1], 'value': [1, '1000000000']}
result = run(mismatch)
assert result.returncode != 0 and json.loads(result.stdout)['reason'] == 'host_guest_root_filesystem_mismatch'
malformed = run(metrics, 'not df data')
assert malformed.returncode != 0 and json.loads(malformed.stdout)['reason'] == 'invalid_readback'
payload = {'prometheus': '{"status":"success","data":null}', 'guest_df': 'Size Avail\n1 1'}
result = subprocess.run([sys.executable, script], input=json.dumps(payload), text=True, capture_output=True)
assert result.returncode != 0 and json.loads(result.stdout)['reason'] == 'prometheus_query_failed'
PY
}

@test "o11y: named-volume capacity receipt fails closed and expansion gate preserves recovery" {
  python3 - "$REPO_ROOT/platform/playbooks/files/inspect-o11y-volume-capacity.py" "$REPO_ROOT/platform/playbooks/verify-o11y-production-budgets.yml" "$REPO_ROOT/platform/playbooks/deploy-o11y.yml" "$REPO_ROOT/platform/playbooks/clean-deploy-o11y.yml" <<'PY'
import importlib.util
import contextlib
import io
import json
import pathlib
import subprocess
import sys
from types import SimpleNamespace
import yaml
from jinja2 import Environment

script, verifier_path, deploy_path, clean_deploy_path = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('volume_capacity', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.sys.argv = [str(script), 'podman']
missing_mount = set()
no_containers = False
partial_containers = False
orphaned_volume = None
orphaned_project_volume = False
podman_volume_path = '/tmp'
du_calls = []
def fake_run(_engine, *args):
    if args[:2] == ('ps', '-a'):
        rows = [] if no_containers else [{'Names': [service]} for service, _dest in module.VOLUMES.values()]
        if partial_containers:
            rows = rows[:-1]
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows))
    if args[:2] == ('inspect', '--format'):
        container = args[-1]
        logical, destination = next((name, dest) for name, (service, dest) in module.VOLUMES.items() if service == container)
        mounts = [] if logical in missing_mount else [{'Type': 'volume', 'Name': f'project_{logical}', 'Source': '/tmp', 'Destination': destination}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(mounts))
    if args[:2] == ('volume', 'inspect'):
        return SimpleNamespace(returncode=0, stdout=json.dumps([{'Mountpoint': '/tmp'}]))
    if args[:3] == ('volume', 'ls', '--format'):
        volumes = []
        if orphaned_volume:
            volumes.append({'Name': orphaned_volume, 'Labels': None})
        if orphaned_project_volume:
            volumes.append({'Name': 'custom-volume', 'Labels': {
                'com.docker.compose.project': 'o11y',
                'com.docker.compose.volume': 'pyroscope-data',
            }})
        return SimpleNamespace(returncode=0, stdout=json.dumps(volumes))
    if args == ('info', '--format', 'json'):
        return SimpleNamespace(returncode=0, stdout=json.dumps({'store': {
            'volumePath': podman_volume_path, 'graphRoot': '/different/graph/root'}}))
    if args[:2] == ('unshare', 'du'):
        du_calls.append((_engine, args))
        return SimpleNamespace(returncode=0, stdout='24 /tmp\n')
    raise AssertionError(args)
module.run = fake_run
module.fs_capacity = lambda _path: (1000, 500, 1)
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 0
receipt = json.loads(captured.getvalue())
assert receipt['status'] == 'observed' and len(receipt['volumes']) == 5
assert receipt['guest_root_filesystem_available_bytes'] == 500
assert [volume['stored_bytes'] for volume in receipt['volumes']] == [24] * 5
assert len(du_calls) == 5 and all(
    call[0] == 'podman' and call[1][1:] == ('du', '-s', '-B1', '--', '/tmp')
    for call in du_calls
)
assert '/tmp' not in captured.getvalue()
missing_mount.add('pyroscope-data')
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 1
failure = json.loads(captured.getvalue())
assert failure['reason'] == 'container_volume_mount_missing' and failure['volume'] == 'pyroscope-data'
partial_containers = True
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 1
failure = json.loads(captured.getvalue())
assert failure['reason'] == 'backend_container_set_partial' and failure['missing_container'] == 'o11y-pyroscope'
partial_containers = False
no_containers = True
module.sys.argv = [str(script), 'podman', '30']
original_allocated_bytes = module.allocated_bytes
module.allocated_bytes = lambda *_args: (_ for _ in ()).throw(AssertionError('threshold mode must skip byte walks'))
orphaned_volume = 'other_pyroscope-data'
module.fs_capacity = lambda path: (1000, 200, 1) if path == '/' else (1000, 500, 2)
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 0
first_deploy = json.loads(captured.getvalue())
assert first_deploy['status'] == 'first_deploy'
assert first_deploy['guest_root_filesystem_available_bytes'] == 200
assert first_deploy['volume_store_filesystem_available_bytes'] == 500
assert first_deploy['volume_store_filesystem_free_percent'] == 50
orphaned_volume = 'o11y_pyroscope-data'
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 1
assert json.loads(captured.getvalue())['reason'] == 'orphaned_o11y_volume'
orphaned_volume = None
orphaned_project_volume = True
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 1
assert json.loads(captured.getvalue())['reason'] == 'orphaned_o11y_volume'
orphaned_project_volume = False
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 0
assert json.loads(captured.getvalue())['status'] == 'first_deploy'
module.fs_capacity = lambda path: (1000, 800, 1) if path == '/' else (1000, 200, 2)
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 1
assert json.loads(captured.getvalue())['reason'] == 'first_deploy_volume_filesystem_free_below_threshold'
podman_volume_path = 'relative/path'
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 1
assert json.loads(captured.getvalue())['reason'] == 'podman_volume_path_unresolved'
podman_volume_path = '/tmp'
no_containers = False
missing_mount.clear()
module.fs_capacity = lambda _path: (1000, 500, 1)
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 0
threshold_receipt = json.loads(captured.getvalue())
assert threshold_receipt['status'] == 'observed'
assert all('stored_bytes' not in volume for volume in threshold_receipt['volumes'])
no_containers = True
module.allocated_bytes = original_allocated_bytes
module.fs_capacity = lambda _path: (1000, 200, 1)
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 1
assert json.loads(captured.getvalue())['reason'] == 'first_deploy_volume_filesystem_free_below_threshold'
module.sys.argv = [str(script), 'docker']
captured = io.StringIO()
with contextlib.redirect_stdout(captured):
    assert module.main() == 1
assert json.loads(captured.getvalue())['reason'] == 'unsupported_container_engine'

verifier = yaml.safe_load(verifier_path.read_text())
verify_play = next(play for play in verifier if play.get('hosts') == 'o11y_svc')
verify_tasks = verify_play['tasks']
volume_task = next(task for task in verify_tasks if task.get('name', '').startswith('Read exact named o11y volume'))
assert volume_task['ansible.builtin.script']['cmd'].endswith('inspect-o11y-volume-capacity.py {{ _engine }}')
engine_guard = next(task for task in verify_tasks if task.get('name', '').startswith('Require the supported container engine'))
assert engine_guard['ansible.builtin.assert']['that'] == "_engine == 'podman'"
deploy = yaml.safe_load(deploy_path.read_text())
phase_one = next(play for play in deploy if play.get('name') == 'Phase 1: Place repo + manage o11y secrets')
names_in_phase = [task['name'] for task in phase_one['tasks']]
receipt_gate = phase_one['tasks'][names_in_phase.index('Require a capacity receipt and nonzero Prometheus cap for retention expansion')]
expansion_expression = Environment().compile_expression(
    phase_one['vars']['_o11y_retention_expansion'].removeprefix('{{').removesuffix('}}').strip()
)
assert expansion_expression(o11y_prom_retention='15d', o11y_loki_retention='7d', o11y_tempo_retention='168h') is False
assert expansion_expression(o11y_prom_retention='90d', o11y_loki_retention='45d', o11y_tempo_retention='1080h') is True
preflight = names_in_phase.index('Read o11y volume backing capacity before retention expansion')
assert preflight < names_in_phase.index('Configure o11y alert provisioning from OpenBao')
phase_two = next(play for play in deploy if play.get('name') == 'Phase 2: Start o11y')
assert phase_two['tasks'][0]['name'] == 'Run deploy.sh (container lifecycle)'
assert '30' in phase_one['tasks'][preflight]['ansible.builtin.script']['cmd']
assert phase_one['tasks'][preflight]['failed_when'] is False
gate_when = phase_one['tasks'][preflight]['when']
assert gate_when == receipt_gate['when']
assert '_o11y_retention_expansion' in gate_when and 'local_mode' in gate_when
assert phase_one['tasks'][preflight + 1]['ansible.builtin.assert']['that'] == '_o11y_volume_preflight.rc == 0'
assert "['observed', 'first_deploy']" in phase_one['tasks'][preflight + 2]['ansible.builtin.assert']['that']
assert phase_one['tasks'][preflight + 1]['when'] == gate_when
assert phase_one['tasks'][preflight + 2]['when'] == gate_when
clean = yaml.safe_load(clean_deploy_path.read_text())
clean_tasks = clean[0]['tasks']
assert clean_tasks[0]['name'] == 'Refuse expanded retention before destructive o11y cleanup'
assert clean_tasks[0]['when'] == 'not (local_mode | default(false) | bool)'
clean_conditions = [Environment().compile_expression(condition) for condition in clean_tasks[0]['ansible.builtin.assert']['that']]
baseline = {'o11y_prom_retention': '15d', 'o11y_loki_retention': '7d', 'o11y_tempo_retention': '168h'}
expanded = {'o11y_prom_retention': '90d', 'o11y_loki_retention': '45d', 'o11y_tempo_retention': '1080h'}
assert all(condition(**baseline) for condition in clean_conditions)
assert not all(condition(**expanded) for condition in clean_conditions)
assert clean_tasks[1]['name'] == 'Destroy existing deployment'
PY
}

@test "o11y: inference dashboards use only recorded vLLM and node-exporter metric names" {
  python3 - "$DEPLOY_DIR/config/grafana/dashboards" "$REPO_ROOT/platform/tests/fixtures" <<'PY'
import json
import pathlib
import re
import sys

dashboards = pathlib.Path(sys.argv[1])
fixtures = pathlib.Path(sys.argv[2])


def names(path):
    return {line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith('#')}


vllm_names = names(fixtures / 'vllm-metric-names-506e66caa3ef.txt')
node_names = names(fixtures / 'node-exporter-metric-names-v1.12.1.txt')
assert len(vllm_names) == 110 and all(name.startswith('vllm:') for name in vllm_names)
assert 'node_memory_MemAvailable_bytes' in node_names

expected = {'inference-latency-capacity', 'inference-fleet-health', 'inference-placement-comparison'}
files = {path.stem: path for path in dashboards.glob('inference-*.json')}
assert set(files) == expected, sorted(files)
used_vllm = set()
for uid, path in files.items():
    dashboard = json.loads(path.read_text())
    assert dashboard['uid'] == uid
    variables = {variable['name']: variable for variable in dashboard['templating']['list']}
    assert 'model_alias' in variables, uid
    assert variables['model_alias']['query']['query'].startswith('label_values(vllm:num_requests_running{job="dgx-spark-vllm"')
    assert variables['model_alias']['query']['query'].endswith(', model_name)')
    assert any(link['url'] == 'https://github.com/uhstray-io/dgx-spark/tree/main/results' for link in dashboard['links'])
    ids = [panel['id'] for panel in dashboard['panels']]
    assert len(ids) == len(set(ids)), uid
    exprs = [variables['model_alias']['query']['query']]
    for panel in dashboard['panels']:
        if panel['type'] == 'text':
            continue
        assert panel['datasource']['uid'] in {'prometheus', 'loki'}, (uid, panel['title'])
        assert panel['targets'], (uid, panel['title'])
        for target in panel['targets']:
            if panel['datasource']['uid'] == 'loki':
                assert target['expr'].startswith('{cluster="dgx-spark", service="vllm"}'), target['expr']
                continue
            exprs.append(target['expr'])
            assert 'job="dgx-spark-' in target['expr'], (uid, panel['title'])
            if 'vllm:' in target['expr']:
                assert 'model_name=~"$model_alias"' in target['expr'], (uid, panel['title'])
    for expr in exprs:
        for name in re.findall(r'vllm:[A-Za-z0-9_:]+', expr):
            assert name in vllm_names, (uid, name)
            used_vllm.add(name)
        for name in re.findall(r'\bnode_[A-Za-z0-9_]+', expr):
            assert name in node_names, (uid, name)
for family in ('vllm:time_to_first_token_seconds_bucket', 'vllm:e2e_request_latency_seconds_bucket',
               'vllm:num_requests_running', 'vllm:num_requests_waiting', 'vllm:kv_cache_usage_perc',
               'vllm:num_preemptions_total', 'vllm:request_time_per_output_token_seconds_bucket'):
    assert family in used_vllm, family
placement = json.loads(files['inference-placement-comparison'].read_text())
assert any('offset $compare_offset' in target['expr'] for panel in placement['panels'] for target in panel.get('targets', []))
PY
}

@test "o11y: inference alert groups render only with the DGX scrape and hold for at least 5m" {
  python3 - "$DEPLOY_DIR/templates/alerts.yml.j2" "$REPO_ROOT/platform/tests/fixtures" <<'PY'
import json
import pathlib
import re
import sys

import yaml
from jinja2 import Environment, StrictUndefined

env = Environment(undefined=StrictUndefined, trim_blocks=True)
env.filters['bool'] = bool
env.filters['to_json'] = json.dumps
template = env.from_string(open(sys.argv[1], encoding='utf-8').read())
fixtures = pathlib.Path(sys.argv[2])
known = set()
for name in ('vllm-metric-names-506e66caa3ef.txt', 'node-exporter-metric-names-v1.12.1.txt'):
    known |= {line.strip() for line in (fixtures / name).read_text().splitlines() if line.strip() and not line.startswith('#')}


def seconds(value):
    return int(value[:-1]) * {'s': 1, 'm': 60, 'h': 3600}[value[-1]]


def own(groups):
    # The Loki-backed platform groups render regardless of the scrape; tested below.
    return [group for group in groups if group['name'] not in ('scheduled-jobs', 'internal-ca')]


for scrape in (False, True):
    groups = own(yaml.safe_load(template.render(local_mode=False, o11y_alerts_enabled=True,
                                                o11y_expected_metrics_targets=[],
                                                dgx_spark_scrape_enabled=scrape))['groups'])
    names = [group['name'] for group in groups]
    if not scrape:
        assert names == ['service-telemetry'], names
assert names == ['service-telemetry', 'inference-failing', 'telemetry-missing', 'memory-thermal', 'benchmark-gate'], names

placeholder = 'inference_benchmark_gate_placeholder'
for enabled in (False, True):
    for canary in (None, 'o11y-fault-probe-' + 'b' * 12):
        values = dict(local_mode=False, o11y_alerts_enabled=enabled, o11y_expected_metrics_targets=[],
                      dgx_spark_scrape_enabled=True)
        if canary:
            values['o11y_alert_canary_service'] = canary
        groups = own(yaml.safe_load(template.render(**values))['groups'])
        rules = {group['name']: group['rules'] for group in groups[1:]}
        assert [rule['uid'] for rule in rules['inference-failing']] == ['inference_queue_stalled']
        assert [rule['uid'] for rule in rules['telemetry-missing']] == ['inference_target_down', 'inference_vllm_metrics_absent']
        assert [rule['uid'] for rule in rules['memory-thermal']] == ['inference_node_memavailable_low', 'inference_node_memfree_low']
        assert [rule['uid'] for rule in rules['benchmark-gate']] == [placeholder]
        active = enabled and canary is None
        for rule in (rule for group in rules.values() for rule in group):
            assert rule['uid'].startswith('inference_') and not rule['uid'].startswith('o11y_')
            assert seconds(rule['for']) >= 300, (rule['uid'], rule['for'])
            assert rule['labels']['cluster'] == 'dgx-spark' and rule['labels']['environment'] == 'prod'
            assert rule['labels']['owner'] == 'platform-operations'
            assert rule['annotations']['runbook_url'] == 'https://github.com/uhstray-io/dgx-spark/blob/main/docs/VLLM-BRINGUP.md'
            assert rule['annotations']['dashboard_url'].startswith('/d/inference-')
            expr = rule['data'][0]['model']['expr']
            for name in re.findall(r'(?:vllm:|\bnode_)[A-Za-z0-9_:]+', expr):
                assert name in known, (rule['uid'], name)
            if rule['uid'] == placeholder:
                assert expr == 'vector(0)'
                assert rule['isPaused'] is True and 'notification_settings' not in rule
                continue
            assert rule['isPaused'] is (not active), rule['uid']
            assert ('notification_settings' in rule) is active, rule['uid']
            if active:
                assert rule['notification_settings']['receiver'] == 'agent-cloud-ops'
                assert rule['notification_settings']['group_by'] == ['service', 'environment', 'cluster', 'alertname']

memory = {rule['uid']: rule for rule in rules['memory-thermal']}
assert memory['inference_node_memavailable_low']['data'][0]['model']['expr'] == 'node_memory_MemAvailable_bytes{job="dgx-spark-node",cluster="dgx-spark"}'
assert memory['inference_node_memavailable_low']['data'][1]['model']['conditions'][0]['evaluator'] == {'type': 'lt', 'params': [536870912]}
assert memory['inference_node_memfree_low']['data'][1]['model']['conditions'][0]['evaluator'] == {'type': 'lt', 'params': [1073741824]}
stalled = rules['inference-failing'][0]['data'][0]['model']['expr']
assert 'vllm:num_requests_waiting{job="dgx-spark-vllm",cluster="dgx-spark"}' in stalled
assert 'rate(vllm:generation_tokens_total{job="dgx-spark-vllm",cluster="dgx-spark"}[5m])) == 0' in stalled
tuned = own(yaml.safe_load(template.render(local_mode=False, o11y_alerts_enabled=True, o11y_expected_metrics_targets=[],
                                           dgx_spark_scrape_enabled=True,
                                           o11y_dgx_spark_memfree_floor_bytes=2147483648))['groups'])[3]['rules']
assert tuned[1]['data'][1]['model']['conditions'][0]['evaluator'] == {'type': 'lt', 'params': [2147483648]}
local = own(yaml.safe_load(template.render(local_mode=True))['groups'])
assert [group['name'] for group in local] == ['service-telemetry']
PY
}

@test "o11y: turning the DGX scrape off withdraws every inference alert rule" {
  python3 - "$DEPLOY_DIR/templates/alerts.yml.j2" <<'PY'
import json
import sys

import yaml
from jinja2 import Environment, StrictUndefined

# Grafana 11.4 provisioning keeps a rule whose group leaves the file, so the
# disabled render must delete each inference rule the enabled render creates.
env = Environment(undefined=StrictUndefined, trim_blocks=True)
env.filters['bool'] = bool
env.filters['to_json'] = json.dumps
template = env.from_string(open(sys.argv[1], encoding='utf-8').read())
base = dict(local_mode=False, o11y_alerts_enabled=True, o11y_expected_metrics_targets=[])

enabled = yaml.safe_load(template.render(**base, dgx_spark_scrape_enabled=True, o11y_inference_probe_enabled=True))
created = [rule['uid'] for group in enabled['groups'] for rule in group['rules']
           if rule['uid'].startswith('inference_')]
assert created, 'enabled render carries no inference rules'
assert 'deleteRules' not in enabled, enabled.get('deleteRules')

for values in (dict(base, dgx_spark_scrape_enabled=False), dict(base), dict(local_mode=True)):
    disabled = yaml.safe_load(template.render(**values))
    assert [group['name'] for group in disabled['groups']
            if group['name'] not in ('scheduled-jobs', 'internal-ca')] == ['service-telemetry'], values
    assert all(not rule['uid'].startswith('inference_')
               for group in disabled['groups'] for rule in group['rules']), values
    assert sorted(entry['uid'] for entry in disabled['deleteRules']) == sorted(created), disabled['deleteRules']
    assert all(entry['orgId'] == 1 for entry in disabled['deleteRules'])
PY
}

@test "o11y: gateway span-to-Loki lines render only when inventory enables them" {
  python3 - "$DEPLOY_DIR" <<'PY'
import json
import pathlib
import sys

from jinja2 import Environment, StrictUndefined

deploy = pathlib.Path(sys.argv[1])
env = Environment(undefined=StrictUndefined)
env.filters['to_json'] = json.dumps
env.filters['bool'] = bool
template = env.from_string((deploy / 'templates/config.alloy.j2').read_text())
base = dict(o11y_cluster='test-cluster', _o11y_forbidden_metric_label_names_regex='(?i)(request)')

for values in (base, dict(base, o11y_gateway_span_logs_enabled=False)):
    off = template.render(**values)
    assert 'spanlogs' not in off and 'gateway_span_logs' not in off
    assert 'traces = [otelcol.processor.batch.traces.input]\n' in off

on = template.render(**base, o11y_gateway_span_logs_enabled=True)
assert 'traces = [otelcol.processor.batch.traces.input, otelcol.connector.spanlogs.gateway.input]' in on
block = on.split('otelcol.connector.spanlogs "gateway" {', 1)[1].split('\n}\n', 1)[0]
assert 'spans = true' in block
# Span attributes would copy request detail into an unindexed-but-retained log line.
assert 'span_attributes' not in block and 'process_attributes' not in block
assert 'logs = [otelcol.processor.attributes.gateway_span_logs.input]' in block
spans = on.split('otelcol.processor.attributes "gateway_span_logs" {', 1)[1].split('\n}\n', 1)[0]
assert 'value  = "span"' in spans and 'value  = "service,signal"' in spans
assert 'logs = [otelcol.exporter.loki.gateway.input]' in spans
assert 'endpoint = "tempo:4317"' in on

client = json.loads((deploy / 'config/grafana/dashboards/agentgateway-client-view.json').read_text())
queries = [t['expr'] for p in client['panels'] for t in p['targets']]
assert any('agentgateway_gen_ai_server_request_duration_bucket{job="agentgateway", identity=~"$identity"}' in q for q in queries)
latency = json.loads((deploy / 'config/grafana/dashboards/inference-latency-capacity.json').read_text())
assert any(link['url'] == '/d/agentgateway-client-view' for link in latency['links'])
PY
}

@test "o11y: scheduled-job silent and internal-CA expiry rules render for every rollout state" {
  python3 - "$DEPLOY_DIR/templates/alerts.yml.j2" <<'PY'
import json
import sys

import yaml
from jinja2 import Environment, StrictUndefined

# Change production-internal-ca task 6.4 (design decision 10). The line schema these
# selectors match is the o11y README's "Scheduled jobs and internal CA expiry".
env = Environment(undefined=StrictUndefined, trim_blocks=True)
env.filters['bool'] = bool
env.filters['to_json'] = json.dumps
template = env.from_string(open(sys.argv[1], encoding='utf-8').read())
LOKI_GROUPS = ('scheduled-jobs', 'internal-ca')


def render(**values):
    text = template.render(**values)
    assert text.count('\ndeleteRules:') <= 1, 'two deleteRules keys: YAML keeps only the last'
    return yaml.safe_load(text)


def loki_rules(doc):
    return {rule['uid']: rule for group in doc['groups'] if group['name'] in LOKI_GROUPS
            for rule in group['rules']}


default = render(local_mode=False, o11y_alerts_enabled=True, o11y_expected_metrics_targets=[])
assert [group['name'] for group in default['groups']] == ['service-telemetry', 'scheduled-jobs', 'internal-ca']
assert all(group['interval'] == '5m' for group in default['groups'] if group['name'] in LOKI_GROUPS)
rules = loki_rules(default)
assert list(rules) == ['o11y_scheduled_job_silent', 'o11y_internal_ca_leaf_expiring',
                       'o11y_internal_ca_intermediate_expiring']
assert 'deleteRules' not in render(local_mode=False, o11y_expected_metrics_targets=[], dgx_spark_scrape_enabled=True,
                                  o11y_inference_probe_enabled=True)

silent = rules['o11y_scheduled_job_silent']
assert silent['title'] == 'Scheduled job silent'
assert silent['data'][0]['model']['expr'] == (
    'absent_over_time({job="renew-internal-certs", kind="run", status="success"}[36h])')
assert silent['data'][0]['relativeTimeRange'] == {'from': 36 * 3600, 'to': 0}
assert silent['data'][1]['model']['conditions'][0]['evaluator'] == {'type': 'gt', 'params': [0.5]}
assert silent['labels']['service'] == '{{ $$labels.job }}'

leaf = rules['o11y_internal_ca_leaf_expiring']
intermediate = rules['o11y_internal_ca_intermediate_expiring']
for rule, role, seconds_left, severity in ((leaf, 'leaf', 7 * 86400, 'critical'),
                                           (intermediate, 'intermediate', 90 * 86400, 'warning')):
    assert rule['data'][0]['model']['expr'] == (
        f'last_over_time({{job="renew-internal-certs", kind="cert", role="{role}"}} '
        '| json remaining_seconds="remaining_seconds" | unwrap remaining_seconds | __error__="" [2d])')
    assert rule['data'][0]['relativeTimeRange'] == {'from': 2 * 86400, 'to': 0}
    assert rule['data'][1]['model']['conditions'][0]['evaluator'] == {'type': 'lt', 'params': [seconds_left]}
    assert rule['labels']['severity'] == severity and rule['labels']['service'] == 'step-ca'

for rule in rules.values():
    query = rule['data'][0]
    assert query['datasourceUid'] == 'loki' and query['model']['queryType'] == 'instant'
    assert rule['condition'] == 'B' and rule['data'][1]['datasourceUid'] == '__expr__'
    assert rule['noDataState'] == 'OK' and rule['executionErrorState'] == 'Alerting'
    assert rule['for'] == '5m' and rule['labels']['owner'] == 'platform-operations'

# Active and routed exactly when alerts are enabled and no canary is running, in local
# and production alike, so the deploy readback (every o11y_ rule active) holds.
canary = 'o11y-fault-probe-' + 'c' * 12
for local in (False, True):
    for enabled in (False, True):
        for probe in (None, canary):
            values = dict(local_mode=local, o11y_alerts_enabled=enabled)
            if probe:
                values['o11y_alert_canary_service'] = probe
            active = enabled and probe is None
            for uid, rule in loki_rules(render(**values)).items():
                assert uid.startswith('o11y_')
                assert rule['isPaused'] is (not active), (uid, values)
                assert ('notification_settings' in rule) is active, (uid, values)
                if active:
                    assert rule['notification_settings'] == {
                        'receiver': 'agent-cloud-ops',
                        'group_by': ['service', 'environment', 'cluster', 'alertname'],
                        'group_wait': '30s', 'group_interval': '5m', 'repeat_interval': '4h'}

# A declared list is ONE rule over every job, each with its own silence; the query
# range covers the longest.
jobs = [{'job': 'renew-internal-certs', 'max_silence_hours': 36},
        {'job': 'inference-personal-keys', 'max_silence_hours': 2}]
listed = loki_rules(render(local_mode=False, o11y_scheduled_jobs=jobs))
assert [uid for uid in listed if uid == 'o11y_scheduled_job_silent'] == ['o11y_scheduled_job_silent']
assert listed['o11y_scheduled_job_silent']['data'][0]['model']['expr'] == (
    'absent_over_time({job="renew-internal-certs", kind="run", status="success"}[36h]) or '
    'absent_over_time({job="inference-personal-keys", kind="run", status="success"}[2h])')
assert listed['o11y_scheduled_job_silent']['data'][0]['relativeTimeRange'] == {'from': 36 * 3600, 'to': 0}

# An empty declaration withdraws the rule through the file's single deleteRules key,
# alongside the inference rules when the scrape is off.
inference = ['inference_queue_stalled', 'inference_target_down', 'inference_vllm_metrics_absent',
             'inference_node_memavailable_low', 'inference_node_memfree_low',
             'inference_benchmark_gate_placeholder']
probe = ['inference_probe_failing', 'inference_probe_stale']
for scrape, deleted in ((True, probe + ['o11y_scheduled_job_silent']),
                        (False, inference + probe + ['o11y_scheduled_job_silent'])):
    empty = render(local_mode=False, o11y_scheduled_jobs=[], o11y_expected_metrics_targets=[],
                   dgx_spark_scrape_enabled=scrape)
    assert 'scheduled-jobs' not in [group['name'] for group in empty['groups']]
    assert 'o11y_scheduled_job_silent' not in loki_rules(empty)
    assert empty['deleteRules'] == [{'orgId': 1, 'uid': uid} for uid in deleted], empty['deleteRules']
PY
}

@test "o11y: the README documents the Loki line schema the CA and silent-job rules select" {
  python3 - "$DEPLOY_DIR/README.md" "$DEPLOY_DIR/templates/alerts.yml.j2" <<'PY'
import re
import sys

readme = open(sys.argv[1], encoding='utf-8').read()
template = open(sys.argv[2], encoding='utf-8').read()
section = readme.split('## Scheduled jobs and internal CA expiry', 1)[1].split('\n## ', 1)[0]
# Every label a rule selects on, and the one body field it unwraps, is in the schema.
for name in ('job', 'kind', 'status', 'role', 'host', 'leaf', 'remaining_seconds', 'not_after',
             'o11y_scheduled_jobs', 'max_silence_hours'):
    assert f'`{name}`' in section, name
assert '`platform/playbooks/tasks/push-loki-lines.yml`' in section
selected = set(re.findall(r'\{job="[^"]+", ((?:[a-z_]+="[^"]+"(?:, )?)+)\}', template.replace('\\"', '"')))
labels = {pair.split('=')[0] for group in selected for pair in group.split(', ')}
assert labels == {'kind', 'status', 'role'}, labels
for label in labels:
    assert f'`{label}`' in section, label
PY
}

@test "o11y: rule labels survive Grafana provisioning's environment interpolation" {
  python3 - "$DEPLOY_DIR/templates/alerts.yml.j2" <<'PY'
import json
import re
import sys

import yaml
from jinja2 import Environment, StrictUndefined

# Grafana v11.4.0 runs rule LABELS through interpolation (rules_types.go: Labels.Value())
# but stores annotations and query models raw. values.go interpolateValue splits on "$$",
# runs os.ExpandEnv on each part and joins the parts with "$"; an unset variable expands
# to "". Mirror that so a label template is checked as Grafana will store it.
def grafana_interpolate(value, environ={}):
    expand = lambda part: re.sub(r'\$(\{([^}]*)\}|[A-Za-z0-9_]+)',
                                 lambda m: environ.get(m.group(2) or m.group(1), ''), part)
    return '$'.join(expand(part) for part in value.split('$$'))


env = Environment(undefined=StrictUndefined, trim_blocks=True)
env.filters['bool'] = bool
env.filters['to_json'] = json.dumps
template = env.from_string(open(sys.argv[1], encoding='utf-8').read())
assert grafana_interpolate('{{ $labels.job }}') == '{{ .job }}'
assert grafana_interpolate('{{ $$labels.job }}') == '{{ $labels.job }}'
for values in (dict(local_mode=False, o11y_alerts_enabled=True, dgx_spark_scrape_enabled=True),
               dict(local_mode=True, o11y_alerts_enabled=True),
               dict(local_mode=False, o11y_scheduled_jobs=[{'job': 'a', 'max_silence_hours': 1}])):
    doc = yaml.safe_load(template.render(**values))
    for rule in (rule for group in doc['groups'] for rule in group['rules']):
        for key, value in rule['labels'].items():
            stored = grafana_interpolate(str(value))
            assert stored == str(value).replace('$$', '$'), (rule['uid'], key, value, stored)
silent = next(rule for group in yaml.safe_load(template.render(local_mode=False))['groups']
              for rule in group['rules'] if rule['uid'] == 'o11y_scheduled_job_silent')
assert grafana_interpolate(silent['labels']['service']) == '{{ $labels.job }}'
PY
}

@test "o11y: synthetic probe alert rules follow the probe flag and read what the probe writes" {
  python3 - "$DEPLOY_DIR/templates/alerts.yml.j2" "$DEPLOY_DIR/probe/inference-probe.sh" "$REPO_ROOT/platform/tests/fixtures" <<'PY'
import json
import pathlib
import re
import sys

import yaml
from jinja2 import Environment, StrictUndefined

env = Environment(undefined=StrictUndefined, trim_blocks=True)
env.filters['bool'] = bool
env.filters['to_json'] = json.dumps
template = env.from_string(open(sys.argv[1], encoding='utf-8').read())
written = set(re.findall(r'^#\s+(inference_probe_[a-z_]+)\{model_name\}', open(sys.argv[2], encoding='utf-8').read(), re.M))
assert written == {'inference_probe_success', 'inference_probe_latency_seconds',
                   'inference_probe_last_run_timestamp_seconds'}, written
vllm = {line.strip() for line in (pathlib.Path(sys.argv[3]) / 'vllm-metric-names-506e66caa3ef.txt').read_text().splitlines()
        if line.strip() and not line.startswith('#')}
probe = ['inference_probe_failing', 'inference_probe_stale']


def rules(**values):
    doc = yaml.safe_load(template.render(local_mode=False, o11y_expected_metrics_targets=[], **values))
    return doc, {rule['uid']: (group['name'], rule) for group in doc['groups'] for rule in group['rules']}


for scrape in (False, True):
    for enabled in (False, True):
        doc, found = rules(o11y_alerts_enabled=enabled, dgx_spark_scrape_enabled=scrape, o11y_inference_probe_enabled=True)
        assert [uid for uid in found if uid in probe] == probe, found.keys()
        assert not any(entry['uid'] in probe for entry in doc.get('deleteRules', []))
        for uid in probe:
            group, rule = found[uid]
            assert group == 'inference-failing', (uid, group)
            assert int(rule['for'][:-1]) >= 5 and rule['for'].endswith('m'), rule['for']
            expr = rule['data'][0]['model']['expr']
            assert '{job="receiver-host"}' in expr and 'by (model_name)' in expr, expr
            assert set(re.findall(r'inference_probe_[a-z_]+', expr)) <= written, expr
            assert rule['isPaused'] is (not enabled)
            assert ('notification_settings' in rule) is enabled
            if enabled:
                assert rule['notification_settings']['receiver'] == 'agent-cloud-ops'
        failing, stale = found['inference_probe_failing'][1], found['inference_probe_stale'][1]
        assert failing['data'][1]['model']['conditions'][0]['evaluator'] == {'type': 'lt', 'params': [0.5]}
        assert stale['data'][0]['model']['expr'].startswith('time() - ')
        assert stale['data'][1]['model']['conditions'][0]['evaluator'] == {'type': 'gt', 'params': [900]}
        # A stopped timer leaves the last success value standing; only no-data alerting sees it.
        assert stale['noDataState'] == 'Alerting' and failing['noDataState'] == 'OK'

    doc, found = rules(o11y_alerts_enabled=True, dgx_spark_scrape_enabled=scrape)
    assert not any(uid in probe for uid in found)
    assert [entry['uid'] for entry in doc['deleteRules'] if entry['uid'] in probe] == probe

# Every vllm: name any rendered rule selects is in the recorded metric list.
_, found = rules(o11y_alerts_enabled=True, dgx_spark_scrape_enabled=True, o11y_inference_probe_enabled=True)
missing = found['inference_vllm_metrics_absent'][1]
assert missing['data'][0]['model']['expr'] == 'absent(vllm:num_requests_running{job="dgx-spark-vllm",cluster="dgx-spark"})'
assert found['inference_target_down'][1]['data'][0]['model']['expr'] == 'up{cluster="dgx-spark"}'
for uid, (_, rule) in found.items():
    for name in re.findall(r'vllm:[A-Za-z0-9_:]+', rule['data'][0]['model']['expr']):
        assert name in vllm, (uid, name)
PY
}

@test "o11y: the probe script's shellcheck and no-literal-key check lives in the probe suite" {
  # Task 3.4's probe-script check is not duplicated here; this pins that it still exists.
  run grep -c 'def test_the_script_holds_no_literal_key_and_passes_shellcheck' "$REPO_ROOT/platform/tests/test_inference_probe.py"
  [ "$status" -eq 0 ]
  [ "$output" = "1" ]
}
