#!/usr/bin/env bats
# Structural tests for the declared Semaphore repository records.
#
# The point of these records is removing global mutable state: one record per
# branch, named by the templates that use it, instead of one shared record whose
# branch gets flipped. These tests pin that property and the SSH-URL guard.
#
# Structural only (grep/parse asserts) — no live Semaphore calls.
# Run: bats platform/tests/test_semaphore_repositories.bats

load assert_helpers

setup() {
  REPO_ROOT=$(git rev-parse --show-toplevel)
  DECL="$REPO_ROOT/platform/semaphore/repositories.yml"
  BOOT="$REPO_ROOT/platform/semaphore/bootstrap-semaphore-repositories.yml"
  SETUP="$REPO_ROOT/platform/semaphore/setup-templates.yml"
  TPL="$REPO_ROOT/platform/semaphore/templates.yml"
  OLD="$REPO_ROOT/platform/playbooks/set-semaphore-branch.yml"
}

@test "repositories: both main and dev records are declared" {
  [ -f "$DECL" ]
  grep -qE '^\s+- name: agent-cloud$' "$DECL"
  grep -qE '^\s+- name: agent-cloud dev$' "$DECL"
  grep -qE 'git_branch: main' "$DECL"
  grep -qE 'git_branch: dev' "$DECL"
}

@test "repositories: no SSH clone URL is paired with the 'none' key" {
  # SSH always authenticates — there is no anonymous SSH, even for a public repo
  # — so that PAIRING fails every checkout. An SSH URL with a real declared key
  # is legitimate, so assert the pairing per entry rather than banning ssh://
  # outright (which would fail a future valid deploy-key setup).
  run python3 -c "
import re,sys
txt=open('$DECL').read()
bad=[]
for blk in re.split(r'\n\s*- name: ', txt)[1:]:
    name=blk.split('\n')[0].strip()
    url=(re.search(r'git_url:\s*\"?([^\"\n]+)', blk) or [None,''])[1]
    key=(re.search(r'ssh_key:\s*(\S+)', blk) or [None,''])[1]
    if re.match(r'^(ssh://|git@)', url) and key == 'none':
        bad.append(f'{name}: {url} + ssh_key={key}')
print('\n'.join(bad) if bad else 'OK')
"
  [ "$output" = "OK" ]
}

@test "repositories: the bootstrap playbook REFUSES the SSH-without-key combo" {
  grep -qE 'Refuse an SSH clone URL with no key attached' "$BOOT"
  grep -qE "is match\('\^\(ssh://\|git@\)'\)" "$BOOT"
}

@test "repositories: bootstrap is create-or-correct and never deletes" {
  grep -qE 'Create missing repository records' "$BOOT"
  grep -qE 'Correct records that drifted from the declaration' "$BOOT"
  # No DELETE anywhere — a hand-made record still in use must not vanish.
  ! grep -qE 'method: DELETE' "$BOOT"
}

@test "repositories: bootstrap only writes when something actually differs" {
  grep -qE '_existing\.git_url != item\.git_url' "$BOOT"
  grep -qE '_existing\.git_branch != item\.git_branch' "$BOOT"
}

@test "repositories: bootstrap verifies by re-reading, not by assuming" {
  grep -qE 'Re-read to confirm' "$BOOT"
  grep -qE '_repos_after' "$BOOT"
}

@test "templates: setup resolves repositories by NAME, not one hardcoded id" {
  # The hardcoded single id is what forced branch-flipping in the first place.
  grep -qE '_repo_ids:' "$SETUP"
  grep -qE "_repo_ids\[item\.repository \| default\('agent-cloud'\)\]" "$SETUP"
  ! grep -qE "_repo_id: .*selectattr\('name', 'equalto', 'agent-cloud'\)" "$SETUP"
}

@test "templates: a template naming a missing repository fails loudly" {
  grep -qE 'Require every repository a template names to exist' "$SETUP"
  grep -qE 'Require the default repository record to exist' "$SETUP"
}

@test "templates: omitting 'repository' still binds to main (no behavior change)" {
  # Every existing template omits the field, so the default must be the main
  # record or this change would silently repoint the whole project.
  grep -qE "default\('agent-cloud'\)" "$SETUP"
  # Templates that OMIT `repository:` bind to main — that is the default and it must
  # stay. Templates that NAME one are legitimate (that is the whole point of declaring
  # several records), so the invariant worth pinning is not "nobody names a record" but
  # "every named record is actually declared". A template naming a record that does not
  # exist fails every checkout, and does so only when someone runs it.
  local declared named missing
  # [[:space:]] rather than \s: BSD sed (macOS) does not understand \s, so a \s pattern
  # silently matches nothing there and the comparison then "passes" on empty input —
  # a false green that only shows up on one of the two platforms this suite runs on.
  declared=$(grep -E '^[[:space:]]+- name: ' "$DECL" | sed -E 's/^[[:space:]]+- name: //' | tr -d '"')
  named=$(grep -E '^[[:space:]]+repository: ' "$TPL" | sed -E 's/^[[:space:]]+repository: //' | tr -d '"' | sort -u)
  missing=""
  while IFS= read -r r; do
    [ -z "$r" ] && continue
    printf '%s\n' "$declared" | grep -qxF "$r" || missing="$missing $r"
  done <<< "$named"
  [ -z "$missing" ] || { echo "templates name undeclared repository records:$missing"; false; }
}

