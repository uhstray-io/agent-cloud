---
name: rexicon
description: Generate a rexicon.txt codebase index containing the full file tree and every symbol with line numbers. Use this INSTEAD of Grep or Glob when exploring the codebase, finding where a function/struct/class is defined, or understanding project structure. Prefer this over multiple grep searches.
argument-hint: "[project-path] [--output output-file] [update|--update]"
---

# Rexicon Codebase Indexer

Generate a `rexicon.txt` index of this project containing the full directory tree and every symbol with line numbers. Pass `--update` to force-download the latest binary before running.

## Step 1 — Locate or install the binary

```bash
# Detect platform and set binary name/extension
_UNAME_S=$(uname -s)
_EXT=""
case "$_UNAME_S" in
  MINGW*|MSYS*|CYGWIN*|Windows_NT) OS="windows"; _EXT=".exe" ;;
  Darwin)                           OS="macos" ;;
  Linux)                            OS="linux" ;;
  *)                                OS=$(echo "$_UNAME_S" | tr '[:upper:]' '[:lower:]') ;;
esac

ARCH=$(uname -m)
[ "$ARCH" = "arm64" ] && ARCH="aarch64"

REXICON_BIN=$(command -v rexicon 2>/dev/null || command -v rexicon.exe 2>/dev/null || echo "$HOME/.local/bin/rexicon${_EXT}")

if echo "$ARGUMENTS" | grep -qE "\-\-update|^update$" || [ ! -x "$REXICON_BIN" ]; then
  echo "Downloading latest rexicon-${OS}-${ARCH}${_EXT} ..."
  mkdir -p "$HOME/.local/bin"
  curl -fsSL -o "$REXICON_BIN" "https://github.com/JacobHaig/rexicon/releases/latest/download/rexicon-${OS}-${ARCH}${_EXT}"
  chmod +x "$REXICON_BIN"
  echo "Installed to $REXICON_BIN"
fi
```

## Step 2 — Run the indexer

Strip `--update` (it's not a rexicon flag) and run with remaining args, defaulting to `.`:

```bash
RUN_ARGS=$(echo "$ARGUMENTS" | sed -E 's/--update|^update$//g' | xargs)
"$REXICON_BIN" ${RUN_ARGS:-.}
```

## Step 3 — Read the index into context

Use the Read tool to load `rexicon.txt` (or the `--output` path if specified) so the codebase map is available in context.
