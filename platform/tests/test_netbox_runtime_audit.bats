#!/usr/bin/env bats

load assert_helpers

@test "NetBox runtime audit is read-only and bound to dev" {
  local audit="$BATS_TEST_DIRNAME/../playbooks/audit-netbox-runtime.yml"
  local templates="$BATS_TEST_DIRNAME/../semaphore/templates.yml"
  grep -qF 'hosts: netbox_svc' "$audit"
  grep -qF 'docker, ps, -a' "$audit"
  grep -qF 'docker, inspect' "$audit"
  grep -qF 'oom={{.State.OOMKilled}} restarts={{.RestartCount}}' "$audit"
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
