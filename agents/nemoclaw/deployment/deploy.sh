#!/usr/bin/env bash
# Deploy NemoClaw — sync config and run locally.
#
# Default: update/migrate (preserves sandbox state).
# --onboard: destructive fresh install via NemoClaw's install.sh.
#
# Usage:
#   ./deploy.sh                 Update (SCP to server, run locally)
#   ./deploy.sh --onboard       Fresh install via install.sh
#   ./deploy.sh --local         Already on the machine — skip SCP/SSH
#   ./deploy.sh --local --onboard
#
# Site-specific config (HOST, REMOTE_USER, SSH_KEY) must be set via
# environment variables or a .env file. See .env.example.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Config (from environment — no hardcoded values) ────────────────
HOST="${NEMOCLAW_HOST:?Set NEMOCLAW_HOST (target VM IP)}"
REMOTE_USER="${NEMOCLAW_USER:?Set NEMOCLAW_USER (SSH user)}"
SSH_KEY="${NEMOCLAW_SSH_KEY:-$HOME/.ssh/nemoclaw}"
REMOTE_DIR="${NEMOCLAW_DEPLOY_DIR:-nemoclaw-deploy}"
NEMOCLAW_REPO="${NEMOCLAW_REPO:-https://github.com/uhstray-io/NemoClaw}"
SECRETS_DIR="${NEMOCLAW_SECRETS_DIR:-$SCRIPT_DIR/secrets}"

# ── Parse arguments ─────────────────────────────────────────────────
LOCAL_MODE=false
DO_ONBOARD=false
for arg in "$@"; do
  case "$arg" in
    --local) LOCAL_MODE=true ;;
    --onboard) DO_ONBOARD=true ;;
  esac
done

# ── Remote mode: SCP + SSH once ─────────────────────────────────────
if ! $LOCAL_MODE; then
  echo "==> Syncing to $REMOTE_USER@$HOST:~/$REMOTE_DIR/..."
  rsync -az --delete \
    --exclude='NemoClaw/' --exclude='node_modules/' --exclude='.claude/' \
    -e "ssh -i $SSH_KEY" \
    "$SCRIPT_DIR/" "$REMOTE_USER@$HOST:~/$REMOTE_DIR/"

  FORWARD_ARGS="--local"
  $DO_ONBOARD && FORWARD_ARGS="$FORWARD_ARGS --onboard"

  echo "==> Running on server..."
  ssh -i "$SSH_KEY" "$REMOTE_USER@$HOST" \
    "export PATH=\$HOME/.local/bin:\$PATH; source ~/.nvm/nvm.sh 2>/dev/null; cd ~/$REMOTE_DIR && bash deploy.sh $FORWARD_ARGS"
  exit $?
fi

# ═══════════════════════════════════════════════════════════════════
# LOCAL MODE — everything below runs directly on the machine
# ═══════════════════════════════════════════════════════════════════

