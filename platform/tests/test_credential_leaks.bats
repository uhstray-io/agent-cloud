#!/usr/bin/env bats
# Repository-wide credential leak regression tests.
# Validates that no credentials, private IPs, or secrets are committed.
#
# Run: bats platform/tests/test_credential_leaks.bats

REPO_ROOT=""

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
}

# ── Helper: list committed files, excluding vendored/test/doc paths ────

_committed_files() {
  # Returns git-tracked files, excluding:
  #   - netbox-docker/ (vendored upstream)
  #   - .gitkeep files
  #   - binary images
  #   - this test file itself
  #   - markdown documentation (plan/, docs/, README, CLAUDE.md)
  #   - test fixture files (test_*.py, test_*.bats, conftest.py)
  #   - example files (*.example)
  local filter="${1:-}"
  git ls-files -- "$REPO_ROOT" \
    | grep -v '^platform/services/netbox/deployment/netbox-docker/' \
    | grep -v '\.gitkeep$' \
    | grep -v '\.png$' \
    | grep -v '\.svg$' \
    | grep -v '\.ico$' \
    | grep -v 'test_credential_leaks\.bats$' \
    | grep -v '\.md$' \
    | grep -v '\.MD$' \
    | grep -v '^plan/' \
    | grep -v '^docs/' \
    | grep -v 'test_.*\.py$' \
    | grep -v 'test_.*\.bats$' \
    | grep -v 'conftest\.py$' \
    | grep -v '\.example$' \
    | grep -v '\.j2$' \
    ${filter:+| grep "$filter"}
}

# ── RFC1918 IP detection ──────────────────────────────────────────────

@test "credential leak: no hardcoded 192.168.x.x IPs in committed files" {
  local violations
  violations=$(
    _committed_files | while IFS= read -r f; do
      [ -f "$REPO_ROOT/$f" ] || continue
      grep -nE '192\.168\.[0-9]+\.[0-9]+' "$REPO_ROOT/$f" \
        | grep -viE 'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|inventory_hostname|test|\.md:' \
        | sed "s|^|$f:|" || true
    done
  )
  if [ -n "$violations" ]; then
    echo "Found hardcoded 192.168.x.x IPs:"
    echo "$violations"
    false
  fi
}

@test "credential leak: no hardcoded 10.x.x.x IPs in committed files" {
  local violations
  violations=$(
    _committed_files | while IFS= read -r f; do
      [ -f "$REPO_ROOT/$f" ] || continue
      # Match 10.N.N.N but not version strings like 10.0 without third octet
      grep -nE '\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b' "$REPO_ROOT/$f" \
        | grep -viE 'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|test|\.md:' \
        | sed "s|^|$f:|" || true
    done
  )
  if [ -n "$violations" ]; then
    echo "Found hardcoded 10.x.x.x IPs:"
    echo "$violations"
    false
  fi
}

@test "credential leak: no hardcoded 172.16-31.x.x IPs in committed files" {
  local violations
  violations=$(
    _committed_files | while IFS= read -r f; do
      [ -f "$REPO_ROOT/$f" ] || continue
      grep -nE '\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b' "$REPO_ROOT/$f" \
        | grep -viE 'target|host:|subnet|scope|example|RFC1918|CIDR|placeholder|template|test|\.md:' \
        | sed "s|^|$f:|" || true
    done
  )
  if [ -n "$violations" ]; then
    echo "Found hardcoded 172.16-31.x.x IPs:"
    echo "$violations"
    false
  fi
}

# ── Hardcoded credential detection ───────────────────────────────────

@test "credential leak: no hardcoded passwords in committed files" {
  local violations
  violations=$(
    _committed_files | while IFS= read -r f; do
      [ -f "$REPO_ROOT/$f" ] || continue
      # Match password/passwd assignments with literal values (8+ chars)
      # Exclude template variables ({{ }}), env var refs ($), comments, and docs
      grep -inE '(password|passwd)\s*[:=]\s*["\x27]?[A-Za-z0-9!@#$%^&*]{8,}' "$REPO_ROOT/$f" \
        | grep -v '{{' \
        | grep -v '\$' \
        | grep -v '^\s*#' \
        | grep -v 'example' \
        | grep -v 'REPLACE_ME' \
        | grep -v 'changeme' \
        | grep -v 'placeholder' \
        | sed "s|^|$f:|" || true
    done
  )
  if [ -n "$violations" ]; then
    echo "Found hardcoded passwords:"
    echo "$violations"
    false
  fi
}

