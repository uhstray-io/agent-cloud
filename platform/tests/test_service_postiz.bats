#!/usr/bin/env bats
# Structural tests for the postiz service (platform/services/postiz). Verifies
# the composable shape: env-parameterized five-container compose (app + its own
# Postgres/Redis + the Temporal workflow engine + that engine's Postgres), only
# the app publishing a port, container-only deploy.sh that refuses to run without
# BOTH rendered env files, the two-file config split that keeps ~60 credential
# slots out of compose's $-interpolation path, a composable deploy playbook, and
# a valid config-as-code Authentik OIDC blueprint whose redirect matches what
# upstream hardcodes. No hardcoded IPs/credentials.
#
# Several of these guard decisions that are easy to "clean up" into a bug — the
# two-file split, --force-recreate, the absent NOT_SECURED, the /settings
# redirect, and the omitted forward_auth. Each has a comment saying why.
#
# Structural only (grep/file asserts) — no live deploy.
# Run: bats platform/tests/test_service_postiz.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DEPLOY_DIR="$REPO_ROOT/platform/services/postiz/deployment"
  PLAYBOOK="$REPO_ROOT/platform/playbooks/deploy-postiz.yml"
  BLUEPRINT="$REPO_ROOT/platform/services/authentik/deployment/blueprints/postiz-oidc.yaml"
}

@test "postiz: compose env-parameterizes image + bind/port" {
  local f="$DEPLOY_DIR/compose.yml"
  [ -f "$f" ]
  grep -qE '\$\{POSTIZ_IMAGE' "$f"
  grep -qE '\$\{POSTIZ_BIND' "$f"
  grep -qE '\$\{POSTIZ_PORT' "$f"
}

@test "postiz: default images are pinned (no :latest drift)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '\$\{POSTIZ_IMAGE:-ghcr\.io/gitroomhq/postiz-app:v[0-9]' "$f"
  refute_grep -qE 'postiz-app:latest' "$f"
  refute_grep -qE ':latest' "$f"
}

@test "postiz: five-container stack (app + pg + redis + temporal + temporal pg)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '^\s+postiz:' "$f"
  grep -qE '^\s+postiz-postgres:' "$f"
  grep -qE '^\s+postiz-redis:' "$f"
  grep -qE '^\s+temporal:' "$f"
  grep -qE '^\s+temporal-postgresql:' "$f"
}

@test "postiz: the BASE topology omits elasticsearch and the workflow UI" {
  local f="$DEPLOY_DIR/compose.yml"
  # Upstream's reference compose adds these three; the BASE deliberately does
  # not — the search node lives ONLY in the gated overlay below.
  refute_grep -qE '^\s+temporal-elasticsearch:' "$f"
  refute_grep -qE '^\s+temporal-ui:' "$f"
  refute_grep -qE '^\s+temporal-admin-tools:' "$f"
  grep -qE 'ENABLE_ES:\s*"false"' "$f"
}

@test "postiz: the search-node overlay is complete, internal, and gated from inventory" {
  # The one add-back the design scoped in advance (D1 / tasks 2.2, 5.7). Fired
  # 2026-08-30: the backend registers >3 Text search attributes at startup and
  # SQL visibility caps at 3, so without this overlay the backend never binds.
  local ov="$DEPLOY_DIR/compose.search.yml"
  [ -f "$ov" ]
  # The overlay flips the engine to ES and adds the node — all three variables,
  # because ENABLE_ES without seeds fails in a different place later.
  grep -qE 'ENABLE_ES:\s*"true"' "$ov"
  grep -qE 'ES_SEEDS:\s*temporal-elasticsearch' "$ov"
  grep -qE 'ES_VERSION:\s*v7' "$ov"
  grep -qE '^\s+temporal-elasticsearch:' "$ov"
  # Internal only: the search node must never publish a host port, and single-node
  # discovery with a bounded heap — this host's only job is publishing posts.
  refute_grep -qE '^\s+ports:' "$ov"
  grep -qE 'discovery.type=single-node' "$ov"
  # The PROD-APPLIED overlay must carry no local-only knob: label=disable would
  # strip SELinux confinement from the one container running an EOL-line JVM.
  # Those live in compose.local.yml with every sibling's.
  refute_grep -q 'label=disable' <<<"$(grep -vE '^[[:space:]]*#' "$ov")"
  grep -qE 'temporal-elasticsearch:' "${DEPLOY_DIR}/compose.local.yml"
  # Pinned image, parameterized like every other one.
  grep -qE '\$\{ELASTICSEARCH_IMAGE:-docker\.io/elasticsearch:7\.17' "$ov"
  # And the gate: the deploy playbook wires the overlay from the inventory var,
  # through the generic COMPOSE_OVERLAYS mechanism in common.sh.
  local pb="${BATS_TEST_DIRNAME}/../playbooks/deploy-postiz.yml"
  grep -qE 'COMPOSE_OVERLAYS:.*compose\.search\.yml.*postiz_temporal_search' "$pb"
}