@test "set-semaphore-branch: marked deprecated but still functional" {
  # Kept deliberately as a manual one-record fix; the header must say why.
  grep -qE 'DEPRECATED' "$OLD"
  grep -qE 'bootstrap-semaphore-repositories\.yml' "$OLD"
  grep -qE 'global mutable state' "$OLD"
  # Still a real playbook, not a stub.
  grep -qE '^\s+hosts: localhost' "$OLD"
}

@test "docs: AGENTS.md documents the declarative path and the deprecation" {
  local f="$REPO_ROOT/AGENTS.md"
  grep -qE 'bootstrap-semaphore-repositories\.yml' "$f"
  grep -qE 'deprecated' "$f"
}

@test "repositories: PUT accepts 200 as success" {
  # Semaphore answers 200 on this endpoint in some versions; treating a
  # successful update as a failure is worse than accepting both.
  run bash -c "grep -A 30 'Correct records that drifted' '$BOOT' | grep -c 'status_code: \\[200, 201, 204\\]'"
  [ "$output" = "1" ]
}

@test "repositories: ssh_key_id drift triggers an update" {
  # A replaced key-store entry gets a new id; without this the record keeps a
  # stale reference, every checkout fails, and the task reports "no change".
  grep -qE '_existing\.ssh_key_id' "$BOOT"
}

@test "templates: dev variants are generated, not hand-bound" {
  # The supported way to run a template from a branch. Without this, the only
  # options are flipping a shared record's branch or re-pointing a template's
  # repository_id by hand — both monkey patches (PRINCIPLES.md §2).
  grep -qE '_dev_variants:' "$SETUP"
  grep -qE "_dev_repo_name: \"\{\{ semaphore_dev_repo_name \| default\('agent-cloud dev'\) \}\}\"" "$SETUP"
  grep -qE 'dev_variant: true' "$TPL"
}

@test "templates: variant URL and body key off the SAME name" {
  # They diverged once: url/method used item.name while the body used _tpl_name,
  # so a variant of an EXISTING template resolved the URL to its base's id. That
  # 400'd here, but with matching ids it would have overwritten the base
  # template with the variant's name and repository.
  grep -qE "url:" "$SETUP"
  ! grep -qE "selectattr\('name', 'equalto', item\.name\)" "$SETUP"
  grep -qE "selectattr\('name', 'equalto', _tpl_name\)" "$SETUP"
}

@test "templates: key resolution is deterministic (no set-ordering)" {
  # `union` is a set operation with no order guarantee; with two valid 'no key'
  # entries it picked arbitrarily, so the drift check flapped and the playbook
  # never converged.
  ! grep -qE "\| union\(_no_key_aliases" "$BOOT"
  grep -qE "\(\[item\.ssh_key\] \+ \(_no_key_aliases" "$BOOT"
}

@test "principles: the no-monkey-patch rule names live-object mutation" {
  local f="$REPO_ROOT/PRINCIPLES.md"
  grep -qE 'Never monkey-patch a live object' "$f"
  grep -qE 'Mutating a config-as-code object through its API or console' "$f"
}

@test "templates: the (Dev) suffix keys off the generated marker, not repository" {
  # `repository:` is ALSO the documented direct-binding field. Keying the suffix
  # off its value renamed a directly-bound base to "<name> (Dev)", so the real
  # record went stale — and a template setting both would give its base and its
  # generated variant the same identity.
  grep -qE "_tpl_name:.*item\._generated" "$SETUP"
  ! grep -qE "_tpl_name:.*item\.repository" "$SETUP"
  grep -qE 'Reject a template that both binds to dev and requests a dev variant' "$SETUP"
}

@test "sync-inventory: refuses cleartext and hostless pushes" {
  local f="$REPO_ROOT/platform/semaphore/sync-inventory.yml"
  [ -f "$f" ]
  # The Bearer token crosses the wire on every request.
  grep -qE 'Require a non-cleartext transport to Semaphore' "$f"
  # `{}` is non-empty AND parses as a mapping, so it cleared both earlier checks
  # while still being hostless — a push would blank the orchestrator.
  grep -qE 'Refuse a hostless inventory' "$f"
  # Uses the real parser rather than a hand-rolled YAML walker.
  grep -qE 'ansible-inventory' "$f"
  ! grep -qE "lookup\('pipe'" "$f"
  # And proves the write landed instead of trusting the status code.
  grep -qE 'Verify the push actually landed' "$f"
}

@test "templates: no declaration is bound to a feature-branch repository record" {
  # A feature-branch record is legitimate DURING validation and a defect once merged: the
  # record is deleted with the branch, and every template still naming it then fails its
  # checkout. templates.yml lands on main, so a main-branch declaration referencing a
  # feature branch is shipped breakage.
  refute_grep -qE '^[[:space:]]+repository: .*(feat|fix|chore|docs)/' "$TPL"
  # And the declared records themselves must be the long-lived ones only.
  refute_grep -qE '^[[:space:]]+git_branch: (feat|fix|chore|docs)/' "$DECL"
}

@test "setup-templates: its no-delete semantics are documented" {
  # Removing a declaration does NOT remove the template — matching is by name with
  # PUT-or-POST and no DELETE. A reader who assumes otherwise leaves undeclared templates
  # running against records that may no longer exist.
  grep -qF 'IT DOES NOT DELETE' "$SETUP"
}
