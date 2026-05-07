#!/usr/bin/env bats
# test_n8n_templates.bats — Verify n8n Jinja2 template renders correctly

TEMPLATE_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/../services/n8n/deployment/templates" && pwd)"

@test "n8n.env.j2 exists" {
  [ -f "$TEMPLATE_DIR/n8n.env.j2" ]
}

@test "n8n.env.j2 contains POSTGRES_PASSWORD placeholder" {
  grep -q '{{ secrets.n8n_admin_password }}' "$TEMPLATE_DIR/n8n.env.j2"
}

@test "n8n.env.j2 contains non-root user password placeholder" {
  grep -q '{{ secrets.n8n_user_password }}' "$TEMPLATE_DIR/n8n.env.j2"
}

@test "n8n.env.j2 contains N8N_ENCRYPTION_KEY placeholder" {
  grep -q '{{ secrets.n8n_encryption_key }}' "$TEMPLATE_DIR/n8n.env.j2"
}

@test "n8n.env.j2 contains DB_POSTGRESDB_PASSWORD for n8n app connection" {
  grep -q 'DB_POSTGRESDB_PASSWORD=' "$TEMPLATE_DIR/n8n.env.j2"
  grep -q '{{ secrets.n8n_user_password }}' "$TEMPLATE_DIR/n8n.env.j2"
}

@test "n8n.env.j2 does not contain hardcoded secrets" {
  ! grep -qiE '(password|secret|key)\s*=\s*[A-Za-z0-9+/]{8,}' "$TEMPLATE_DIR/n8n.env.j2"
}

@test "n8n.env.j2 has POSTGRES_NON_ROOT_USER for init script" {
  grep -q 'POSTGRES_NON_ROOT_USER=' "$TEMPLATE_DIR/n8n.env.j2"
  grep -q 'POSTGRES_NON_ROOT_PASSWORD=' "$TEMPLATE_DIR/n8n.env.j2"
}

@test "n8n.env.j2 has N8N_OWNER_PASSWORD for bootstrap" {
  grep -q 'N8N_OWNER_PASSWORD={{ secrets.n8n_owner_password }}' "$TEMPLATE_DIR/n8n.env.j2"
}

@test "n8n.env.j2 uses only secrets.* Jinja2 placeholders" {
  local bad_placeholders
  bad_placeholders=$(grep -oE '\{\{[^}]+\}\}' "$TEMPLATE_DIR/n8n.env.j2" | grep -v 'secrets\.' || true)
  [ -z "$bad_placeholders" ]
}
