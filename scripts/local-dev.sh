#!/usr/bin/env bash
# local-dev.sh — local-dev wrapper (plan/development/LOCAL-DEV-DEPLOYMENT.md).
#
# "make bootstraps, Semaphore operates": this wrapper only provisions initial
# resources and talks to the LOCAL Semaphore API afterwards. It structurally
# refuses anything non-local (inventory hosts, OpenBao address) — laptop→prod
# accidents are blocked here AND by tasks/assert-orchestrated.yml.
#
# Subcommands: preflight | init | guard | bootstrap | deploy <service> |
#              validate | clean | promote

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INV="${REPO_ROOT}/platform/inventory/local-dev.yml"
EXAMPLE="${INV}.example"
STATE="${HOME}/.agent-cloud-local/credentials.env"

info() { printf '[local-dev] %s\n' "$*"; }
die()  { printf '[local-dev] ERROR: %s\n' "$*" >&2; exit 1; }

preflight() {
  local missing=0 tool
  for tool in podman ansible-playbook ansible-inventory python3 curl git; do
    command -v "$tool" >/dev/null 2>&1 || { info "missing tool: $tool"; missing=1; }
  done
  [ "$missing" -eq 0 ] || die "install the toolchain first: brew bundle (repo root)"
  podman machine inspect --format '{{.State}}' 2>/dev/null | grep -q running \
    || die "podman machine is not running — run: podman machine start"
  info "preflight OK"
}

guard() {
  # Refuse any inventory that could reach beyond this machine.
  local file="${1:-$INV}"
  [ -f "$file" ] || die "inventory not found: $file (run: make local-init)"
  ansible-inventory -i "$file" --list > /tmp/agent-cloud-local-inv.json 2>/dev/null \
    || die "inventory does not parse: $file"
  python3 - "$file" <<'PY' || exit 1
import json, sys
data = json.load(open("/tmp/agent-cloud-local-inv.json"))
hv = data.get("_meta", {}).get("hostvars", {})
LOCAL = {"127.0.0.1", "localhost", "::1"}
bad = []
for host, v in hv.items():
    conn = v.get("ansible_connection", "")
    addr = str(v.get("ansible_host", host))
    if conn != "local" and addr not in LOCAL:
        bad.append(f"{host} (ansible_host={addr}, connection={conn or 'ssh'})")
    bao = str(v.get("openbao_addr", "http://127.0.0.1:8200"))
    if not (bao.startswith("http://127.0.0.1") or bao.startswith("http://localhost")
            or bao.startswith("http://local-openbao")):
        bad.append(f"{host} (openbao_addr={bao})")
if bad:
    print("[local-dev] ERROR: REFUSING non-local inventory entries:", file=sys.stderr)
    for b in bad:
        print(f"[local-dev]   - {b}", file=sys.stderr)
    sys.exit(1)
print(f"[local-dev] guard OK: {len(hv)} host(s), all local-only")
PY
}

init() {
  preflight
  if [ -f "$INV" ]; then
    info "working inventory already exists: $INV"
  else
    sed -e "s|__REPO_DIR__|${REPO_ROOT}|g" -e "s|__HOME_DIR__|${HOME}|g" \
      "$EXAMPLE" > "$INV"
    info "created $INV from example"
  fi
  guard "$INV"
}

bootstrap() {
  preflight
  guard "$INV"
  ansible-playbook -i "$INV" \
    "${REPO_ROOT}/platform/playbooks/bootstrap-local-dev.yml" --tags bootstrap
}

_load_state() {
  [ -f "$STATE" ] || die "no local state ($STATE) — run: make local-bootstrap"
  set -a
  # shellcheck source=/dev/null
  source "$STATE"
  set +a
}

_api() { curl -sf -H "Authorization: Bearer ${SEMAPHORE_TOKEN}" "$@"; }

