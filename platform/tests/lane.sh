#!/usr/bin/env bash
# Run one test lane of the Rust suite (plan 24): commit, push, ci or full.
#
# CI calls this script rather than nextest directly, so which tests a lane runs is decided
# here and in areas.yml and src/lanes.rs, never in .github/workflows/lint-and-test.yml (a
# workflow change is a pinned merge-guard transition). `testsel` prints the nextest filterset
# for the lane: the staged paths for commit, the paths since origin/$BASE_REF (origin/dev when
# BASE_REF is unset) for push and ci, nothing read from git for full or for ci against main
# (a promotion runs everything).
set -euo pipefail
cd "$(dirname "$0")"

lane=${1:?usage: lane.sh commit|push|ci|full}
case "$lane" in
  commit | push | full) profile=default ;;
  ci) profile=ci ;;
  *) echo "lane.sh: unknown lane '$lane' (commit, push, ci or full)" >&2; exit 2 ;;
esac

# The hooks' lanes skip, with a message, on a workstation without the runner, as the pre-push
# hook does for bats; CI's Rust (tests) job still runs the suite. The ci and full lanes never
# pass without running it.
if ! cargo nextest --version > /dev/null 2>&1; then
  case "$lane" in
    commit | push)
      echo "lane.sh: cargo-nextest is not installed, so the $lane lane did not run (CI runs it)." >&2
      exit 0 ;;
    *) echo "lane.sh: the $lane lane needs cargo and cargo-nextest." >&2; exit 1 ;;
  esac
fi

filter=$(cargo run --locked --quiet --bin testsel -- "$lane")
echo "lane.sh: $lane runs $filter" >&2
# testsel reads the change with the caller's git environment: from a pre-commit hook, git's
# GIT_INDEX_FILE names the index being committed. The tests run without it, as the pre-push
# hook runs its suites: an exported GIT_DIR points every git command a test runs at this
# repository, even from a scratch directory (docs/MISTAKES.md 3.7). The list is the one
# .githooks/pre-push clears.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY \
  GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX GIT_NAMESPACE
exec cargo nextest run --locked --profile "$profile" --no-tests=warn -E "$filter"
