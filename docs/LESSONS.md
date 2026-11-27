# Lessons

Author: Joseph A. Wisneski IV

Constraints learned from accepted work, one per entry, newest last, so the next plan starts where the last one ended; failures go in [`docs/MISTAKES.md`](MISTAKES.md). An entry lands in the pull request that taught it and is never rewritten: a later entry supersedes it, and the old one gains a `Superseded by L-NNN` line. The format lives here: a `### L-NNN · date · area` heading, then ACCEPTED (the work and its pull request), DERIVED (the constraint, stated so a planner can apply it without this context) and APPLIES TO (the work or paths it constrains).

### L-001 · 2026-11-17 · tests

- **ACCEPTED:** #1199 (AC-968).
- **DERIVED:** When the code under test splits a wall-clock deadline into reserves, size the test's deadline from the smallest window the split leaves, not from the total. When the code only caps each call, pass its production budget constant, so only those caps bind.
- **APPLIES TO:** tests that pass `deadline=`, `timeout=` or a budget to runner or helper code under `platform/tests/` and `platform/playbooks/files/`.

### L-002 · 2026-11-17 · tests

- **ACCEPTED:** #1199 (AC-968).
- **DERIVED:** Prove a fix for a test that fails only under load by injecting the delay into the code path (a late wake-up, a slow child start): the old budget must fail and the new one pass. Passes under a CPU busy-loop are not evidence.
- **APPLIES TO:** fixes for timing-dependent tests.

### L-003 · 2026-11-17 · playbooks

- **ACCEPTED:** #1193 (AC-856).
- **DERIVED:** A playbook that reads Proxmox's Ceph endpoints reports a field an older release lacks as unknown or partial, never as empty, and states the release range a figure's unit holds for. Proxmox passes Ceph's values through unscaled and changes their shape between releases: `percent_used` is a fraction of 1 only from Ceph 13 on, and the filesystem listing names every data pool only from pve-manager `fbb0eb7657`.
- **APPLIES TO:** playbooks and helpers under `platform/playbooks/` that read `nodes/<node>/ceph/*` or other Proxmox endpoints passing Ceph data through.

### L-004 · 2026-11-17 · tooling

- **ACCEPTED:** #1200 (AC-965).
- **DERIVED:** rustup gives a directory override precedence over a `rust-toolchain.toml` in the same directory, so CI's `rustup override set` decides CI's compiler and the toolchain file decides only a workstation's. A Rust version change moves the file's `channel`, `Cargo.toml`'s `rust-version` and every `rustup toolchain install` and `rustup override set` in the Rust (tests) job together; `local_dev_setup::the_rust_toolchain_file_matches_cargo_and_ci` refuses a mismatch.
- **APPLIES TO:** Rust version bumps: `platform/tests/rust-toolchain.toml`, `platform/tests/Cargo.toml` and the Rust (tests) job in `.github/workflows/lint-and-test.yml`.

### L-005 · 2026-11-17 · tests

- **ACCEPTED:** #1195 (AC-951).
- **DERIVED:** The Rust test crate's library target sets `test = false` (`platform/tests/Cargo.toml`), so a `#[cfg(test)]` test inside `platform/tests/src/` never runs. Test every branch of a shared helper from a `suite/support_*.rs` module through its public API; a branch that real repository input cannot reach needs a public entry that takes made-up input, or it stays untested.
- **APPLIES TO:** shared support code in `platform/tests/src/` and the support PRs that add or change it.

### L-006 · 2026-11-17 · tests

- **ACCEPTED:** #1196 (AC-961, AC-963).
- **DERIVED:** A Rust test helper that replaces a Python or BATS one names in its doc every way its output differs from the original, found by running both on the same input; "same as the Python tests" is never written without that run. Python's `text=True` (`harness_sandbox.py`'s `run`) turns `\r\n` and a lone `\r` into `\n` and raises on bytes the locale's encoding cannot decode, so a port that assumes the two agree passes or fails on line endings alone.
- **APPLIES TO:** helpers in `platform/tests/src/` that stand in for a Python fixture or a BATS helper, and the port briefs that cite them.

### L-007 · 2026-11-17 · playbooks

- **ACCEPTED:** #1206 (AC-856).
- **DERIVED:** Proxmox's `GET /access/permissions` maps each held privilege to its propagate flag, so a privilege is held when its key is present, whatever the value (0 included); Proxmox's own permission check tests only that the key is defined. A check that reads the value as the grant refuses a token Proxmox would allow.
- **APPLIES TO:** playbooks and helpers under `platform/playbooks/` that read `access/permissions` to decide whether a token may act.

### L-008 · 2026-11-17 · playbooks

