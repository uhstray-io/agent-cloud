# o11y PBS backup and protected copy implementation plan

## Scope

Implement four Dev-bound, exact-reviewed-source Semaphore workflows in a strict
sequence: create a fresh o11y backup on the declared primary PBS, verify the
exact artifact independently, pull it to a separately declared protected PBS,
then prove the destination can read it and is isolated from primary-side
deletion. This work does not provision either PBS guest, alter scheduled backup
jobs, change retention, reserve or restore a target, or claim immutable storage.

## Contracts and evidence

All workflows emit the closed `o11y-pbs-*/v1` receipt envelope and fixed
category fields. `platform/playbooks/files/o11y_pbs_receipts.py` owns receipt
validation, strict PBS manifest normalization, canonical artifact fingerprints,
freshness, and completed-task/output binding. Public receipts contain reviewed
source identity, inventory/source/artifact fingerprints, time bounds, fixed
role categories, and typed evidence only; private selectors and raw service
responses stay inside protected Semaphore task state. Receipts attest to
controller/template/source-admin/protected-environment/launcher trust, not to
cryptographic or immutable task history.

Semaphore assigns a task ID only after the playbook exits, so the producer
cannot truthfully include its own completed-task ID in its receipt. Receipt
evidence records the operation UPID and artifact identity. Consumers bind that
receipt to the authenticated completed-task ID returned by Semaphore, then
carry that ID as a fixed selector into the next workflow. No task ID is
discovered by guessing from a template/commit pair or self-declared by the
playbook.

For canonical artifact hashing, the pinned PBS Reader uses the fixed
`admin/datastore/o11y/download-decoded` route with the exact snapshot type, ID,
and time and `file-name=index.json.blob`. PBS performs DataBlob decoding; the
producer's duplicate-key rejecting parser validates the decoded official
manifest and returns only the normalized digest. The raw response stays in
the no-log parser call and never enters a receipt or visible task output.

The inventory provenance workflow compares the exact regular
`inventory/production.yml` Git blob at the caller-pinned private revision to the
sole current static Ansible inventory source's complete bytes. It verifies the
remote branch tip, tree entry type, exact public Semaphore bindings, and a
duplicate-safe static YAML parse, and returns hashes/categories only. Existing
site-config clone and OpenBao transport helpers are reused.

The source Reader identity is managed by the standalone
`Configure o11y PBS Copy Access (Dev)` workflow. It is a separate stable
user/token pair; primary backup setup remains independently runnable and does
not depend on this optional credential. Its token fields are additive under
`secret/services/pbs` (`o11y_copy_reader_*`) and never replace or share the
PVE `DatastoreBackup` token. Its only grants are the built-in
`DatastoreReader` role on `/datastore/o11y` for both user and token with
propagation disabled. Unknown, broader, or partially persisted state refuses;
the generated token is persisted and read back before its ACL is granted.

The same standalone workflow manages a separate destination copy-manager
identity. It is never reused by PVE or the primary Reader. It receives only
`Datastore.Audit` plus `Datastore.Backup` on `/datastore/o11y`, `RemoteAdmin` on `/remote` only for
remote creation, `RemoteAdmin` on `/remote/<declared remote>` for that remote's
configuration, and `RemoteSyncOperator` on its exact `/o11y` remote datastore
path. Every grant is non-propagating; it receives no `Datastore.Prune`. API
requests use the shared same-connection certificate-pinning helper, fixed
paths/methods, and bounded JSON responses. Secret request bodies remain in
protected Ansible memory and never enter argv or intermediary files. PBS
management writes use its native form encoding. `pbs_svc` and `pbs_copy_svc`
are independent pre-existing guests; this workflow refuses when either is
missing and provisions neither.

## Implementation order

1. Add and test the shared pure receipt/artifact module first; commit it so the
   isolated restore/cleanup consumer can merge and import it without duplicating
   schema rules.
2. Add guarded inventory provenance and a reusable fixed-input Ansible task;
   reject missing or ambiguous private/runtime inventory before authenticated
   service requests.
3. Add four fixed-contract playbooks using existing PBS, PVE, OpenBao, and
   Semaphore helpers. Backup and verification bind to one actual UPID and exact
   snapshot; destination sync is destination-initiated, preserves existing
   snapshots, and disables vanished-item removal and pruning.
4. Add four Semaphore catalog definition proposals as a new implementation
   artifact for later consolidated integration; do not edit the shared catalog
   file in this branch.
5. Add focused synthetic tests for malformed/mutated receipts and manifests,
   task/output linkage, chain ordering, private-inventory byte equality and
   cleanup, and refusal before non-read-only requests.

## Verification and activation gates

Run the focused tests, Ruff, YAML/playbook syntax checks, strict OpenSpec
validation, and changed-file/history secret scans. Any unsupported installed
PBS API, verification, sync, or readback schema remains a runtime activation
gate. Live Semaphore, OpenBao, Proxmox, PBS, deployments, policy application,
and private API reads are outside this implementation task. Isolated-restore
acceptance and seven-day capacity evidence remain separate gates.
