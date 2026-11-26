## Context

See [proposal.md](proposal.md) for motivation and [the capability spec](specs/pbs-vm-restore-gate/spec.md) for required behavior. The GET-only pinned thick-LVM preflight passed in Dev-bound Semaphore task 6992, but it deliberately left write authorization false. The API pin and direct PV join detect identity drift and storage lineage but do not independently prove chassis origin. Operator decision (2026-10-29): physical-origin/BMC/chassis attestation is not a prerequisite; retain the API identity pin, exact storage/VG/PV lineage, SMART, and headroom gates. Historical initial-planning observations only: the private inventory snapshot consulted then lacked a PBS VM host declaration, and a workstation Proxmox template query failed using a stale local token identity. These observations do not describe current private inventory or prove current API credentials. Current declaration and provenance acceptance must come from the private inventory readback; live discovery must use the current OpenBao credential through Semaphore. The existing `provision-vm.yml` reads private inventory and clones a cloud-init template; its default template is not evidence of a supported Debian base. [PBS installation](https://pbs.proxmox.com/docs/installation.html) supports installation on Debian, and [PBS requirements](https://pbs.proxmox.com/docs/system-requirements.html) call for at least four recommended cores, 4 GiB base memory plus storage cache, and 32 GiB OS space.

## Goals / Non-Goals

**Goals:** Make PBS bootstrap, backup, and isolated restore repeatable and fail-closed; keep exact private identities and credentials out of public code and durable logs; preserve current o11y data and deployment state.

**Non-Goals:** Formatting any host physical disk, claiming an API identity pin proves chassis origin, destructive o11y redeploy, claiming a PBS protected bit is immutable storage, or increasing o11y retention in this change.

## Decisions

1. **Discover before declaring.** Add a dedicated GET-only Dev-bound survey using the current OpenBao Proxmox credential. It reports sanitized counts for visible cloud-init templates, VM records, and store metadata without printing identities. Proxmox may ACL-filter the VM and storage lists, so visibility remains explicitly unverified and the survey does not prove a conflict-free census. The write workflow must independently read back the exact declared VMID and store immediately before mutation. A private site-config PR declares the PBS target, node/storage placement, static IP, exact Debian template, OS storage, data storage, VMID, and 512-GiB data disk. NetBox/IPAM and pfSense DHCP checks must precede the private declaration. Alternative: trust the workstation token or choose IDs by inspection; rejected because the token is stale and those choices would not be repeatable.
2. **Reuse the shared VM lifecycle.** Extend `provision-vm.yml` only where it lacks a declared Debian template selection; use its existing OpenBao, clone, cloud-init, exact-target, and readback guards. If discovery finds no supported Debian template, a separate reviewed Semaphore workflow creates one from a pinned image before VM provisioning. Alternative: install PBS packages on the current Ubuntu template or build a VM manually; rejected because PBS supports Debian and manual state would drift.
3. **Keep the data disk separate.** The OS VM is provisioned first. A separate idempotent Dev-bound workflow re-runs the private-inventory-pinned storage preflight immediately before allocating exactly one declared 512-GiB virtual disk on the declared thick-LVM store. It checks the VM's exact node and free bus slot and verifies the Proxmox task/readback. It never addresses a host physical device. The guest workflow proves the attached virtual disk is uniquely identifiable and blank before filesystem creation, then mounts it by UUID. Alternative: enlarge the OS root disk or initialize a host PV; rejected because it confuses backup data with OS lifecycle and raises data-loss risk.
4. **Source secrets and restrict deletion.** Task 4.2 obtains the PVE backup token through OpenBao. Task 4.1's local privileged CLI path creates no PBS API credential and no Linux root password. PBS uses a datastore-scoped backup role without prune/delete; pruning and verification run on PBS under separate server authority. Read back effective permissions and an actual backup before changing the existing PVE backup job. [PBS roles](https://pbs.proxmox.com/docs/config/acl/man5.html) distinguish `DatastoreBackup` from prune-capable roles. A protected snapshot or client role is deletion resistance, not immutable-copy proof; that separate gate remains open.
5. **Restore without touching the source.** Select a fresh artifact by exact VMID/time/storage identity, use the existing restore preflight to check the separate target's current availability, then independently recheck availability immediately before any allocation or restore write. The preflight does not reserve a VMID. Restore without overwrite, inspect guest filesystem and telemetry volumes, and emit a sanitized receipt. Remove only a target that this run created and still matches its ownership marker, after the receipt is durable. Alternative: restore over o11y or rely on snapshot presence; rejected because neither proves recoverability without risking the live service.

### Isolated restore executor source child

The 5.2 source child implements the restore as a schema-2 versioned CAS action
journal. It records distinct restore, isolation, start, guest-exec, stop and
delete actions; each mutation has a persisted intent before the call and a
typed verified effect after the return handle/readback. Restore, start, stop and
delete keep their real Proxmox UPIDs; guest execution keeps the QGA PID; the
configuration isolation update keeps a request fingerprint. Schema-1 journal
records are refused rather than adopted or migrated. Uncertain results retain
both the target and record.

The target remains disconnected with onboot/startup disabled and no passthrough
or shared host backing before its first start. The checker runs only through
QGA after the isolated VM starts and requires data evidence, not path presence:
all five rootless Compose volume identities, ownership and mount relationships,
Grafana SQLite integrity through a read-only WAL-aware connection, and bounded
historical reads from Prometheus, Loki, Tempo and Pyroscope. Quiet backends are
accepted only when a versioned authenticated prerequisite receipt explicitly
proves that expectation. The receipt itself stays in protected task output;
public output contains fixed categories, counts and hashes.

Restore and cleanup are separate Dev-bound tasks. Restore authenticates
successful backup, PBS verification, protected-copy sync and readiness tasks
through their fixed receipts, the authenticated project/task/status/time/output,
and the exact reviewed commit. Current Semaphore does not retain historical
inventory, environment, repository, or playbook bindings; this path relies on
the trusted cooperating controller's receipt attestation and makes no
cryptographic or immutable-history claim. A shared producer-owned proof module
provides the closed receipt schema, exact-byte inventory provenance check,
and stable artifact fingerprint. Source, copy, and restore-target hosts, nodes,
storage IDs, and network mode remain static private-inventory declarations.
The artifact location is not a private selector: restore derives the closed
`normalized_restore_locator` from the authenticated copy-readiness receipt,
then checks that locator against fresh destination PVE content and the PBS
manifest. Readiness also binds the dedicated destination restore Reader identity
and PVE storage configuration fingerprints; restore re-reads both and refuses
drift. Restore uses only `o11y-copy-restore@pbs!pve-restore` for direct PBS
manifest reads, while the destination copy manager remains confined to the
producer workflow. Cleanup re-authenticates the successful
restore task and exact private receipt, compares the entire coordination
record/version, checks the stopped owned target, and requires exact
authoritative deletion readback. See the source-child runbook in
`implementation/isolated-restore-executor-runbook.md`.

Artifact admission allows only the reviewed provisioner-shaped cloud-init
configuration, the provisioner's exact `serial0: socket`, and PVE's canonical
`meta: creation-qemu=<version>,ctime=<epoch>` pair. Other serial settings and
unknown or malformed metadata are refused in both artifact and target
validation. A backed `ide2` cloud-init auxiliary volume is tracked separately
from included backup disks; its source VMID and target store/VMID are exact,
and all source cloud-init network, user, SSH-key, and UUID metadata is removed
in the journaled isolation update before boot. Unsupported auxiliary formats
and arbitrary CD-ROMs are refused before target mutation. Installed PVE
behavior remains a live compatibility gate.

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
The PBS workflow source is merged to Dev by PR #726 at
`c4eee56be4860702df537d40b71d8d85ad229a33`, including the serial-aware guest
disk attachment contract. Dev catalog readback confirms all five reviewed task
definitions have the expected repository, playbook, inventory, environment,
and sole `expected_repository_sha` field. Four publication task runs succeeded
at `c4eee`; a fifth invocation at reviewed PR #727 merge `391a4f7` stopped
after creating the Proxmox template. The independent catalog readback confirms
definitions only; it does not establish success of all five invocations or
accept the created template's configuration and provenance. Review and source
publication are complete, while private inventory/IP prerequisites and live
template, VM, disk, guest, and service acceptance remain open.

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
plus the separate seven-day capacity gate. The independent GET-only
restore-target survey still accepts its private source-artifact selector and
reserves no VMID; that selector is not an authority for the write-capable
protected restore. The protected restore consumes only the authenticated
copy-readiness locator described above. Neither preflight establishes
allocation authority or ACL census visibility.

### Reserved restore target and coordination foundation

Every cooperating managed PVE writer must authenticate the fixed OpenBao
coordination record before preparatory or PVE writes and refuse the declared
restore-target VMID clusterwide, including when the record is absent or in its
terminal `cleaned` phase. The declaration is private inventory data. Its
fingerprint is SHA-256 over sorted compact UTF-8 JSON of exactly the normalized
fields `vmid`, `node`, `storage_id`, `network_mode`, and `headroom_percent`;
node/VMID identity and the selected backup artifact have separate fingerprints.
Booleans, malformed values, and noncanonical representations are rejected.

The guard reads the fixed KV v2 metadata and data paths through the exact
`service_url` on the sole private `openbao_svc` inventory host and authenticates
with the protected Semaphore environment's `BAO_ROLE_ID` and `BAO_SECRET_ID`.
It does not use a launch-supplied public URL or lowercase credential extra var.
The environment binding and Semaphore launch/edit permissions remain part of
the trusted boundary; source guards cannot defeat a privileged launcher. Both
exact reads must prove true 404 absence before an undeclared record can be
treated as absent. Unauthorized, malformed, tombstoned, unavailable, mismatched,
or declaration-removed-with-record state refuses before side effects.

The guard is GET-only; the separate coordination writer may need metadata
`read` and data `create`/`read`/`update` capabilities. A source policy does not
establish the actual injected role's capabilities. Policy publication/readback
and effective-role verification are activation gates. All assigned managed
QEMU writers, including legacy direct PVE-token operator modes, fail closed if
Bao coordination cannot authenticate. This cooperative boundary does not
cover privileged users or unmanaged code. Target absence, restore success,
cleanup proof, and task-output authenticity remain future workflow
responsibilities and cannot be inferred from these guards.

### Protected execution-environment follow-up (2026-10-29)

Dev-bound capability task 10282 refused protected credential extra vars from
its shared Semaphore environment before authentication. Retain that refusal.
The repair uses the existing isolated seed-environment provisioner to give each
owned protected PBS workflow an empty environment with encrypted AppRole inputs.
The source environment and unrelated template bindings remain unchanged.

Add one Dev-bound controller wrapper with an exact reviewed-checkout gate,
fixed controller-loopback API destination, and an OpenBao destination anchored
to the sole private inventory service declaration before credentials are used.
Its selectable targets are only the five explicit Dev catalog rows named in
task 5.2f: the five protected PBS/restore targets. Each declares a distinct
isolated environment. Generic VM writer isolation is handled separately below.
Reuse the provisioner's base-instance selection for these already-suffixed names
and require their declared repository to remain `agent-cloud dev`; do not
generate another Dev suffix or change the shared provisioner's variant policy.

Supply and verify explicit project, inventory, and current source-environment
bindings. Preserve the provisioner's source preservation, encrypted credential
handling, single-template ownership, pre-mutation rechecks, and ambiguous-write
readback. A scoped publisher may create the new controller entry and publish
surveys; existing target rebinding goes only through this reviewed provisioner.
Do not mutate the shared environment, use a full-catalog publication, or weaken
the restore preflight to admit legacy credential extra vars. Generic VM
templates are outside this wrapper's bounded convergence scope; their
environment isolation follows the separate general-writer provisioner decision
below.

Converge the capability-verification template first and rerun its read-only
verifier. Only a successful effective-policy/capability receipt permits the
remaining four owned environment convergences. Their later VM/disk operations
still require their independent declaration, identity, capacity, writer-drain,
and lifecycle gates. Source tests and environment creation are not live restore
or backup acceptance.

### Semaphore environment-array compatibility repair (2026-10-30)

At reviewed Dev `338799e5e66a3a2260856258319200b3bbba25d3`, scoped
publication task 10684 succeeded and readback proved one new controller with
unchanged prior template configurations, source environment, repository and
inventory. Environment task 10688 later failed its final binding assertion.
Reconciliation found the dedicated environment created with empty JSON and
plaintext configuration and exactly two encrypted AppRole inputs, but the
selected template remained bound to the shared source environment. That partial result authorized no retry, capability acceptance, or subsequent
protected workflow. It is a historical checkpoint superseded for environment
convergence by the verified receipts below; reconcile state before any future
write and never retry an uncertain operation blindly.

The failed revision changed only legacy `environment_id` while copying the
live `environment_ids` array unchanged. The pinned upstream model treats the
array as authoritative and falls back to the scalar only when the array is
absent ([Template model](https://github.com/semaphoreui/semaphore/blob/0c3f373/db/Template.go#L284-L290));
the SQL update applies that rule before writing environment associations
([store](https://github.com/semaphoreui/semaphore/blob/0c3f373/db/sql/template.go#L150-L154)).
The repaired shared provisioner writes and verifies a consistent paired
scalar/array binding while preserving every other setting and survey, encrypted
inputs, source/unrelated environments and templates, active-work refusal, and
safe rerun behavior. The acceptance receipt below verifies this repaired
behavior across the five declared environments.

The shared seed client now enforces the complete effective binding before
staging credentials, when proving exclusive ownership/active use, and immediately
before task submission. It retains legacy scalar-only compatibility and refuses
empty, malformed, contradictory or multi-environment seed bindings and drift.
Regressions cover a supplied array winning over the scalar, secondary-group
ownership and pre-submit drift. The existing isolated-environment metadata
guards remain in place. Review, final-head CI, guarded Dev merge, and the
reconciled runtime acceptance are recorded below; capability acceptance
preceded convergence of the other four workflows.

### General VM writers' environments (2026-10-30)

Task 5.2a put the preflight and guard in front of the five general VM writers,
while the earlier version of the protected-wrapper section kept generic
templates on the shared environment. Together those made the writers fail on
every production launch (tasks 9493, 10259, 10295). The shared environment holds
the AppRole only as the plaintext extra vars the preflight refuses, and the
writers log in only from `BAO_ROLE_ID`/`BAO_SECRET_ID`. Operator decision
2026-10-30: give the five writers isolated environments through the same
reviewed provisioner. Neither the guard nor the shared environment changes.
`test_restore_guard_environment.py` now requires every template that reaches the
preflight to declare one.
The `main`-bound copies need the same environments (`seed_variant=main`) before `dev` is
promoted, because promotion brings them the guard.

### Verified isolated-environment acceptance (2026-10-31)

At merged Dev SHA `d41482be4944d5916030fbbbde147e5a686238f8`, tasks 10752,
10762, 10763, 10765, and 10769 accepted all five declared isolated
PBS/restore environment bindings. Readback verified empty plaintext and
extra-var configuration, encrypted AppRole metadata, sole-template ownership,
and preserved shared/unrelated settings, surveys, repository, and inventory.
Capability task 10761 verified source policy plus effective data and metadata
capabilities for fixed coordination records only; it performed no record
read/write and authorizes no allocation or restore operation. Scoped publisher
task 10767 created the missing `Provision PBS VM (Dev)` workflow catalog
row. Rerun task 10770 converged to the same binding and encrypted-input
metadata without duplicate environments or inputs; the binding update request remains part of
that rerun. No VM, disk, filesystem, backup, restore, or cleanup operation was
performed. Task 5.2g is complete; all parent declaration, writer-drain, guest,
backup/restore, capacity, retention, and growth gates remain separate and open.
Physical-origin attestation remains waived. The PBS package-source choice is
pending the user's decision; neither enterprise subscription readiness nor
no-subscription risk acceptance is declared. See the [task receipt](tasks.md)
and [Dev runbook](implementation/dev-bootstrap-publication-runbook.md).

## Risks / Trade-offs

- **Failure-domain scope** → the PBS target placement and its relationship to the source are defined by private site-config inventory and are not asserted in public code. Keep retention growth blocked until the independent-copy requirement is met.
- **Physical origin is not independently proved** → user decision dated 2026-10-29 removes chassis/BMC attestation as a prerequisite. Keep API identity pins and exact storage/VG/PV lineage checks; every write rechecks them and only allocates a virtual disk. No host disk operation is authorized.
- **Concurrent storage growth** → re-evaluate VG/PV/disk capacity immediately before allocation and refuse on mismatch or less than 30% residual headroom. A preflight receipt is not a reservation.
- **NetBox assignment identity** → provisioning accepts only the exact unassigned address reservation. An active assignment has no proven Proxmox VMID mapping in the current API response, so reruns refuse before writes if discovery activates it. Add a reviewed identity mapping before relying on provisioning to reconcile an active record.
- **Semaphore environment identity** → the candidate survey receives OpenBao connection fields from its declared Semaphore environment. Its runtime guard must accept those fields while proving the endpoint equals the single private OpenBao inventory declaration and refusing a launch-supplied `service_url`; otherwise it refuses before a Proxmox query. Launch-time placement overrides remain forbidden.
- **Restore-target claim visibility and role scope** → cooperating writers check only the fixed Bao record and exact private declaration. The read policy addition is narrow source; verify the actual role binding and effective capabilities before activation. A missing declaration plus two exact-path 404s permits ordinary unrelated VMIDs, while any ambiguous record state refuses.
- **Other PVE writers** → a reserved-target guarantee exists only when every cooperating writer is guarded and old tasks/template variants are drained. All assigned managed QEMU mutators, including direct PVE-token modes, must use the guard. Privileged unmanaged writers remain outside this boundary.
- **Template or guest disk ambiguity** → refuse provisioning or formatting until the Debian template and exact blank guest disk are verified. Existing mismatched VMs/disks are preserved.
- **Incomplete backup protection** → PBS backup-only ACLs stop the routine client from deleting backups but do not stop PBS administrators or a host compromise. An independent protected-copy mechanism and isolated restore remain separate retention gates.

## Migration Plan

1. Merge and publish the GET-only discovery workflow; run at an exact Dev revision. Reconcile one private PBS host and static IP only after the survey and IPAM/DHCP checks pass.
2. Merge the guarded VM/data-disk and guest workflows; run their check/preflight path, then create only the new PBS VM. Verify Proxmox config, guest mount, PBS health, datastore, and scoped credential readback before directing backups to it.
3. Produce and verify one PBS o11y backup, then perform an isolated restore to an independently declared target. Preserve the existing backup job destination until the new path proves usable; on failure, stop new work and leave the source VM untouched.
4. Close the separate immutable-copy and seven-day capacity gates before any later VM growth or 90d/45d/45d retention change. Rollback disables the new backup destination without deleting the PBS datastore or existing artifacts.
