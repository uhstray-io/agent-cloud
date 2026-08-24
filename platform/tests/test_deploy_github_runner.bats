#!/usr/bin/env bats
# Structural tests for deploy-github-runner.yml.
#
# The properties here are the ones whose violation is silent: a token minted once and
# reused across hosts, a credential task that runs ON the semi-trusted host instead of
# the controller, no_log spread wide enough to censor the failure you need to read, or
# an install that proceeds without a digest.
#
# Run: bats platform/tests/test_deploy_github_runner.bats

setup() {
  PLAYBOOK="$BATS_TEST_DIRNAME/../playbooks/deploy-github-runner.yml"
  [ -f "$PLAYBOOK" ]
}

@test "deploy-runner: credential work is delegated to the controller, not the host" {
  # The runner host is DENIED OpenBao by its firewall declaration, so it could not fetch
  # its own secret even holding a credential — which is the point. Minting therefore
  # happens on the controller and the token is handed over in memory.
  grep -A14 'Authenticate to OpenBao' "$PLAYBOOK" | grep -q 'delegate_to: localhost'
  grep -A14 'Read the App private key' "$PLAYBOOK" | grep -q 'delegate_to: localhost'
  # Window sized to the task, not to a guessed line count: read from the task name to
  # the next task boundary, so adding a line inside the task cannot fail this for the
  # wrong reason (docs/MISTAKES.md §2 — a test that fails for its own reasons teaches
  # people to edit the test).
  sed -n '/Mint a single-use runner registration token/,/^    - name:/p' "$PLAYBOOK" \
    | grep -q 'delegate_to: localhost'
}

@test "deploy-runner: the registration token is minted PER HOST, never once for all" {
  # A registration token is single use. run_once would hand the same token to the second
  # runner, which fails with an expiry message that is really a reuse.
  #
  # Checked against EXECUTABLE lines only: the header comment names run_once precisely to
  # explain why it is absent, and a check that forbids naming the hazard discourages the
  # comment that documents it.
  local code
  code=$(grep -vE '^\s*#' "$PLAYBOOK")
  ! printf '%s' "$code" | grep -q 'run_once'"'"
}

@test "deploy-runner: no_log covers the credential boundary and nothing else" {
  # A censored deploy is an undiagnosable one. Auth, the secret read, the mint and the
  # register step carry no_log; the verification must not.
  # Five: OpenBao auth, the key read, the mint, the classification (which touches both
  # the key and the token), and the register step. The classification exists so that a
  # no_log failure is still diagnosable — it emits a verdict, never a value.
  local n
  n=$(grep -c 'no_log: true' "$PLAYBOOK")
  [ "$n" -eq 5 ]
  grep -A8 'Confirm the runner service is active' "$PLAYBOOK" | grep -qv 'no_log' || true
  ! grep -A8 'Confirm the runner service is active' "$PLAYBOOK" | grep -q 'no_log: true'
  ! grep -A6 'Report' "$PLAYBOOK" | grep -q 'no_log: true'
}

@test "deploy-runner: the runner account is asserted to hold no sudo rights" {
  # Asserted, not assumed: a recycled host may carry a stale sudoers entry, and the
  # whole isolation claim rests on this being false.
  grep -qF 'sudo -l -U' "$PLAYBOOK"
  grep -qF "'(ALL' not in" "$PLAYBOOK"
  grep -qF "'NOPASSWD' not in" "$PLAYBOOK"
}

@test "deploy-runner: install refuses to proceed without a published digest" {
  grep -qF '_runner_sha256 | length == 64' "$PLAYBOOK"
  grep -qF '_hooks_sha256 | length == 64' "$PLAYBOOK"
  # Read from the release metadata for the pinned version, not hardcoded anywhere.
  grep -qF 'releases/tags/v' "$PLAYBOOK"
  ! grep -qE '_sha256: "[0-9a-f]{64}"' "$PLAYBOOK"
}

@test "deploy-runner: the OpenBao transport guard is included" {
  grep -qF 'tasks/assert-bao-transport.yml' "$PLAYBOOK"
}

@test "deploy-runner: lingering is enabled for the runner account, not ansible_user" {
  # ansible_user holds NOPASSWD sudo after hardening; the runner must hold none, so it
  # runs under its own account and that account is the one that must linger.
  grep -qF 'tasks/enable-linger.yml' "$PLAYBOOK"
  grep -qF 'linger_user: "{{ _account }}"' "$PLAYBOOK"
}

@test "deploy-runner: every become_user is paired with an explicit become" {
  # ansible-lint partial-become: an unpaired become_user silently runs as the play's
  # become target instead of the named account.
  local users becomes
  users=$(grep -c 'become_user:' "$PLAYBOOK")
  becomes=$(grep -c 'become: true' "$PLAYBOOK")
  [ "$users" -gt 0 ]
  [ "$becomes" -ge "$users" ]
}

@test "deploy-runner: the issuer accepts a client id, and no client secret appears" {
  # Upstream recommends the App's CLIENT ID as the JWT issuer and accepts the app id.
  # The OAuth client SECRET plays no part in App authentication and must not appear.
  grep -qF 'github_app_client_id' "$PLAYBOOK"
  ! grep -qiE 'client_secret|GITHUB_APP_SECRET' "$PLAYBOOK"
}

@test "deploy-runner: a declaration without labels or group is refused" {
  # A runner with no labels cannot be targeted; one with no group lands in the default
  # group, which may admit repositories this plane must not serve.
  grep -qF "(github_runner_labels | default([])) | length > 0" "$PLAYBOOK"
  grep -qF "github_runner_group | default('') | length > 0" "$PLAYBOOK"
}

@test "deploy-runner: the installation id is discovered, not a required input" {
  # It is a fact the forge already knows and is derivable from the App credential we
  # hold. Requiring it by hand adds a transcribed value that can be wrong and that
  # changes silently if the App is reinstalled.
  # Discovery moved into the signer, which resolves the installation from the App
  # credential it already holds. The property under test is unchanged: the id is never a
  # required input.
  ! grep -qF '_install_id | length > 0' "$PLAYBOOK"
  grep -qF 'github_app_token.py' "$PLAYBOOK"
  grep -qF 'installation_id' "$BATS_TEST_DIRNAME/../lib/github_app_token.py"
}

@test "deploy-runner: acl is installed, because become-to-unprivileged needs setfacl" {
  # Every step after the account is created runs as that unprivileged account, and
  # Ansible needs setfacl to hand its temp files to a become target that is neither root
  # nor the connecting user. Without acl the register step dies in the become plumbing —
  # raised outside the normal result path, so failed_when: false does not catch it, and
  # under the no_log the token requires it appears as a censored fatal with no cause.
  grep -qF 'ca-certificates, acl]' "$PLAYBOOK"
}

@test "deploy-runner: carries no real addresses" {
  ! grep -qE '(192\.168\.[0-9]+\.[0-9]+|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)' "$PLAYBOOK"
}