@test "postiz: temporal's Postgres stays on 16 (its supported ceiling)" {
  local f="$DEPLOY_DIR/compose.yml"
  # Deliberately NOT tracking the app's Postgres 17 — Temporal 1.28 supports
  # up to PG16. An "alignment" cleanup here would break the engine.
  grep -qE '\$\{TEMPORAL_POSTGRES_IMAGE:-docker\.io/postgres:16' "$f"
  grep -qE '\$\{POSTIZ_POSTGRES_IMAGE:-docker\.io/postgres:17' "$f"
}

@test "postiz: only the app publishes a port (datastores + engine stay internal)" {
  local f="$DEPLOY_DIR/compose.yml"
  # A single ports: stanza — the app's. This is what makes the host firewall
  # need exactly one service rule; a stray publish would add another.
  [ "$(grep -cE '^\s+ports:' "$f")" -eq 1 ]
}

@test "postiz: the mounted app config is actually loaded into the container" {
  # Mounting the Option B file is not sufficient on its own: the image entrypoint
  # is a generic node wrapper that never reads it, so the app starts with no
  # DATABASE_URL and crash-loops (observed: 216 restarts, hidden by
  # `restart: always` looking like a slow boot).
  local f="$REPO_ROOT/platform/services/postiz/deployment/compose.yml"

  # READ into the environment, never SOURCED. `.` subjects every line to shell
  # parsing and the template renders values unquoted, so a value containing
  # `$(...)` EXECUTES (measured) — command execution for anyone who can write the
  # secret path, and it reintroduces the `${...}` corruption Option B prevents.
  # This assertion previously pinned the sourcing form in place.
  # Asserted on the COMMAND VALUE ONLY, not the file. A first attempt grepped the
  # whole file and passed against a mutated command, because the explanatory
  # comment beside it contains the same text — the test was satisfied by prose
  # describing the property rather than by the property.
  local cmd
  cmd=$(awk '/^      - >-$/{f=1;next} f&&/^      [^ ]/{exit} f&&/^  /{print}' "$f" | tr '\n' ' ')
  [ -n "$cmd" ]

  # READ into the environment, never SOURCED. `.` subjects every line to shell
  # parsing and the template renders values unquoted, so a value containing
  # `$(...)` EXECUTES (measured) — command execution for anyone who can write the
  # secret path, and it reintroduces the `${...}` corruption Option B prevents.
  refute_contains "$cmd" ". /config/postiz.env"
  assert_contains "$cmd" 'export "$$l"'

  # `set -a` still matters: without it the values are shell-local and the app's
  # child processes never see them.
  assert_contains "$cmd" "set -a"

  # `read` returns false on a final line with no trailing newline and would drop
  # it silently — the same defect as a `while read` loop over a .env file.
  assert_contains "$cmd" '|| [ -n "$$l" ]'

  # NOT via env_file, which compose interpolates. Measured under podman-compose:
  # `${HOME}` inside an env_file value is expanded before the container sees it,
  # so a client secret containing `${` would be corrupted.
  refute_grep -qE '^\s+env_file:' "$f"

  # Done as `command:`, NOT an `entrypoint:` wrapper inheriting the image CMD via
  # "$@": podman-compose sets Cmd to null when entrypoint is overridden, so the
  # wrapper execs nothing and the container exits 0 instantly — a silent no-op.
  grep -qE '^\s+command:$' "$f"
  refute_grep -qE '^\s+entrypoint:$' "$f"

  # Because the CMD is copied, pin it. Upstream postiz-app v2.23.0 ships
  # CMD ["sh","-c","nginx && pnpm run pm2"]; if that changes, this fails loudly
  # instead of the copy silently drifting.
  grep -qF 'nginx && pnpm run pm2' "$f"
}

