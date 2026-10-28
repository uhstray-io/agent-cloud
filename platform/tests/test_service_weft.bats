#!/usr/bin/env bats
# Structural tests for the weft service (platform/services/weft) and its playbooks. weft is the
# first service here deployed from signed images by digest, with a backup before every deploy and
# an automatic rollback, so most of these guard one of those properties rather than a file's shape.
#
# Structural only (parsed YAML and scoped greps) — no live deploy.
# Run: bats platform/tests/test_service_weft.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/weft/deployment"
  PB="$REPO_ROOT/platform/playbooks"
}

# The services of one compose file, with each one's volume sources, as "<service> <source>" lines.
volume_sources() {
  python3 - "$1" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
for name, svc in doc['services'].items():
    for v in (svc or {}).get('volumes', []) or []:
        source = v.split(':', 1)[0] if isinstance(v, str) else v.get('source', '')
        print(name, source)
PY
}

@test "weft: only_the_workspace_containers_mount_the_workspace_directory" {
  # The workspace directory holds the catalog, its seal and the checkouts. D537: the catalog is
  # opened only by the board, the orchestrator and capture, all on one kernel; the agent and
  # egress-proxy containers the orchestrator starts never get it (least privilege: they hold a
  # credential or run an agent), and the front door, which spans companies, never sees any
  # workspace's data. A fourth service mounting it, or the door mounting it, is the leak.
  command -v python3 >/dev/null || skip "python3 not installed"
  python3 -c 'import yaml' 2>/dev/null || skip "PyYAML not installed"
  local ws door
  ws=$(volume_sources "$DEPLOY_DIR/compose.workspace.yml" | awk '$2 == "${WEFT_WORKSPACE_DIR}" {print $1}' | sort | tr '\n' ' ')
  [ "$ws" = "board capture orchestrator " ] || { echo "workspace dir mounted by: $ws"; false; }
  door=$(volume_sources "$DEPLOY_DIR/compose.door.yml")
  refute_grep -qF 'WEFT_WORKSPACE_DIR' <<<"$door"
  # Every service of the workspace stack is one of the three: a service added later is reviewed
  # against this list, not waved through.
  local services
  services=$(python3 -c 'import sys,yaml; print(" ".join(sorted(yaml.safe_load(open(sys.argv[1]))["services"])))' \
    "$DEPLOY_DIR/compose.workspace.yml")
  [ "$services" = "board capture orchestrator" ]
  # The one-off containers deploy.sh runs (backup, restore, health) are the workspace image's own,
  # and nothing in the playbooks starts another container with the directory mounted.
  refute_grep -rqE -- '-v[ ,"]+[^ ]*(_weft_workspace_dir|WEFT_WORKSPACE_DIR)' "$PB"/*weft*.yml "$PB"/tasks/weft-*.yml
}

@test "weft: only the orchestrator gets the podman socket, never the board" {
  # The socket lets a container do anything the workspace user can on the VM. The orchestrator
  # needs it to start agent containers; the board is the internet-facing process, and with the
  # socket a compromised board could start a container with any mount.
  command -v python3 >/dev/null || skip "python3 not installed"
  python3 -c 'import yaml' 2>/dev/null || skip "PyYAML not installed"
  local holders
  holders=$(volume_sources "$DEPLOY_DIR/compose.workspace.yml" | awk '$2 == "${WEFT_PODMAN_SOCKET}" {print $1}' | tr '\n' ' ')
  [ "$holders" = "orchestrator " ] || { echo "socket mounted by: $holders"; false; }
  refute_grep -qF 'podman.sock' "$DEPLOY_DIR/compose.door.yml"
}

@test "weft: deploy_sh_never_touches_openbao" {
  # Critical Deployment Rule 2: deploy.sh is container lifecycle only. The credential boundary
  # is Ansible: OpenBao -> memory -> template -> file. A deploy script that could reach OpenBao
  # is one whose compromise reads every secret its token can.
  local f="$DEPLOY_DIR/deploy.sh" code
  [ -x "$f" ]
  # The code, not its comments: the header says in words that it never reads OpenBao.
  code=$(grep -vE '^\s*#' "$f")
  refute_grep -qiE 'openbao|vault|\bbao[ _-]|bao-client|secret_id|role_id|X-Vault-Token|/v1/(secret|auth)' <<<"$code"
  refute_grep -qE 'gen_secret|put_secret|get_secret|store_token_in_openbao|openssl rand' <<<"$code"
  # It reads only the files Ansible rendered, and never sources one into its environment.
  refute_grep -qE '^\s*(source|\.)\s+.*(\.env|secrets\.env|door\.key)' <<<"$code"
}

@test "weft: every image runs by digest, never by tag" {
  # Signatures are checked on digests. A tag can be moved after the check, so an image pulled by
  # tag is not the image that was verified. deploy.sh refuses a non-digest reference, the
  # compose files take the image only from .env, and .env takes it only from release.json.
  assert_grep -qF 'require_digest' "$DEPLOY_DIR/deploy.sh"
  assert_grep -qE '@sha256:\[0-9a-f\]\{64\}' "$DEPLOY_DIR/deploy.sh"
  assert_grep -qE '^\s+image: \$\{WEFT_WORKSPACE_IMAGE\}$' "$DEPLOY_DIR/compose.workspace.yml"
  assert_grep -qE '^\s+image: \$\{WEFT_DOOR_IMAGE\}$' "$DEPLOY_DIR/compose.door.yml"
  assert_grep -qF "_weft_images['weft-workspace']" "$DEPLOY_DIR/templates/workspace.env.j2"
  assert_grep -qF "_weft_images['weft-door']" "$DEPLOY_DIR/templates/door.env.j2"
  refute_grep -rqE ':latest|localhost/weft' "$DEPLOY_DIR/templates" "$DEPLOY_DIR"/compose.*.yml
  # The agent images weft.toml names come from the same release.json.
  assert_grep -qF "images['weft-builder']" "$DEPLOY_DIR/templates/weft.toml.j2"
  assert_grep -qF "images['weft-proxy']" "$DEPLOY_DIR/templates/weft.toml.j2"
}

@test "weft: only images signed by weft's images.yml on main are accepted" {
  # An image built from a branch carries that branch's identity. The check is an exact identity
  # (never --certificate-identity-regexp, which a branch name could satisfy) and an exact issuer.
  local cosign="$PB/tasks/verify-cosign.yml"
  assert_grep -qF -- "'--certificate-identity', _cosign_identity" "$cosign"
  assert_grep -qF -- "'--certificate-oidc-issuer', _cosign_issuer" "$cosign"
  refute_grep -qF 'regexp' "$cosign"
  # References must be digests before anything is checked.
  assert_grep -qF "reject('match', '^[^@\\\\s]+@sha256:[0-9a-f]{64}\$')" "$cosign"
  # The pinned signer is images.yml on refs/heads/main, and release.json must say the same.
  assert_grep -qF "/.github/workflows/images.yml@refs/heads/main" "$PB/vars/weft.yml"
  assert_grep -qF "_weft_release_json.signed_by.identity == _weft_signer" "$PB/tasks/weft-release.yml"
  # release.json is verified before it is read.
  local verify read
  verify=$(grep -n 'name: "Verify release.json was signed' "$PB/tasks/weft-release.yml" | cut -d: -f1)
  read=$(grep -n 'name: "Read release.json"' "$PB/tasks/weft-release.yml" | cut -d: -f1)
  [ -n "$verify" ] && [ -n "$read" ] && [ "$verify" -lt "$read" ]
}

@test "weft: the front door's key is a 0600 file, never an environment variable" {
  # An environment variable is inherited by every child, shown by podman inspect and readable in
  # /proc/<pid>/environ; this key signs for every company's workspace.
  assert_grep -qF 'WEFT_FRONT_DOOR_KEY_FILE: /run/weft/door.key' "$DEPLOY_DIR/compose.door.yml"
  assert_grep -qF './door.key:/run/weft/door.key:ro' "$DEPLOY_DIR/compose.door.yml"
  refute_grep -qE 'WEFT_FRONT_DOOR_KEY[:=]' "$DEPLOY_DIR/compose.door.yml"
  local tpl
  tpl=$(sed -n '/_env_templates:/,/^$/p' "$PB/weft-door.yml")
  assert_grep -qF '{ src: door.key.j2, dest: door.key, mode: "0600" }' <<<"$tpl"
  # Generated once (hex, for the front door's parser), then reused: a new key breaks every
  # workspace's sign-in at once.
  assert_grep -qF '{ name: signing_key, type: hex, length: 64 }' "$PB/weft-door.yml"
}

@test "weft: the_workspace_env_names_no_model_key_or_forge_token" {
  # D539: a workspace's model keys and forge token are entered by its admin on Settings >
  # Credentials and live only in its catalog, under its seal. Nothing the platform renders may
  # carry one or name a variable for one: a key in an env file or in OpenBao is a second copy of a
  # company's credential that the rack's operator holds, and weft's D539 exists to end that.
  command -v python3 >/dev/null || skip "python3 not installed"
  python3 -c 'import yaml' 2>/dev/null || skip "PyYAML not installed"
  run python3 - "$DEPLOY_DIR/compose.workspace.yml" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
bad = []
for name, svc in doc['services'].items():
    if svc.get('env_file'):
        bad.append(f'{name}: env_file {svc["env_file"]}')
    for var in (svc.get('environment') or {}):
        if any(word in var.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
            bad.append(f'{name}: {var}')
print('\n'.join(bad))
sys.exit(1 if bad else 0)
PY
  [ "$status" -eq 0 ] || { echo "workspace environment carries: $output"; false; }
  # No template renders a key, and weft.toml names no variable to read one from.
  [ ! -e "$DEPLOY_DIR/templates/secrets.env.j2" ]
  refute_grep -qE 'key_from_env|token_from_env' "$DEPLOY_DIR/templates/weft.toml.j2"
  refute_grep -qE '\{\{[^}]*(secrets|_resolved)' "$DEPLOY_DIR/templates/weft.toml.j2"
  # And no playbook asks inventory or OpenBao for one.
  refute_grep -rqE 'weft_secret_env|weft_tracker_token_env' "$PB" "$DEPLOY_DIR"
  [ ! -e "$PB/vars/secret-declarations/weft-workspace.yml" ]
}

@test "weft: every path weft.toml writes through is inside the workspace directory" {
  # One VM is one workspace, but a path outside its directory is one a restore, a backup or a
  # second workspace would not find, and two workspaces must never share a clone or a socket.
  local f="$DEPLOY_DIR/templates/weft.toml.j2" all inside
  all=$(grep -cE '^(wake|into|checkouts|recordings) = |^"[^"]+" = "' "$f")
  inside=$(grep -cE '^(wake|into|checkouts|recordings) = "\{\{ ws \}\}/|^"[^"]+" = "\{\{ ws \}\}/' "$f")
  [ "$all" -ge 5 ]
  [ "$all" -eq "$inside" ] || { echo "$((all - inside)) path(s) outside the workspace directory"; false; }
}

@test "weft: a rollback never deletes the database it replaces" {
  # The restore sets the current file aside (<db>.failed-<version>-<time>) instead of removing
  # it: what people wrote while the failed version ran is kept there.
  local rb="$PB/tasks/weft-rollback.yml"
  assert_grep -qF 'restore, --from, "{{ _weft_backup_set }}", --label, "failed-{{ _weft_to }}"' "$rb"
  refute_grep -rqE '\b(rm|unlink)\b.*(weft\.db|front-door\.db)' "$PB"/*weft*.yml "$PB"/tasks/weft-*.yml "$DEPLOY_DIR/deploy.sh"
  # The only things a weft task removes are its own lock and its download directory.
  command -v python3 >/dev/null || skip "python3 not installed"
  python3 -c 'import yaml' 2>/dev/null || skip "PyYAML not installed"
  run python3 - "$PB"/tasks/weft-*.yml <<'PY'
import sys, yaml
def tasks(items):
    for t in items or []:
        yield t
        for key in ('block', 'rescue', 'always'):
            yield from tasks(t.get(key))
bad = []
for f in sys.argv[1:]:
    for t in tasks(yaml.safe_load(open(f))):
        body = t.get('ansible.builtin.file') or t.get('file')
        if isinstance(body, dict) and 'absent' in str(body.get('state', '')) + str(t.get('loop', '')):
            if 'deploying' not in body['path'] and '_weft_incoming' not in body['path']:
                bad.append(f + ': ' + body['path'])
print('\n'.join(bad))
sys.exit(1 if bad else 0)
PY
  [ "$status" -eq 0 ] || { echo "removes: $output"; false; }
}

@test "weft: the automatic rollback runs only inside a deploy's own watch window" {
  # D535: a restore is automatic only with the copy the deploy took minutes earlier, inside its
  # watch. The only automatic caller is weft-deploy.yml's rescue; a person's rollback names the
  # host twice and the backup set.
  local callers
  callers=$(grep -rlE 'include_tasks: (tasks/)?weft-rollback\.yml' "$PB" | sed "s#$PB/##" | sort | tr '\n' ' ')
  [ "$callers" = "rollback-weft-workspace.yml tasks/weft-deploy.yml " ] || { echo "callers: $callers"; false; }
  local rescue
  rescue=$(sed -n '/^      rescue:/,$p' "$PB/tasks/weft-deploy.yml")
  assert_grep -qF 'include_tasks: weft-rollback.yml' <<<"$rescue"
  assert_grep -qF "confirm_rollback | default('') == inventory_hostname" "$PB/rollback-weft-workspace.yml"
}

@test "weft: a rollout deploys nothing without the declared approver's approval of rack" {
  # Publishing a release is something any token with contents:write can do; the approval of the
  # `rack` environment by the declared approver is what the rollout checks, through GitHub's API.
  local ro="$PB/roll-out-weft-release.yml"
  assert_grep -qF "selectattr('login', 'equalto', weft_release_approver)" "$ro"
  assert_grep -qF "/approvals" "$ro"
  # The check precedes queueing any host.
  local check queue
  check=$(grep -n "name: \"Require the declared approver's approval of rack" "$ro" | cut -d: -f1)
  queue=$(grep -n 'name: "Queue the front door"' "$ro" | cut -d: -f1)
  [ -n "$check" ] && [ -n "$queue" ] && [ "$check" -lt "$queue" ]
}

@test "weft: a rollout goes one workspace at a time and stops at the first failure" {
  local pb="$PB/weft-workspace.yml"
  assert_grep -qE '^  serial: 1$' "$pb"
  assert_grep -qE '^  max_fail_percentage: 0$' "$pb"
  # A rollback always fails the run when it was automatic, so the next workspace never starts.
  assert_grep -qF "when: not (_weft_rollback_by_hand | default(false) | bool) or not (_weft_rolled_back | default(false) | bool)" \
    "$PB/tasks/weft-rollback.yml"
}

@test "weft: the Discord webhook is a credential in every step that holds it" {
  local nd="$PB/tasks/notify-discord.yml"
  local post
  post=$(sed -n '/name: "Post to Discord"/,/^$/p' "$nd")
  assert_grep -qF 'no_log: true' <<<"$post"
  # A name inside a commit title must not ping anybody.
  assert_grep -qF 'parse: []' <<<"$post"
  # Read from OpenBao, never a file or an argument.
  assert_grep -qF "secret/data/services/weft/discord:webhook_url" "$PB/tasks/weft-credentials.yml"
  local read
  read=$(sed -n '/name: "Read weft.s Discord webhook/,/^$/p' "$PB/tasks/weft-credentials.yml")
  assert_grep -qF 'no_log: true' <<<"$read"
}

@test "weft: each weft playbook has a Semaphore template" {
  command -v python3 >/dev/null || skip "python3 not installed"
  python3 -c 'import yaml' 2>/dev/null || skip "PyYAML not installed"
  run python3 - "$REPO_ROOT" <<'PY'
import os, sys, yaml
root = sys.argv[1]
declared = {t['playbook'] for t in yaml.safe_load(open(os.path.join(root, 'platform/semaphore/templates.yml')))['templates']}
# weft-door.yml and weft-workspace.yml are the plays the deploy launchers import; nobody
# launches them directly, so they have no template of their own.
imported = {'weft-door.yml', 'weft-workspace.yml'}
playbooks = sorted(f for f in os.listdir(os.path.join(root, 'platform/playbooks'))
                   if 'weft' in f and f.endswith('.yml') and f not in imported)
missing = [p for p in playbooks if 'platform/playbooks/' + p not in declared]
print('\n'.join(missing))
sys.exit(1 if missing or len(playbooks) != 7 else 0)
PY
  [ "$status" -eq 0 ] || { echo "no template for: $output"; false; }
}

@test "weft: a workspace is addressed by its company name, and its id stays a claim audience" {
  # Jacob's decision: <company>.<domain>. The route, the front door's address template and every
  # health check must agree on the name; a route built from the id while the front door redirects
  # to the name sends every sign-in to a hostname Caddy does not serve.
  local site="$DEPLOY_DIR/templates/caddy-site.j2"
  assert_grep -qF 'host {{ h.weft_workspace_name }}.{{ weft_domain }}' "$site"
  refute_grep -qF 'h.weft_workspace_id' "$site"
  assert_grep -qF 'WEFT_DOOR_WORKSPACE_URL=https://{name}.{{ weft_domain }}' "$DEPLOY_DIR/templates/door.env.j2"
  refute_grep -qF '{id}' "$DEPLOY_DIR/templates/door.env.j2"
  # Every host the edge does not route by name goes to the front door, which redirects an old
  # name or an id address, answers 503 for a name not routed yet, and 404 otherwise. A 404 at the
  # edge would break every link to a workspace that was just renamed.
  local catch_all
  catch_all=$(sed -n '/^	handle {$/,/^	}$/p' "$site")
  assert_grep -qF 'reverse_proxy {{ hostvars[door].weft_door_bind' <<<"$catch_all"
  refute_grep -qF 'respond 404' "$site"
  local pb
  for pb in "$PB/weft-workspace.yml" "$PB/rollback-weft-workspace.yml"; do
    assert_grep -qF '"https://{{ weft_workspace_name }}.{{ weft_domain }}"' "$pb"
  done
  refute_grep -rqF 'https://{{ weft_workspace_id }}.' "$PB" "$DEPLOY_DIR"
  # The id is still what weft.toml binds sign-in claims to.
  assert_grep -qF 'known_as = "{{ weft_workspace_id }}"' "$DEPLOY_DIR/templates/weft.toml.j2"
}
