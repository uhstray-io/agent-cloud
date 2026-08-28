#!/usr/bin/env bats
# A keypair minted into OpenBao and never copied out leaves the operator holding
# no key. That is not a filing inconvenience: it makes the workstation half of the
# two-path access proof impossible, and harden-ssh.yml must not run without it
# (PRINCIPLES.md §5). Measured 2026-08-26 on the postiz host — the store held the
# pair, the host authorized both public halves, and no operator key existed
# anywhere, so the only route in was the password that hardening removes.
#
# The onboarding checklist had called that backup a step for as long as it existed.
# It was a step with no mechanism.

load assert_helpers

setup() {
  PBDIR="${BATS_TEST_DIRNAME}/../playbooks"
  SHARED="${PBDIR}/tasks/backup-ssh-key-to-site-config.yml"
  BACKUP="${PBDIR}/backup-service-ssh-key.yml"
  GEN="${PBDIR}/generate-service-ssh-key.yml"
}

@test "ssh backup: the shared task exists and both callers reach it" {
  [ -f "$SHARED" ]
  # Anchored to the module KEY at task indent, not a bare substring: both callers
  # also NAME the shared task in comments, which a substring match would accept
  # after the real include was deleted (docs/MISTAKES.md 2.15).
  assert_grep -qE '^[[:space:]]+ansible\.builtin\.include_tasks: tasks/backup-ssh-key-to-site-config\.yml$' "$BACKUP"
  assert_grep -qE '^[[:space:]]+ansible\.builtin\.include_tasks: tasks/backup-ssh-key-to-site-config\.yml$' "$GEN"
}

@test "ssh backup: exactly ONE file writes a private key to a backup directory" {
  # CLOSED COUNT, not "the shared task contains the write". Two callers need this
  # behaviour, and the failure mode is one of them growing its own copy — which a
  # presence check cannot see. Six independently-worded copies of a security rule
  # is how this repo's transport guard drifted (docs/MISTAKES.md, §2.12 lineage).
  local writers
  writers=$(grep -rl 'dest: "{{ _bk_dest_dir }}/id_ed25519"' "$PBDIR" | sort)
  local n
  n=$(printf '%s\n' "$writers" | grep -c . || true)
  if [ "$n" != "1" ] || [ "$writers" != "$SHARED" ]; then
    echo "expected exactly one writer ($SHARED); found $n:" >&2
    printf '%s\n' "$writers" >&2
    return 1
  fi
}