- **ACCEPTED:** #1206 (AC-856).
- **DERIVED:** A vmid that this cluster does not know is "gone" only on a storage no other cluster can write to: node-local storage, or CephFS on this cluster's own Ceph (no monitor list). On shared NFS, CIFS, PBS or an external CephFS the vmid may be another cluster's guest, and a storage whose sharing is unknown counts as shared. Work that deletes by "guest no longer exists" restricts itself the same way, or needs the operator to declare each pair. That no client outside this cluster's storage configuration mounts its own CephFS comes from the operator (operator, 2026-11-18), not from a Proxmox or Ceph read: no read the run makes shows such a mount, so the work states it as an assumption the operator confirms.
- **APPLIES TO:** playbooks that remove or prune Proxmox volumes because their guest is missing.

### L-009 · 2026-11-18 · tests

- **ACCEPTED:** #1186 (AC-949).
- **DERIVED:** On macOS (Darwin 25.5, 2026-11-18), `mktemp -d` ignores TMPDIR, both with no template and with `-t`: the directory is made under the per-user temporary directory. A test that points a script's temporary files at a scratch directory through TMPDIR puts a stand-in `mktemp` first on the script's PATH, one that passes an explicit template under `$TMPDIR` (`FAKE_MKTEMP` in `suite/pre_push_uv.rs`). Without it, a check that the script cleaned up reads a directory the script never used, and passes.
- **APPLIES TO:** port and hook tests that run a shell script under a scratch TMPDIR, such as `.githooks/pre-push` and the scripts it calls.

### L-010 · 2026-11-18 · tests

- **ACCEPTED:** #1204 (AC-972).
- **DERIVED:** A guard that recognises a whole template expression by regex anchors both ends and refuses, in any free-form tail, every character that could open another expression or call: `{`, `}`, `(` and `)`. Each refused character has a fixture that must fail.
- **APPLIES TO:** regex-based checks in `platform/tests/suite/` that accept a templated value only when it is exactly one expression (store lookups, guard bindings).

### L-011 · 2026-11-19 · o11y

- **ACCEPTED:** #1211 (AC-999).
- **DERIVED:** A Grafana alert whose query joins branches with `or` (the macro `inference_rule` in `alerts.yml.j2`: query A, then a threshold on A with reducer `last`) wraps the whole union in `count()` and thresholds it `gt 0` when the branches return different kinds of value. Without the wrap, `gt 0` compares each branch's own value: a hit ratio of exactly 0 is a firing branch and fails `gt 0`, so the stop line is missed (checked with promtool 3.1.0 on 2026-11-19: the raw union returned 288, 0.95 and 0.5 for three firing cases, and its zero-ratio case returned 0, which `> 0` dropped). `count()` returns a positive count for a non-empty union and no sample for an empty one, so the `gt 0` threshold passes exactly when any branch returns a series; the empty result is then the rule's no-data state, which must be `OK`.
- **APPLIES TO:** Grafana alert rules in `platform/services/o11y/deployment/templates/alerts.yml.j2` whose expression is an `or` of conditions that return different values (a count, a peak, a ratio). An `or` of indicators that each return 1 needs no wrap.

### L-012 · 2026-11-19 · tests

- **ACCEPTED:** #1212 (AC-997).
- **DERIVED:** `cargo fmt --check` does not check the modules under `platform/tests/suite/`: `build.rs` writes their `mod` lines to `$OUT_DIR/suite_modules.rs`, which `suite/main.rs` pulls in with `include!`, and `cargo fmt` does not follow an `include!` (`platform/tests/build.rs:3-9`), so it passes a misformatted one. A brief or DONE WHEN that proves a suite module's formatting names `rustfmt --check --edition 2024 platform/tests/suite/<module>.rs`, the check `suite/rust_format.rs` runs.
- **APPLIES TO:** briefs, DONE WHEN lines and local checks for any change under `platform/tests/suite/`.

### L-013 · 2026-11-21 · process

