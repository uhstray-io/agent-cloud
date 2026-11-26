#!/usr/bin/env bats
# Structural tests for the plane-mcp service (platform/services/plane-mcp). Plane's MCP server runs as
# its own compose project on Plane's stack network. These guard the decisions that are easy to "tidy"
# into a bug: one restart-always service with no base-file port, the publish only outside local mode,
# tool payload logging off, the OAuth store on its own Redis DB, a render that refuses empty OAuth
# credentials, and a deploy.sh that is container lifecycle only.
#
# Structural only (grep and parse of the files on disk). No live deploy.
# Run: bats platform/tests/test_service_plane_mcp.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/plane-mcp/deployment"
}

@test "plane-mcp: compose.yml is one restart-always service named plane-mcp, with no host port" {
  run python3 - "$DEPLOY_DIR" <<'PY'
import sys, yaml
d = sys.argv[1]
doc = yaml.safe_load(open(f"{d}/compose.yml"))
assert list(doc["services"]) == ["mcp"], list(doc["services"])
mcp = doc["services"]["mcp"]
assert mcp.get("restart") == "always", mcp.get("restart")
assert mcp.get("container_name") == "plane-mcp", mcp.get("container_name")
assert "ports" not in mcp, "compose.yml must not publish a host port; compose.publish.yml does"
assert mcp.get("env_file") == [".env"], mcp.get("env_file")
PY
  [ "$status" -eq 0 ] || { echo "$output"; false; }
}

@test "plane-mcp: compose.yml joins only Plane's stack network, external, never created here" {
  run python3 - "$DEPLOY_DIR" <<'PY'
import sys, yaml
d = sys.argv[1]
doc = yaml.safe_load(open(f"{d}/compose.yml"))
assert doc["networks"] == {"plane": {"external": True, "name": "plane_default"}}, doc["networks"]
assert doc["services"]["mcp"]["networks"] == ["plane"], doc["services"]["mcp"].get("networks")
PY
  [ "$status" -eq 0 ] || { echo "$output"; false; }
}

@test "plane-mcp: the image is fully qualified and its tag comes from .env, with no second default" {
  local f="$DEPLOY_DIR/compose.yml"
  # docker.io/ is required: a short name fails on podman hosts with no unqualified-search registry.
  grep -qxF '    image: docker.io/makeplane/plane-mcp-server:${PLANE_MCP_VERSION:?set PLANE_MCP_VERSION in .env}' "$f"
  refute_grep -qE 'makeplane/plane-mcp-server:v[0-9]' "$f"
}

@test "plane-mcp: the publish overlay is one mapping, from the inventory bind and port, to 8211" {
  run python3 - "$DEPLOY_DIR" <<'PY'
import sys, yaml
d = sys.argv[1]
pub = yaml.safe_load(open(f"{d}/compose.publish.yml"))
assert list(pub["services"]) == ["mcp"], list(pub["services"])
assert pub["services"]["mcp"]["ports"] == ["${PLANE_MCP_BIND:-127.0.0.1}:${PLANE_MCP_PORT:-8211}:8211"], pub["services"]["mcp"]["ports"]
PY
  [ "$status" -eq 0 ] || { echo "$output"; false; }
}

@test "plane-mcp: the local overlay joins local-dev and publishes nothing" {
  run python3 - "$DEPLOY_DIR" <<'PY'
import sys, yaml
d = sys.argv[1]
loc = yaml.safe_load(open(f"{d}/compose.local.yml"))
assert loc["networks"] == {"local-dev": {"external": True}}, loc["networks"]
svc = loc["services"]["mcp"]
assert "ports" not in svc, "local mode must not publish a host port"
assert svc["networks"] == ["local-dev"], svc["networks"]
PY
  [ "$status" -eq 0 ] || { echo "$output"; false; }
}

@test "plane-mcp: env.j2 turns tool payload logging off and keeps user info out of the logs" {
  local f="$DEPLOY_DIR/templates/env.j2"
  # Whole-line matches: a commented-out or prefixed line must not satisfy the guard.
  grep -qxF 'LOG_PAYLOADS=false' "$f"
  grep -qxF 'LOG_USER_INFO=false' "$f"
  refute_grep -qE '^LOG_PAYLOADS=true' "$f"
}

@test "plane-mcp: env.j2 gives the OAuth token store its own Redis DB and the server-to-server Plane URL" {
  local f="$DEPLOY_DIR/templates/env.j2"
  grep -qxF 'REDIS_URL=redis://plane-redis:6379/1' "$f"
  refute_grep -qF 'REDIS_URL=redis://plane-redis:6379/0' "$f"
  grep -qxF 'PLANE_INTERNAL_BASE_URL=http://api:8000' "$f"
  # PLANE_BASE_URL is the browser-facing Plane URL, so it is the inventory's public one.
  grep -qxF 'PLANE_BASE_URL={{ plane_public_url }}' "$f"
  grep -qxF 'PLANE_OAUTH_PROVIDER_BASE_URL={{ plane_mcp_public_url }}' "$f"
}

