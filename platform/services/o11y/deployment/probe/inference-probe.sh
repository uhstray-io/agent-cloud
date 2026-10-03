#!/usr/bin/env bash
# inference-probe.sh — one synthetic chat completion through the public inference
# hostname, recorded as node_exporter textfile metrics.
#
# Author: Joseph A. Wisneski IV <stray@uhstray.io>
#
# Change inference-telemetry-production, task 3.3. `/health` answering proves the
# host is reachable; it does not prove the model serves. This sends one short chat
# completion (reasoning effort `none`) through the same public path every client
# uses (Cloudflare -> Caddy -> gateway/vLLM) and writes:
#
#   inference_probe_success{model_name}                  1 on a 200 with choices, else 0
#   inference_probe_latency_seconds{model_name}          curl's total request time
#   inference_probe_last_run_timestamp_seconds{model_name}  when this sample was taken
#
# A request that exceeds INFERENCE_PROBE_TIMEOUT_SECONDS (the latency budget) is a
# failure. The systemd timer runs it every five minutes; deploy-o11y.yml renders the
# environment file and the units. Every input arrives in the environment
# (systemd EnvironmentFile, mode 0600):
#
#   INFERENCE_PROBE_URL             base URL ending in /v1, https only (inventory)
#   INFERENCE_PROBE_MODEL           served model name (inventory)
#   INFERENCE_PROBE_KEY             API key (OpenBao, rendered at deploy)
#   INFERENCE_PROBE_TIMEOUT_SECONDS latency budget, default 30
#   INFERENCE_PROBE_TEXTFILE_DIR    default /var/lib/node_exporter/textfile
#
# The key never reaches a process argument list: it goes to curl as a config line on
# stdin from the printf builtin. Nothing here prints the key or the response body.
# The metrics file is written to a temporary name in the same directory and renamed,
# so node_exporter never reads a half-written file. Exit status: 0 when a sample was
# recorded (a failed probe is a recorded sample), 1 when no sample could be written.

set -uo pipefail

TEXTFILE_DIR="${INFERENCE_PROBE_TEXTFILE_DIR:-/var/lib/node_exporter/textfile}"
METRICS_FILE="${TEXTFILE_DIR}/inference_probe.prom"
TIMEOUT="${INFERENCE_PROBE_TIMEOUT_SECONDS:-30}"
CURL_BIN="${INFERENCE_PROBE_CURL:-curl}"
JQ_BIN="${INFERENCE_PROBE_JQ:-jq}"

log() { printf 'inference-probe: %s\n' "$*" >&2; }

