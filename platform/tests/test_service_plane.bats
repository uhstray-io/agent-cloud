#!/usr/bin/env bats
# Structural tests for the Plane service (platform/services/plane). Plane's own Compose file is
# fetched per deploy and never committed (EULA), so these guard agent-cloud's side of it: the
# per-file pins, the overlay that replaces Plane's restart policy, replica switches, bind mounts
# and publishes, the .env guard against Plane's shipped default credentials, first-run
# configuration in the same run, and no_log scoped to the credential tasks.
#
# Each guards a decision that is easy to "clean up" into a bug; the service's CLAUDE.md and the
# change's design.md (spikes 0.1, 0.2 and the runtime results) record why.
# Structural only — no live deploy.
# Run: bats platform/tests/test_service_plane.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/plane/deployment"
  OVERLAY="$DEPLOY_DIR/compose.agentcloud.yml"
  ENV_TPL="$DEPLOY_DIR/templates/env.j2"
  PLAYBOOK="$REPO_ROOT/platform/playbooks/deploy-plane.yml"
  CLEAN="$REPO_ROOT/platform/playbooks/clean-deploy-plane.yml"
}

@test "plane: no Plane-authored file is committed" {
  # Plane's EULA (section 2.1) forbids redistributing them; the deploy fetches them per run.
  [ -z "$(git -C "$REPO_ROOT" ls-files platform/services/plane/deployment/compose.yml platform/services/plane/deployment/docker-compose.yml platform/services/plane/deployment/variables.env)" ]
  grep -qx 'compose.yml' "$DEPLOY_DIR/.gitignore"
}

@test "plane: the release is pinned by per-file sha256, and an unpinned release is refused" {
  # Per file, not the zip: the endpoint stamps entry dates per request, so the zip digest moves.
  grep -qE '^\s+compose: [0-9a-f]{64}$' "$PLAYBOOK"
  grep -qE '^\s+variables: [0-9a-f]{64}$' "$PLAYBOOK"
  grep -qF '_plane_version in _plane_setup_pins' "$PLAYBOOK"
  grep -qF 'fetch_setup.py' "$PLAYBOOK"
}

@test "plane: the overlay switches optional services off with a profile, not replicas" {
  # podman-compose 1.6.0 ignores deploy.replicas: 0 and would start Plane AI, Argus and email.
  local svc
  for svc in importer-worker email pi-api pi-beat pi-worker pi-agent-worker argus pi-db-init pi-migrator argus-migrator; do
    python3 - "$OVERLAY" "$svc" <<'PY'
import sys, yaml
class L(yaml.SafeLoader): pass
L.add_multi_constructor('!', lambda l, s, n: l.construct_sequence(n) if isinstance(n, yaml.SequenceNode) else l.construct_scalar(n))
s = yaml.load(open(sys.argv[1]), Loader=L)["services"][sys.argv[2]]
assert s.get("profiles") == ["disabled"], (sys.argv[2], s)
PY
  done
}

@test "plane: the overlay replaces every bind mount with a named volume" {
  # Data in the engine: compose down -v removes it, and the deploy tree's git update or rsync
  # never touches it. Every volumes list is an !override, and none starts with a path.
  refute_grep -qE '^\s+- \.?/' "$OVERLAY"
  # Comments may name INSTALL_DIR to explain the change; no setting may use it.
  [ -z "$(grep -vE '^\s*#' "$OVERLAY" | grep -F 'INSTALL_DIR' || true)" ]
  [ "$(grep -c 'volumes: !override' "$OVERLAY")" -ge 10 ]
}

@test "plane: only the proxy publishes, plain HTTP on the inventory bind" {
  [ "$(grep -c 'ports:' "$OVERLAY")" -eq 1 ]
  grep -qF 'ports: !override' "$OVERLAY"
  grep -qF '"${PLANE_BIND:-127.0.0.1}:${PLANE_PORT:-8080}:80"' "$OVERLAY"
}

@test "plane: deploy.sh applies the overlay and refuses a Plane service it does not name" {
  grep -qF 'COMPOSE_OVERLAYS="compose.agentcloud.yml' "$DEPLOY_DIR/deploy.sh"
  grep -qF 'compose.agentcloud.yml does not name these services' "$DEPLOY_DIR/deploy.sh"
  # Container lifecycle only: no secret handling, no OpenBao.
  [ -z "$(grep -vE '^\s*#' "$DEPLOY_DIR/deploy.sh" | grep -iE '\b(bao|openbao|vault|secret_id|role_id)\b' || true)" ]
}