@test "credential leak: no hardcoded API keys or tokens in committed files" {
  local violations
  violations=$(
    _committed_files | while IFS= read -r f; do
      [ -f "$REPO_ROOT/$f" ] || continue
      # Match token/api_key/secret_key assignments with literal values
      grep -inE '(api_key|apikey|secret_key|secret_id|token)\s*[:=]\s*["\x27]?[A-Za-z0-9_-]{20,}' "$REPO_ROOT/$f" \
        | grep -v '{{' \
        | grep -v '\$' \
        | grep -v '^\s*#' \
        | grep -v 'example' \
        | grep -v 'REPLACE_ME' \
        | grep -v 'placeholder' \
        | grep -v 'dummyKey' \
        | grep -v 'dummy_key' \
        | grep -v 'LOCAL_FAKE' \
        | sed "s|^|$f:|" || true
    done
  )
  if [ -n "$violations" ]; then
    echo "Found hardcoded API keys or tokens:"
    echo "$violations"
    false
  fi
}

# ── Jinja2 template namespace validation ─────────────────────────────

@test "credential leak: Jinja2 templates use approved variable namespaces" {
  local templates
  templates=$(find "$REPO_ROOT/platform/services" -name "*.j2" 2>/dev/null)
  [ -n "$templates" ] || skip "no Jinja2 templates found"

  local violations=""
  for tmpl in $templates; do
    # Extract {{ variable }} patterns, excluding filters (|), lookups, and dict access
    # Approved prefixes: secrets, _, ansible_, inventory_hostname, service_, monorepo_,
    #                    container_, hostvars, groups, item, lookup, netbox_, diode_
    local bare_vars
    bare_vars=$(
      grep -oE '\{\{[\s]*[a-zA-Z][a-zA-Z0-9_.]*' "$tmpl" \
        | sed 's/{{[[:space:]]*//' \
        | grep -vE '^(secrets|_|ansible_|inventory_hostname|service_|monorepo_|container_|hostvars|groups|item|lookup|netbox_|diode_|openbao_|bao_|discovery_|orb_)' \
        || true
    )
    if [ -n "$bare_vars" ]; then
      local basename
      basename=$(basename "$tmpl")
      violations="${violations}${basename}: ${bare_vars}\n"
    fi
  done

  if [ -n "$violations" ]; then
    echo "Jinja2 templates with variables outside approved namespaces:"
    printf '%b' "$violations"
    echo ""
    echo "Approved namespaces: secrets.*, _*, ansible_*, inventory_hostname,"
    echo "  service_*, monorepo_*, container_*, hostvars, groups, item"
    false
  fi
}

# ── Gitignore coverage ───────────────────────────────────────────────

@test "credential leak: .gitignore covers secrets/ directory" {
  grep -qE '^\s*secrets/' "$REPO_ROOT/.gitignore"
}

@test "credential leak: .gitignore covers *.secret files" {
  grep -qE '^\s*\*\.secret' "$REPO_ROOT/.gitignore"
}

@test "credential leak: .gitignore covers *.key files" {
  grep -qE '^\s*\*\.key' "$REPO_ROOT/.gitignore"
}

@test "credential leak: .gitignore covers *.pem files" {
  grep -qE '^\s*\*\.pem' "$REPO_ROOT/.gitignore"
}

@test "credential leak: .gitignore covers runtime .env files" {
  # Must have a pattern that covers config/*.env or similar runtime env files
  grep -qE '(\.env|\*\*/config/\*\.env|\*\.env)' "$REPO_ROOT/.gitignore"
}

# ── Tracked file detection ───────────────────────────────────────────

@test "credential leak: no .env files tracked by git (only .env.example)" {
  local tracked_env
  tracked_env=$(git ls-files -- "$REPO_ROOT" | grep '\.env$' | grep -v '\.example' || true)
  if [ -n "$tracked_env" ]; then
    echo "Found .env files tracked by git (should be .env.example only):"
    echo "$tracked_env"
    false
  fi
}

