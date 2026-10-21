## Context

See [proposal.md](proposal.md) for motivation and [the capability spec](specs/pbs-vm-restore-gate/spec.md) for required behavior. The GET-only pinned thick-LVM preflight passed in Dev-bound Semaphore task 6992, but it deliberately left write authorization false. The selected disk is operator-attested local hardware; the API pin and direct PV join detect drift but do not independently prove its chassis origin. The merged private inventory has no PBS VM host declaration. A direct workstation Proxmox template query failed with a stale local token identity; the next live discovery must use the current OpenBao credential through Semaphore. The existing `provision-vm.yml` reads private inventory and clones a cloud-init template; its default template is not evidence of a supported Debian base. [PBS installation](https://pbs.proxmox.com/docs/installation.html) supports installation on Debian, and [PBS requirements](https://pbs.proxmox.com/docs/system-requirements.html) call for at least four recommended cores, 4 GiB base memory plus storage cache, and 32 GiB OS space.

## Goals / Non-Goals

**Goals:** Make PBS bootstrap, backup, and isolated restore repeatable and fail-closed; keep exact private identities and credentials out of public code and durable logs; preserve current o11y data and deployment state.

**Non-Goals:** Formatting any host physical disk, treating an API identity pin as independent chassis proof, destructive o11y redeploy, claiming a PBS protected bit is immutable storage, or increasing o11y retention in this change.

## Decisions

1. **Discover before declaring.** Add a dedicated GET-only Dev-bound survey using the current OpenBao Proxmox credential. It reports sanitized counts for visible cloud-init templates, VM records, and store metadata without printing identities. Proxmox may ACL-filter the VM and storage lists, so visibility remains explicitly unverified and the survey does not prove a conflict-free census. The write workflow must independently read back the exact declared VMID and store immediately before mutation. A private site-config PR declares the PBS target, node/storage placement, static IP, exact Debian template, OS storage, data storage, VMID, and 512-GiB data disk. NetBox/IPAM and pfSense DHCP checks must precede the private declaration. Alternative: trust the workstation token or choose IDs by inspection; rejected because the token is stale and those choices would not be repeatable.
2. **Reuse the shared VM lifecycle.** Extend `provision-vm.yml` only where it lacks a declared Debian template selection; use its existing OpenBao, clone, cloud-init, exact-target, and readback guards. If discovery finds no supported Debian template, a separate reviewed Semaphore workflow creates one from a pinned image before VM provisioning. Alternative: install PBS packages on the current Ubuntu template or build a VM manually; rejected because PBS supports Debian and manual state would drift.
3. **Keep the data disk separate.** The OS VM is provisioned first. A separate idempotent Dev-bound workflow re-runs the private-inventory-pinned storage preflight immediately before allocating exactly one declared 512-GiB virtual disk on the declared thick-LVM store. It checks the VM's exact node and free bus slot and verifies the Proxmox task/readback. It never addresses a host physical device. The guest workflow proves the attached virtual disk is uniquely identifiable and blank before filesystem creation, then mounts it by UUID. Alternative: enlarge the OS root disk or initialize a host PV; rejected because it confuses backup data with OS lifecycle and raises data-loss risk.
4. **Source secrets and restrict deletion.** Task 4.2 obtains the PVE backup token through OpenBao. Task 4.1's local privileged CLI path creates no PBS API credential and no Linux root password. PBS uses a datastore-scoped backup role without prune/delete; pruning and verification run on PBS under separate server authority. Read back effective permissions and an actual backup before changing the existing PVE backup job. [PBS roles](https://pbs.proxmox.com/docs/config/acl/man5.html) distinguish `DatastoreBackup` from prune-capable roles. A protected snapshot or client role is deletion resistance, not immutable-copy proof; that separate gate remains open.
5. **Restore without touching the source.** Select a fresh artifact by exact VMID/time/storage identity, use the existing restore preflight to check the separate target's current availability, then independently recheck availability immediately before any allocation or restore write. The preflight does not reserve a VMID. Restore without overwrite, inspect guest filesystem and telemetry volumes, and emit a sanitized receipt. Remove only a target that this run created and still matches its ownership marker, after the receipt is durable. Alternative: restore over o11y or rely on snapshot presence; rejected because neither proves recoverability without risking the live service.

### Implementation refinements from the read-only census

The census found no template with proven Debian provenance. A second GET-only
Dev-bound candidate survey therefore checks two distinct VMIDs through
Proxmox's exact-ID endpoint and reports eligible store identities only in
private Semaphore output. The template publisher pins one dated official
Debian 13 generic QCOW2 cloud image by SHA-512 and requires an active
file-based store with `import` content enabled. Proxmox verifies TLS, digest,
and image format before template creation. This artifact has no detached
signature; the fixed digest and official HTTPS source are the stated
provenance assurance.

The PBS-specific wrapper uses the shared VM lifecycle with private template
selection, an authoritative NetBox address record, exact Proxmox ID/config
checks, the Debian cloud-init login, and a guest Debian 13 check. Completed
PBS VM reruns may retain the exact later 512-GiB `scsi1` data disk on the
pinned thick-LVM store; the shared lifecycle never writes or resizes it.
Ambiguous partial clones or mismatched disks refuse before mutation.

Before publishing the template, a separate Dev-bound workflow must enable
Proxmox `import` content on the exact private `proxmox_pbs_import_storage_id`
declared alongside `proxmox_pbs_storage_node`. The VM image store is a distinct
inventory field and is not assumed to be file-based. Because storage content
configuration is cluster-wide, the workflow checks the declared node is online,
reads that exact store's node status and cluster configuration, and refuses
inactive, non-file, or node-mismatched stores. It preserves the existing content
list, sends the replacement list with the GET response digest as Proxmox's
concurrency guard, and verifies readback. Check mode performs the same guarded
reads but never writes. The 2026-10-18 candidate census in task 7081 found zero
active import stores. After private inventory reconciliation, Dev-bound task
7128 confirmed the update was needed in check mode, task 7129 applied the
observed-digest content union and verified readback, and task 7131 confirmed
idempotence. Task 7132 then found active import candidates. All ran at reviewed
Dev `a7f348067bdd3cec935f8ae64ceeb1e49492c7c3`. Template publication
still requires the separate PBS host declaration and inventory readback.
The Dev-bound Semaphore template itself was created through the reviewed
single-template publisher: task 7155 passed check mode, and task 7156 verified
the resulting playbook and Dev bindings. This publishes only the automation
entry; no Proxmox template or PBS VM exists yet.
The guest-disk attachment implementation is prepared on a separate public
branch. Its missing-disk path reuses the pinned read-only survey and preflight
before one digest-protected Proxmox VM-config update; an exact-disk rerun checks
the declared guest and disk without requiring headroom for a second allocation.
Review, Dev merge, Semaphore publication, and live validation remain pending.

Production Semaphore task 7106 refused before any Proxmox request because the
workflow required HTTPS for the inventory OpenBao URL. The shared transport
guard permits internal RFC1918 HTTP and refuses public cleartext, so the workflow
now checks that the inventory URL is present and exactly matches Semaphore's
OpenBao URL before applying that shared guard ahead of AppRole authentication.
Task 7106 made no Proxmox read or write. The reviewed rerun above closed the
import-content gate; it did not create a template or PBS VM.

### PBS guest and service source contracts

The attached virtual disk's Proxmox serial is the stable guest selector:
`ac-pbs-data-<vm_vmid>`. It is 18 ASCII bytes at the six-digit VMID limit and
fits Proxmox's 20-byte virtual-drive serial field. The guest accepts exactly one
matching whole disk at the declared 512-GiB size; Linux device enumeration order
is not an identity. The whole-disk ext4 filesystem is mounted by UUID at
`/srv/pbs-data`, with `noatime` and fstab/readback checks.

The shared guest helper is part of the exact reviewed checkout and is installed
root-owned at `/usr/local/lib/agent-cloud-prepare-pbs-guest-disk.py` with SHA-256
readback. Its no-follow datastore helper confines `/srv/pbs-data/datastore` to a
real direct child on the same device and refuses nested mounts. Task 4.1
independently re-proves Proxmox/guest serial, size, filesystem UUID, and mount,
then checks that the installed helper bytes match the checkout before invoking
its read-only containment validation. That dependency is code, not a durable
receipt or marker.

Repository selection is explicit in private inventory: `enterprise` requires
an already installed PBS CLI reporting an active subscription before changes;
`no-subscription` has no default and additionally requires the operator's
explicit `pbs_accept_no_subscription_risk: true`. Safe subscription-key
provisioning is a separate dependency. A fresh Debian guest cannot meet the
enterprise prerequisite until it is provisioned out-of-band through an approved
mechanism; this change does not pass subscription keys through command-line
arguments. Task 4.1 uses the privileged local PBS CLI and creates no Linux root
password, PBS API token, or unused OpenBao secret. The scoped PVE token lifecycle
belongs to task 4.2.

The guest and service workflows are source preparation only until exact-head
review, final CI, Dev merge, Semaphore publication/readback, and guarded
Dev-bound execution. Their source and tests do not establish that a Proxmox
template, PBS VM, data disk, filesystem, mount, or PBS service exists live.

The current task 4.4 planning candidate is a second PBS guest with its own
datastore/admin boundary, destination-initiated pull sync, a source
`DatastoreReader` identity, `remove-vanished=false`, and independent pruning
initially disabled. This reduces dependence on the ordinary PVE backup
credential and primary-PBS availability; it does not remove destination PBS
administrator, shared hypervisor administration, or shared backing storage
from the trusted boundary, and is not an absolute immutability claim. Do not
declare or provision that guest before GET-only placement/capacity evidence and
installed PBS sync-schema verification. Acceptance needs destination-side
protection checks and an isolated restore while the primary is unavailable,
plus the separate seven-day capacity gate. The source-only restore-target
preflight requires a private exact-artifact declaration; it reserves no VMID
and does not establish allocation authority or ACL census visibility.

## Risks / Trade-offs

- **Failure-domain scope** → the PBS target placement and its relationship to the source are defined by private site-config inventory and are not asserted in public code. Keep retention growth blocked until the independent-copy requirement is met.
- **Physical origin not independently proved** → preserve the user's local-hardware attestation separately from the API identity pin. Every write rechecks the pin and only allocates a virtual disk; no host disk operation is authorized.
- **Concurrent storage growth** → re-evaluate VG/PV/disk capacity immediately before allocation and refuse on mismatch or less than 30% residual headroom. A preflight receipt is not a reservation.
- **NetBox assignment identity** → provisioning accepts only the exact unassigned address reservation. An active assignment has no proven Proxmox VMID mapping in the current API response, so reruns refuse before writes if discovery activates it. Add a reviewed identity mapping before relying on provisioning to reconcile an active record.
- **Semaphore environment identity** → the candidate survey receives OpenBao connection fields from its declared Semaphore environment. Its runtime guard must accept those fields while proving the endpoint equals the single private OpenBao inventory declaration and refusing a launch-supplied `service_url`; otherwise it refuses before a Proxmox query. Launch-time placement overrides remain forbidden.
- **Template or guest disk ambiguity** → refuse provisioning or formatting until the Debian template and exact blank guest disk are verified. Existing mismatched VMs/disks are preserved.
- **Incomplete backup protection** → PBS backup-only ACLs stop the routine client from deleting backups but do not stop PBS administrators or a host compromise. An independent protected-copy mechanism and isolated restore remain separate retention gates.

## Migration Plan

1. Merge and publish the GET-only discovery workflow; run at an exact Dev revision. Reconcile one private PBS host and static IP only after the survey and IPAM/DHCP checks pass.
2. Merge the guarded VM/data-disk and guest workflows; run their check/preflight path, then create only the new PBS VM. Verify Proxmox config, guest mount, PBS health, datastore, and scoped credential readback before directing backups to it.
3. Produce and verify one PBS o11y backup, then perform an isolated restore to an independently declared target. Preserve the existing backup job destination until the new path proves usable; on failure, stop new work and leave the source VM untouched.
4. Close the separate immutable-copy and seven-day capacity gates before any later VM growth or 90d/45d/45d retention change. Rollback disables the new backup destination without deleting the PBS datastore or existing artifacts.
