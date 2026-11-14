#!/usr/bin/env bats
# Structural tests for the weft service (platform/services/weft-door and weft-workspace) and its
# playbooks. weft is deployed from signed images by digest, to a release a person pins per host in
# site-config, and only when a person launches a deploy; most of these guard one of those
# properties rather than a file's shape.
#
# Structural only (parsed YAML and scoped greps) — no live deploy.
# Run: bats platform/tests/test_service_weft.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  WS_DIR="$REPO_ROOT/platform/services/weft-workspace/deployment"
  DOOR_DIR="$REPO_ROOT/platform/services/weft-door/deployment"
  PB="$REPO_ROOT/platform/playbooks"
  TEMPLATES="$REPO_ROOT/platform/semaphore/templates.yml"
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
  ws=$(volume_sources "$WS_DIR/compose.yml" | awk '$2 == "${WEFT_WORKSPACE_DIR}" {print $1}' | sort | tr '\n' ' ')
  [ "$ws" = "board capture orchestrator " ] || { echo "workspace dir mounted by: $ws"; false; }
  door=$(volume_sources "$DOOR_DIR/compose.yml")
  refute_grep -qF 'WEFT_WORKSPACE_DIR' <<<"$door"
  # Every service of the workspace stack is one of the three: a service added later is reviewed
  # against this list, not waved through.
  local services
  services=$(python3 -c 'import sys,yaml; print(" ".join(sorted(yaml.safe_load(open(sys.argv[1]))["services"])))' \
    "$WS_DIR/compose.yml")
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
  holders=$(volume_sources "$WS_DIR/compose.yml" | awk '$2 == "${WEFT_PODMAN_SOCKET}" {print $1}' | tr '\n' ' ')
  [ "$holders" = "orchestrator " ] || { echo "socket mounted by: $holders"; false; }
  refute_grep -qF 'podman.sock' "$DOOR_DIR/compose.yml"
}

@test "weft: each service directory has one compose.yml and a lifecycle-only deploy.sh" {
  # One codebase, one compose.yml and one deploy.sh per service (PRINCIPLES.md §1): the front door
  # and a workspace are two services, so two directories, and the shared compose() is untouched.
  local dir f code
  for dir in "$WS_DIR" "$DOOR_DIR"; do
    [ -f "$dir/compose.yml" ]
    f="$dir/deploy.sh"
    [ -x "$f" ]
    # The code, not its comments: the header says in words that it never reads OpenBao.
    code=$(grep -vE '^\s*#' "$f")
    refute_grep -qiE 'openbao|vault|\bbao[ _-]|bao-client|secret_id|role_id|X-Vault-Token|/v1/(secret|auth)' <<<"$code"
    refute_grep -qE 'gen_secret|put_secret|get_secret|store_token_in_openbao|openssl rand' <<<"$code"
    # It reads only the files Ansible rendered, and never sources one into its environment.
    refute_grep -qE '^\s*(source|\.)\s+.*(\.env|secrets\.env|door\.key)' <<<"$code"
    refute_grep -qF 'COMPOSE_BASE' "$f"
  done
  [ ! -e "$REPO_ROOT/platform/services/weft" ]
  refute_grep -qF 'COMPOSE_BASE' "$REPO_ROOT/platform/lib/common.sh"
}

@test "weft: every image runs by digest, never by tag" {
  # Signatures are checked on digests. A tag can be moved after the check, so an image pulled by
  # tag is not the image that was verified. deploy.sh refuses a non-digest reference, the
  # compose files take the image only from .env, and .env takes it only from release.json.
  local dir
  for dir in "$WS_DIR" "$DOOR_DIR"; do
    assert_grep -qF 'require_digest' "$dir/deploy.sh"
    assert_grep -qE '@sha256:\[0-9a-f\]\{64\}' "$dir/deploy.sh"
    refute_grep -qE '^\s+image: .*:-' "$dir/compose.yml"
    refute_grep -rqE ':latest|localhost/weft' "$dir/templates" "$dir/compose.yml"
  done
  # A required variable, never a default: any default would be a tag.
  assert_grep -qE '^\s+image: \$\{WEFT_WORKSPACE_IMAGE:\?[^}]+\}$' "$WS_DIR/compose.yml"
  assert_grep -qE '^\s+image: \$\{WEFT_DOOR_IMAGE:\?[^}]+\}$' "$DOOR_DIR/compose.yml"
  assert_grep -qF "_weft_images['weft-workspace']" "$WS_DIR/templates/workspace.env.j2"
  assert_grep -qF "_weft_images['weft-door']" "$DOOR_DIR/templates/door.env.j2"
  # The agent images weft.toml names come from the same release.json.
  assert_grep -qF "images['weft-builder']" "$WS_DIR/templates/weft.toml.j2"
  assert_grep -qF "images['weft-proxy']" "$WS_DIR/templates/weft.toml.j2"
}