@test "plane: .env closes sign-up from first boot and refuses Plane's shipped credentials" {
  grep -qF "'ENABLE_SIGNUP': '0'" "$ENV_TPL"
  grep -qF "'SITE_ADDRESS': ':80'" "$ENV_TPL"
  grep -qF 'is empty or still Plane' "$ENV_TPL"
  local k
  # RABBITMQ_PASSWORD is the name Plane's Compose reads; variables.env sets another one.
  for k in SECRET_KEY LIVE_SERVER_SECRET_KEY SILO_HMAC_SECRET_KEY AES_SECRET_KEY PI_INTERNAL_SECRET \
           CURSOR_WEBHOOK_SECRET POSTGRES_PASSWORD DATABASE_URL FOLLOWER_POSTGRES_URI \
           RABBITMQ_PASSWORD AMQP_URL MINIO_ROOT_PASSWORD AWS_SECRET_ACCESS_KEY MACHINE_SIGNATURE; do
    grep -qF "'$k'" "$ENV_TPL" || { echo "missing $k"; false; }
  done
  # Every credential comes from OpenBao.
  refute_grep -qE "'(SECRET_KEY|POSTGRES_PASSWORD|MINIO_ROOT_PASSWORD)': '[^']" "$ENV_TPL"
}

@test "plane: every credential is generated once into OpenBao" {
  local s
  for s in plane_secret_key plane_live_server_secret_key plane_silo_hmac_secret_key plane_aes_secret_key \
           plane_pi_internal_secret plane_cursor_webhook_secret plane_db_password plane_rabbitmq_password \
           plane_minio_password plane_machine_signature plane_automation_password; do
    grep -qE "name: $s, type: (random|django)" "$PLAYBOOK" || { echo "missing $s"; false; }
  done
  grep -qF 'tasks/manage-secrets.yml' "$PLAYBOOK"
  grep -qF 'tasks/place-monorepo.yml' "$PLAYBOOK"
}

@test "plane: no_log is only on the two first-run configuration tasks" {
  python3 - "$PLAYBOOK" <<'PY'
import sys, yaml
plays = yaml.safe_load(open(sys.argv[1]))
logged = sorted(t["name"] for p in plays for t in p.get("tasks", []) if t.get("no_log"))
assert logged == ["Converge instance setup, invitation-only sign-up and the workspace",
                  "Keep only the configuration summary"], logged
PY
}

@test "plane: first-run configuration runs in the deploy, before verify" {
  # Until the first admin signs up, anyone who reaches /god-mode/ claims the instance.
  assert_precedes "$PLAYBOOK" 'Run deploy\.sh \(container lifecycle\)' 'Converge instance setup'
  assert_precedes "$PLAYBOOK" 'Converge instance setup' 'API answers through the proxy'
  # The password reaches the script on stdin, never on a command line.
  grep -qF "'password': _resolved.plane_automation_password" "$PLAYBOOK"
  refute_grep -qE 'print\(.*password' "$DEPLOY_DIR/configure_instance.py"
}

@test "plane: the proxy stays down until the instance is set up" {
  # The proxy is the only way in, and the first admin to sign up claims the instance, so
  # setup runs against the API directly and the proxy starts only afterwards.
  local f="$DEPLOY_DIR/deploy.sh"
  grep -qF 'compose_staged_up()' "$f"
  grep -qF "grep -vx proxy" "$f"
  assert_precedes "$f" 'if instance_set_up; then' 'compose up -d --force-recreate --no-deps proxy'
  grep -qF 'PLANE_PROXY_HELD=true' "$f"
  grep -qF "'base': 'http://localhost:8000'" "$PLAYBOOK"
  assert_precedes "$PLAYBOOK" 'Converge instance setup' 'Start the proxy now that the instance is set up'
  grep -qF "'PLANE_PROXY_HELD=true' in _deploy_proxy.stdout_lines" "$PLAYBOOK"
}

@test "plane: verify probes the API through the proxy, not /" {
  # The proxy serves / from the web frontend, which stays up over a dead API (MISTAKES 2.19).
  grep -qF "http://proxy:80/api/instances/" "$PLAYBOOK"
  grep -qF "assert not d['config']['enable_signup']" "$PLAYBOOK"
}

@test "plane: the service-deploy step result is recorded last" {
  local last
  last=$(grep -E '^\s+- name: ' "$PLAYBOOK" | tail -1)
  [[ "$last" == *"Record the service-deploy step result"* ]]
  grep -qF 'tasks/emit-service-deploy-result.yml' "$PLAYBOOK"
}

@test "plane: clean deploy names the compose project so down -v finds the volumes" {
  grep -qF 'tasks/clean-service.yml' "$CLEAN"
  grep -qF 'COMPOSE_PROJECT_NAME: plane' "$CLEAN"
  grep -qF "'COMPOSE_PROJECT_NAME': 'plane'" "$ENV_TPL"
}
