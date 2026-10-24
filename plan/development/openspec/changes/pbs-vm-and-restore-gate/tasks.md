## 1. Discover and reconcile the target

- [x] 1.1 Add a Dev-bound GET-only Proxmox template/VM/store survey using the existing OpenBao credential, exact reviewed SHA, fixed sanitized visible counts, explicit unverified inventory visibility, and refusal tests for missing or ambiguous evidence; address authority is checked in 1.3.
- [x] 1.2 Review, merge, publish, and run the survey from Dev; record the exact task and evidence in the observability plan.
- [ ] 1.3 Resolve the PBS static IP and VMID through NetBox/IPAM and pfSense DHCP checks; declare one PBS host, Debian template, node, OS disk, selected data store, 512-GiB disk, and operator physical-origin attestation in private site-config. Review, merge, sync, and read back the exact inventory bytes.
- [x] 1.4 Declare a distinct `proxmox_pbs_import_storage_id` in private inventory, then run the reviewed Dev-bound import-content workflow. Task 7128 proved the update was needed in read-only check mode; task 7129 applied the observed-digest update and verified readback; task 7131 proved a second run changed nothing. The follow-up GET-only survey, task 7132, found active import candidates. All four ran at reviewed Dev `a7f348067bdd3cec935f8ae64ceeb1e49492c7c3`. Task 7106 had refused before any Proxmox request, and the earlier task 7081 had found zero active import stores.

## 2. Provision a supported PBS VM

- [ ] 2.1 If discovery finds no supported Debian cloud-init template, add a pinned-image, idempotent Semaphore template-publication workflow and verify its Proxmox readback before VM creation.
- [x] 2.2 Extend the shared VM provisioning declaration only as needed for the Debian template; refuse conflicting VMID, node, address, template, or existing disk before any write. Add focused failure and rerun tests.
- [ ] 2.3 Run the reviewed Dev-bound VM provisioner for only the declared PBS host; verify the Proxmox task, VM config, static address, guest OS, and unchanged o11y VM.

## 3. Attach and mount the data disk

- [x] 3.1 Add a separate idempotent Dev-bound virtual-disk attachment workflow that re-runs the pinned preflight immediately before allocation, checks the exact guest/bus/store, and refuses concurrent drift or existing mismatched disks. Test refusal and safe rerun. **Source complete:** PR #715 passed final CI and CodeRabbit review, then merged to `dev` at `621396b660c120056fc835900d70a38618f87535` (reviewed head `bc0ca6d3b29ee1db8ef78bcfccf8a0e7f75451bc`). Follow-up serial-aware attachment changes are included in the independently reviewed guest source head `05acf2269081cf9e273fb77c0a5d1ffbd66ad280` and integrated here by merge `c07a91e5`; the allocation request carries `serial=ac-pbs-data-<vm_vmid>` and exact readback requires it. That follow-up is not yet merged to Dev or published/read back there, so no live disk allocation is authorized. This checkbox records source completion, not live acceptance.
- [ ] 3.2 Attach only the 512-GiB guest disk through Proxmox API and verify its task/config readback; never select or format a host physical device.
- [ ] 3.3 Add guest-side blank-disk identity and content guards, filesystem creation, UUID mount, and readback. Run only after the new virtual disk is proven unique; verify a second run preserves the data.
  - [x] 3.3a Source workflow: independently reviewed guest head `05acf2269081cf9e273fb77c0a5d1ffbd66ad280` and service head `4b4ef07c7bee21adee14ec55900de56ba5a76d28` are integrated by normal merges `c07a91e5` and `72f7a24b`; serial-to-device, UUID mount, no-follow path checks, helper checksum, and source tests are recorded in `plan/development/05-observability.md`.
  - [ ] 3.3b Live acceptance: after 3.2 is complete and serial-aware attachment source is merged/published/read back from Dev, run guest preparation twice and verify preserved data, exact UUID mount, and datastore path. No PBS VM, data disk, filesystem, or mount has been validated live.