@test "weft: a host runs only the release.json digest a person pinned, signed by images.yml on main" {
  # A rack host's weft_release is <version>@sha256:<digest>, written in site-config by a person.
  # The digest is what cosign verifies and oras fetches, so a tag moved after the pin changes
  # nothing. Only staging also takes a bare commit.
  assert_grep -qF "_weft_pin_pattern: '^(v[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{40})@sha256:[0-9a-f]{64}\$'" "$PB/vars/weft.yml"
  local rel="$PB/tasks/weft-release.yml"
  assert_grep -qF "_weft_pin | default('') | string is match(_weft_pin_pattern)" "$rel"
  assert_grep -qF "weft_environment | default('') == 'staging' and _weft_pin | default('') | string is match(_weft_commit_pattern)" "$rel"
  # The rack runs releases only; the rollout refuses any other pin before anything changes.
  assert_grep -qF "weft_environment | default('') != 'rack' or _weft_pin | string is match('^v')" "$rel"
  assert_grep -qF "hostvars[item].weft_release | default('') | string is match(_weft_pin_pattern)" "$PB/roll-out-weft-release.yml"
  # The exact identity (never --certificate-identity-regexp, which a branch name could satisfy).
  local cosign="$PB/tasks/verify-cosign.yml"
  assert_grep -qF -- "'--certificate-identity', _cosign_identity" "$cosign"
  assert_grep -qF -- "'--certificate-oidc-issuer', _cosign_issuer" "$cosign"
  refute_grep -qF 'regexp' "$cosign"
  assert_grep -qF "reject('match', '^[^@\\\\s]+@sha256:[0-9a-f]{64}\$')" "$cosign"
  assert_grep -qF "/.github/workflows/images.yml@refs/heads/main" "$PB/vars/weft.yml"
  assert_grep -qF "(_wvi_release.signed_by | default({})).identity | default('') == _weft_signer" "$PB/tasks/weft-verify-images.yml"
  # release.json is verified before it is fetched, and fetched to stdout, never to a file.
  local verify fetch
  verify=$(grep -n 'name: "Verify release.json was signed' "$rel" | cut -d: -f1)
  fetch=$(grep -n 'name: "Fetch release.json by its digest, to stdout"' "$rel" | cut -d: -f1)
  [ -n "$verify" ] && [ -n "$fetch" ] && [ "$verify" -lt "$fetch" ]
  refute_grep -qE 'slurp:|release-incoming|_weft_incoming' "$rel"
}

