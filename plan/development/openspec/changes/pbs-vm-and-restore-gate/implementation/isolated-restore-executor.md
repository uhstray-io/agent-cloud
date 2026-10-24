# Isolated o11y restore executor source plan

**Status:** source implementation complete; review, CI, publication, and live acceptance remain open
**Scope:** OpenSpec `pbs-vm-and-restore-gate`, task 5.2, bounded source child
**Integration:** consumer worktree started from the exact `dev`-derived feature base and normally merged foundation and shared PBS receipt commits; local implementation checkpoints remain unpublished.

This record covers source implementation and focused local verification only. It
does not close OpenSpec 5.2 or establish any Semaphore, OpenBao, Proxmox, private
inventory, protected-copy, capacity, or live restore/cleanup acceptance.

## Goal and safety boundary

Add two separate Dev-only Semaphore workflows: one restores a freshly
authenticated exact PBS artifact to the privately declared disconnected target
and proves the historical guest data, and a later task authenticates the
completed restore task before deleting only the stopped VM that the restore
workflow created. Neither workflow can select production o11y, write to the
source VM, or retry an uncertain mutation.

The public checkout contains workflow logic and fixed schemas only. Hostnames,
VMIDs, artifact identity, Semaphore receipts, OpenBao values, and live task IDs
remain private. Public tests use synthetic fixtures. A failure after a possible
Proxmox mutation retains the VM and coordination record for operator review.

## Implementation sequence

1. Extend the coordination helper and include with a strict schema version 2
   action journal. Each Proxmox lifecycle write requires an intent CAS before
   the request and a returned-handle/effect CAS after it. Journal restore UPID
   independently from later start, isolation, guest-exec, stop, and delete
   handles. Existing schema 1 records are refused; no migration, nonce adoption,
   timeout takeover, or cleanup recovery is inferred.
2. Add pure guest-inspection and task-output validators. Consume the
   producer-owned `o11y_pbs_receipts.py` contract for artifact identity,
   closed receipt validation and actual runtime-inventory byte provenance;
   do not duplicate those rules in the consumer. The guest checker uses
   a closed bounded input schema, fixed commands/paths, rootless Podman volume
   ownership and mount identity checks, and historical backend data readability
   and integrity checks. Output is limited to fixed categories, counts, and
   hashes. The task validator authenticates one completed successful Semaphore
   task against the expected project and task ID, reviewed public commit,
   bounded times/output, fixed contract/playbook attestation, and one unique
   complete bounded receipt. Semaphore does not retain historical
   environment/inventory/repository/playbook bindings; current metadata is not
   treated as historical evidence. This is a cooperating trusted-controller
   attestation, not a cryptographic or immutable-history claim.
3. Add `restore-o11y-isolated.yml`. Validate internal/private launch inputs and
   the exact reviewed checkout before secret lookup. Authenticate prior backup,
   PBS verification, protected-copy sync, and copy-readiness task outputs in
   strict order. Enforce the fixed 24-hour artifact age. Inspect the exact
   artifact configuration and reject unsupported restore inputs before target
   reservation. Recheck target capacity and VMID availability, reserve with
   version CAS, restore stopped, apply and read back disconnected isolation,
   start only the target, run fixed guest inspection through QGA, stop the target,
   and CAS the receipt-bound `awaiting_receipt` state. Print one private
   identity-bearing receipt only after all proofs succeed.
4. Add `cleanup-o11y-isolated-restore.yml` as a separately launched operation.
   Authenticate the exact completed restore task and receipt, compare its full
   coordination snapshot/version with fresh OpenBao state, verify the owned
   target is stopped and unchanged, transfer CAS ownership to cleanup, record
   intent and the real `qmdestroy` UPID, prove successful task completion and
   exact authoritative VMID absence, then CAS `cleaned`.
5. Add bounded Dev-only Semaphore catalog templates, operator runbook, and
   focused tests for transition/CAS matrices, writer ordering, URI defaults,
   forged/stale/duplicate task output, restore isolation and no-retry behavior,
   guest historical-data integrity, and owned-target-only cleanup.

All five source steps are now implemented in the consumer worktree. The shared
receipt validator's explicit playbook-mode API and exact restore/cleanup launch
shapes are integrated from the producer's normal merge. The restore launch binds
four separately authenticated prerequisite task IDs plus the public and private
revision pins. Cleanup derives the receipt from the authenticated restore task
output and accepts only four selectors; historical restore and current cleanup
private pins are independently proven and need not be equal. Tests cover their
allowed difference and rejection when either proof or target/declaration binding
is invalid. The playbooks and catalog remain source-only until review, CI,
publication, and private gates pass.

## Acceptance for this source child

- Source changes are limited to consumer-owned files plus the explicitly
  integrated producer and foundation merge commits.
- Focused tests exercise the production validators/checkers and prove refusal
  occurs before the first possible write for invalid inputs.
- Source checks establish YAML/Python validity and OpenSpec structural validity.
- Diff and introduced-history scans contain no private identities, credentials,
  live receipts, or telemetry content.
- OpenSpec 5.2 stays open. The separate protected-copy live proof while primary
  PBS is unavailable, effective OpenBao role/capability readback, all-writer
  rollout/drain, actual Dev task execution, and seven-day capacity gate remain
  activation/acceptance prerequisites.

## Stop conditions

Stop and retain the target and journal on any unknown Proxmox response, failed
or ambiguous readback, stale journal version, missing task binding, incomplete
receipt, unexpected config/device/mount/volume ownership, unavailable or empty
historical backend data that was expected, or unproven authoritative absence.
Do not auto-retry restore or cleanup, adopt an existing nonce, infer absence from
an arbitrary API error or filtered list, SSH to the guest, install packages,
pull images, recreate volumes, or manually deploy containers.
