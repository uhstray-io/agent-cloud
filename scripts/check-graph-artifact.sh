#!/usr/bin/env sh
# check-graph-artifact.sh — refuse a commit whose codebase-memory graph artifact is
# inconsistent. Reads the git INDEX, so it judges exactly what would be committed.
#
# Why: codebase-memory-mcp's auto-index names a project after the checkout path
# ("Users-...-agent-cloud", "...-agent-cloud-check-mode-standard") and on
# 2026-09-23 rewrote .codebase-memory/artifact.json under that name while the
# graph it describes was deleted from the working tree. Committed, that ships
# metadata with no graph, under a project ID no documented query uses
# (AGENTS.md "Memory & specs": project `agent-cloud`).
#
# Refuses, only when a file under .codebase-memory/ is staged:
#   1. graph.db.zst staged for deletion, or absent from the index
#   2. artifact.json whose "project" is not the documented ID
#   3. artifact.json whose "compressed_size" differs from the staged graph's size
#      (the metadata describes some other graph)
#
# To regenerate correctly: index_repository with name="agent-cloud" and
# persistence=true, then stage BOTH files together.
set -eu

EXPECTED_PROJECT="${GRAPH_ARTIFACT_PROJECT:-agent-cloud}"
ART=.codebase-memory/artifact.json
GRAPH=.codebase-memory/graph.db.zst

git diff --cached --quiet -- .codebase-memory && exit 0

fail() {
  printf 'COMMIT BLOCKED: %s\n' "$1" >&2
  printf '  Regenerate with index_repository name="%s" persistence=true and stage both\n' "$EXPECTED_PROJECT" >&2
  printf '  files, or drop the change: git restore --staged --worktree .codebase-memory\n' >&2
  exit 1
}

if git diff --cached --name-only --diff-filter=D -- "$GRAPH" | grep -q .; then
  fail "$GRAPH is staged for deletion; the committed graph would vanish."
fi
git cat-file -e ":$GRAPH" 2>/dev/null || fail "$GRAPH is not in the index."
git cat-file -e ":$ART" 2>/dev/null || fail "$ART is not in the index."

graph_size=$(git cat-file -s ":$GRAPH")
if ! msg=$(git show ":$ART" | python3 -c '
import json, sys
expected, size = sys.argv[1], int(sys.argv[2])
try:
    meta = json.load(sys.stdin)
except ValueError:
    sys.exit("artifact.json is not valid JSON.")
project, recorded = meta.get("project"), meta.get("compressed_size")
if project != expected:
    sys.exit(f"artifact.json project is {project!r}, expected {expected!r}.")
if recorded != size:
    sys.exit(f"artifact.json says compressed_size {recorded}, staged graph is {size} bytes.")
' "$EXPECTED_PROJECT" "$graph_size" 2>&1); then
  fail "$msg"
fi
exit 0