@test "credential leak: no private key files tracked by git" {
  local tracked_keys
  tracked_keys=$(git ls-files -- "$REPO_ROOT" \
    | grep -E '\.(key|pem|p12|pfx)$' \
    | grep -v '\.example' \
    | grep -v '\.pub$' \
    || true)
  if [ -n "$tracked_keys" ]; then
    echo "Found private key files tracked by git:"
    echo "$tracked_keys"
    false
  fi
}

@test "credential leak: no .secret files tracked by git" {
  local tracked_secrets
  tracked_secrets=$(git ls-files -- "$REPO_ROOT" | grep '\.secret$' || true)
  if [ -n "$tracked_secrets" ]; then
    echo "Found .secret files tracked by git:"
    echo "$tracked_secrets"
    false
  fi
}

# ── Secrets must not reach Semaphore-run playbooks as extra vars ──────────────
# Semaphore persists a task's extra-var JSON in its own database and serves it
# back over its API indefinitely (verified against a completed task, whose
# `environment` field came back verbatim). So `-e <secret>=...` leaves a live
# credential in plaintext outside OpenBao. Survey fields were already known to
# persist; extra vars were wrongly believed to be safe, and three templates said
# so. An encrypted environment secret is the channel these playbooks' own OpenBao
# AppRole credentials already arrive on.

@test "secret-writing playbooks prefer an environment secret over an extra var" {
  # ORDER is the assertion. With the extra var as the first operand, a run passing
  # BOTH uses the persisted one even though the secret is configured — the leak,
  # still open. Each playbook must put the env lookup first.
  local pb="$REPO_ROOT/platform/playbooks"

  # store-ssh-password.yml's own ordering is asserted in test_host_access_gate.bats,
  # which lands with that change; this file covers the two seed playbooks.
  grep -qE "_bao_value:.*lookup\('env', *'BAO_VALUE'\) *\| *default\(bao_value" \
    "$pb/seed-openbao-key.yml"
  grep -qE "lookup\('env', *_env_name\) *$" "$pb/seed-postiz-secrets.yml"
  grep -qE '_env_name: "\{\{ _seed_env\[item\] \}\}"' "$pb/seed-postiz-secrets.yml"
  grep -qF "map('regex_replace', '^', 'SEED_')" "$pb/seed-postiz-secrets.yml"

  # The reverse order must not appear.
  ! grep -qE "_bao_value:.*bao_value *\| *default\(lookup" "$pb/seed-openbao-key.yml"
  ! grep -qE "lookup\('vars', 'seed_' ~ item[^)]*\) *\| *default\(lookup\('env'" \
    "$pb/seed-postiz-secrets.yml"
}

@test "seed-openbao-key uses the resolved value everywhere, not the raw extra var" {
  # The change-detection comparison matters as much as the writes: comparing the
  # stored value against the RAW extra var while writing the resolved one would
  # misjudge whether a write is needed on every environment-supplied run.
  local f="$REPO_ROOT/platform/playbooks/seed-openbao-key.yml"
  # The write path is now tasks/bao-merge-keys.yml; the property this test owns
  # is that the RESOLVED value (_bao_value — env-first) is what reaches it, and
  # that change detection happens against that same dict inside the shared task.
  grep -qE '_bm_data: "\{\{ \{bao_key: _bao_value\} \}\}"' "$f"
  refute_grep -qE '_bm_data: "\{\{ \{bao_key: bao_value\} \}\}"' "$f"
  # (_bm_set IS the caller's _bm_data; the shared task also accepts a _bm_remove list.)
  local bm="$REPO_ROOT/platform/playbooks/tasks/bao-merge-keys.yml"
  grep -qF '_bm_set: "{{ _bm_data | default({}) }}"' "$bm"
  grep -qE 'combine\(_bm_set\)\) != _bm_current' "$bm"
  # The validation gate must test the resolved value too, or a run supplying only
  # the environment would fail the check it just satisfied.
  grep -qE '^\s+- _bao_value \| length > 0' "$f"
  ! grep -qE '^\s+- bao_value is defined' "$f"
}

