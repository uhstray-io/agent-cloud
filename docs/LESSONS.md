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

### L-004 · 2026-11-17 · playbooks

- **ACCEPTED:** #1206 (AC-856).
- **DERIVED:** Proxmox's `GET /access/permissions` maps each held privilege to its propagate flag, so a privilege is held when its key is present, whatever the value (0 included); Proxmox's own permission check tests only that the key is defined. A check that reads the value as the grant refuses a token Proxmox would allow.
- **APPLIES TO:** playbooks and helpers under `platform/playbooks/` that read `access/permissions` to decide whether a token may act.

### L-005 · 2026-11-17 · playbooks

- **ACCEPTED:** #1206 (AC-856).
- **DERIVED:** A vmid that this cluster does not know is "gone" only on a storage no other cluster can write to: node-local storage, or CephFS on this cluster's own Ceph (no monitor list). On shared NFS, CIFS, PBS or an external CephFS the vmid may be another cluster's guest, and a storage whose sharing is unknown counts as shared. Work that deletes by "guest no longer exists" restricts itself the same way, or needs the operator to declare each pair.
- **APPLIES TO:** playbooks that remove or prune Proxmox volumes because their guest is missing.
