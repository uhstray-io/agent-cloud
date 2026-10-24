# Isolated o11y restore executor runbook

The restore and cleanup entrypoints are implemented as source workflows for
OpenSpec 5.2. This runbook is not activation or live acceptance evidence. Keep
5.2 open until review and CI pass, the feature is published to Dev, private
declarations are accepted, OpenBao capabilities and writer coverage are
verified, protected-copy recovery passes with primary PBS unavailable, and a
Dev-bound restore and separate cleanup succeed.

## Restore transaction

1. Reject all forged internal/private identity, task evidence, URL, TLS,
   credential and declaration variables before credential lookup. Require the
   exact reviewed clean checkout.
2. Authenticate the exact completed backup, PBS verification, protected-copy
   sync, and copy-readiness Semaphore tasks in that order. The authenticated
   task must match the expected project, task ID, successful completed status,
   reviewed public commit SHA, safe launch fields, and ordered bounded output.
   Require exactly one closed, fingerprint-valid receipt whose fixed
   contract/playbook is allowlisted at that reviewed SHA. Compare its exact
   artifact fingerprint with the selected artifact and require exact private
   inventory-byte provenance. Current template and repository metadata prove
   only current configuration; Semaphore does not retain historical values
   for those bindings. This is a trusted-controller attestation, not an
   immutable-history or cryptographic-signature claim. The shared prerequisite
   proof module owns the canonical receipt schema and artifact fingerprint.
   Missing or ambiguous task/output/API evidence refuses.
3. Read the selected artifact's exact configuration and validate its complete
   included disk set before reservation or restore. Refuse hooks, extra
   arguments, passthrough devices, host/shared mounts, excluded disks, CD-ROMs,
   and unrecognized disk layouts.
4. Recheck artifact freshness against the fixed 24-hour limit, exact VMID
   absence, disconnected target configuration, node and storage availability,
   and capacity. Only then reserve the five-field target
   declaration, target identity, exact artifact, source revision, private
   inventory revision, and fresh owner nonce with KV-v2 CAS.
5. CAS a restore intent, send one `qmrestore` request with overwrite, start,
   live-restore, HA management and uniqueness disabled, then retain its exact
   UPID and verify task completion, stopped VM state, node and VMID. An
   ambiguous response leaves the claim and target untouched for operator
   review.
6. CAS isolation intent, remove every NIC and host escape device, force
   onboot/startup off, set the ownership marker, read back the full target
   config and disks, and record the exact response/config fingerprints.
7. CAS start intent, start only the target, verify its start UPID and running
   state, then recheck the target config and isolation fingerprint.
8. Through QGA only, run the exact reviewed guest checker with bounded
   historical-data expectations derived from authenticated prerequisite
   receipts. Require rootless ownership and mount proof for all five Compose
   volumes, read-only Grafana SQLite integrity, and bounded native historical
   Prometheus, Loki, Tempo and Pyroscope data reads at the backup timestamp.
   Explicit historical `quiet` expectations are permitted only when the
   authenticated receipts prove them; unavailable, empty when expected,
   malformed, or unreadable evidence fails.
9. CAS stop intent, stop the target, verify the exact stop UPID, `stopped` state,
   and unchanged isolation config. Store a complete private receipt in the
   authenticated Semaphore task output; the public summary exposes only fixed
   categories, counts and hashes. Require the Semaphore task to complete
   successfully before cleanup is offered.

## Separate cleanup transaction

Cleanup accepts exactly four selectors: the current cleanup code SHA, current
private site-config SHA, successful restore task ID, and reviewed restore code
SHA. It accepts no caller receipt, fingerprint, VMID, locator, nonce, or target
binding. It re-fetches the task, complete output, template, and repository
metadata using OpenBao-sourced Semaphore credentials and the pinned HTTPS
endpoint. The authenticated historical task environment and receipt bind the
restore's private pin and public SHA. Cleanup independently proves that the
current runtime inventory bytes match its current private pin; the two private
pins may differ when unrelated private changes occur. It then compares the full
receipt coordination record/version against fresh OpenBao state, verifies the
same owned target is stopped with unchanged marker, disks, isolation and no
pending mutation, and CAS-transfers ownership to a fresh cleanup nonce.

After a cleanup intent CAS, send only the exact target's `qmdestroy` request
with an exact-UPID return. Require the task to stop with `exitstatus=OK`, then
prove exact VMID absence with the authoritative node config 404 and successful
`nextid` echo for the same VMID. Arbitrary 500 responses, filtered list
absence, task errors, timeouts and malformed reads are not absence proof.
Finally CAS the terminal `cleaned` record. Every ambiguous condition preserves
the target and journal; no retry, TTL takeover, nonce adoption, automatic rescue
delete, or source VM operation is allowed.

## Required source receipt contracts

The upstream backup, PBS verification and protected-copy workflows emit
versioned receipts through the shared proof module. Restore authenticates them
in this fixed order: backup, exact-snapshot PBS verification, destination-initiated
protected-copy sync with `remove-vanished=false`, and independent destination
readiness. Readiness leaves `final_acceptance=pending_isolated_restore`; the
isolated restore must prove recovery while the primary PBS guest is unavailable.
The shared module owns the canonical receipt envelope, exact-byte inventory
provenance check, and stable PBS artifact fingerprint. The restore executor
consumes that interface and rejects omitted, unknown, stale or ambiguous proof.
The inventory provenance task must read the actual `ansible_inventory_sources`
static YAML bytes used by the running Semaphore invocation, compare them byte
for byte with `inventory/production.yml` from the observed, reviewed
site-config Git commit, and derive `private_sha`, `inventory_provenance`, and
`inventory_fingerprint` from those observed bytes. Comparing a Git clone only
with the current Semaphore API inventory record is insufficient because that
record may differ from the file used for this run. The source task reuses the
existing OpenBao-sourced deploy-key clone helper, pins `site-config` `main` to
the reviewed commit, checks its observed clone HEAD and remote tip, rejects
inventory symlinks and non-static/multiple runtime sources, and removes the
scratch clone in `always`. It does not invoke a push helper. Live activation
remains blocked until those prerequisite workflows and source-side exact-byte
inventory proof are reviewed, published, and accepted. Effective OpenBao role,
writer-drain, protected-copy failover, target-placement and live
restore/cleanup evidence also remain open. No live service was exercised as
part of this source implementation.
