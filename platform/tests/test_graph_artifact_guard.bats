#!/usr/bin/env bats
# The graph-artifact commit gate (scripts/check-graph-artifact.sh), replayed in a
# throwaway repo so each case stages exactly the state it is about.
# Origin: 2026-09-23, auto-index rewrote artifact.json under a path-derived project
# name while the graph was deleted (docs/MISTAKES.md 6.6).

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  GUARD="$REPO_ROOT/scripts/check-graph-artifact.sh"
  cd "$BATS_TEST_TMPDIR"
  git init -q repo && cd repo
  git config user.email t@example.invalid && git config user.name t
  mkdir .codebase-memory
  printf 'graph-bytes' > .codebase-memory/graph.db.zst
  write_meta agent-cloud 11
  git add -A && git commit -qm base
}

write_meta() {  # $1 project, $2 compressed_size
  printf '{"schema_version": 2, "project": "%s", "compressed_size": %s}\n' "$1" "$2" \
    > .codebase-memory/artifact.json
}

@test "graph guard: nothing staged under .codebase-memory passes" {
  printf 'x' > other && git add other
  run sh "$GUARD"
  [ "$status" -eq 0 ]
}

@test "graph guard: a consistent regeneration passes" {
  printf 'new-graph-bytes!' > .codebase-memory/graph.db.zst
  write_meta agent-cloud 16
  git add -A
  run sh "$GUARD"
  [ "$status" -eq 0 ]
}

@test "graph guard: refuses the graph staged for deletion (the 2026-09-23 state)" {
  git rm -q --cached .codebase-memory/graph.db.zst
  write_meta Users-stray-Documents-GitHub-agent-cloud 2391196
  git add .codebase-memory/artifact.json
  run sh "$GUARD"
  [ "$status" -ne 0 ]
  assert_contains "$output" "staged for deletion"
}

@test "graph guard: refuses a path-derived project id" {
  write_meta Users-stray-Documents-GitHub-agent-cloud 11
  git add -A
  run sh "$GUARD"
  [ "$status" -ne 0 ]
  assert_contains "$output" "expected 'agent-cloud'"
}

@test "graph guard: refuses metadata that describes a different graph" {
  write_meta agent-cloud 999
  git add -A
  run sh "$GUARD"
  [ "$status" -ne 0 ]
  assert_contains "$output" "compressed_size 999"
}

@test "graph guard: refuses artifact.json that is not JSON" {
  printf 'not json' > .codebase-memory/artifact.json
  git add -A
  run sh "$GUARD"
  [ "$status" -ne 0 ]
  assert_contains "$output" "not valid JSON"
}

@test "graph guard: wired into the pre-commit suite for .codebase-memory changes" {
  local cfg="$REPO_ROOT/.pre-commit-config.yaml"
  assert_grep -qF 'entry: sh scripts/check-graph-artifact.sh' "$cfg"
  assert_grep -qF 'files: ^\.codebase-memory/' "$cfg"
}
