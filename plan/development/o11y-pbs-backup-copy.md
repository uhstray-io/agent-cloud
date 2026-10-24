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

Authority is split by the kind of fact: Git and the exact private inventory
revision declare static placement (endpoints, datastore, PVE storage ID, and
nodes); authenticated predecessor receipts identify the completed backup
artifact; live API readbacks prove where that artifact currently appears and
whether its contents are readable. The dynamic artifact locator is derived
from the authenticated backup VMID/time and the declared destination storage
ID. A per-snapshot private selector is neither required nor accepted; a legacy
selector still present in inventory causes an explicit migration refusal.
Private inventory bytes and SHA remain identical across the task chain.

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
identity. It is never reused by PVE, the source Reader, or the destination
restore Reader. It receives only `Datastore.Audit`, `Datastore.Backup`, and the
built-in `DatastoreReader` role (effective `Datastore.Read` and
`Datastore.Verify`) on `/datastore/o11y`; the Reader role is needed for
destination manifest readback. It also receives `RemoteAdmin` on `/remote` only for
remote creation, `RemoteAdmin` on `/remote/<declared remote>` for that remote's
configuration, and `RemoteSyncOperator` on its exact `/o11y` remote datastore
path. Every grant is non-propagating; the manager has no `Datastore.Prune` or
broad datastore role, and its credential is never used by PVE. API
requests use the shared same-connection certificate-pinning helper, fixed
paths/methods, and bounded JSON responses. Secret request bodies remain in
protected Ansible memory and never enter argv or intermediary files. PBS
management writes use its native form encoding. `pbs_svc` and `pbs_copy_svc`
are independent pre-existing guests; this workflow refuses when either is
missing and provisions neither.

The destination restore Reader is a fourth PBS identity with its own token
fields under `secret/services/pbs`. Both its user and token receive only the
built-in `DatastoreReader` role on `/datastore/o11y`, propagation disabled.
The reviewed Proxmox role composition is `Datastore.Audit`, `Datastore.Read`,
and `Datastore.Verify`: it can inspect/list the datastore, read arbitrary
backup content, and verify snapshots, but grants no backup creation, prune,
delete, or remote administration. The restore credential is persisted by
OpenBao CAS and read back before ACL grants. A separate Dev-bound registration
workflow reads PVE API authority from `secret/services/proxmox`, registers only
the static declared storage, and puts only the destination restore token into
PVE. It preserves existing storage rows and backup jobs and proves content
access through every declared node.

Credential creation and API access use separate paths. The management workflow
runs `proxmox-backup-manager` only on the authenticated PBS guests to create the
fixed user/token objects and apply exact ACL entries. Token material is captured
from stdout only inside `no_log` task state, merged into the existing
`secret/services/pbs` record with CAS, read back, and then authenticated through
the pinned HTTPS helper before ACL changes. Existing-but-unrecorded tokens,
partial identities, or broader effective grants refuse without rotation. API
configuration requests reuse that same-connection pinning transport; the
certificate is checked before an auth header or secret body is sent.

The primary PBS API contract is explicitly version-gated to the reviewed
4.2.8-1 package. The helper uses the documented snapshot verification API and
the PBS-decoded manifest download route, so DataBlob decompression remains
inside PBS and only a strict normalized manifest digest leaves the protected
request state. No transport disables verification without binding the exact
peer certificate on that request.

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
4. Add the real Semaphore catalog declarations for Configure o11y PBS Copy
   Access and Register o11y PBS Restore Storage; keep selectors and credentials
   out of surveys.
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
