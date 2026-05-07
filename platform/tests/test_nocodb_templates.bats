#!/usr/bin/env bats
# test_nocodb_templates.bats — Verify NocoDB Jinja2 template renders correctly

TEMPLATE_DIR="$(cd "$(dirname "$BATS_TEST_FILENAME")/../services/nocodb/deployment/templates" && pwd)"

@test "nocodb.env.j2 exists" {
  [ -f "$TEMPLATE_DIR/nocodb.env.j2" ]
}

@test "nocodb.env.j2 contains POSTGRES_PASSWORD placeholder" {
  grep -q '{{ secrets.postgres_password }}' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 contains NC_AUTH_JWT_SECRET placeholder" {
  grep -q '{{ secrets.jwt_secret }}' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 contains NC_DB connection string with placeholder" {
  grep -q '{{ secrets.postgres_password }}' "$TEMPLATE_DIR/nocodb.env.j2"
  grep -q 'NC_DB=' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 does not contain hardcoded secrets" {
  ! grep -qiE '(password|secret)\s*=\s*[A-Za-z0-9+/]{8,}' "$TEMPLATE_DIR/nocodb.env.j2"
}

@test "nocodb.env.j2 uses only secrets.* Jinja2 placeholders" {
  local bad_placeholders
  bad_placeholders=$(grep -oE '\{\{[^}]+\}\}' "$TEMPLATE_DIR/nocodb.env.j2" | grep -v 'secrets\.' || true)
  [ -z "$bad_placeholders" ]
}
