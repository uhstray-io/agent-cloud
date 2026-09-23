#!/usr/bin/env bats

load assert_helpers

@test "NetBox runtime audit is read-only and bound to dev" {
  local audit="$BATS_TEST_DIRNAME/../playbooks/audit-netbox-runtime.yml"
  local templates="$BATS_TEST_DIRNAME/../semaphore/templates.yml"
  grep -qF 'hosts: netbox_svc' "$audit"
  grep -qF 'docker, ps, -a' "$audit"
  grep -qF 'docker, inspect' "$audit"
  grep -qF 'oom={{.State.OOMKilled}} restarts={{.RestartCount}}' "$audit"
  grep -qF 'policy={{.HostConfig.RestartPolicy.Name}}' "$audit"
  grep -qF 'image_ref={{.Config.Image}} image_id={{.Image}}' "$audit"
  grep -qF '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}:{{.Destination}},{{end}}{{end}}' "$audit"
  grep -qF 'storage: "{{ _storage.stdout_lines }}"' "$audit"
  grep -qF 'argv: [uptime, -s]' "$audit"
  grep -qF 'netbox-postgres-1' "$audit"
  grep -qF 'netbox-redis-cache-1' "$audit"
  grep -qF "/login/" "$audit"
  refute_grep -Eq 'ansible.builtin.(shell|script)|method: (POST|PUT|PATCH|DELETE)|docker, (start|stop|restart|run)' "$audit"
  grep -A2 -F 'name: Audit NetBox Runtime' "$templates" | grep -qF 'dev_variant: true'
}

@test "NetBox token bootstrap preserves the Docker error when no output exists" {
  local bootstrap="$BATS_TEST_DIRNAME/../playbooks/provision-netbox-automation-token.yml"
  grep -qF "_permissions.stdout_lines | default([]) | last | default('')" "$bootstrap"
}

@test "NetBox recovery publishes only a dev-bound template with preflight default" {
  python3 - "$BATS_TEST_DIRNAME/../semaphore/templates.yml" "$BATS_TEST_DIRNAME/../playbooks/recover-netbox-runtime.yml" <<'PY'
import json
import sys
import yaml
from jinja2 import Environment

templates = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["templates"]
recovery = [item for item in templates if item["playbook"] == "platform/playbooks/recover-netbox-runtime.yml"]
assert len(recovery) == 1
assert recovery[0]["name"] == "Recover NetBox Runtime (Dev)"
assert recovery[0]["repository"] == "agent-cloud dev"
assert not recovery[0].get("dev_variant", False)
assert recovery[0]["survey_vars"][0]["default_value"] == "false"

plays = yaml.safe_load(open(sys.argv[2], encoding="utf-8"))
assert plays[0]['ansible.builtin.import_playbook'] == 'preflight-target-group.yml'
assert plays[0]['vars'] == {'preflight_group': 'netbox_svc', 'preflight_group_expected': 'netbox_svc'}
controller_tasks = plays[1]['tasks']
assert any(task.get('ansible.builtin.command', {}).get('argv') == ['git', 'rev-parse', 'refs/remotes/origin/dev'] for task in controller_tasks)
assert any('_controller_revision.stdout == _dev_revision.stdout' in task.get('ansible.builtin.assert', {}).get('that', []) for task in controller_tasks)
tasks = plays[2]["tasks"]
assert not any("ansible.builtin.git" in task for task in tasks)
assert any("ansible.builtin.stat" in task and "checksum_algorithm" in task["ansible.builtin.stat"] for task in tasks)
checksum, = (task for task in tasks if task['name'] == 'Require the host Compose file to match reviewed dev')
assert 'rstrip=false' in checksum['ansible.builtin.assert']['that']
assert any("ansible.builtin.script" in task for task in tasks)
env = Environment()
env.filters["from_json"] = json.loads
backing, = (task for task in tasks if task['name'] == 'Converge existing backing services without pull or build')
argv = env.compile_expression(backing['ansible.builtin.command']['argv'].strip('{} '))
for action, force in (("recreate", True), ("start", False)):
    rendered = argv(_before={'stdout': json.dumps({'postgres': {'planned_action': action}})}, item='postgres')
    assert ('--force-recreate' in rendered) is force
    assert ('--no-recreate' in rendered) is not force
    assert rendered[-1] == 'postgres' and '--pull' in rendered and 'never' in rendered
app, = (task for task in tasks if task['name'] == 'Converge the existing NetBox app without pull or build')
app_argv = env.compile_expression(app['ansible.builtin.command']['argv'].strip('{} '))
for action, force in (("recreate", True), ("start", False)):
    rendered = app_argv(_before={'stdout': json.dumps({'netbox': {'planned_action': action}})})
    assert ('--force-recreate' in rendered) is force
    assert ('--no-recreate' in rendered) is not force
    assert rendered[-1] == 'netbox'
assert "planned_action != 'noop'" in backing['when']
assert "planned_action != 'noop'" in app['when']
assert any(task['name'] == 'Require started and untouched containers to keep their identity' for task in tasks)
PY
}

@test "NetBox recovery hashes the same Compose bytes on controller and host" {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  cat > "$BATS_TEST_TMPDIR/checksum.yml" <<'YAML'
- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - ansible.builtin.stat:
        path: "{{ source_compose }}"
        checksum_algorithm: sha256
      register: source_stat
    - ansible.builtin.assert:
        that: source_stat.stat.checksum == (lookup('file', source_compose, rstrip=false) | hash('sha256'))
YAML
  run env ANSIBLE_LOCAL_TEMP="$BATS_TEST_TMPDIR/ansible" ansible-playbook \
    -i localhost, "$BATS_TEST_TMPDIR/checksum.yml" \
    -e "source_compose=$BATS_TEST_DIRNAME/../services/netbox/deployment/docker-compose.yml"
  [ "$status" -eq 0 ]
}