@test "ssh backup: the private key is written 0600 and the write is no_log" {
  # Scoped to the private-key task block, not the file: the public key is written
  # in the same file at 0644, so a file-wide grep for either property proves
  # nothing about which task carries it (docs/MISTAKES.md 2.15).
  local blk
  blk=$(awk '/^- name: "Write the private key"/ { f = 1; next }
             f && /^- / { exit }
             f { print }' "$SHARED")
  [ -n "$blk" ]
  assert_grep -qE '^[[:space:]]*mode: "0600"' <<<"$blk"
  assert_grep -qE '^[[:space:]]*no_log: true' <<<"$blk"
}

@test "ssh backup: overwriting a DIFFERENT key requires an explicit opt-in" {
  # The guard's whole value is that it fires by DEFAULT. A guard whose condition
  # forgot the force flag would refuse forever; one that forgot the exists check
  # would never fire. Pin both halves of the condition on the guard's own task.
  local blk
  blk=$(awk '/^- name: "Refuse to overwrite a DIFFERENT key"/ { f = 1; next }
             f && /^- / { exit }
             f { print }' "$SHARED")
  [ -n "$blk" ]
  assert_grep -q '_bk_existing.stat.exists' <<<"$blk"
  assert_grep -q '_bk_force' <<<"$blk"
  # And the comparison is against the material, so an identical file is a no-op
  # rather than a refusal — otherwise re-running could never converge.
  assert_grep -q '_bk_existing_material.content | b64decode' <<<"$blk"
}

@test "ssh backup: the generator never skips the backup silently" {
  # A silent skip is how "the backup is a documented step" became "the backup
  # never happened and nothing said so". If a run takes no backup it must say the
  # key exists only in the store, because that sentence is what stops someone
  # hardening a host nobody can log into.
  local blk
  blk=$(awk '/^    - name: "Say plainly that no backup was taken"/ { f = 1; next }
             f && /^    - name:/ { exit }
             f { print }' "$GEN")
  [ -n "$blk" ]
  assert_grep -q 'NO site-config BACKUP TAKEN' <<<"$blk"
  # Fires on BOTH ways the backup can be absent — no directory passed, or one
  # passed that is not there. Either alone leaves a hole.
  assert_grep -q '_sc_dir | length == 0' <<<"$blk"
  assert_grep -q '_sc.stat.exists' <<<"$blk"
}

@test "ssh backup: the operator playbook guards the transport it reads over" {
  # A private key crosses this connection. Counted against _bao_url, the same way
  # every other OpenBao-reaching play is counted in test_credential_leaks.bats —
  # a play that resolves an address and does not guard it is unguarded regardless
  # of what the rest of the file does.
  local n_url n_inc
  n_url=$(grep -cE '^    _bao_url:' "$BACKUP")
  n_inc=$(grep -cE 'include_tasks: tasks/assert-bao-transport\.yml' "$BACKUP")
  [ "$n_url" -gt 0 ]
  [ "$n_inc" -eq "$n_url" ]
  # And the guard runs BEFORE the first request that carries a credential — a
  # guard placed after the AppRole login has already sent the secret_id.
  local guard_line first_uri_line
  guard_line=$(grep -nE 'include_tasks: tasks/assert-bao-transport\.yml' "$BACKUP" | head -1 | cut -d: -f1)
  first_uri_line=$(grep -nE '^[[:space:]]+ansible\.builtin\.uri:' "$BACKUP" | head -1 | cut -d: -f1)
  [ -n "$guard_line" ]; [ -n "$first_uri_line" ]
  [ "$guard_line" -lt "$first_uri_line" ]
}

@test "ssh backup: the operator playbook never writes to the store" {
  # It exists to move material OUT. A write path here could rotate or destroy the
  # only copy of a live key, and this playbook is the thing an operator reaches for
  # when they already cannot get in. CLOSED: enumerate the HTTP methods it uses and
  # require they are all reads, rather than blacklisting the verbs that scare us.
  local methods
  methods=$(awk '/^[[:space:]]*method:/ { gsub(/^[[:space:]]+method:[[:space:]]*/, ""); print }' "$BACKUP" | sort -u)
  # POST is permitted for exactly one thing: exchanging an AppRole for a token,
  # which is an auth call against auth/approle/login and writes no secret.
  if [ "$methods" != "$(printf 'GET\nPOST')" ] && [ "$methods" != "GET" ]; then
    echo "unexpected HTTP methods in $BACKUP:" >&2
    printf '%s\n' "$methods" >&2
    return 1
  fi
  # EVERY POST, individually, targets the login endpoint — not "at least one
  # does", which a second secret-writing POST would slip past. Each uri block is
  # extracted and its own url checked.
  local offenders
  offenders=$(awk '
    /^[[:space:]]+ansible\.builtin\.uri:/ { inblk = 1; url = ""; method = ""; next }
    inblk && /^[[:space:]]+url:/    { url = $0 }
    inblk && /^[[:space:]]+method:/ { method = $2 }
    inblk && /^[[:space:]]+- name:/ {
      if (method == "POST" && url !~ /auth\/approle\/login/) print "POST to: " url
      inblk = 0
    }
    END { if (inblk && method == "POST" && url !~ /auth\/approle\/login/) print "POST to: " url }
  ' "$BACKUP")
  if [ -n "$offenders" ]; then
    echo "a POST that is not the AppRole login:" >&2
    printf '%s\n' "$offenders" >&2
    return 1
  fi
  refute_grep -qE 'method: (PUT|DELETE|PATCH)' "$BACKUP"
}