@test "postiz: nothing probes the workflow engine on loopback" {
  # The engine binds its frontend to the CONTAINER IP, so nothing listens on
  # 127.0.0.1 inside it. A loopback probe is refused forever. This happened TWICE:
  # once in the compose healthcheck (the app gates on service_healthy, so it never
  # started) and again in the deploy's verify step, which retried ten times against
  # an engine that was answering SERVING throughout. Fixing one copy and leaving
  # the other is what made the second failure possible.
  #
  # Asserted repo-wide for this port rather than per file, because the bug was a
  # missed copy.
  # Scoped to EXECUTABLE artifacts. Markdown legitimately quotes the bad address
  # when stating the prohibition, and the previous exclusion — `grep -v '^.*#'` —
  # dropped any line containing a `#` ANYWHERE, so a live
  # `--address 127.0.0.1:7233  # temporal` would have passed it. Comment-only
  # lines are excluded by anchoring `#` to the start.
  run bash -c "find '$REPO_ROOT/platform' \\( -name '*.yml' -o -name '*.sh' -o -name '*.j2' \\) -type f \\
                 -exec grep -Hn '127\\.0\\.0\\.1:7233' {} + 2>/dev/null \\
               | grep -vE ':[[:space:]]*#' || true"
  [ -z "$output" ]

  # Both places must resolve the container's own address, with no pipe (a piped
  # awk would need pipefail, which ansible-lint rejects).
  local c="$REPO_ROOT/platform/services/postiz/deployment/compose.yml"
  local d="$REPO_ROOT/platform/playbooks/deploy-postiz.yml"
  grep -qF 'a=$$(hostname -i)' "$c"
  grep -qF 'a=$(hostname -i)' "$d"
  refute_grep -qF 'hostname -i | awk' "$c"
  refute_grep -qF 'hostname -i | awk' "$d"
}

@test "postiz: healthchecks on all five containers, app dependencies gated" {
  local f="$DEPLOY_DIR/compose.yml"
  [ "$(grep -c 'healthcheck:' "$f")" -eq 5 ]
  grep -q 'pg_isready' "$f"
  grep -qE 'redis-cli ping' "$f"
  grep -qE 'temporal operator cluster health' "$f"
  # The app waits on its datastores AND the workflow engine — an app that came
  # up without the engine would look healthy and never publish anything.
  grep -qE 'temporal:\s*$' "$f"
  grep -qE 'condition: service_healthy' "$f"
}

@test "postiz: config is a bind-mounted FILE, not a directory (Option B)" {
  local f="$DEPLOY_DIR/compose.yml"
  # Binding the single file leaves the postiz-config named volume serving the
  # rest of /config. A directory bind here would shadow it.
  grep -qE '\./config/postiz\.env:/config/postiz\.env:ro' "$f"
  grep -qE 'postiz-config:/config/' "$f"
}

@test "postiz: app config does NOT go through compose interpolation" {
  local f="$DEPLOY_DIR/compose.yml"
  # The whole point of Option B: ~60 social credential slots must not pass
  # through compose, where a '$' in a client secret is silently mangled. So the
  # app service must have no env_file and no credential-bearing environment.
  refute_grep -qE '^\s+env_file:' "$f"
  refute_grep -qE 'JWT_SECRET' "$f"
  # Assert the absence of the SETTING, not of the name. The name legitimately
  # appears in comments explaining why the app config is not passed through
  # compose; matching the bare token made a correct file fail.
  refute_grep -qE '^[^#]*DATABASE_URL[:=]' "$f"
  refute_grep -qE 'CLIENT_SECRET' "$f"
}

@test "postiz: compose has no hardcoded credentials or IPs" {
  local f="$DEPLOY_DIR/compose.yml"
  # Upstream's compose ships postiz-user/postiz-password and temporal/temporal;
  # the previous stub in this repo carried them too. They must be gone.
  refute_grep -qE 'postiz-password|my-postiz-password' "$f"
  refute_grep -qE 'POSTGRES_PWD:\s*temporal\s*$' "$f"
  refute_grep -qE '(192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.)' "$f"
  # Every password is a substitution reference.
  grep -qE 'POSTGRES_PASSWORD: \$\{POSTIZ_DB_PASSWORD\}' "$f"
  grep -qE 'POSTGRES_PWD: \$\{TEMPORAL_DB_PASSWORD\}' "$f"
}

