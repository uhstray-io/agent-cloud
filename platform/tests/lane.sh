#!/usr/bin/env bash
# Run one test lane of the Rust suite (plan 24): commit, push, ci or full.
#
# CI calls this script rather than nextest directly, so which tests a lane runs is decided
# here and in the files it reads, never in .github/workflows/lint-and-test.yml (a workflow
# change is a pinned merge-guard transition). Until lane selection lands (plan 24, Phase 2),
# every lane runs the whole suite.
set -euo pipefail
cd "$(dirname "$0")"

lane=${1:?usage: lane.sh commit|push|ci|full}
case "$lane" in
  commit | push | full) profile=default ;;
  ci) profile=ci ;;
  *) echo "lane.sh: unknown lane '$lane' (commit, push, ci or full)" >&2; exit 2 ;;
esac

exec cargo nextest run --locked --profile "$profile"