# A completion is at least one choice that produced something: non-empty message
# content or a finish_reason. `"choices": []`, an error object or a non-JSON page is
# not. jq when the host has it, else python3; neither present is a failed sample.
has_completion() {
  if command -v "$JQ_BIN" >/dev/null 2>&1; then
    "$JQ_BIN" -e 'any((.choices // [])[]?;
        ((.message.content // "") | tostring | length > 0) or
        ((.finish_reason // "") | tostring | length > 0))' "$1" >/dev/null 2>&1
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$1" <<'PY' 2>/dev/null
import json, sys
def produced(choice):
    if not isinstance(choice, dict):
        return False
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return bool(content) or bool(choice.get("finish_reason"))
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        doc = json.load(f)
except (OSError, ValueError):
    sys.exit(1)
choices = doc.get("choices") if isinstance(doc, dict) else None
sys.exit(0 if isinstance(choices, list) and any(map(produced, choices)) else 1)
PY
  else
    log "neither jq nor python3 is available to read the response"
    return 1
  fi
}

# Label values are escaped per the Prometheus text exposition format.
label_escape() {
  local v="$1"
  v="${v//\\/\\\\}"
  v="${v//\"/\\\"}"
  printf '%s' "${v//$'\n'/\\n}"
}

write_metrics() {
  local success="$1" latency="$2" labels tmp
  labels="model_name=\"$(label_escape "${INFERENCE_PROBE_MODEL:-unknown}")\""
  tmp="$(mktemp "${TEXTFILE_DIR}/.inference_probe.prom.XXXXXX")" || {
    log "cannot create a temporary file in ${TEXTFILE_DIR}"
    return 1
  }
  if ! {
    printf '# HELP inference_probe_success 1 if the last synthetic chat completion through the public inference hostname succeeded within its latency budget, else 0.\n'
    printf '# TYPE inference_probe_success gauge\n'
    printf 'inference_probe_success{%s} %s\n' "$labels" "$success"
    printf '# HELP inference_probe_latency_seconds Total request time of the last synthetic chat completion.\n'
    printf '# TYPE inference_probe_latency_seconds gauge\n'
    printf 'inference_probe_latency_seconds{%s} %s\n' "$labels" "$latency"
    printf '# HELP inference_probe_last_run_timestamp_seconds Unix time the last synthetic probe sample was taken.\n'
    printf '# TYPE inference_probe_last_run_timestamp_seconds gauge\n'
    printf 'inference_probe_last_run_timestamp_seconds{%s} %s\n' "$labels" "$(date +%s)"
  } >"$tmp"; then
    rm -f "$tmp"
    log "cannot write ${tmp}"
    return 1
  fi
  # mktemp creates 0600; node_exporter runs as another identity and must read it.
  if ! { chmod 0644 "$tmp" && mv -f "$tmp" "$METRICS_FILE"; }; then
    rm -f "$tmp"
    log "cannot publish ${METRICS_FILE}"
    return 1
  fi
}

record() {
  write_metrics "$1" "$2" || exit 1
  exit 0
}

# --- validate inputs: a misconfiguration is a recorded failure, not silence ---
if [ -z "${INFERENCE_PROBE_URL:-}" ] || [ -z "${INFERENCE_PROBE_MODEL:-}" ] || [ -z "${INFERENCE_PROBE_KEY:-}" ]; then
  log "INFERENCE_PROBE_URL, INFERENCE_PROBE_MODEL and INFERENCE_PROBE_KEY are all required"
  record 0 0
fi
case "$INFERENCE_PROBE_URL" in
  https://*/v1) ;;
  *) log "INFERENCE_PROBE_URL must be an https URL ending in /v1"; record 0 0 ;;
esac
# The key is placed inside a quoted curl config value; refuse anything that could
# end the quote or start a new option.
if ! [[ "$INFERENCE_PROBE_KEY" =~ ^[A-Za-z0-9._~+/=-]+$ ]]; then
  log "INFERENCE_PROBE_KEY contains characters outside the token alphabet"
  record 0 0
fi
if ! [[ "$INFERENCE_PROBE_MODEL" =~ ^[A-Za-z0-9._:/@-]+$ ]]; then
  log "INFERENCE_PROBE_MODEL contains characters outside the model-name alphabet"
  record 0 0
fi
if ! [[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  log "INFERENCE_PROBE_TIMEOUT_SECONDS must be a positive integer"
  record 0 0
fi

body_file="$(mktemp)" || { log "cannot create a response file"; record 0 0; }
trap 'rm -f "$body_file"' EXIT

payload=$(printf '{"model":"%s","messages":[{"role":"user","content":"Reply with one word: pong"}],"max_tokens":16,"reasoning_effort":"none","stream":false}' \
  "$INFERENCE_PROBE_MODEL")

# printf is a shell builtin, so the key is never a process argument. --disable must be
# curl's FIRST argument to take effect: it stops curl reading the user's ~/.curlrc, so
# nothing on the host can add options (a proxy, another header) to this request.
write_out=$(printf 'header = "Authorization: Bearer %s"\n' "$INFERENCE_PROBE_KEY" |
  "$CURL_BIN" --disable --config - \
    --silent --show-error \
    --proto '=https' \
    --max-time "$TIMEOUT" \
    --header 'Content-Type: application/json' \
    --data-binary "$payload" \
    --output "$body_file" \
    --write-out '%{http_code} %{time_total}' \
    "${INFERENCE_PROBE_URL}/chat/completions")
curl_rc=$?

http_code="${write_out%% *}"
latency="${write_out##* }"
[[ "$latency" =~ ^[0-9]+(\.[0-9]+)?$ ]] || latency=0

# A 200 that carries no completion (an error object, empty choices, a challenge page)
# is a failure.
if [ "$curl_rc" -eq 0 ] && [ "$http_code" = "200" ] && has_completion "$body_file"; then
  record 1 "$latency"
fi

log "probe failed: curl exit ${curl_rc}, HTTP ${http_code:-none}, ${latency}s"
record 0 "$latency"