@test "postiz: deploy.sh is container-lifecycle only (no secret handling)" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ]
  [ -x "$f" ]
  refute_grep -qE 'gen_secret|put_secret|get_secret|bao |vault |openbao' "$f"
  grep -q 'detect_runtime' "$f"
  grep -q 'wait_for_healthy postiz' "$f"
}

@test "postiz: deploy.sh requires BOTH rendered env files before starting" {
  local f="$DEPLOY_DIR/deploy.sh"
  grep -qE '\.env" \] \|\| error' "$f"
  # The app config is a bind SOURCE: if absent, the runtime creates a directory
  # there and the app boots with no configuration at all. Fail early instead.
  grep -q 'config/postiz.env' "$f"
}

@test "postiz: deploy.sh force-recreates so re-rendered config applies" {
  local f="$DEPLOY_DIR/deploy.sh"
  # A bind-mounted file's CONTENT change is not a compose-spec change, so a
  # plain 'up -d' would leave the old container running with stale config.
  grep -qE 'compose up -d --force-recreate' "$f"
}

@test "postiz: env.j2 carries ONLY compose-substitution values" {
  local f="$DEPLOY_DIR/templates/env.j2"
  [ -f "$f" ]
  grep -qE '^POSTIZ_IMAGE=' "$f"
  grep -qE '^POSTIZ_BIND=' "$f"
  grep -qE '^POSTIZ_DB_PASSWORD=\{\{ secrets\.postiz_db_password \}\}' "$f"
  # App config must NOT leak into this file (that would reintroduce the
  # interpolation hazard Option B exists to avoid).
  refute_grep -qE '^JWT_SECRET=|^POSTIZ_OAUTH|^X_API_KEY=' "$f"
}

@test "postiz: postiz.env.j2 sources every secret from OpenBao, none literal" {
  local f="$DEPLOY_DIR/templates/postiz.env.j2"
  [ -f "$f" ]
  grep -qE '^JWT_SECRET=\{\{ secrets\.postiz_jwt_secret \}\}' "$f"
  grep -qE '^POSTIZ_OAUTH_CLIENT_SECRET=\{\{ secrets\.postiz_oidc_client_secret \}\}' "$f"
  grep -qE '^TEMPORAL_ADDRESS=temporal:7233' "$f"
  # No literal credential values anywhere.
  refute_grep -qiE '(secret|password|api_key|token)=[A-Za-z0-9]{8}' "$f"
}

@test "postiz: NOT_SECURED is absent (upstream documents it as dev-only)" {
  # The prior developer-machine .env set it; it disables security checks and
  # must never reach an internet-reachable host.
  # The template carries a NOTE saying this is deliberately absent, so match the
  # ASSIGNMENT rather than the name.
  refute_grep -qE '^NOT_SECURED=' "$DEPLOY_DIR/templates/postiz.env.j2"
  refute_grep -qE 'NOT_SECURED' "$DEPLOY_DIR/compose.yml"
}

@test "postiz: POSTIZ_OAUTH_SCOPE is not templated (upstream never reads it)" {
  # It appears in upstream's compose and .env.example, but the provider code
  # hardcodes the scope. Templating it would imply control we do not have.
  # Anchored: the name appears in a comment explaining WHY it is omitted, so
  # assert there is no actual assignment rather than no mention.
  refute_grep -qE '^POSTIZ_OAUTH_SCOPE=' "$DEPLOY_DIR/templates/postiz.env.j2"
}

@test "postiz: registration lockdown is config, not a manual host step" {
  local f="$DEPLOY_DIR/templates/postiz.env.j2"
  grep -qE 'DISABLE_REGISTRATION=.*postiz_disable_registration' "$f"
}

