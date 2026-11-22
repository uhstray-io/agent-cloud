#!/usr/bin/env bats
# weft's Semaphore templates: every launchable weft playbook has one, none is scheduled, and none
# speaks to Discord. The playbooks themselves are held by test_service_weft.bats.
#
# Run: bats platform/tests/test_weft_templates.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  TEMPLATES="$REPO_ROOT/platform/semaphore/templates.yml"
}

@test "weft: each weft playbook has a Semaphore template, and none is scheduled" {
  # A person launches every weft deploy (PRINCIPLES.md §4, the AI Invariant): a schedule would be
  # a convergence nobody launched.
  command -v python3 >/dev/null || skip "python3 not installed"
  python3 -c 'import yaml' 2>/dev/null || skip "PyYAML not installed"
  run python3 - "$REPO_ROOT" <<'PY'
import os, sys, yaml
root = sys.argv[1]
templates = yaml.safe_load(open(os.path.join(root, 'platform/semaphore/templates.yml')))['templates']
declared = {t['playbook'] for t in templates}
# weft-door.yml, weft-workspace.yml and weft-edge.yml are the plays the launchers import; nobody
# launches them directly, so they have no template of their own.
imported = {'weft-door.yml', 'weft-workspace.yml', 'weft-edge.yml'}
playbooks = sorted(f for f in os.listdir(os.path.join(root, 'platform/playbooks'))
                   if 'weft' in f and f.endswith('.yml') and f not in imported)
missing = [p for p in playbooks if 'platform/playbooks/' + p not in declared]
scheduled = [t['name'] for t in templates if 'weft' in t['playbook'] and 'schedule' in t]
print('\n'.join(missing + scheduled) or ' '.join(playbooks))
sys.exit(1 if missing or scheduled or len(playbooks) != 6 else 0)
PY
  [ "$status" -eq 0 ] || { echo "missing or scheduled: $output"; false; }
}

@test "weft: the weft templates say nothing of Discord" {
  # Semaphore's task history is the record of a deploy. Other services' templates may name Discord.
  local section
  section=$(sed -n '/^  # ── weft ─/,/^  - name: Deploy Postiz/p' "$TEMPLATES")
  assert_grep -qF 'Deploy weft Staging' <<<"$section"
  refute_grep -qiE 'discord' <<<"$section"
}
