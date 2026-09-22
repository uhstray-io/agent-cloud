#!/usr/bin/env bats
# The commit-msg hook refuses AI attribution (AGENTS.md Git Conventions; docs/MISTAKES.md 5.8).
load assert_helpers

setup() {
  HOOK="$BATS_TEST_DIRNAME/../../.githooks/commit-msg"
  MSG="$BATS_TEST_TMPDIR/msg"
}

@test "commit-msg: a plain message passes" {
  printf 'feat(x): do a thing\n\nBecause reasons.\n' > "$MSG"
  run "$HOOK" "$MSG"
  [ "$status" -eq 0 ]
}

@test "commit-msg: an assistant co-author trailer is refused" {
  printf 'feat(x): do a thing\n\nCo-Authored-By: Claude Opus <noreply@anthropic.com>\n' > "$MSG"
  run "$HOOK" "$MSG"
  [ "$status" -eq 1 ]
}

@test "commit-msg: a session link trailer is refused" {
  printf 'feat(x): do a thing\n\nClaude-Session: https://claude.ai/code/session_x\n' > "$MSG"
  run "$HOOK" "$MSG"
  [ "$status" -eq 1 ]
}

@test "commit-msg: a generated-with footer is refused" {
  printf 'feat(x): do a thing\n\nGenerated with [Claude Code](https://claude.com/claude-code)\n' > "$MSG"
  run "$HOOK" "$MSG"
  [ "$status" -eq 1 ]
}

@test "commit-msg: a human co-author still passes" {
  printf 'feat(x): pair work\n\nCo-Authored-By: A Teammate <teammate@example.com>\n' > "$MSG"
  run "$HOOK" "$MSG"
  [ "$status" -eq 0 ]
}