- **ACCEPTED:** #1214 (AC-1026).
- **DERIVED:** A ledger candidate is written down with the source that proves it (a PR comment or review id, a Plane comment's item and time, or a batch-index decision number). The ledger writer checks every fact at its source, and a chat message or a session's memory cannot be checked later.
- **APPLIES TO:** batch coordinators and owners keeping a ledger candidate list for a closing ledger pass.

### L-014 · 2026-11-26 · process

- **ACCEPTED:** #1226 (AC-1034).
- **DERIVED:** A review pass costs about the same whatever the size of the diff, because each pass agent starts with about 130k tokens of context before it reads the change. A two-line docs pull request took 1,524,367 reported tokens for two `/simplify` rounds, one `/security-review` and one review (coordinator's sum; `docs/MISTAKES.md` 5.25). Estimate pass cost as rounds × agents × that starting context, with the round counts of `AGENTS.md` Branch Workflow step 4.
- **APPLIES TO:** briefs and batch plans that estimate or cap review-pass tokens; any rule that sets how many `/simplify` or `/security-review` rounds a pull request runs.

### L-015 · 2026-11-26 · platform

- **ACCEPTED:** #1218 (AC-1025).
- **DERIVED:** An internal-CA leaf for a listener that another repository starts must exist, and stay current, before that listener serves TLS, because its launcher refuses to start TLS without the files (declared by that repository, not checked by this one). The renewal's "not in use, left alone" rule for a leaf nothing uses yet (`platform/playbooks/renew-internal-certs.yml:70-72`) would let such a leaf expire before the other side switches. A plan for such a leaf gives it an inventory switch meaning "the listener serves TLS now", and renews it with its host while the switch is off. The switch is declared, not read from the node, so the plan says that while it is off nothing compares the served leaf with the issued one, and how the build will close that gap (`production-internal-ca` design, decision 2 of 2026-11-21).
- **APPLIES TO:** planning any leaf in `internal_leaves` whose user is not deployed by this repository (the DGX Spark nodes today), and any change to the renewal's proof classes.

### L-016 · 2026-11-26 · tests

- **ACCEPTED:** #1219 (AC-1029).
- **DERIVED:** An "Enforcement moved" note names only the replacing Rust tests in code spans, as `platform/tests/suite/<module>.rs::<fn>`, and refers to the deleted file in plain words ("the BATS file this entry cites"). The ledger citation check reads every code span shaped like a test path or a `test_` name as a citation (`platform/tests/suite/ledger.rs`, parse rule in its module comment) and compares the unresolved ones against the ratchet as a multiset, so a deleted file's name put in a code span again is one more unresolved citation, and the check fails.
- **APPLIES TO:** any edit to `docs/MISTAKES.md` that mentions a deleted or renamed test, notes written by a port pull request included.

### L-017 · 2026-11-26 · playbooks

- **ACCEPTED:** #1221 (AC-924).
- **DERIVED:** A guard that reads a fact set by another play of the same playbook (`hostvars[...]._fact`) asserts the fact's full shape as its first task, with no `when` the fact's own value can skip. A `--limit` that leaves the setting play with no host, or a `--start-at-task` past it, skips that play without an error, so the fact arrives empty; a guard gated on `_fact | length > 0` then skips itself, and the writes in the plays that still run go ahead unchecked.
- **APPLIES TO:** multi-play playbooks under `platform/playbooks/` whose checks read a fact another play sets, before any write.

### L-018 · 2026-11-26 · playbooks

- **ACCEPTED:** #1217 (AC-930).
- **DERIVED:** A probe that must prove a TLS listener refuses a client without a certificate reads from the socket right after the handshake, before it writes anything, and counts only the alert itself (`certificate_required` under TLS 1.3, `handshake_failure` in a TLS 1.2 handshake) as the refusal. A request written after the listener has sent its alert and closed meets a closed socket, and the client then sees a reset, an EOF or a broken pipe, which one depending on the platform and on timing (a broken pipe on macOS, a reset or EOF on Linux, for the same Python `ssl` server). None of these says why the connection closed, and a listener that does not ask for a certificate can close the same way, so neither "count it as a refusal" nor "count it as an error" is right.
- **APPLIES TO:** any playbook or test under `platform/playbooks/` or `platform/tests/` that classifies a TLS failure as a client-certificate refusal.

### L-019 · 2026-11-26 · ci

- **ACCEPTED:** #1233 (AC-1095).
- **DERIVED:** Prove a fix for a CodeQL finding against CodeQL before the `dev` → `main` promotion. CodeQL runs here as GitHub's default setup, which analyses pull requests into `main` and not into `dev`, so a feature pull request gets no analysis of its own, and a finding first appears on the promotion as a review thread that must be resolved before the merge. A green CodeQL check on a pull request into `dev` is no proof: it can be the result of another pull request's analysis of the same head commit. To prove the fix, open a draft pull request into `main` from the branch (a pull request into `main` that is not a promotion needs the operator's OK), read its CodeQL check and the open alerts for `refs/pull/<n>/head` once every `Analyze` job has finished (the check can conclude early as neutral, "configurations not found", while an analysis still runs), then close it unmerged.
- **APPLIES TO:** a fix for a CodeQL finding, and a change that adds credential-like literals or names (a parameter or constant named like a password, secret or token), test fixtures included.

### L-020 · 2026-11-26 · playbooks

- **ACCEPTED:** #1228 (AC-1060).
- **DERIVED:** Raising a version default in an agent-cloud playbook (`github_runner_version | default(...)` in `deploy-github-runner.yml`) changes nothing on a host whose site-config inventory pins its own value, and production pins the runner version there. A version bump that must reach production plans the site-config pin change as its own pull request, and the deploy run follows that one, not the agent-cloud merge.
- **APPLIES TO:** version pins and other defaults in `platform/playbooks/` that site-config can override, and the plans that bump them.
