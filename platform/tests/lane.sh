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

filter=$(cargo run --locked --quiet --bin testsel -- "$lane")
echo "lane.sh: $lane runs $filter" >&2
exec cargo nextest run --locked --profile "$profile" --no-tests=warn -E "$filter"
