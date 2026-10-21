# PBS guest data filesystem source preparation

Task 3.3 prepares the PBS data filesystem only inside the single privately declared
`pbs_svc` guest. The workflow requires the exact reviewed repository SHA, refuses
local-mode execution, and re-reads the exact QEMU VM config from Proxmox using the
OpenBao-sourced API credential immediately before guest validation. It reuses the
shared validator to require the declared VM identity, only the declared `scsi1` data
disk, its pinned store and size, and the expected serial. Its `lsblk` inventory must
then show exactly one whole disk with that Proxmox-set serial
`ac-pbs-data-<vm_vmid>` and the declared 512-GiB size. The serial is the identity link;
size is only a consistency check. The workflow never selects a device by Linux
enumeration order or formats a Proxmox host physical device.

Before creating ext4, the helper refuses partitions, child devices, existing
filesystem types, mounts, or partition UUIDs. `wipefs` must report no signatures and
the target mountpoint must not already be mounted. An existing filesystem is accepted
only when it is ext4 on the uniquely serial-matched whole disk and is already mounted
at `/srv/pbs-data`; that rerun preserves contents. If a run stops after `mkfs` but
before mount, a filesystem labelled `pbs-data` with a valid UUID is safely resumed by
UUID; an unlabeled or differently mounted filesystem refuses. The mount is recorded
by filesystem UUID in fstab. Duplicate filesystem UUIDs refuse before mounting, and
readback checks that the selected device, UUID, filesystem type, and target all agree.
A pre-existing unmounted mountpoint must be an empty directory; the nonrecursive
Ansible census includes files, directories, and symlinks so mounting cannot hide existing
content. The future PBS datastore directory is `/srv/pbs-data/datastore`. The workflow
copies the exact SHA-gated helper source from the controller checkout to the bounded
root-owned guest path `/usr/local/lib/agent-cloud-prepare-pbs-guest-disk.py`, checks its
SHA-256 readback, and passes raw guest `lsblk` JSON to its parser over command stdin.
Datastore creation uses a no-follow directory descriptor rooted at `/srv/pbs-data`; it
refuses datastore symlinks, filesystem-device mismatch, and any nested mount at or below
the datastore. Reruns leave datastore contents and permissions untouched. The read-only
`--validate-datastore-path` mode exposes the same path-containment checks for the PBS
service workflow, which must independently verify its Proxmox/guest disk and mount
identity before configuring the datastore.

The serial value is 18 ASCII bytes for the six-digit maximum supported VMID and below
Proxmox's documented 20-byte maximum for a virtual drive serial. Proxmox's `scsi[n]`
configuration accepts `serial=<serial>`; its option sequence is parsed independently
of size and serial order. See the [Proxmox VM configuration reference](https://raw.githubusercontent.com/proxmox/pve-docs/master/generated/qm.conf.5-opts.adoc)
and [PBS storage documentation](https://pbs.proxmox.com/docs/storage.html), which
documents ext4 as a supported datastore filesystem and datastores as directories on a
standard filesystem.

This is source-only preparation. It has not run against Semaphore, a PBS VM, or live
Proxmox state. Formatting and mounted-filesystem readback still require a reviewed
Dev-bound execution after task 3.2 has attached and read back the serial-bearing
virtual disk.