@test "postiz: unseeded social provider slots render empty, not absent" {
  local f="$DEPLOY_DIR/templates/postiz.env.j2"
  # Every slot rendering (empty when unseeded) is what makes enabling a new
  # platform a seed + redeploy with no code change.
  grep -qE "^X_API_KEY=\{\{ secrets\.postiz_x_api_key \| default\(''\) \}\}" "$f"
  grep -qE "^REDDIT_CLIENT_ID=\{\{ secrets\.postiz_reddit_client_id \| default\(''\) \}\}" "$f"
}

@test "postiz: local overlay adds the CA trust the OIDC flow needs" {
  local f="$DEPLOY_DIR/compose.local.yml"
  [ -f "$f" ]
  grep -q 'step-ca-bundle.crt' "$f"
  grep -q 'NODE_EXTRA_CA_CERTS' "$f"
  grep -q 'local-dev' "$f"
  # The overlay must NOT re-publish a port: compose merges ports by APPENDING,
  # so this would publish a second one rather than replacing the base's.
  refute_grep -qE '^\s+ports:' "$f"
}

@test "postiz: deploy playbook is composable and scopes no_log to secrets" {
  [ -f "$PLAYBOOK" ]
  grep -q 'tasks/manage-secrets.yml' "$PLAYBOOK"
  grep -q 'tasks/place-monorepo.yml' "$PLAYBOOK"
  grep -q 'tasks/enable-linger.yml' "$PLAYBOOK"
  grep -q 'tasks/distribute-ca-root.yml' "$PLAYBOOK"
  # Renders both files, and creates the config dir first (template does not
  # create parent directories).
  grep -qE 'dest: config/postiz\.env' "$PLAYBOOK"
  grep -qE 'state: directory' "$PLAYBOOK"
  # no_log must NOT appear on the deploy/verify tasks — a past failure in this
  # repo was censored exactly that way and made the run undiagnosable.
  refute_grep -qE 'no_log' "$PLAYBOOK"
}

@test "postiz: playbook shared-reads the OIDC secret rather than storing a copy" {
  grep -qE '_shared_reads' "$PLAYBOOK"
  grep -qE 'from_service: authentik' "$PLAYBOOK"
  grep -qE 'postiz_oidc_client_secret' "$PLAYBOOK"
}

@test "postiz: playbook declares the three stateful secrets as generate-once" {
  grep -qE 'name: postiz_jwt_secret, type: random' "$PLAYBOOK"
  grep -qE 'name: postiz_db_password, type: random' "$PLAYBOOK"
  grep -qE 'name: postiz_temporal_db_password, type: random' "$PLAYBOOK"
  # Social credentials are never generated — they are the operator's own.
  grep -qE 'name: postiz_x_api_key, type: existing' "$PLAYBOOK"
}

@test "postiz: playbook verifies the workflow engine, not just the app" {
  # An app that is healthy while the engine is unreachable looks fine and
  # silently never publishes anything.
  grep -qE 'temporal operator cluster health' "$PLAYBOOK"
}

@test "postiz: OIDC blueprint redirect matches what upstream hardcodes" {
  [ -f "$BLUEPRINT" ]
  # Upstream builds the redirect as ${FRONTEND_URL}/settings — there is no
  # configurable callback path, so the registered redirect must end in /settings.
  grep -qE '/settings' "$BLUEPRINT"
  grep -qE 'matching_mode: strict' "$BLUEPRINT"
  grep -qE 'client_secret: !Env POSTIZ_OIDC_CLIENT_SECRET' "$BLUEPRINT"
  # email is load-bearing: identity comes from /userinfo, so a missing email
  # claim fails sign-in outright.
  grep -qE 'scope_name, email' "$BLUEPRINT"
  grep -qE 'scope_name, openid' "$BLUEPRINT"
  grep -qE 'scope_name, profile' "$BLUEPRINT"
  # No secret literal in git.
  refute_grep -qiE 'client_secret: [A-Za-z0-9]{8}' "$BLUEPRINT"
}

@test "postiz: the OIDC client secret chain has no copy anywhere" {
  local ak="$REPO_ROOT/platform/services/authentik/deployment/templates/env.j2"
  local akpb="$REPO_ROOT/platform/playbooks/deploy-authentik.yml"
  # authentik generates it, exposes it to the worker, the blueprint reads it via
  # !Env, and postiz shared-reads the same key. Five links, one value.
  grep -qE 'name: postiz_oidc_client_secret, type: random' "$akpb"
  grep -qE '^POSTIZ_OIDC_CLIENT_SECRET=\{\{ secrets\.postiz_oidc_client_secret \}\}' "$ak"
}

