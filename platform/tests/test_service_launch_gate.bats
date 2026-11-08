#!/usr/bin/env bats
# Structural tests for the launch-gate container files (platform/services/launch-gate/deployment).
#
# The gate is the only identity that may ask Semaphore to launch on an agent's behalf, so its container is
# held to a fixed shape: read-only source, config and certificate directory; a declared bind; no capability
# and no privilege gain; restart always; a deploy script that hashes what the process reads at start and
# never the leaf, which the process re-reads itself. The app's behaviour is tested in test_launch_gate*.py.
#
# Run: bats platform/tests/test_service_launch_gate.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/launch-gate/deployment"
}

@test "launch-gate: the base compose takes its image from the inventory and fails without one" {
  assert_grep -qF '${LAUNCH_GATE_IMAGE:?set launch_gate_image in the inventory}' "$DEPLOY_DIR/compose.yml"
}

@test "launch-gate: restart is always, so podman's boot unit starts the gate after a reboot" {
  assert_grep -qxF '    restart: always' "$DEPLOY_DIR/compose.yml"
}

@test "launch-gate: the HTTPS port is published on a declared bind, loopback by default, and the container listens on 8443" {
  assert_grep -qxF '      - "${LAUNCH_GATE_BIND:-127.0.0.1}:${LAUNCH_GATE_PORT:-8443}:8443"' "$DEPLOY_DIR/compose.yml"
  assert_grep -qF "'listen_port': 8443," "$DEPLOY_DIR/templates/config.json.j2"
}

@test "launch-gate: source, config and the certificate directory are mounted read-only" {
  assert_grep -qxF '      - ./app:/app:ro' "$DEPLOY_DIR/compose.yml"
  assert_grep -qxF '      - ./config.json:/etc/launch-gate/config.json:ro' "$DEPLOY_DIR/compose.yml"
  assert_grep -qxF '      - ./certs:/certs:ro' "$DEPLOY_DIR/compose.yml"
}

@test "launch-gate: no file under certs/ is mounted by name, because renewal swaps the leaf underneath it" {
  # A single-file mount pins the inode a renewal replaces. Only the directory may be mounted.
  refute_grep -qE '\./certs/[^:]*:' "$DEPLOY_DIR/compose.yml" "$DEPLOY_DIR/compose.prod.yml" "$DEPLOY_DIR/compose.local.yml"
}

@test "launch-gate: the container is read-only, drops every capability and gains no privilege" {
  assert_grep -qxF '    read_only: true' "$DEPLOY_DIR/compose.yml"
  assert_grep -qxF '    cap_drop:' "$DEPLOY_DIR/compose.yml"
  assert_grep -qxF '      - ALL' "$DEPLOY_DIR/compose.yml"
  assert_grep -qxF '      - no-new-privileges:true' "$DEPLOY_DIR/compose.yml"
}

@test "launch-gate: the process never writes bytecode into the read-only source" {
  # -B, not PYTHONDONTWRITEBYTECODE: the -I the command also passes ignores every PYTHON* variable.
  assert_grep -qxF '      - -B' "$DEPLOY_DIR/compose.yml"
  refute_grep -q 'PYTHONDONTWRITEBYTECODE' "$DEPLOY_DIR/compose.yml"
}

@test "launch-gate: the base command runs the app isolated, from /app, on the mounted config" {
  assert_grep -qxF '      - python' "$DEPLOY_DIR/compose.yml"
  assert_grep -qxF '      - -I' "$DEPLOY_DIR/compose.yml"
  assert_grep -qxF '      - /app/launch_gate.py' "$DEPLOY_DIR/compose.yml"
  assert_grep -qxF '      - /etc/launch-gate/config.json' "$DEPLOY_DIR/compose.yml"
}

@test "launch-gate: the production overlay runs the process as uid 1000 through keep-id, so the 0600 key is readable" {
  assert_grep -qxF '    userns_mode: "keep-id:uid=1000,gid=1000"' "$DEPLOY_DIR/compose.prod.yml"
  assert_grep -qxF '    user: "1000:1000"' "$DEPLOY_DIR/compose.prod.yml"
}

@test "launch-gate: no overlay declares command, so the base command stands" {
  refute_grep -qE '^[[:space:]]+command:' "$DEPLOY_DIR/compose.prod.yml" "$DEPLOY_DIR/compose.local.yml"
}

@test "launch-gate: production never disables SELinux labelling; only the local overlay does" {
  refute_grep -qF 'label=disable' "$DEPLOY_DIR/compose.yml" "$DEPLOY_DIR/compose.prod.yml"
  assert_grep -qxF '      - label=disable' "$DEPLOY_DIR/compose.local.yml"
}

