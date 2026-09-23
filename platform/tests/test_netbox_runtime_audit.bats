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
  python3 - "$BATS_TEST_DIRNAME/../semaphore/templates.yml" <<'PY'
import sys
import yaml

templates = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["templates"]
recovery = [item for item in templates if item["playbook"] == "platform/playbooks/recover-netbox-runtime.yml"]
assert len(recovery) == 1
assert recovery[0]["name"] == "Recover NetBox Runtime (Dev)"
assert recovery[0]["repository"] == "agent-cloud dev"
assert not recovery[0].get("dev_variant", False)
assert recovery[0]["survey_vars"][0]["default_value"] == "false"
PY
}