@test "postiz: registered in the Authentik app catalog with prod guards" {
  local f="$REPO_ROOT/platform/services/authentik/deployment/app-catalog.yml"
  grep -qE '^  postiz:' "$f"
  grep -qE 'file: postiz-oidc\.yaml' "$f"
  # prod_required + verify_redirect are what make the deploy fail loudly rather
  # than provisioning a local-dev URL into production.
  grep -qE 'prod_required: \[postiz_redirect_uri, postiz_launch_url\]' "$f"
  grep -qE 'verify_redirect: postiz_redirect_uri' "$f"
}

@test "postiz: local Caddy route has NO forward_auth gate" {
  local f="$REPO_ROOT/platform/inventory/local-dev.yml.example"
  grep -qE 'host: "postiz\.agent-cloud\.test", upstream: "postiz:5000"' "$f"
  # An edge gate would also gate /api/public/v1 and break n8n's API-key calls.
  refute_grep -qE 'postiz\.agent-cloud\.test.*forward_auth' "$f"
}

@test "postiz: Semaphore templates exist for deploy, clean, and seeding" {
  local t="$REPO_ROOT/platform/semaphore/templates.yml"
  local tl="$REPO_ROOT/platform/semaphore/templates-local.yml"
  grep -qE '^  - name: Deploy Postiz$' "$t"
  grep -qE '^  - name: Clean Deploy Postiz$' "$t"
  grep -qE '^  - name: Seed Postiz Secrets$' "$t"
  grep -qE 'Deploy Postiz \(Local\)' "$tl"
  grep -qE 'Clean Deploy Postiz \(Local\)' "$tl"
}

@test "postiz: seeding template stores no credential in Semaphore's database" {
  local t="$REPO_ROOT/platform/semaphore/templates.yml"
  # Semaphore PERSISTS survey values, so the nine credentials must be
  # launch-time extra vars, never survey fields.
  # Anchored to the real YAML key: the template's comment says "NO survey_vars,
  # deliberately", so an unanchored match hits the prose explaining the rule.
  # State-based range: stop at the NEXT template of any name, not the next
  # non-'S' one — a later 'S'-prefixed template would otherwise extend the range
  # and count its survey_vars as ours.
  run bash -c "awk '/^  - name: Seed Postiz Secrets\$/{f=1;next} f&&/^  - name: /{exit} f' '$t' | grep -cE '^    survey_vars:'"
  [ "$output" = "0" ]
}

@test "postiz: public inventory placeholder leaks no real address" {
  local f="$REPO_ROOT/platform/inventory/production.yml"
  grep -qE 'postiz_svc:' "$f"
  grep -qE '\{\{ postiz_host \}\}' "$f"
  # Real addresses belong in site-config, never here.
  refute_grep -qE 'postiz.*(192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.)' "$f"
}

@test "postiz: psql credential check preserves its exit status" {
  local f="$REPO_ROOT/platform/playbooks/validate-secrets.yml"
  # A `| head` pipeline returns HEAD's status, so a FAILED psql would report
  # VALID — defeating the only thing this check exists to detect.
  run bash -c "grep -A 20 'Validate postiz Postgres password' '$f' | grep -c 'head -3'"
  [ "$output" = "0" ]
  grep -qE 'ansible\.builtin\.command:' "$f"
}

@test "postiz: first-path secret creation is atomic (CAS 0)" {
  local f="$REPO_ROOT/platform/playbooks/seed-postiz-secrets.yml"
  # Two runs racing a first-time seed both see 404; without CAS the later POST
  # replaces the earlier writer's keys.
  grep -qE 'cas: 0' "$f"
  grep -qE 'Merge instead, when another run won the create race' "$f"
}

