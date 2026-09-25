#!/usr/bin/env bats
# Local NetBox through the local Semaphore, discovery confined to local-dev
# (change service-deployment-workflow, platform/netbox-local, tasks 6.1-6.2).
load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  PB="$REPO_ROOT/platform/playbooks"
  NB="$REPO_ROOT/platform/services/netbox/deployment"
}

@test "placement installs podman only on podman hosts (a Docker NetBox host is left alone)" {
  blk=$(sed -n '/name: "Ensure podman + compose entrypoint"/,/^$/p' "$PB/tasks/place-monorepo.yml")
  assert_grep -qF "when: (container_engine | default('podman')) == 'podman'" <<<"$blk"
}

@test "deploy-netbox places the monorepo through the shared task, never a bare clone" {
  assert_grep -qF 'include_tasks: tasks/place-monorepo.yml' "$PB/deploy-netbox.yml"
  refute_grep -qE 'ansible\.builtin\.git:' "$PB/deploy-netbox.yml"
}

@test "deploy-netbox passes the local overlay and loopback publish from inventory only" {
  assert_grep -qF "COMPOSE_OVERLAYS: \"{{ netbox_compose_overlays | default([]) | join(' ') }}\"" "$PB/deploy-netbox.yml"
  assert_grep -qF "NETBOX_HOST_IP: \"{{ netbox_host_ip | default('') }}\"" "$PB/deploy-netbox.yml"
}

@test "the NetBox compose wrapper adds overlays after the base file, and none by default" {
  blk=$(sed -n '/^compose() {/,/^}/p' "$NB/lib/common.sh")
  assert_grep -qF 'files=(-f "${compose_dir}/docker-compose.yml")' <<<"$blk"
  assert_grep -qF 'for overlay in ${COMPOSE_OVERLAYS:-}; do' <<<"$blk"
  assert_grep -qF 'which does not exist in' <<<"$blk"
}

@test "deploy.sh moves a non-git netbox-docker copy aside instead of failing or deleting it" {
  assert_grep -qF 'mv "${NETBOX_DOCKER_DIR}" "${stale}"' "$NB/deploy.sh"
  refute_grep -qE 'rm -rf "?\$\{?NETBOX_DOCKER_DIR' "$NB/deploy.sh"
  assert_grep -qF 'netbox-docker.stale-*/' "$NB/.gitignore"
}

@test "deploy.sh no longer requires openssl (it generates no secrets)" {
  refute_grep -qE '^command -v openssl' "$NB/deploy.sh"
}

@test "orb-agent: local-dev checks the discovery scope before any credential or config" {
  pb="$PB/deploy-orb-agent.yml"
  scope=$(grep -nF 'tasks/assert-local-discovery-scope.yml' "$pb" | head -1 | cut -d: -f1)
  creds=$(grep -nF 'tasks/manage-diode-credentials.yml' "$pb" | head -1 | cut -d: -f1)
  [ -n "$scope" ] && [ "$scope" -lt "$creds" ]
  # both plays stop when local discovery is disabled, and the first removes an agent a previous
  # run started before it stops (PR 195 Codex review): three uses of the condition
  [ "$(grep -cF "not (_local_discovery_enabled | default(false) | bool)" "$pb")" -eq 3 ]
  assert_precedes "$pb" 'remove a previously started orb agent' 'stop here when no local discovery target is declared'
  assert_grep -qF 'rm -f netbox-orb-agent' "$pb"
  refute_grep -qE 'ansible\.builtin\.git:' "$pb"
}

@test "orb-agent: the pfSense and Proxmox workers are gated, and off on local-dev" {
  assert_grep -qF '{% if discovery_workers_enabled | default(true) | bool %}' "$NB/templates/agent.yaml.j2"
  assert_grep -qE '^\{% endif %\}$' "$NB/templates/agent.yaml.j2"
}

@test "the local scope guard reads the live engine and runs under --check" {
  t="$PB/tasks/assert-local-discovery-scope.yml"
  assert_grep -qF 'network ls -q' "$t"
  assert_grep -qF 'discovery_scope.py' "$t"
  [ "$(grep -cF 'check_mode: false' "$t")" -eq 2 ]
}

@test "verify-service-persistence selects containers by deploy dir and reports systemd-enablement" {
  pb="$PB/verify-service-persistence.yml"
  # the selector lives once, in the shared task, and all three workflow readers use it
  assert_grep -qF "'label=com.docker.compose.project.working_dir=' ~ _deploy_dir" "$PB/tasks/list-service-containers.yml"
  for f in verify-service-persistence snapshot-firewall snapshot-service-assessment; do
    assert_grep -qF 'include_tasks: tasks/list-service-containers.yml' "$PB/$f.yml"
  done
  assert_grep -qF "_restart_ok: \"{{ ['always'] if (_rootless_podman | bool) else ['always', 'unless-stopped'] }}\"" "$pb"
  assert_grep -qF 'step_result_step: systemd-enablement' "$pb"
  # an empty selection is a failure, never a vacuous pass
  assert_grep -qF 'no containers carry the compose working_dir label' "$pb"
}

@test "NetBox containers are found by compose labels, not a guessed name separator" {
  lib="$NB/lib/common.sh"
  refute_grep -qF 'CONTAINER_SEP' "$lib"
  assert_grep -qF 'label=com.docker.compose.service=$1' "$lib"
  assert_grep -qF 'NETBOX_PG_VOLUME="netbox_netbox-postgres"' "$lib"
}

@test "the local controller's OpenBao policy is production's file, not an inline fork" {
  bs="$PB/bootstrap-local-dev.yml"
  blk=$(sed -n '/name: "Write local-semaphore policy"/,/status_code/p' "$bs")
  assert_grep -qF "config/policies/semaphore-read.hcl" <<<"$blk"
  refute_grep -qF 'path "secret/data/services/*"' <<<"$blk"
}