## 4. Configure PBS and backup access

The bounded Dev publication and bootstrap sequence is in
[`implementation/dev-bootstrap-publication-runbook.md`](implementation/dev-bootstrap-publication-runbook.md).
Its prerequisites are not accepted yet; the live acceptance tasks below remain
open.

- [ ] 4.1 Add supported Debian PBS package, TLS, service, datastore, and maintenance configuration through Semaphore with OpenBao-sourced secrets and scoped `no_log`; read back guest mount and PBS health.
  - [x] 4.1a Source workflow: reviewed service head `4b4ef07c7bee21adee14ec55900de56ba5a76d28` is integrated by merge `72f7a24b`; the exact-SHA Dev catalog entry, serial/UUID mount checks, datastore containment checks, repository gate, and read-only verify path are documented in `plan/development/05-observability.md`.
  - [ ] 4.1b Live acceptance: publish/read back the catalog, configure only the declared PBS guest, then verify mount UUID, PBS health, datastore and maintenance state. Enterprise requires an already active subscription; no-subscription requires explicit inventory acceptance. No PBS service or repository subscription has been validated live.
- [ ] 4.2 Create and verify a datastore-scoped PVE backup credential without prune/delete rights; register the PBS target through code and preserve the existing backup destination until verified.
  - [x] 4.2a Source workflow: reviewed/fix-verified backup-access head `9f6b041a4f85ef56b92f7fe14cc7001c1b9c1fd5` is integrated by normal merge `4f02fdc0`; exact `/datastore/o11y` `DatastoreBackup` ACLs, OpenBao lifecycle, PVE readback and verify-only refusal rules are documented in `plan/development/05-observability.md`.
  - [ ] 4.2b Live acceptance: verify actual credential, ACL, PVE storage registration, and unchanged backup jobs on the declared hosts. No live identity, token, ACL, or PVE storage has been created or verified.
- [ ] 4.3 Run one o11y backup to PBS and verify a fresh artifact, successful task, source VMID, content accessibility, and PBS-side verification result using sanitized receipts.
- [ ] 4.4 Implement and verify a local protected-copy mechanism separate from the PVE backup credential. Document its threat boundary; do not call a PBS protected bit or token ACL immutable-copy proof by itself.
  - [x] 4.4a Planning candidate: a second local PBS guest with its own datastore/admin boundary, destination-initiated pull, source `DatastoreReader`, `remove-vanished=false`, and initially disabled independent pruning. This is a candidate, not a declared or provisioned target.
  - [ ] 4.4b Obtain GET-only capacity/placement evidence and verify installed PBS sync schema before declaring the second target. Keep the separate seven-day growth/retention forecast gate.
  - [ ] 4.4c Implement the protected destination workflow and verify destination-side protection plus an isolated restore while the primary PBS guest is unavailable; document that destination PBS/hypervisor admin and shared backing storage remain trusted failure domains.

## 5. Prove isolated restore and preserve growth gates

- [ ] 5.1 Declare and preflight a distinct restore target with sufficient capacity and no conflicting VM or network identity.
  - [x] 5.1a Source workflow: merge-reviewed GET-only `Preflight o11y Restore Target (Dev)` validates a private disconnected target and one exact selected artifact. It is available in source only; no target is declared, reserved, or live-accepted.
  - [ ] 5.1b Private declaration and live acceptance: declare the exact target and artifact in private inventory, publish/read back the catalog, and confirm current VMID availability, capacity and identity collision checks. The target is disconnected; VMID availability authority and ACL census visibility remain unverified.
- [ ] 5.2 Add an idempotent, exact-artifact restore workflow that never overwrites o11y; verify restored guest filesystem and telemetry volumes, then clean up only the target this workflow created after a durable sanitized receipt.
- [ ] 5.3 Record review, final-head CI, merged SHA, Dev-bound task IDs, backup protection, and restore evidence in the estate OpenSpec and observability plan. Keep VM growth and 90d/45d/45d retention blocked until the separate seven-day capacity gate passes.
