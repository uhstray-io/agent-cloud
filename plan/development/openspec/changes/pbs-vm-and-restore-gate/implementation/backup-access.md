# Task 4.2 — PBS backup access and PVE registration

## Scope

Provision the PVE o11y backup identity, persist its stable PBS API token in OpenBao, reconcile the narrow PBS ACL, and register the dedicated PBS storage through the Proxmox API. This task establishes access and configuration only. It does not redirect scheduled backup jobs; the existing destination and job configuration remain unchanged until task 4.3 proves a backup and verifies it.

## Identity lifecycle

Use the passwordless PBS realm user `o11y-backup@pbs` and stable token ID `pve-o11y`. The token owner must remain the named user. Treat the user and token as one identity. Read PBS version, user list, token list, and token configuration before mutation. Require installed PBS 4.2.8-1 semantics (or an explicitly reviewed compatible schema): the effective-permissions response must be parsed as supported current output, where `propagate` is a boolean property of each permission record, not a grant value. Fail closed on unsupported versions or fields. The relevant current-version contracts are the [PBS 4.2 user-management guide](https://pbs.proxmox.com/docs/user-management.html#effective-permissions) and [proxmox-backup-manager 4.2.8-1 manual](https://pbs.proxmox.com/docs/proxmox-backup-manager/man1.html).

Resolve exactly six states before deciding an action:

1. User and token absent, with no OpenBao copy: create the passwordless user and mint one token without ACLs; capture its secret privately; write and read back its identity, secret, datastore, and verified PBS certificate fingerprint in OpenBao using guarded CAS; only then grant ACLs.
2. User and token present, valid OpenBao copy matching the stable token identity: reuse the token, authenticate with it, validate its effective permissions, and reconcile only the exact intended ACL if absent.
3. Token exists but OpenBao copy is absent: refuse; do not remint or recover through a second path.
4. OpenBao copy exists but PBS token is absent: refuse; do not rotate automatically.
5. Stored identity, secret, user, or token metadata conflicts; secret is invalid; or token is expired: refuse and require a separately reviewed recovery/rotation procedure.
6. User exists while token is absent, or token exists while user is absent: refuse partial identity state; preserve it for investigation.

All token-bearing PBS CLI output, facts, registrations, tasks, and Ansible results stay inside scoped `no_log` credential steps. Never place token material in argv, environment, survey variables, output, cache, or an intermediary file. Use local PBS CLI operations as the configured root user through Ansible `become`; do not require or set a Linux root password.

## PBS ACL contract

Grant `DatastoreBackup` at exactly `/datastore/o11y`, with propagation false. Assign the ACL separately to both `o11y-backup@pbs` and `o11y-backup@pbs!pve-o11y`; require their effective permission intersection to authorize backup. Read back the complete effective-permissions result for both identities after mutation and before any PVE registration.

The validator accepts only the expected backup privilege at the exact datastore path. It rejects unknown privileges, grants on another datastore, inherited or ancestor paths, propagation, and destructive or retention-changing privileges. PBS effective-permission JSON reports privilege names with their `propagate` boolean; a privilege such as `Datastore.Prune` is present as an actual grant even when `propagate` is false, so any prune/delete/admin privilege is denied. Do not interpret the boolean as whether a privilege is granted.

Grant order is: prove runtime version/schema, read current users/tokens/ACLs, create or reuse the identity, persist/read back token in OpenBao, validate authenticated token identity, validate current grants, add only missing exact ACL entries, then read back and validate both effective-permission sets. A CAS conflict or unknown concurrent OpenBao state aborts before the PBS ACL grant. Use only a guarded OpenBao merge with compare-and-set; if the shared guarded merge helper cannot guarantee CAS, stop and report the blocker instead of falling back to an unguarded create/update.

## PVE storage registration

Before writes, read the exact target storage ID, its full node configuration, all existing backup jobs, and node membership. Register only the private declared storage identity using HTTPS API form fields: type `pbs`; private storage ID; PBS server, port 8007, datastore `o11y`; username `o11y-backup@pbs!pve-o11y`; stored token secret; previously verified SHA-256 TLS fingerprint; content `backup`; declared private node list; prune-backups `keep-all=1`.

Obtain the certificate fingerprint from authenticated guest certificate information. A direct PBS auth probe must validate the CA or pin and verify the fingerprint before sending authentication headers, on the same connection. A probe that disables certificate checks without a bound fingerprint is forbidden.

If the storage ID is absent, create it once, then re-read all state. If a concurrent create wins, accept only an exact full readback match. If it exists already, do nothing only when every managed field matches exactly. Refuse mismatches, duplicate IDs, unknown or malformed configuration, fingerprint mismatch, and API responses that fail exact readback. Preserve every pre-existing storage and every backup job byte-for-byte. Do not redirect, edit, disable, or schedule any existing job in task 4.2.

## Check mode and verification

Check mode performs the same read-only PBS version/schema, identity-state, full effective-permissions, certificate, PVE storage, and backup-job preflights. It reports a fixed sanitized plan or refusal; it performs zero PBS mutations, OpenBao writes, or PVE API writes.

Focused tests use pure state/permission planners and mock PBS CLI, OpenBao CAS, and PVE HTTP boundaries. Cover all six identity states; malformed, duplicate, ancestor, cross-store, propagated, unknown, prune, and delete grants; OpenBao CAS conflict ordering; token mismatch; fingerprint mismatch with proof that no credential-bearing request was sent; exact registration rerun; create race with exact/mismatched reread; unchanged backup-job/storage snapshots; and zero mutation in check mode.

## Boundaries and dependencies

Use the existing reviewed PBS bootstrap sequence only through its checked-out, guarded Semaphore Dev path. Keep private hostnames, IPs, storage IDs, node names, and other real inventory values in site-config. Do not change the shared Semaphore catalog or inventory in this task. Task 4.3 must separately verify a successful backup and artifact before scheduled cutover; isolated restore and retention growth remain later gates.
