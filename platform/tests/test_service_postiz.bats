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
  ! grep -qE 'postiz-app:latest' "$f"
  ! grep -qE ':latest' "$f"
}

@test "postiz: five-container stack (app + pg + redis + temporal + temporal pg)" {
  local f="$DEPLOY_DIR/compose.yml"
  grep -qE '^\s+postiz:' "$f"
  grep -qE '^\s+postiz-postgres:' "$f"
  grep -qE '^\s+postiz-redis:' "$f"
  grep -qE '^\s+temporal:' "$f"
  grep -qE '^\s+temporal-postgresql:' "$f"
}

@test "postiz: the trimmed topology omits elasticsearch and the workflow UI" {
  local f="$DEPLOY_DIR/compose.yml"
  # Upstream's reference compose adds these three; we deliberately do not.
  # The engine runs standard visibility on its own Postgres instead.
  ! grep -qE '^\s+temporal-elasticsearch:' "$f"
  ! grep -qE '^\s+temporal-ui:' "$f"
  ! grep -qE '^\s+temporal-admin-tools:' "$f"
  grep -qE 'ENABLE_ES:\s*"false"' "$f"
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
  ! grep -qE '^\s+env_file:' "$f"
  ! grep -qE 'JWT_SECRET' "$f"
  ! grep -qE 'DATABASE_URL' "$f"
  ! grep -qE 'CLIENT_SECRET' "$f"
}

@test "postiz: compose has no hardcoded credentials or IPs" {
  local f="$DEPLOY_DIR/compose.yml"
  # Upstream's compose ships postiz-user/postiz-password and temporal/temporal;
  # the previous stub in this repo carried them too. They must be gone.
  ! grep -qE 'postiz-password|my-postiz-password' "$f"
  ! grep -qE 'POSTGRES_PWD:\s*temporal\s*$' "$f"
  ! grep -qE '(192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.)' "$f"
  # Every password is a substitution reference.
  grep -qE 'POSTGRES_PASSWORD: \$\{POSTIZ_DB_PASSWORD\}' "$f"
  grep -qE 'POSTGRES_PWD: \$\{TEMPORAL_DB_PASSWORD\}' "$f"
}

@test "postiz: deploy.sh is container-lifecycle only (no secret handling)" {
  local f="$DEPLOY_DIR/deploy.sh"
  [ -f "$f" ]
  [ -x "$f" ]
  ! grep -qE 'gen_secret|put_secret|get_secret|bao |vault |openbao' "$f"
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
  ! grep -qE '^JWT_SECRET=|^POSTIZ_OAUTH|^X_API_KEY=' "$f"
}

@test "postiz: postiz.env.j2 sources every secret from OpenBao, none literal" {
  local f="$DEPLOY_DIR/templates/postiz.env.j2"
  [ -f "$f" ]
  grep -qE '^JWT_SECRET=\{\{ secrets\.postiz_jwt_secret \}\}' "$f"
  grep -qE '^POSTIZ_OAUTH_CLIENT_SECRET=\{\{ secrets\.postiz_oidc_client_secret \}\}' "$f"
  grep -qE '^TEMPORAL_ADDRESS=temporal:7233' "$f"
  # No literal credential values anywhere.
  ! grep -qiE '(secret|password|api_key|token)=[A-Za-z0-9]{8}' "$f"
}

@test "postiz: NOT_SECURED is absent (upstream documents it as dev-only)" {
  # The prior developer-machine .env set it; it disables security checks and
  # must never reach an internet-reachable host.
  ! grep -qE 'NOT_SECURED' "$DEPLOY_DIR/templates/postiz.env.j2"
  ! grep -qE 'NOT_SECURED' "$DEPLOY_DIR/compose.yml"
}

@test "postiz: POSTIZ_OAUTH_SCOPE is not templated (upstream never reads it)" {
  # It appears in upstream's compose and .env.example, but the provider code
  # hardcodes the scope. Templating it would imply control we do not have.
  # Anchored: the name appears in a comment explaining WHY it is omitted, so
  # assert there is no actual assignment rather than no mention.
  ! grep -qE '^POSTIZ_OAUTH_SCOPE=' "$DEPLOY_DIR/templates/postiz.env.j2"
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
  ! grep -qE '^\s+ports:' "$f"
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
  ! grep -qE 'no_log' "$PLAYBOOK"
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
  ! grep -qiE 'client_secret: [A-Za-z0-9]{8}' "$BLUEPRINT"
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
  ! grep -qE 'postiz\.agent-cloud\.test.*forward_auth' "$f"
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
  ! grep -qE 'postiz.*(192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.)' "$f"
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

@test "postiz: cleartext OpenBao guard rejects lookalike hostnames" {
  local f="$REPO_ROOT/platform/playbooks/seed-postiz-secrets.yml"
  # A prefix match on 'http://10\.' also accepts http://10.evil.example/ — a
  # public host — which would send the AppRole creds over cleartext.
  grep -qE '127\(\\\\\.\[0-9\]\{1,3\}\)\{3\}' "$f"
  grep -qE '\(\[:/\]\|\$\)' "$f"
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
  ! grep -qE '^- Create the public DNS record\.$' "$REPO_ROOT/platform/services/postiz/deployment/README.md"
  ! grep -qE 'Operator: create the `postiz\.uhstray\.io` DNS record' "$REPO_ROOT/plan/development/14-postiz-social-publishing.md"
}