# _run_template <playbook-rel-path> [extra-vars-json]
_run_template() {
  local playbook="$1" extra="${2:-}"
  _load_state
  local base="${SEMAPHORE_URL}/api/project/${SEMAPHORE_PROJECT_ID}"
  local tid
  tid=$(_api "${base}/templates" | python3 -c "
import json, sys
ts = json.load(sys.stdin)
m = [t for t in ts if t.get('playbook') == '$playbook']
print(m[0]['id'] if m else '')")
  [ -n "$tid" ] || die "no template registered for playbook: $playbook"
  local body="{\"template_id\": ${tid}, \"project_id\": ${SEMAPHORE_PROJECT_ID}"
  [ -n "$extra" ] && body="${body}, \"environment\": $(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$extra")"
  body="${body}}"
  local task
  task=$(curl -sf -X POST -H "Authorization: Bearer ${SEMAPHORE_TOKEN}" \
    -H "Content-Type: application/json" -d "$body" "${base}/tasks" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
  info "dispatched task ${task} (template ${tid}: ${playbook}) — watching..."
  local status="waiting" i=0
  while [ $i -lt 450 ]; do
    status=$(_api "${base}/tasks/${task}" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
    case "$status" in success|error|stopped) break ;; esac
    sleep 4; i=$((i + 1))
  done
  _api "${base}/tasks/${task}/output" \
    | python3 -c "import json,sys; [print(l['output']) for l in json.load(sys.stdin)]" | tail -40
  info "task ${task}: ${status}"
  [ "$status" = "success" ]
}

deploy() {
  local svc="${1:-}"
  [ -n "$svc" ] || die "usage: local-dev.sh deploy <service>"
  guard "$INV"
  _run_template "platform/playbooks/deploy-${svc}.yml"
}

validate() {
  guard "$INV"
  _run_template "platform/playbooks/validate-all.yml"
}

clean() {
  info "removing local control plane containers + state..."
  podman rm -f local-openbao local-semaphore 2>/dev/null || true
  podman volume rm -f local-semaphore-data 2>/dev/null || true
  rm -f "$STATE"
  info "clean. Re-create with: make local-bootstrap"
}

resolver() {
  # Wire macOS split-DNS at the configured dev zone to the local hickory-dns
  # (127.0.0.1:<dns_port>). Opt-in + interactive: it is the only sudo step in
  # the local story, so it is never folded into `init`/`bootstrap`.
  guard "$INV"
  command -v ansible-inventory >/dev/null || die "ansible-inventory not found"
  ansible-inventory -i "$INV" --host dns-local > /tmp/agent-cloud-dns-host.json 2>/dev/null \
    || die "no dns-local host in $INV — add the dns_svc group (see the example)"
  local zone port
  zone=$(python3 -c "import json;print(json.load(open('/tmp/agent-cloud-dns-host.json')).get('dns_zone',''))")
  port=$(python3 -c "import json;print(json.load(open('/tmp/agent-cloud-dns-host.json')).get('dns_port','5300'))")
  [ -n "$zone" ] || die "dns_zone is not set on dns-local"
  local target="/etc/resolver/${zone}"
  info "About to write ${target} (needs sudo):"
  printf '    nameserver 127.0.0.1\n    port %s\n' "$port"
  printf '[local-dev] proceed? [y/N] '
  local ans; read -r ans
  case "$ans" in
    y|Y|yes) ;;
    *) info "skipped — test manually with: dig @127.0.0.1 -p ${port} foo.${zone}"; return 0 ;;
  esac
  sudo mkdir -p /etc/resolver
  printf 'nameserver 127.0.0.1\nport %s\n' "$port" | sudo tee "$target" >/dev/null
  info "wrote ${target} — *.${zone} now resolves via local DNS"
  info "verify: dscutil --dns | grep -A2 ${zone}  ||  dig foo.${zone}"
}

promote() {
  info "running fast pre-push checks..."
  command -v shellcheck >/dev/null && shellcheck -S warning "${REPO_ROOT}"/scripts/*.sh "${REPO_ROOT}"/platform/lib/*.sh
  command -v yamllint >/dev/null && yamllint -c "${REPO_ROOT}/.yamllint.yml" "${REPO_ROOT}/platform/playbooks" "${REPO_ROOT}/platform/semaphore"
  local branch
  branch=$(git -C "$REPO_ROOT" branch --show-current)
  case "$branch" in
    main|dev) die "refusing to promote from ${branch} — work on a feature branch" ;;
  esac
  info "pushing ${branch} and opening PR into dev..."
  git -C "$REPO_ROOT" push -u origin "$branch"
  gh pr create --base dev --fill
}

case "${1:-}" in
  preflight) preflight ;;
  guard)     guard "${2:-$INV}" ;;
  init)      init ;;
  bootstrap) bootstrap ;;
  deploy)    shift; deploy "$@" ;;
  validate)  validate ;;
  resolver)  resolver ;;
  clean)     clean ;;
  promote)   promote ;;
  *) cat <<EOF
usage: scripts/local-dev.sh <subcommand>
  preflight          verify toolchain + podman machine
  init               create working inventory from the committed example
  guard [file]       refuse non-local inventories (used by every subcommand)
  bootstrap          stand up local OpenBao + Semaphore + templates
  deploy <service>   run the service's deploy template via LOCAL Semaphore
  validate           run Validate All via LOCAL Semaphore
  resolver           wire macOS /etc/resolver/<zone> to the local DNS (sudo)
  clean              remove local control plane (containers, volume, state)
  promote            fast checks, push feature branch, open PR into dev
EOF
     exit 2 ;;
esac
