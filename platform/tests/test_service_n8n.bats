#!/usr/bin/env bats
# Structural tests for the composable n8n service (platform/services/n8n).
# Verifies the legacy secret-generating deploy.sh path was replaced by the
# composable pattern: env-parameterized compose reading a templated .env,
# container-only deploy.sh (no secret gen / owner setup), an env template that
# pulls the stateful N8N_ENCRYPTION_KEY + DB creds from OpenBao, and the Task-0
# pre-seed playbook for the in-place prod migration.
#
# Run: bats platform/tests/test_service_n8n.bats

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/n8n/deployment"
  PB_DIR="$REPO_ROOT/platform/playbooks"
}

@test "n8n: compose env-parameterizes the image and reads the templated .env" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{N8N_IMAGE' "$f"
  grep -qF 'env_file: .env' "$f"
  ! grep -qF 'env_file: ./config/n8n.env' "$f"
}

@test "n8n: deploy.sh is container-only — no secret gen / owner setup / API key" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ] && [ -x "$f" ]
  grep -q 'common.sh' "$f"
  grep -qE '\bcompose (pull|up)' "$f"
  ! grep -qE 'gen_secret|put_secret|generate_n8n_env|owner/setup|rawApiKey|store_token_in_openbao' "$f"
}

@test "n8n: env template sources the stateful key + DB creds from OpenBao" {
  local f="$DEPLOY_DIR/templates/n8n.env.j2"
  [ -f "$f" ]
  grep -qF 'N8N_ENCRYPTION_KEY={{ secrets.encryption_key }}' "$f"
  grep -qF 'POSTGRES_PASSWORD={{ secrets.db_admin_password }}' "$f"
  grep -qF 'POSTGRES_NON_ROOT_PASSWORD={{ secrets.db_user_password }}' "$f"
  ! grep -qiE 'LOCAL_FAKE|password=[A-Za-z0-9]{8}' "$f"
}

@test "n8n: local overlay adds caps/SELinux/local-dev but does NOT republish ports" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'mem_limit:' "$f"
  grep -q 'label=disable' "$f"
  grep -q 'local-dev' "$f"
  ! grep -qE '^[[:space:]]*ports:' "$f"
}

@test "n8n: deploy playbook is composable (place-monorepo + manage-secrets), not the legacy wrapper" {
  local f="$PB_DIR/deploy-n8n.yml"
  grep -q 'tasks/place-monorepo.yml' "$f"
  grep -q 'tasks/manage-secrets.yml' "$f"
  ! grep -q 'import_playbook: deploy-service.yml' "$f"
  # the three secret definitions incl. the stateful encryption key
  grep -q 'name: encryption_key' "$f"
}

@test "n8n: Task-0 pre-seed playbook exists for the in-place prod migration" {
  local f="$PB_DIR/seed-n8n-secrets.yml"
  [ -f "$f" ]
  grep -q "secret/data/services/n8n" "$f"
  grep -q 'encryption_key' "$f"
}