@test "no Semaphore template still instructs operators to pass a secret as -e" {
  # The operator-facing docs are the actual leak surface: the playbook change is
  # cosmetic while the template tells people to use the persisted form.
  local t="$REPO_ROOT/platform/semaphore/templates.yml"
  ! grep -qE '^\s*#\s*-e bao_value=<secret>' "$t"
  ! grep -qE '^\s*#\s*-e seed_[a-z_]+=\.\.\.' "$t"
  # And the replacement channel is named for each.
  grep -q "BAO_VALUE" "$t"
  grep -q "SEED_X_API_KEY" "$t"
  # NOTE: the Store SSH Password template's own correction (SSH_PASSWORD) is
  # asserted in test_host_access_gate.bats, which lands with that change.
}

# ── The OpenBao transport guard ───────────────────────────────────────────────
# The AppRole secret_id, the client token OpenBao returns, and whatever secret is
# being read or written all cross that connection. http:// is permitted to
# loopback/RFC1918 because the platform's OpenBao sits on an internal VLAN;
# public cleartext is refused.

@test "NetBox token bootstrap guards AppRole transport before login" {
  assert_guard_precedes_first_uri "$REPO_ROOT/platform/playbooks/provision-netbox-automation-token.yml"
}

@test "every play that resolves an OpenBao URL includes the transport guard" {
  # Counted, not merely present: a playbook with three plays and one include
  # leaves two plays unguarded, and a file-wide grep cannot tell the difference.
  local pb="$REPO_ROOT/platform/playbooks"
  local shared="$pb/tasks/assert-bao-transport.yml"
  [ -f "$shared" ]

  # Exactly ONE definition of the rule may exist. Six independently-worded copies
  # is how the userinfo bypass survived in two playbooks after being fixed in the
  # other three.
  # `run` so a no-match grep (exit 1, which is the PASSING case here) does not
  # abort the test before the assertion is evaluated.
  run bash -c "grep -l 'Refusing to send secret material' '$pb'/*.yml 2>/dev/null || true"
  [ -z "$output" ]
  grep -q 'Refusing to send secret material' "$shared"

  # The strict policy composes the bounded IPv4 fragment for loopback HTTP;
  # the behavioral table below verifies the expanded regex and octet boundaries.
  grep -F "_pat_strict:" "$shared" | grep -Fq "{{ _pat_loopback }}"
  grep -Fq "_pat_loopback: '127(?:\.{{ _pat_ipv4_octet }}){3}'" "$shared"

  # Every playbook that reaches OpenBao, not just the ones the guard started in.
  # Follow static task imports recursively because an extracted survey task may
  # include the shared guard from its own directory. Resolve each include from
  # the file that contains it, matching Ansible's task-file-relative lookup.
  # Population is selected by the condition (the play resolves a store URL),
  # never by a hand-kept list of names.
  #
  # A RATCHET rather than a blanket assertion: every unguarded play must be named
  # in the ratchet file, and every play named there must still be unguarded.
  local ratchet="$REPO_ROOT/platform/tests/known_unguarded_bao_plays.txt"
  [ -f "$ratchet" ]
  run python3 - "$pb" "$ratchet" "$shared" <<'PY'
import sys
from pathlib import Path

import yaml

playbooks, ratchet_path, shared_path = map(Path, sys.argv[1:])
shared_path = shared_path.resolve()
ratchet = {
    line.strip()
    for line in ratchet_path.read_text().splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}


def count_guard(tasks, source, stack, overrides=None):
    overrides = overrides or {}
    count = 0
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for key in ("ansible.builtin.import_tasks", "ansible.builtin.include_tasks", "import_tasks", "include_tasks"):
            target = task.get(key)
            if isinstance(target, dict):
                target = target.get("file")
            if not isinstance(target, str) or "{{" in target:
                continue
            resolved = (source.parent / target).resolve()
            if resolved == shared_path:
                count += 1
            elif resolved.is_file() and resolved not in stack:
                nested = overrides.get(resolved)
                if nested is None:
                    nested = yaml.safe_load(resolved.read_text())
                count += count_guard(
                    nested if isinstance(nested, list) else [],
                    resolved,
                    stack | {resolved},
                    overrides,
                )
        for section in ("block", "rescue", "always"):
            count += count_guard(task.get(section), source, stack, overrides)
    return count


seen = set()
unguarded = set()
for playbook in sorted(playbooks.glob("*.yml")):
    plays = yaml.safe_load(playbook.read_text()) or []
    for play in plays:
        if not isinstance(play, dict) or "_bao_url" not in (play.get("vars") or {}):
            continue
        seen.add(playbook.name)
        if count_guard(play.get("tasks"), playbook, {playbook.resolve()}) < 1:
            unguarded.add(playbook.name)

# Prove the recursive discovery catches the exact failure mode: removing the
# nested include from the standalone survey makes that play unguarded and would
# fail the ratchet as a new exception.
survey_playbook = playbooks / "survey-o11y-pbs-physical-storage.yml"
survey_play = next(
    play
    for play in yaml.safe_load(survey_playbook.read_text())
    if isinstance(play, dict) and "_bao_url" in (play.get("vars") or {})
)
survey_task_file = (playbooks / "tasks/o11y-pbs-physical-storage-survey.yml").resolve()
mutated_survey_tasks = yaml.safe_load(survey_task_file.read_text())
guard_task = next(
    task
    for task in mutated_survey_tasks
    if task.get("name") == "Refuse public cleartext OpenBao transport"
)
guard_task["ansible.builtin.include_tasks"] = "missing-guard.yml"
mutated_guard_count = count_guard(
    survey_play.get("tasks"),
    survey_playbook,
    {survey_playbook.resolve()},
    {survey_task_file: mutated_survey_tasks},
)
if mutated_guard_count != 0 or survey_playbook.name in ratchet:
    errors = ["transport guard check did not detect removal of the extracted survey's nested guard"]
else:
    errors = []

# Dynamic include mappings are used to propagate verify tags to child tasks.
# Count their file target, while refusing malformed mappings with no file value.
mapping_guard = count_guard(
    [{"ansible.builtin.include_tasks": {
        "file": "tasks/assert-bao-transport.yml",
        "apply": {"tags": ["verify"]},
    }}],
    survey_playbook,
    {survey_playbook.resolve()},
)
malformed_mapping_guard = count_guard(
    [{"ansible.builtin.include_tasks": {"apply": {"tags": ["verify"]}}}],
    survey_playbook,
    {survey_playbook.resolve()},
)
if mapping_guard != 1 or malformed_mapping_guard != 0:
    errors.append("guard discovery must count a mapping-form include file and ignore a malformed mapping")

errors += [f"NEW unguarded play not in the ratchet: {name}" for name in sorted(unguarded - ratchet)]
errors += [f"{name} is now guarded — remove it from the ratchet" for name in sorted(ratchet - unguarded)]
errors += [f"ratchet names a playbook that no longer exists: {name}" for name in sorted(ratchet) if not (playbooks / name).is_file()]
if len(seen) < 8:
    errors.append(f"transport guard coverage unexpectedly found only {len(seen)} playbooks")
if errors:
    print("\n".join(errors), file=sys.stderr)
    raise SystemExit(1)
print(f"transport guard coverage checked for {len(seen)} playbooks")
PY
  [ "$status" -eq 0 ] || { printf '%s\n' "$output"; return 1; }
}

