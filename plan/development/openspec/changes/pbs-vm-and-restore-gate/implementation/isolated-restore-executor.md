# Isolated o11y restore executor source plan

**Status:** source implementation complete; corrected-head CI and live acceptance remain open
**Scope:** OpenSpec `pbs-vm-and-restore-gate`, task 5.2, bounded source child
**Integration:** consumer worktree started from the exact `dev`-derived feature base and normally merged foundation, shared PBS receipt, capability, and `dev` commits. Its current Dev history includes PR #728 merge `06ad8c32358518a1dc52181fde10439a8d431980` and PR #733 merge `e68ca3de948914cb5ba820c92e12627277d468d6` (reviewed PR #733 head `32d5d49e5b01d7c0e5811738aef7f4cbd93ddf17`). Both are now normally merged into the consumer feature branch. This source work is tracked in PR #734; CI on the corrected head remains pending.

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
   strict order. Derive the artifact locator only from authenticated readiness,
   then compare fresh PVE content, the PBS manifest, the destination Reader
   identity, and PVE storage fingerprints to that receipt. Refuse legacy
   inventory or caller selectors. Enforce the fixed 24-hour artifact age.
   Inspect the exact artifact configuration, admit only the reviewed cloud-init
   provisioner shape plus `serial0: socket` and canonical PVE
   `meta: creation-qemu=<version>,ctime=<epoch>`, and reject other serial or
   metadata settings before target reservation. Recheck target capacity and VMID availability, reserve with
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
revision pins. Dynamic artifact location comes from copy readiness, while PBS
host/storage placement and target identity remain private static configuration.
Restore uses the dedicated destination Reader, not the copy Manager, for PBS
manifest reads. Cleanup derives the receipt from the authenticated restore task
output and accepts only four selectors; historical restore and current cleanup
private pins are independently proven and need not be equal. Tests cover their
allowed difference and rejection when either proof or target/declaration binding
is invalid. Independent reviews covered the full PBS/restore and capability
source scopes; actionable findings were addressed and verified. Final
combined-head CI remains pending. The playbooks and catalog remain source-only
until CI, publication, and private acceptance prerequisites pass.

## Pre-publication verification follow-up

An earlier 420-case focused helper suite did not exercise all cross-cutting
playbook-discovery contracts. Before publication, the bounded cohort therefore
also includes the full `platform/tests/test_check_mode_contract.py`,
`platform/tests/test_extra_var_refusal_tests.py`,
`platform/tests/test_pve_token_id_required.py`, and
`platform/tests/test_o11y_restore_target_guard.py`, plus the full
`ansible-lint platform/playbooks/ platform/semaphore/` command. These checks
found missing explicit check-mode handling on read probes and mutation guards,
new PVE-token playbooks absent from the
coverage table, and an old-schema fixture inconsistent with the strict schema-2
record. The fixes preserve the production guards and broaden exact coverage;
they do not relax the tests. The local check-mode run before producer-branch
integration exposed six producer-owned failures. After the approved producer
and Dev merges, the complete check-mode module passed (367 tests). The restore
target-guard, extra-var refusal, and PVE-token modules passed together (113
tests). Ruff passed for the three changed Python test modules. Full Ansible
lint exited 0 with four warnings (three Jinja-formatting warnings and the
restore play's task-count warning); syntax checks passed for all six affected
entrypoints, with expected host-pattern warnings on PBS inventory patterns.
Strict OpenSpec status showed all four planning artifacts complete and
validation passed. The earlier PR CI run remains failed on its old head; these
local source checks do not replace CI on the corrected head or establish
runtime acceptance.

## Independent review corrections

A bounded independent review of integrated head `91eddf9794ded04dba45bcc64eca173e3624f134`
identified three caller-path defects. The destination Reader saved-key fact is
now established in its own `set_fact` task before the record builder consumes
it. Copy-readiness manifest calls explicitly select the fixed `source` and
`destination_restore` profiles; the helper continues to reject a Manager under
the destination restore profile. Artifact parsing, target preflight, and target
readback share the same exact `serial0: socket` and PVE creation-meta predicate,
and reject arbitrary serial settings and extra, reordered, or malformed meta
fields. Regression tests execute the actual Reader task sequence under local
Ansible, render the actual copy-readiness manifest task into the PBS helper, and
exercise artifact and target-readback validators with accepted and refused
configurations. On the corrected source, the bounded combined restore/PBS,
gateway, and cross-cutting cohort passed 1,033 tests plus 46 subtests. The
corrected feature head still requires final CI and does not establish any live
PBS, Proxmox, or protected-restore acceptance.

CI run `40079059919` on head `a76c1ef4` failed one Python-core check:
`test_no_recursive_task_vars.py::test_no_task_var_resolves_to_itself` found a
self-referential `site_config_base_sha` override in the cleanup provenance
include. The complete job log showed 5,517 passed, 5 skipped, 283 subtests, and
that single failure. The aggregated `gh run view --log-failed` output was
truncated, so diagnosis used the complete job log. The cleanup include now
inherits its already-validated current launch pin directly, matching restore's
include pattern. A caller-path regression executes the selected cleanup include
and the real provenance selector assertion with distinct current and historical
pins; side-effecting provenance tasks are stubbed. The regression, cleanup
pin-independence test, and recursive-task-var guard passed together (3 tests).
These follow-up checks do not replace CI on the corrected head.

The next review found that cleanup's target-config adapter lacked the source
cloud-init expectation required by the shared target validator and therefore
refused every real cleanup. Cleanup now has a separate closed
`cleanup_target_config` operation: it first matches the canonical full current
configuration digest to `isolation_config_sha256` from the authenticated restore
receipt, then derives auxiliary presence from those exact bound bytes and runs
the existing target ownership, disk, storage, and isolation checks. The restore
`target_config` contract and v3 receipt remain unchanged. A local Ansible
regression runs the real restore receipt builder and selected cleanup tasks;
backed and explicit `none,media=cdrom` auxiliary cases reach only a synthetic
delete-gate sentinel, while post-receipt changes and wrong hash, auxiliary,
disk, marker, target, or journal cases stop before it. The full restore
coordination module passed (18 tests), the adjacent target-config cohort passed
(3 tests), Ruff and both playbook syntax checks passed, and targeted Ansible lint
exited 0 with its existing complexity warning. CI run `40081723791` was green
on the pre-review `7a66e13` head; this review correction requires a new final-head
CI run. These checks remain source-only and do not establish live cleanup or
restore acceptance.

Strict branch-history TruffleHog scans from base `391a4f7c` used
`--no-verification --fail --fail-on-scan-errors --no-update` and reported zero
verified or unverified findings. TruffleHog logged that sandbox policy blocked
temporary-process cleanup after the scans had completed.

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

The current restore entrypoint has no primary-PBS outage control or evidence
field in its v3 receipt. Its success therefore cannot satisfy OpenSpec 4.4c;
the outage condition and its evidence require a separately reviewed design.

## Stop conditions

Stop and retain the target and journal on any unknown Proxmox response, failed
or ambiguous readback, stale journal version, missing task binding, incomplete
receipt, unexpected config/device/mount/volume ownership, unavailable or empty
historical backend data that was expected, or unproven authoritative absence.
Do not auto-retry restore or cleanup, adopt an existing nonce, infer absence from
an arbitrary API error or filtered list, SSH to the guest, install packages,
pull images, recreate volumes, or manually deploy containers.
