#!/usr/bin/env bats
# test_nocodb_templates.bats — Verify NocoDB Jinja2 template renders correctly

TEMPLATE_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/../services/nocodb/deployment/templates" && pwd)"

@test "nocodb.env.j2 exists" {
  [ -f "$TEMPLATE_DIR/nocodb.env.j2" ]
}

@test "nocodb.env.j2 contains POSTGRES_PASSWORD placeholder" {
  grep -q '{{ secrets.nocodb_pg_password }}' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 contains NC_AUTH_JWT_SECRET placeholder" {
  grep -q '{{ secrets.nocodb_jwt_secret }}' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 contains NC_DB connection string with URL-encoded password" {
  grep -q 'NC_DB=pg://postgres:5432' "$TEMPLATE_DIR/nocodb.env.j2"
  grep -q 'urlencode' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 has NC_INVITE_ONLY_SIGNUP" {
  grep -q 'NC_INVITE_ONLY_SIGNUP=true' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 has NC_DISABLE_TELE" {
  grep -q 'NC_DISABLE_TELE=true' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 has NOCODB_ADMIN_PASSWORD for bootstrap" {
  grep -q 'NOCODB_ADMIN_PASSWORD={{ secrets.nocodb_admin_password }}' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 does not contain hardcoded secrets" {
  ! grep -qiE '(password|secret)\s*=\s*[A-Za-z0-9+/]{8,}' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 uses only secrets.* Jinja2 placeholders" {
  local bad_placeholders
  bad_placeholders=$(grep -oE '\{\{[^}]+\}\}' "$TEMPLATE_DIR/nocodb.env.j2" | grep -v 'secrets\.' || true)
  [ -z "$bad_placeholders" ]
}