@test "postiz: the seed playbook uses the shared cleartext OpenBao guard" {
  # This test used to assert the pattern INLINE in this playbook, including its
  # `([:/]|$)` tail. That tail was the bug: it accepted
  # http://127.0.0.1:80@<public-host>/, where everything before the @ is URL
  # userinfo and the request actually goes to the public host — so the test was
  # pinning the vulnerable form in place.
  #
  # The rule now lives in exactly one file and its behaviour (18 URL cases,
  # including four userinfo bypasses) is asserted in test_credential_leaks.bats.
  # Duplicating the pattern here is what let the fix miss this playbook, so this
  # test only checks that the playbook delegates to the shared guard.
  local f="$REPO_ROOT/platform/playbooks/seed-postiz-secrets.yml"
  grep -qE 'include_tasks: tasks/assert-bao-transport\.yml' "$f"
  refute_grep -q 'Refusing to send secret material' "$f"
}

@test "postiz: every provider slot has a seedable key and a declaration" {
  run python3 -c "
import re
tpl=open('$REPO_ROOT/platform/services/postiz/deployment/templates/postiz.env.j2').read()
seed=set(re.findall(r'^      - ([a-z0-9_]+)\$', open('$REPO_ROOT/platform/playbooks/seed-postiz-secrets.yml').read(), re.M))
dep=set(re.findall(r'name: postiz_([a-z0-9_]+), type: existing', open('$REPO_ROOT/platform/playbooks/deploy-postiz.yml').read()))
print('OK' if seed == dep else f'DRIFT seed-only={sorted(seed-dep)} declared-only={sorted(dep-seed)}')
"
  [ "$output" = "OK" ]
}

@test "postiz: docs do not tell an operator to create DNS by hand" {
  # The zone is config-as-code via OpenTofu and the record already exists;
  # a manual instruction contradicts the edge-as-code standard.
  refute_grep -qE '^- Create the public DNS record\.$' "$REPO_ROOT/platform/services/postiz/deployment/README.md"
  refute_grep -qE 'Operator: create the `postiz\.uhstray\.io` DNS record' "$REPO_ROOT/plan/development/14-postiz-social-publishing.md"
}

# ── deploy.sh lifecycle (phase 5.10) ─────────────────────────────────────────

@test "postiz: deploy.sh rejects an unknown argument instead of ignoring it" {
  # A typo'd flag that is silently ignored produces a deploy that did something
  # other than what the operator asked for, with no signal.
  local f="$DEPLOY_DIR/deploy.sh"
  grep -qE '\*\) echo "Unknown option' "$f"
  grep -qE 'exit 1' "$f"
}

@test "postiz: deploy.sh waits on the APP container, not merely on compose up" {
  # `compose up -d` returns as soon as the containers are CREATED. The app runs
  # Prisma migrations and the engine provisions its schema before either answers,
  # so a deploy that stops at `up` reports success while nothing is usable yet.
  local f="$DEPLOY_DIR/deploy.sh"
  grep -qE 'wait_for_healthy postiz [0-9]+' "$f"
  # The timeout must be generous enough for first boot. 300s was measured as
  # sufficient; anything under 120 would fail a cold start on this stack.
  local secs
  secs=$(grep -oE 'wait_for_healthy postiz [0-9]+' "$f" | grep -oE '[0-9]+$')
  [ "$secs" -ge 120 ]
}

@test "postiz: deploy.sh force-recreates because config is a bind-mounted FILE" {
  # A change to the CONTENT of a bind-mounted file is not a compose-spec change,
  # so a plain `up -d` leaves the running container with the previous config
  # loaded — the re-rendered secrets would silently not take effect.
  local f="$DEPLOY_DIR/deploy.sh"
  grep -qE 'compose up -d --force-recreate' "$f"
}

@test "postiz: deploy.sh fails loudly when the bind-mount source is missing" {
  # A missing bind source does not error: the runtime CREATES A DIRECTORY at that
  # path, and the app then starts with no configuration — the same silent failure
  # class as the config never being loaded at all.
  local f="$DEPLOY_DIR/deploy.sh"
  grep -qE '\[ -f "\$\{SCRIPT_DIR\}/config/postiz.env" \]' "$f"
  grep -q 'silently become a directory' "$f"
}

@test "postiz: the app healthcheck probes the BACKEND path, not the frontend" {
  # nginx serves / from the frontend, so a probe on / stays green while the
  # backend is dead — which is exactly how a backend that never bound sat
  # "starting" behind a green-looking / for a whole validation phase. /api/ is
  # proxied to the backend, so its answer (even a 404) proves the process bound.
  local f="$DEPLOY_DIR/compose.yml"
  grep -q "http://127.0.0.1:5000/api/" "$f"
  refute_grep -qE "get\('http://127\.0\.0\.1:5000/'," "$f"
}

