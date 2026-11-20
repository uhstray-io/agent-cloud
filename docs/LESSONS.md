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

### L-011 · 2026-11-19 · tests

- **ACCEPTED:** the strict-opening sudo-password check (AC-997).
- **DERIVED:** `cargo fmt --check` does not check the modules under `platform/tests/suite/`: `build.rs` writes their `mod` lines to `$OUT_DIR/suite_modules.rs`, which `suite/main.rs` pulls in with `include!`, and `cargo fmt` does not follow an `include!` (`suite/rust_format.rs:3-6`), so it passes a misformatted one. A brief or DONE WHEN that proves a suite module's formatting names `rustfmt --check --edition 2024 platform/tests/suite/<module>.rs`, the check `suite/rust_format.rs` runs.
- **APPLIES TO:** briefs, DONE WHEN lines and local checks for any change under `platform/tests/suite/`.