@test "the transport pattern accepts internal endpoints and refuses public ones" {
  # Tests the regex itself rather than running a playbook: the Unit Tests CI job
  # installs pytest and bats, not ansible, so an execution test would fail there.
  # The pattern is what actually decides, so it is what gets tested.
  #
  # The pattern must be decoded the way JINJA decodes it. In the YAML the dots are
  # written `\\.`, and Jinja processes escapes in its string literals, so what the
  # regex engine finally receives is `\.`. Compiling the raw file text instead would
  # treat it as "a literal backslash then any character" and the whole table would
  # come out wrong — passing or failing for entirely the wrong reason.
  run python3 -c "
import re, codecs, sys
src = open('$REPO_ROOT/platform/playbooks/tasks/assert-bao-transport.yml').read()
patterns = dict(re.findall(r\"^\\s+(_pat_[a-z0-9_]+): '(.*?)'\", src, re.M))
assert all(name in patterns for name in ('_pat_strict', '_pat_https', '_pat_single_label', '_pat_ipv4_octet')), 'patterns not found in the shared task'
def expand(name):
    pattern = patterns[name]
    for _ in range(5):
        expanded = re.sub(r\"\\{\\{\\s*(_pat_[a-z0-9_]+)\\s*\\}\\}\",
                          lambda match: patterns[match.group(1)], pattern)
        if expanded == pattern: break
        pattern = expanded
    return codecs.decode(pattern, 'unicode_escape')
pat = expand('_pat_strict')
pat_https = expand('_pat_https')
pat_single = expand('_pat_single_label')
# The single-label branch must be gated on local_mode in the assert expression —
# unconditional single-label acceptance is what the resolver search-suffix
# refusal below exists to prevent.
assert re.search(r'local_mode \| default\(false\).*is match\(_pat_single_label\)', src, re.S), 'single-label branch not gated on local_mode'

# Generic RFC1918 examples, deliberately NOT the platform's real endpoint —
# that address is site data and lives in site-config, not here.
accept = [
    'https://bao.example.com:8200',        # example: TLS anywhere is fine
    'https://bao.example.com.:8200',       # DNS root dot is a valid FQDN
    'http://localhost:8200',               # example: loopback by name
    'http://127.0.0.1:8200',               # example: loopback by address
    'http://10.1.2.3:8200',                # example: RFC1918 10/8
    'http://10.1.2.3',                     # example: no port at all
    'http://192.168.0.1:8200/v1',          # example: RFC1918 192.168/16 with path
    'http://172.16.0.1:8200',              # example: RFC1918 lower bound
    'http://172.31.255.254:8200',          # example: RFC1918 upper bound
    'https://[::1]:8200',                   # example: bracketed IPv6 TLS host
]
refuse = [
    'https://',                             # empty authority
    'https://bad..example',                 # malformed DNS authority
    'https://public.example:bad',           # malformed port
    'https://' + 'user:FAKE@public.example', # userinfo is not the host
    'http://bao.example.com:8200',         # example: public host, cleartext
    'http://10.evil.example/',             # example: prefix-match bypass
    'http://192.168.0.1.evil.example/',    # example: private-looking public host
    'http://172.32.0.1:8200',              # example: just OUTSIDE RFC1918
    'http://172.15.0.1:8200',              # example: just BELOW RFC1918
    'http://1270.0.0.1:8200',              # example: not loopback
    # userinfo bypasses: everything before the @ is credentials, not the host, so
    # the request actually goes to the PUBLIC host after it. These carry
    # trufflehog:ignore because a <user>:<pass>@<host> shape is exactly what a
    # URI-credential detector looks for — the strings hold no secret, they are the
    # attack they exist to prove is refused.
    'http://127.0.0.1:80@bao.evil.example/v1/auth/approle/login',  # trufflehog:ignore
    'http://127.0.0.1@bao.evil.example/v1',                        # trufflehog:ignore
    'http://10.1.2.3:8200@bao.evil.example/v1',                    # trufflehog:ignore
    'http://192.168.0.1:8200@bao.evil.example/',                   # trufflehog:ignore
    'http://local-openbao@bao.evil.example/',                      # trufflehog:ignore
    'http://local-openbao.evil.example:8200/',  # dotted = public FQDN space, refused
    'http://local-openbao:8200',           # single-label: allowed ONLY under local_mode, strict refuses
]
def url(*octets): return 'http://' + '.'.join(map(str, octets)) + ':8200'
accept.extend(url(*octets) for octets in ((10, 0, 0, 0), (10, 255, 255, 255)))
refuse.extend(url(*octets) for octets in (
    (10, 999, 2, 3), (10, 1, 2, 256), (192, 168, 256, 1),
    (172, 16, 999, 3), (127, 256, 0, 1),
))
bad = []
def accepted(u):
    return bool(re.match(pat, u) and
                (not u.startswith('https://') or re.match(pat_https, u)))
for u in accept:
    if not accepted(u): bad.append('should ACCEPT: ' + u)
for u in refuse:
    if accepted(u): bad.append('should REFUSE: ' + u)
# The local_mode branch's own pattern: single-label only, same trailing anchor.
sl_accept = ['http://local-openbao:8200', 'http://local-openbao:8200/v1',
             'http://host.containers.internal:8000']   # podman's engine-host name, exact
sl_refuse = [
    'http://host.containers.internal.evil.example/',   # the exact name is not a prefix
    'http://host.containers.internal@evil.example/',   # trufflehog:ignore — userinfo
    'http://evil.containers.internal:8000',            # only the one host-gateway name
    'http://local-openbao@bao.evil.example/',   # trufflehog:ignore — userinfo
    'http://local-openbao.evil.example:8200/',  # dotted = public FQDN space
    'https://anything',                          # wrong scheme for this branch
]
for u in sl_accept:
    if not re.match(pat_single, u): bad.append('single-label should ACCEPT: ' + u)
for u in sl_refuse:
    if re.match(pat_single, u): bad.append('single-label should REFUSE: ' + u)
if bad:
    print('\\n'.join(bad)); sys.exit(1)
print('all %d cases correct' % (len(accept) + len(refuse) + len(sl_accept) + len(sl_refuse)))
"
  [ "$status" -eq 0 ]
  [[ "$output" == *"all 43 cases correct"* ]]
}

@test "repo: no generated Python bytecode is tracked" {
  # A __pycache__ under platform/lib/ was committed because the ignore rule was scoped to
  # one service. Bytecode is interpreter-specific, unreviewable as source, and churns on
  # every run.
  local root tracked
  root=$(git rev-parse --show-toplevel)
  tracked=$(cd "$root" && git ls-files | grep -E '\.py[cod]$|(^|/)__pycache__/' || true)
  [ -z "$tracked" ] || { echo "tracked bytecode: $tracked"; false; }
  # And the ignore rule must be general, not per-service.
  grep -qE '^__pycache__/$' "$root/.gitignore"
  grep -qE '^\*\.py\[cod\]$' "$root/.gitignore"
}

@test "bao-merge-keys: creation is CAS-guarded, and losing the race falls back to a merge" {
  # Without options.cas: 0 the create path can overwrite a concurrent creation,
  # dropping its sibling keys — the same clobbering race merge-patch closes on
  # the update path. Measured: cas:0 answers 400 on an existing path, 200 fresh.
  local t="$REPO_ROOT/platform/playbooks/tasks/bao-merge-keys.yml"
  local blk
  blk=$(task_block "$t" "Create the new path")
  [ -n "$blk" ]
  assert_grep -qE '^        cas: 0' <<<"$blk"
  assert_grep -qE 'status_code: \[200, 400\]' <<<"$blk"
  # And the 400 (lost race) path merges instead of failing or clobbering.
  blk=$(task_block "$t" "Losing the create race")
  [ -n "$blk" ]
  assert_grep -q 'method: PATCH' <<<"$blk"
  assert_grep -q 'merge-patch' <<<"$blk"
}

@test "seed-openbao-key: the read-only access check ends the play before any write" {
  # Run once after Provision Seed Environment, before the first real seed: it must
  # prove the seed's own capabilities and stop, even when a value is staged.
  local f="$REPO_ROOT/platform/playbooks/seed-openbao-key.yml"
  local t="$REPO_ROOT/platform/playbooks/tasks/assert-bao-seed-access.yml"
  assert_precedes "$f" 'include_tasks: tasks/assert-bao-seed-access.yml' 'include_tasks: tasks/bao-merge-keys.yml'
  assert_precedes "$f" 'ansible.builtin.meta: end_play' 'include_tasks: tasks/bao-merge-keys.yml'
  local blk
  blk=$(task_block "$f" 'End read-only verification before every secret-store write')
  assert_grep -qF 'when: bao_verify_access_only | default(false) | bool' <<<"$blk"
  # The value check is skipped only in access-check mode.
  blk=$(task_block "$f" 'Validate the secret value is present')
  assert_grep -qF 'when: not (bao_verify_access_only | default(false) | bool)' <<<"$blk"
  # Capabilities decide, never a GET status alone; every token-bearing call is no_log.
  assert_grep -qF '/v1/sys/capabilities-self' "$t"
  # The patch-vs-create rule lives in filter_plugins/seed_access.py (test_seed_access_rule.py).
  assert_grep -qF '_missing: "{{ _caps | seed_access_missing(_exists | bool) }}"' "$t"
  [ "$(grep -c 'no_log: true' "$t")" -eq 2 ]
}