@test "postiz api key: read-only capture, fixed path, key-bearing steps no_log" {
  # The stored Organization.apiKey IS the bearer token (getOrgByApiKey compares
  # the Authorization header directly to the column), so this playbook must only
  # READ it — a write path could clobber the key every caller depends on — and
  # nothing it prints may carry the value.
  local pb="${BATS_TEST_DIRNAME}/../playbooks/store-postiz-api-key.yml"
  [ -f "$pb" ]
  # SELECT only: no mutating SQL anywhere in the play.
  refute_grep -qiE 'UPDATE|INSERT|DELETE|ALTER' <<<"$(grep -vE '^[[:space:]]*#' "$pb")"
  # The store path and key are fixed, never caller-supplied.
  assert_grep -qE '^    _bao_path: "services/postiz"$' "$pb"
  assert_grep -qE '^    _bao_key: "postiz_api_key"$' "$pb"
  refute_grep -qE '^    _bao_(path|key): "\{\{' "$pb"
  # Every step that touches the key value is no_log'd: the DB read, the parse,
  # the OpenBao fetch/patch/verify, and the round-trip assert.
  local blk n
  while IFS= read -r n; do
    blk=$(awk -v name="$n" '$0 ~ "- name: \""name {f=1} f&&/^    - name:/&&!($0 ~ "- name: \""name){exit} f{print}' "$pb")
    [ -n "$blk" ]
    assert_grep -q 'no_log: true' <<<"$blk"
  done < <(printf '%s\n' "Read the API key" "Parse what the database" "Fetch the existing secret" "Merge the key" "Verify the stored key" "Require the round trip")
  # The one debug prints a LENGTH, never the value: inside the Report task block,
  # every line naming _api_key must pipe it through length. Scoped to the whole
  # block — the msg is a folded scalar, so a fixed -A window can miss the line
  # that actually carries the value (this refute was mutation-tested into shape).
  local rep
  rep=$(awk '/- name: "Report \(names and lengths only/{f=1;next} f&&/^    - name:/{exit} f{print}' "$pb")
  [ -n "$rep" ]
  [ -z "$(grep -oE '_api_key[^|]*' <<<"$rep" | grep -v '_api_key \| length')" ]
  assert_grep -q '_api_key | length' <<<"$rep"
  # Transport guard precedes the first request that carries a credential.
  local g u
  g=$(grep -nE 'include_tasks: tasks/assert-bao-transport\.yml' "$pb" | head -1 | cut -d: -f1)
  u=$(grep -nE '^[[:space:]]+ansible\.builtin\.uri:' "$pb" | head -1 | cut -d: -f1)
  [ -n "$g" ]; [ -n "$u" ]; [ "$g" -lt "$u" ]
  # Declared as a Semaphore template with a (Dev) variant.
  grep -qE '^  - name: Store Postiz API Key$' "${BATS_TEST_DIRNAME}/../semaphore/templates.yml"
}

@test "postiz: teardown resolves the compose-file SUPERSET, so overlay-only resources die too" {
  # A service/volume that exists only in an overlay (the search node,
  # postiz-es-data) is invisible to a base-only `down -v` — a "destroy
  # everything" rebuild would attach to a search node carrying the old
  # cluster's state. Both teardown branches must glob the overlays in.
  local cs="${BATS_TEST_DIRNAME}/../playbooks/tasks/clean-service.yml"
  [ "$(grep -c 'compose\.\*\.yml' "$cs")" -ge 2 ]
  # And the PROD branch skips compose.local.yml — it references local-only
  # externals a prod host does not have, which can void the whole down -v.
  grep -qE 'compose\.local\.yml\) continue' "$cs"
  # And the base still comes first (overlays reference its services).
  local first_base first_glob
  first_base=$(grep -nE 'for f in docker-compose\.yml compose\.yml' "$cs" | head -1 | cut -d: -f1)
  first_glob=$(grep -n 'compose\.\*\.yml' "$cs" | head -1 | cut -d: -f1)
  [ -n "$first_base" ]; [ -n "$first_glob" ]; [ "$first_base" -lt "$first_glob" ]
}
