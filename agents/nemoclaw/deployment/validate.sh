#!/usr/bin/env bash
# Validate that the NemoClaw deployment is working end-to-end.
# Runs locally on the server. Called by deploy.sh after every deploy.
#
# Usage:  ./validate.sh  (or ./validate.sh --local)

set -euo pipefail

# Ensure local tools are on PATH (openshell, nemoclaw, node)
export PATH="$HOME/.local/bin:$PATH"
[ -s "$HOME/.nvm/nvm.sh" ] && . "$HOME/.nvm/nvm.sh"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SANDBOX_NAME=$(python3 -c "import json; d=json.load(open('$SCRIPT_DIR/config/sandboxes.json')); print(d['defaultSandbox'])" 2>/dev/null || echo "uhstray-io-assistant")

# ── Helpers ─────────────────────────────────────────────────────────

PASS=0
FAIL=0
TOTAL=0

check() {
  local name="$1" cmd="$2"
  TOTAL=$((TOTAL + 1))
  if (set +o pipefail; eval "$cmd") >/dev/null 2>&1; then
    PASS=$((PASS + 1))
    echo "  ✓ $name"
  else
    FAIL=$((FAIL + 1))
    echo "  ✗ $name"
  fi
}

sandbox() {
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o GlobalKnownHostsFile=/dev/null -o LogLevel=ERROR \
    -o "ProxyCommand=openshell ssh-proxy --gateway-name nemoclaw --name $SANDBOX_NAME" \
    "sandbox@openshell-$SANDBOX_NAME" "$1" 2>/dev/null
}

echo ""
echo "=== NemoClaw Deployment Validation ==="
echo "    Sandbox: $SANDBOX_NAME"
echo ""

# ── 1. Infrastructure ───────────────────────────────────────────────
echo "--- Infrastructure ---"

check "Docker running" \
  'docker ps --format "{{.Names}}" | grep -q openshell-cluster'

check "Gateway healthy" \
  'openshell status 2>&1 | grep -q Connected'

check "Sandbox exists and Ready" \
  "openshell sandbox list 2>&1 | grep -q '$SANDBOX_NAME'"

check "NemoClaw source is uhstray-io fork" \
  'cd ~/.nemoclaw/source && git remote get-url origin 2>/dev/null | grep -q uhstray-io'

# ── 2. DNS ──────────────────────────────────────────────────────────
echo ""
echo "--- DNS Resolution ---"

check "DNS proxy running in pod" \
  "docker exec openshell-cluster-nemoclaw kubectl exec -n openshell $SANDBOX_NAME -- cat /tmp/dns-proxy.log 2>/dev/null | grep -q dns-proxy"

check "DNS: sandbox resolves google.com" \
  'sandbox "python3 -c \"import socket; socket.getaddrinfo(\\\"google.com\\\", 443, type=socket.SOCK_STREAM)\""'

check "DNS: sandbox resolves googleapis.com" \
  'sandbox "python3 -c \"import socket; socket.getaddrinfo(\\\"generativelanguage.googleapis.com\\\", 443, type=socket.SOCK_STREAM)\""'

# ── 3. Inference ────────────────────────────────────────────────────
echo ""
echo "--- Inference ---"

# Clean stale session locks
sandbox 'rm -f /sandbox/.openclaw-data/agents/main/sessions/*.lock 2>/dev/null' || true

check "Inference provider configured" \
  'openshell inference get 2>&1 | grep -qi nvidia'

check "Inference reachable from sandbox" \
  'sandbox "curl -sf https://inference.local/v1/models --insecure" | grep -qv "not configured"'

VALIDATE_SESSION="val$(date +%s)"

check "Agent responds to prompt" \
  "sandbox 'cd /sandbox && set -a && source /sandbox/.env 2>/dev/null && set +a && openclaw agent --agent main --local -m \"Say hello\" --session-id ${VALIDATE_SESSION} 2>&1 | grep -qiE hello'"

# ── 4. Web Search ───────────────────────────────────────────────────
echo ""
echo "--- Web Search ---"

check "GEMINI_API_KEY present in sandbox" \
  'sandbox "test -s /sandbox/.env && grep -q GEMINI_API_KEY /sandbox/.env"'

check "Google preset enabled" \
  "nemoclaw $SANDBOX_NAME policy-list 2>&1 | grep -qE '●.*google'"

check "Web search returns results" \
  "sandbox 'cd /sandbox && set -a && source /sandbox/.env 2>/dev/null && set +a && openclaw agent --agent main --local -m \"Use web search to find the current date\" --session-id ${VALIDATE_SESSION}ws 2>&1 | grep -qiE \"202[4-9]|date|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday\"'"

# ── 5. Policies ─────────────────────────────────────────────────────
echo ""
echo "--- Policies ---"

check "At least 5 presets enabled" \
  "test \$(nemoclaw $SANDBOX_NAME policy-list 2>&1 | grep -c '●') -ge 5"

# ── Summary ─────────────────────────────────────────────────────────
echo ""
echo "=== Results: $PASS/$TOTAL passed, $FAIL failed ==="
echo ""

if [ "$FAIL" -gt 0 ]; then
  echo "VALIDATION FAILED — do NOT push to GitHub until all checks pass."
  exit 1
else
  echo "ALL CHECKS PASSED — safe to push."
  exit 0
fi