export PATH="$HOME/.local/bin:$PATH"
[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"

read -r SANDBOX_NAME POLICIES < <(python3 -c "
import json; d=json.load(open('$SCRIPT_DIR/config/sandboxes.json'))
n=d['defaultSandbox']; print(n, ','.join(d['sandboxes'][n]['policies']))
")

# Load secrets into env vars (NemoClaw reads these during onboard)
[ -f "$SECRETS_DIR/nvidia-api-key.txt" ] && export NVIDIA_API_KEY=$(cat "$SECRETS_DIR/nvidia-api-key.txt" | tr -d '[:space:]')
[ -f "$SECRETS_DIR/gemini-api-key.txt" ] && export GEMINI_API_KEY=$(cat "$SECRETS_DIR/gemini-api-key.txt" | tr -d '[:space:]')
[ -f "$SECRETS_DIR/discord-bot-token.txt" ] && export DISCORD_BOT_TOKEN=$(cat "$SECRETS_DIR/discord-bot-token.txt" | tr -d '[:space:]')

# Helper: run inside the sandbox
run_in_sandbox() {
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o GlobalKnownHostsFile=/dev/null -o LogLevel=ERROR \
    -o "ProxyCommand=openshell ssh-proxy --gateway-name nemoclaw --name $SANDBOX_NAME" \
    "sandbox@openshell-$SANDBOX_NAME" "$1"
}

# Helper: build .env content from loaded secrets
build_env_file() {
  local content=""
  [ -n "${GEMINI_API_KEY:-}" ] && content="${content}GEMINI_API_KEY=$GEMINI_API_KEY\n"
  [ -n "${DISCORD_BOT_TOKEN:-}" ] && content="${content}DISCORD_BOT_TOKEN=$DISCORD_BOT_TOKEN\n"
  printf '%s' "$content"
}

# ── Step 1: Sync NemoClaw source ────────────────────────────────────
echo "==> [1/5] Syncing NemoClaw source..."

if [ ! -d "$HOME/.nemoclaw/source/.git" ]; then
  echo "    Cloning fork..."
  rm -rf "$HOME/.nemoclaw/source"
  git clone "$NEMOCLAW_REPO" "$HOME/.nemoclaw/source"
else
  cd "$HOME/.nemoclaw/source"
  git remote set-url origin "$NEMOCLAW_REPO" 2>/dev/null || true
  BEHIND=$(git fetch origin 2>/dev/null && git rev-list --count HEAD..origin/main 2>/dev/null || echo "0")
  if [ "$BEHIND" -gt 0 ] 2>/dev/null; then
    echo "    $BEHIND commits behind. Pulling..."
    git stash -q 2>/dev/null || true
    git pull origin main -q 2>/dev/null
    npm install --ignore-scripts -q 2>/dev/null
    echo "    $(git log --oneline -1)"
  else
    echo "    Up to date."
  fi
  cd "$SCRIPT_DIR"
fi

# ── Step 2: Sync config files ──────────────────────────────────────
echo "==> [2/5] Syncing config..."
cp "$SCRIPT_DIR/config/credentials.json" ~/.nemoclaw/credentials.json 2>/dev/null || true

mkdir -p ~/.nemoclaw/source/nemoclaw-blueprint/policies/presets
for preset in "$SCRIPT_DIR"/config/presets/*.yaml; do
  [ -f "$preset" ] && cp "$preset" ~/.nemoclaw/source/nemoclaw-blueprint/policies/presets/
done

mkdir -p ~/.nemoclaw/source/nemoclaw-blueprint/config
for cfg in "$SCRIPT_DIR"/config/*.json; do
  [ -f "$cfg" ] && [ "$(basename "$cfg")" != "credentials.json" ] && \
    cp "$cfg" ~/.nemoclaw/source/nemoclaw-blueprint/config/
done
echo "    Done."

# ── Branch: onboard vs update ───────────────────────────────────────
if $DO_ONBOARD; then
  echo "==> [3/5] Running NemoClaw install + onboard (DESTRUCTIVE)..."
  echo "    Sandbox: $SANDBOX_NAME"
  echo "    Policies: $POLICIES"

  export NEMOCLAW_NON_INTERACTIVE=1
  export NEMOCLAW_SANDBOX_NAME="$SANDBOX_NAME"
  export NEMOCLAW_POLICY_MODE=custom
  export NEMOCLAW_POLICY_PRESETS="$POLICIES"
  export NEMOCLAW_RECREATE_SANDBOX=1

  cd "$HOME/.nemoclaw/source"
  bash install.sh --non-interactive
  cd "$SCRIPT_DIR"

  echo "==> [4/5] Post-onboard setup..."
  echo "    Setting up DNS proxy..."
  bash ~/.nemoclaw/source/scripts/setup-dns-proxy.sh nemoclaw "$SANDBOX_NAME" 2>&1 || true

  echo "    Injecting env vars..."
  ENV_CONTENT=$(build_env_file)
  [ -n "$ENV_CONTENT" ] && run_in_sandbox "printf '%s' '$ENV_CONTENT' > /sandbox/.env && chmod 600 /sandbox/.env" || true

  echo "    Restarting daemon..."
  run_in_sandbox 'pkill -f openclaw 2>/dev/null || true' || true
  sleep 2
  run_in_sandbox 'rm -f /tmp/openclaw-*/gateway.*.lock; cd /sandbox && set -a && source .env 2>/dev/null && set +a && nohup nemoclaw-start > /tmp/gateway.log 2>&1 &' || true
  sleep 15

else
  echo "==> [3/5] Injecting env vars..."
  ENV_CONTENT=$(build_env_file)
  [ -n "$ENV_CONTENT" ] && run_in_sandbox "printf '$ENV_CONTENT' > /sandbox/.env && chmod 600 /sandbox/.env" 2>/dev/null \
    || echo "    WARNING: Could not write .env (sandbox may not be running)"

  echo "==> [4/5] Running openclaw doctor..."
  run_in_sandbox 'openclaw doctor --fix 2>&1 | tail -5' 2>/dev/null \
    || echo "    Doctor completed with warnings or sandbox not available."

  echo "    Re-applying DNS proxy..."
  bash ~/.nemoclaw/source/scripts/setup-dns-proxy.sh nemoclaw "$SANDBOX_NAME" 2>&1 \
    || echo "    DNS proxy setup completed with warnings."
fi

# ── Validate ────────────────────────────────────────────────────────
echo "==> [5/5] Validating..."
bash "$SCRIPT_DIR/validate.sh" --local

echo ""
echo "==> Deploy complete. Connect with:"
echo "    nemoclaw $SANDBOX_NAME connect"
echo ""