@test "weft: nothing deploys weft unless a person launches it" {
  # The AI Invariant (PRINCIPLES.md §4): no target an AI wrote is converged without a person
  # launching it. So no weft template is scheduled, and no weft playbook decides what to deploy
  # from GitHub (the newest release, an approval, main's newest commit), where an agent with a
  # token could write.
  command -v python3 >/dev/null || skip "python3 not installed"
  python3 -c 'import yaml' 2>/dev/null || skip "PyYAML not installed"
  run python3 - "$TEMPLATES" <<'PY'
import sys, yaml
bad = [t['name'] for t in yaml.safe_load(open(sys.argv[1]))['templates']
       if ('weft' in t['name'] or 'weft' in t['playbook']) and 'schedule' in t]
print('\n'.join(bad))
sys.exit(1 if bad else 0)
PY
  [ "$status" -eq 0 ] || { echo "scheduled: $output"; false; }
  refute_grep -rqE 'releases/latest|/approvals|actions/workflows/[a-z]+\.yml/runs' "$PB"/*weft*.yml "$PB"/tasks/weft-*.yml
  refute_grep -rqE 'weft_release_approver|follow-weft-main' "$PB" "$TEMPLATES"
}

@test "weft: a failed deploy fails the run and rolls nothing back by itself" {
  # The release to go back to is the host's previous pin, a person's choice in site-config, and
  # going back restores data. So the deploy's rescue only fails, and the one restore is in the
  # person's rollback, which names the host twice and the backup set.
  # The last rescue in the file is the deploy's own (the first belongs to the stop and backup, and
  # starts what was running again when the backup does not verify).
  local rescue
  rescue=$(tac "$PB/tasks/weft-deploy.yml" | sed '/^  rescue:$/q' | tac)
  assert_grep -qE '^  rescue:$' <<<"$rescue"
  assert_grep -qF 'ansible.builtin.fail:' <<<"$rescue"
  refute_grep -qE 'deploy\.sh, (restore|up)|include_tasks' <<<"$rescue"
  local restorers
  restorers=$(grep -rlE 'deploy\.sh, restore' "$PB" | sed "s#$PB/##" | sort | tr '\n' ' ')
  [ "$restorers" = "rollback-weft-host.yml " ] || { echo "restore called from: $restorers"; false; }
  assert_grep -qF "confirm_rollback | default('') == inventory_hostname" "$PB/rollback-weft-host.yml"
  [ ! -e "$PB/tasks/weft-rollback.yml" ]
}

@test "weft: a restore never deletes the database it replaces" {
  # The restore sets the current file aside (<db>.failed-<version>-<time>) instead of removing
  # it: what people wrote while the failed version ran is kept there.
  assert_grep -qF 'restore, --from, "{{ weft_restore_from }}", --label, "failed-{{ _weft_leaving }}"' "$PB/rollback-weft-host.yml"
  refute_grep -rqE '\b(rm|unlink)\b.*(weft\.db|front-door\.db)' "$PB"/*weft*.yml "$PB"/tasks/weft-*.yml "$WS_DIR/deploy.sh" "$DOOR_DIR/deploy.sh"
  # No weft task removes a file.
  command -v python3 >/dev/null || skip "python3 not installed"
  python3 -c 'import yaml' 2>/dev/null || skip "PyYAML not installed"
  run python3 - "$PB"/tasks/weft-*.yml "$PB"/*weft*.yml <<'PY'
import sys, yaml
def tasks(items):
    for t in items or []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in ('block', 'rescue', 'always', 'tasks'):
            yield from tasks(t.get(key))
bad = []
for f in sys.argv[1:]:
    for t in tasks(yaml.safe_load(open(f))):
        body = t.get('ansible.builtin.file') or t.get('file')
        if isinstance(body, dict) and 'absent' in str(body.get('state', '')):
            bad.append(f + ': ' + str(body.get('path')))
print('\n'.join(bad))
sys.exit(1 if bad else 0)
PY
  [ "$status" -eq 0 ] || { echo "removes: $output"; false; }
}

@test "weft: the seal has one origin, OpenBao, and the VM never writes it back" {
  # manage-secrets generates it once (32 bytes as 64 hex digits) at the workspace's path, and the
  # play renders seal.key from it before the first start. Nothing reads seal.key off a VM into
  # OpenBao, so there is never a second copy to disagree with.
  local ws="$PB/weft-workspace.yml"
  assert_grep -qF '{ name: seal, type: hex, length: 64 }' "$ws"
  assert_grep -qF 'service_name: "{{ _weft_secrets }}/workspaces/{{ weft_workspace_id }}"' "$ws"
  assert_grep -qF 'stdin: "{{ _resolved.seal }}"' "$ws"
  local seal_on render deploy
  render=$(grep -n 'name: "Render seal.key (0600) from OpenBao"' "$ws" | cut -d: -f1)
  deploy=$(grep -n 'name: "One deploy"' "$ws" | cut -d: -f1)
  [ -n "$render" ] && [ -n "$deploy" ] && [ "$render" -lt "$deploy" ]
  refute_grep -rqE 'seal_key_b64|weft-seal\.yml' "$PB" "$REPO_ROOT/platform/services"
  # seal.key is written, never read back.
  seal_on=$(grep -rnE 'slurp:' "$PB"/*weft*.yml "$PB"/tasks/weft-*.yml || true)
  [ -z "$seal_on" ] || { echo "$seal_on"; false; }
}

@test "weft: the front door's key is a 0600 file, never an environment variable" {
  # An environment variable is inherited by every child, shown by podman inspect and readable in
  # /proc/<pid>/environ; this key signs for every company's workspace.
  assert_grep -qF 'WEFT_FRONT_DOOR_KEY_FILE: /run/weft/door.key' "$DOOR_DIR/compose.yml"
  assert_grep -qF './door.key:/run/weft/door.key:ro' "$DOOR_DIR/compose.yml"
  refute_grep -qE 'WEFT_FRONT_DOOR_KEY[:=]' "$DOOR_DIR/compose.yml"
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
  run python3 - "$WS_DIR/compose.yml" <<'PY'
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
  [ ! -e "$WS_DIR/templates/secrets.env.j2" ]
  refute_grep -qE 'key_from_env|token_from_env' "$WS_DIR/templates/weft.toml.j2"
  refute_grep -qE '\{\{[^}]*(secrets|_resolved)' "$WS_DIR/templates/weft.toml.j2"
  # And no playbook asks inventory or OpenBao for one.
  refute_grep -rqE 'weft_secret_env|weft_tracker_token_env' "$PB" "$WS_DIR"
  [ ! -e "$PB/vars/secret-declarations/weft-workspace.yml" ]
}

@test "weft: the GitHub record is written with a token minted for the run, scoped to deployments" {
  # No long-lived token: a GitHub App installation token, minted on the controller for the one
  # staging run that records a deployment, for weft's repository alone and deployments alone.
  local tok="$PB/tasks/weft-github-app-token.yml"
  assert_grep -qF 'deployments: write' "$tok"
  assert_grep -qF '"{{ _wgt_permissions | to_json }}"' "$tok"
  assert_grep -qF '"{{ _wgt_repository }}"' "$tok"
  [ "$(sed -n '/_wgt_permissions:/,/^$/p' "$tok" | grep -cE '^\s+[a-z_]+: (read|write)$')" -eq 2 ]
  refute_grep -rqE 'services/weft/github|_weft_github_token: >-' "$PB"
  # Only Deploy weft Staging records anything on GitHub, after both staging hosts passed.
  local callers
  callers=$(grep -rlE 'include_tasks: (tasks/)?weft-github-(app-token|deployment)\.yml' "$PB" | sed "s#$PB/##" | sort | tr '\n' ' ')
  [ "$callers" = "deploy-weft-staging.yml " ] || { echo "callers: $callers"; false; }
}

@test "weft: deploys say nothing to Discord; Semaphore's task history is the record" {
  refute_grep -rqiE 'discord' "$PB"/*weft*.yml "$PB"/tasks/weft-*.yml
  [ ! -e "$PB/tasks/notify-discord.yml" ]
  [ ! -e "$PB/tasks/reconcile-discord-webhook.yml" ]
}

@test "weft: a rollout goes one workspace at a time and stops at the first failure" {
  local pb="$PB/weft-workspace.yml"
  assert_grep -qE '^  serial: 1$' "$pb"
  assert_grep -qE '^  max_fail_percentage: 0$' "$pb"
  # And no play after a failure runs: not the next workspace, not staging's GitHub record.
  assert_grep -qE '^  any_errors_fatal: true$' "$pb"
  [ "$(grep -cE '^  any_errors_fatal: true$' "$PB/weft-door.yml")" -eq 2 ]
}

@test "weft: a workspace is addressed by its company name, and its id stays a claim audience" {
  # Jacob's decision: <company>.<domain>. The route, the front door's address template and every
  # health check must agree on the name; a route built from the id while the front door redirects
  # to the name sends every sign-in to a hostname Caddy does not serve.
  local site="$DOOR_DIR/templates/caddy-site.j2"
  assert_grep -qF 'host {{ h.weft_workspace_name }}.{{ weft_domain }}' "$site"
  refute_grep -qF 'h.weft_workspace_id' "$site"
  assert_grep -qF 'WEFT_DOOR_WORKSPACE_URL=https://{name}.{{ weft_domain }}' "$DOOR_DIR/templates/door.env.j2"
  refute_grep -qF '{id}' "$DOOR_DIR/templates/door.env.j2"
  # Every host the edge does not route by name goes to the front door, which redirects an old
  # name or an id address, answers 503 for a name not routed yet, and 404 otherwise. A 404 at the
  # edge would break every link to a workspace that was just renamed.
  local catch_all
  catch_all=$(sed -n '/^	handle {$/,/^	}$/p' "$site")
  assert_grep -qF 'reverse_proxy {{ hostvars[door].weft_door_bind' <<<"$catch_all"
  refute_grep -qF 'respond 404' "$site"
  assert_grep -qF '"https://{{ weft_workspace_name }}.{{ weft_domain }}"' "$PB/weft-workspace.yml"
  assert_grep -qF "'https://' ~ weft_workspace_name ~ '.' ~ weft_domain" "$PB/rollback-weft-host.yml"
  refute_grep -rqF 'https://{{ weft_workspace_id }}.' "$PB" "$WS_DIR" "$DOOR_DIR"
  # The id is still what weft.toml binds sign-in claims to.
  assert_grep -qF 'known_as = "{{ weft_workspace_id }}"' "$WS_DIR/templates/weft.toml.j2"
}