@test "launch-gate: the local overlay joins local-dev with a memory cap, a local uid and the TLS-name alias, and does not republish the ports" {
  assert_grep -qxF '    mem_limit: 128m' "$DEPLOY_DIR/compose.local.yml"
  assert_grep -qxF '    user: "1001:0"' "$DEPLOY_DIR/compose.local.yml"
  assert_grep -qxF '      local-dev:' "$DEPLOY_DIR/compose.local.yml"
  assert_grep -qxF '        aliases:' "$DEPLOY_DIR/compose.local.yml"
  assert_grep -qxF '          - ${LAUNCH_GATE_TLS_NAME:?set by the deploy}' "$DEPLOY_DIR/compose.local.yml"
  assert_grep -qF 'external: true' "$DEPLOY_DIR/compose.local.yml"
  refute_grep -qE '^[[:space:]]+ports:' "$DEPLOY_DIR/compose.local.yml"
  # The alias is a local-dev fact only: the base file names none, and production never loads the overlay.
  refute_grep -qE 'aliases|LAUNCH_GATE_TLS_NAME' "$DEPLOY_DIR/compose.yml" "$DEPLOY_DIR/compose.prod.yml"
}

@test "launch-gate: the rendered env, config and certificates are gitignored" {
  assert_grep -qxF '.env' "$DEPLOY_DIR/.gitignore"
  assert_grep -qxF 'config.json' "$DEPLOY_DIR/.gitignore"
  assert_grep -qxF 'certs/' "$DEPLOY_DIR/.gitignore"
}

@test "launch-gate: deploy.sh is an executable bash script that sources common.sh and runs with pipefail" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -x "$f" ]
  assert_grep -qxF '#!/usr/bin/env bash' "$f"
  assert_grep -qxF 'set -euo pipefail' "$f"
  assert_grep -qF 'source "${LIB_DIR}/common.sh"' "$f"
}

@test "launch-gate: deploy.sh generates no secret and calls no OpenBao helper" {
  refute_grep -qE '\b(gen_secret|put_secret|get_secret|bao_|BAO_)' "$DEPLOY_DIR/deploy.sh"
}

@test "launch-gate: deploy.sh hashes the app, the config and the trust bundle, and never the leaf directory" {
  local f="$DEPLOY_DIR/deploy.sh"
  assert_grep -qxF '  compose_up_if_changed app config.json certs/step-ca-bundle.crt' "$f"
  refute_grep -qE 'compose_up_if_changed.*certs( |$)' "$f"
  refute_grep -qE 'compose_up_if_changed.*certs/[^ ]+/' "$f"
}

@test "launch-gate: deploy.sh loads the production overlay outside LOCAL_MODE only" {
  local f="$DEPLOY_DIR/deploy.sh"
  assert_grep -qF 'if [ "${LOCAL_MODE:-}" != "true" ]; then' "$f"
  assert_grep -qF 'COMPOSE_OVERLAYS="compose.prod.yml ${COMPOSE_OVERLAYS:-}"' "$f"
}

@test "launch-gate: deploy.sh never deletes or moves anything under the deployment directory" {
  refute_grep -qE '\brm |\bmv |--recursive' "$DEPLOY_DIR/deploy.sh"
}

@test "launch-gate: env.j2 renders the fixed keys from the approved namespaces" {
  local f="$DEPLOY_DIR/templates/env.j2"
  assert_grep -qxF 'LAUNCH_GATE_IMAGE={{ _launch_gate_image }}' "$f"
  assert_grep -qxF 'LAUNCH_GATE_BIND={{ _launch_gate_bind }}' "$f"
  assert_grep -qxF 'LAUNCH_GATE_PORT={{ _launch_gate_port }}' "$f"
  assert_grep -qxF 'LAUNCH_GATE_TLS_NAME={{ _launch_gate_tls_name }}' "$f"
  assert_grep -qxF 'LAUNCH_GATE_SEMAPHORE_TOKEN={{ secrets.semaphore_api_token }}' "$f"
}

@test "launch-gate: env.j2 renders one caller token line per caller, in sorted order" {
  local f="$DEPLOY_DIR/templates/env.j2"
  assert_grep -qF '{% for _caller in _launch_gate_callers | sort %}' "$f"
  assert_grep -qF "LAUNCH_GATE_CALLER_TOKEN_{{ _caller | upper | replace('-', '_') }}={{ secrets['caller_' ~ _caller ~ '_token'] }}" "$f"
  assert_grep -qxF '{% endfor %}' "$f"
}

@test "launch-gate: config.json.j2 holds no secret and no launch-gate token variable" {
  local f="$DEPLOY_DIR/templates/config.json.j2"
  refute_grep -qiF 'secret' "$f"
  refute_grep -qF 'LAUNCH_GATE_' "$f"
}

@test "launch-gate: AGENTS.md is a relative symlink to CLAUDE.md, as the other service directories are" {
  [ -L "$DEPLOY_DIR/AGENTS.md" ]
  [ "$(readlink "$DEPLOY_DIR/AGENTS.md")" = "CLAUDE.md" ]
  [ -f "$DEPLOY_DIR/CLAUDE.md" ]
}

@test "launch-gate: an init process runs as PID 1, so a stop delivers SIGTERM instead of waiting out the timeout" {
  # Python as PID 1 ignores SIGTERM; the first local deploy's stop ended in SIGKILL (exit 137) after 10 s.
  assert_grep -qxF '    init: true' "$DEPLOY_DIR/compose.yml"
}
