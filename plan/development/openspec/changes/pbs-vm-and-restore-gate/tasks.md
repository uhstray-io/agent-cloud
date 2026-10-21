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

- [x] 3.1 Add a separate idempotent Dev-bound virtual-disk attachment workflow that re-runs the pinned preflight immediately before allocation, checks the exact guest/bus/store, and refuses concurrent drift or existing mismatched disks. Test refusal and safe rerun. **Source complete:** PR #715 passed final CI and CodeRabbit review, then merged to `dev` at `621396b660c120056fc835900d70a38618f87535` (reviewed head `bc0ca6d3b29ee1db8ef78bcfccf8a0e7f75451bc`). The focused attachment and bootstrap tests pass in the integration source validation. The PR's initial virtual-disk allocation request does not set the required `ac-pbs-data-<vm_vmid>` serial. A serial-aware bootstrap enhancement must be reviewed, merged, and read back before any live disk allocation; no disk was allocated by the PR. This checkbox records source completion, not live acceptance.
- [ ] 3.2 Attach only the 512-GiB guest disk through Proxmox API and verify its task/config readback; never select or format a host physical device.
- [ ] 3.3 Add guest-side blank-disk identity and content guards, filesystem creation, UUID mount, and readback. Run only after the new virtual disk is proven unique; verify a second run preserves the data.

## 4. Configure PBS and backup access

- [ ] 4.1 Add supported Debian PBS package, TLS, service, datastore, and maintenance configuration through Semaphore with OpenBao-sourced secrets and scoped `no_log`; read back guest mount and PBS health.
- [ ] 4.2 Create and verify a datastore-scoped PVE backup credential without prune/delete rights; register the PBS target through code and preserve the existing backup destination until verified.
- [ ] 4.3 Run one o11y backup to PBS and verify a fresh artifact, successful task, source VMID, content accessibility, and PBS-side verification result using sanitized receipts.
- [ ] 4.4 Implement and verify a local protected-copy mechanism separate from the PVE backup credential. Document its threat boundary; do not call a PBS protected bit or token ACL immutable-copy proof by itself.

## 5. Prove isolated restore and preserve growth gates

- [ ] 5.1 Declare and preflight a distinct restore target with sufficient capacity and no conflicting VM or network identity.
- [ ] 5.2 Add an idempotent, exact-artifact restore workflow that never overwrites o11y; verify restored guest filesystem and telemetry volumes, then clean up only the target this workflow created after a durable sanitized receipt.
- [ ] 5.3 Record review, final-head CI, merged SHA, Dev-bound task IDs, backup protection, and restore evidence in the estate OpenSpec and observability plan. Keep VM growth and 90d/45d/45d retention blocked until the separate seven-day capacity gate passes.
