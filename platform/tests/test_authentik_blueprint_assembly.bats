#!/usr/bin/env bats
# deploy-authentik assembles blueprints-active/ from the enabled apps. It used to
# delete and recreate the directory every run, so every file re-rendered and the
# play reported changed on a second run with identical inputs (Semaphore task
# 2846: changed=3 while deploy.sh left the containers untouched). These tests run
# the REAL assembly tasks, lifted out of the playbook by name, against a scratch
# dir: a second run changes nothing, a de-selected app's file is removed once and
# then stays converged, and check mode writes nothing.

load assert_helpers

setup() {
  command -v ansible-playbook >/dev/null 2>&1 || skip "ansible-playbook not available"
  DEPLOY="${BATS_TEST_DIRNAME}/../playbooks/deploy-authentik.yml"
  T="$BATS_TEST_TMPDIR"
  mkdir -p "$T/tpl/blueprints" "$T/tpl/templates" "$T/deploy"
  printf 'version: 1\n# shared\n' > "$T/tpl/blueprints/shared.yaml"
  printf 'version: 1\n# app {{ app_marker }}\n' > "$T/tpl/blueprints/app-a.yaml"
  printf 'version: 1\n# app b\n' > "$T/tpl/blueprints/app-b.yaml"
  printf 'version: 1\n{%% for a in authentik_apps %%}# {{ a }}\n{%% endfor %%}' \
    > "$T/tpl/templates/zz-sso-bindings.yaml.j2"
  python3 - "$DEPLOY" "$T/play.yml" <<'PY'
import sys, yaml
plays = yaml.safe_load(open(sys.argv[1]))
tasks = [t for p in plays for t in p.get('tasks', [])]
names = [t.get('name', '') for t in tasks]
start = names.index('Ensure the active-blueprints dir exists')
end = next(i for i, n in enumerate(names)
           if n.startswith('Remove blueprints no longer in the active set'))
play = [{'hosts': 'localhost', 'connection': 'local', 'gather_facts': False,
         'tasks': tasks[start:end + 1]}]
yaml.safe_dump(play, open(sys.argv[2], 'w'), sort_keys=False)
PY
}

# _run <apps-json> [extra ansible args...] -> sets $out
_run() {
  local apps="$1"; shift
  local ev
  ev="{\"_deploy_dir\":\"$T/deploy\",\"_authentik_tpls\":\"$T/tpl\",\"app_marker\":\"m\","
  ev+="\"_shared_blueprints\":[\"shared.yaml\"],\"_enabled_apps\":$apps,"
  ev+="\"authentik_app_catalog\":{\"a\":{\"file\":\"app-a.yaml\"},\"b\":{\"file\":\"app-b.yaml\"}}}"
  out=$(ANSIBLE_STDOUT_CALLBACK=default ansible-playbook -i localhost, "$T/play.yml" \
    -e "$ev" "$@" 2>&1) || { echo "$out"; return 1; }
}

_changed() { grep -oE 'changed=[0-9]+' <<<"$out" | head -1 | cut -d= -f2; }

@test "blueprint assembly: second run with identical inputs reports zero changed" {
  _run '["a","b"]'
  [ "$(_changed)" -gt 0 ] || { echo "$out"; false; }
  _run '["a","b"]'
  [ "$(_changed)" -eq 0 ] || { echo "$out"; false; }
  [ -f "$T/deploy/blueprints-active/app-b.yaml" ]
}

@test "blueprint assembly: de-selected app is removed with one prune, then stable" {
  _run '["a","b"]'
  _run '["a"]'
  [ ! -e "$T/deploy/blueprints-active/app-b.yaml" ]
  [ -f "$T/deploy/blueprints-active/app-a.yaml" ]
  # zz-sso-bindings re-renders (b dropped) + the prune: exactly two changes.
  [ "$(_changed)" -eq 2 ] || { echo "$out"; false; }
  _run '["a"]'
  [ "$(_changed)" -eq 0 ] || { echo "$out"; false; }
}

@test "blueprint assembly: a stray file in the dir is pruned" {
  _run '["a"]'
  printf 'stale\n' > "$T/deploy/blueprints-active/removed-app.yaml"
  _run '["a"]'
  [ ! -e "$T/deploy/blueprints-active/removed-app.yaml" ]
  [ "$(_changed)" -eq 1 ] || { echo "$out"; false; }
}

@test "blueprint assembly: check mode reports the prune but writes nothing" {
  _run '["a","b"]'
  local before; before=$(cd "$T/deploy" && find . -type f -exec shasum {} + | sort)
  _run '["a"]' --check
  [ "$(_changed)" -gt 0 ] || { echo "$out"; false; }
  [ -f "$T/deploy/blueprints-active/app-b.yaml" ]
  [ "$before" = "$(cd "$T/deploy" && find . -type f -exec shasum {} + | sort)" ]
}

@test "blueprint assembly: a hidden stale file is pruned" {
  _run '["a"]'
  printf 'stale\n' > "$T/deploy/blueprints-active/.foo.yaml"
  _run '["a"]'
  [ ! -e "$T/deploy/blueprints-active/.foo.yaml" ]
  [ "$(_changed)" -eq 1 ] || { echo "$out"; false; }
}

@test "blueprint assembly: a stale symlink is pruned, its target untouched" {
  _run '["a"]'
  printf 'outside\n' > "$T/outside.yaml"
  ln -s "$T/outside.yaml" "$T/deploy/blueprints-active/link.yaml"
  _run '["a"]'
  [ ! -L "$T/deploy/blueprints-active/link.yaml" ]
  [ "$(cat "$T/outside.yaml")" = "outside" ]
  _run '["a"]'
  [ "$(_changed)" -eq 0 ] || { echo "$out"; false; }
}

@test "blueprint assembly: first-ever --check (dir absent) succeeds and writes nothing" {
  _run '["a","b"]' --check
  [ ! -e "$T/deploy/blueprints-active" ]
}
