#!/usr/bin/env bats
# Tests that Django shell commands in playbooks render valid Python.
# Catches Jinja2/f-string/heredoc conflicts before deployment.

setup() {
  PLAYBOOKS_DIR="$BATS_TEST_DIRNAME/../playbooks"
}

@test "cleanup-netbox: all heredocs are single-quoted and at least one exists" {
  local file="$PLAYBOOKS_DIR/cleanup-netbox.yml"
  [ -f "$file" ]

  # Unquoted heredocs let bash expand $vars inside Python f-strings
  ! grep -E "<<\s+PYSCRIPT\b" "$file"

  # At least one quoted heredoc exists
  grep -q "<< 'PYSCRIPT'" "$file"
}