@test "plane-mcp: env.j2 refuses to render without the OAuth client id and secret" {
  local f="$DEPLOY_DIR/templates/env.j2"
  grep -qxF '{%- if not _plane_mcp_client_id -%}' "$f"
  grep -qxF '{%- if not _plane_mcp_client_secret -%}' "$f"
  grep -qF "mandatory('env.j2: _plane_mcp_client_id is empty" "$f"
  grep -qF "mandatory('env.j2: _plane_mcp_client_secret is empty" "$f"
  # The guards come before the first rendered line of the .env body.
  assert_precedes "$f" 'mandatory\(' '^COMPOSE_PROJECT_NAME=plane-mcp$'
}

@test "plane-mcp: the OAuth credentials are only OpenBao-sourced variables, never literals" {
  local f="$DEPLOY_DIR/templates/env.j2"
  grep -qxF 'PLANE_OAUTH_PROVIDER_CLIENT_ID={{ _plane_mcp_client_id }}' "$f"
  grep -qxF 'PLANE_OAUTH_PROVIDER_CLIENT_SECRET={{ _plane_mcp_client_secret }}' "$f"
  refute_grep -qE '^PLANE_OAUTH_PROVIDER_CLIENT_(ID|SECRET)=[^{]' "$f"
  # No credential variable in the compose files' live config: those are committed, the .env is not.
  # Comments are excluded, so a comment may name the variable it explains.
  local f code
  for f in compose.yml compose.local.yml compose.publish.yml; do
    code="$BATS_TEST_TMPDIR/$f.code"
    grep -vE '^[[:space:]]*#' "$DEPLOY_DIR/$f" > "$code"
    refute_grep -qE 'CLIENT_(ID|SECRET)' "$code"
  done
}

@test "plane-mcp: env.j2 and the compose files carry no RFC1918 address" {
  refute_grep -qE '192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.' \
    "$DEPLOY_DIR/templates/env.j2" "$DEPLOY_DIR/compose.yml" "$DEPLOY_DIR/compose.local.yml" "$DEPLOY_DIR/compose.publish.yml"
}

@test "plane-mcp: deploy.sh is an executable bash script that sources common.sh and runs under strict mode" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  head -1 "$f" | grep -qxF '#!/usr/bin/env bash'
  grep -qxF 'set -euo pipefail' "$f"
  grep -qF 'source "${LIB_DIR}/common.sh"' "$f"
}

@test "plane-mcp: deploy.sh is container lifecycle only: no secret generation and no OpenBao" {
  local f="$DEPLOY_DIR/deploy.sh" code="$BATS_TEST_TMPDIR/code.sh"
  # Comments are excluded, so the header may explain what the script does not do.
  grep -vE '^[[:space:]]*#' "$f" > "$code"
  refute_grep -qiE 'openssl|gen_secret|put_secret|get_secret|openbao|vault|secret_id|role_id' "$code"
}

@test "plane-mcp: deploy.sh refuses without .env and without plane_default, then waits until running" {
  local f="$DEPLOY_DIR/deploy.sh"
  grep -qF '${SCRIPT_DIR}/.env not found' "$f"
  grep -qF 'network inspect plane_default' "$f"
  grep -qF 'Deploy Plane first' "$f"
  # Verified before anything is pulled or started.
  assert_precedes "$f" '^  step_verify_inputs' '^  step_pull_image'
  assert_precedes "$f" '^  step_pull_image' '^  step_start'
  assert_precedes "$f" '^  step_start' '^  step_wait_running'
  # The container must stay up through a settle window with no restart: RestartCount is compared.
  grep -qF "container_field \"\$name\" '{{.RestartCount}}'" "$f"
  grep -qF 'did not stay running' "$f"
}

@test "plane-mcp: deploy.sh starts through compose_up_if_changed, so an .env change recreates it" {
  grep -qF 'compose_up_if_changed' "$DEPLOY_DIR/deploy.sh"
  refute_grep -qE 'compose (up|pull) -d' "$DEPLOY_DIR/deploy.sh"
}

@test "plane-mcp: deploy.sh applies the publish overlay only outside local mode" {
  local f="$DEPLOY_DIR/deploy.sh" code="$BATS_TEST_TMPDIR/code.sh" block="$BATS_TEST_TMPDIR/publish-block.sh"
  grep -vE '^[[:space:]]*#' "$f" > "$code"
  # Named exactly once in the code, and that one mention sits inside the not-local-mode branch.
  [ "$(grep -c 'compose.publish.yml' "$code")" -eq 1 ]
  sed -n '/LOCAL_MODE:-}" != "true"/,/^fi$/p' "$code" > "$block"
  grep -qF 'export COMPOSE_OVERLAYS="compose.publish.yml' "$block"
}

@test "plane-mcp: AGENTS.md is a symlink to CLAUDE.md, the one source" {
  [ -L "$DEPLOY_DIR/AGENTS.md" ]
  [ "$(readlink "$DEPLOY_DIR/AGENTS.md")" = "CLAUDE.md" ]
}

@test "plane-mcp: the rendered .env is gitignored" {
  grep -qxF '.env' "$DEPLOY_DIR/.gitignore"
}
