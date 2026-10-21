# PBS service configuration implementation (task 4.1)

`platform/playbooks/configure-pbs.yml` is the Semaphore entry point. It refuses
launch-time identity overrides, requires the exact reviewed checkout, one PBS
guest, Debian 13, and an explicit private `pbs_repository_channel` choice. The
supported values are `enterprise` and `no-subscription`; there is no default.
The no-subscription repository is for testing/non-production and Proxmox says
it is not recommended for production, so it also requires the private
`pbs_accept_no_subscription_risk: true` acceptance. Enterprise requires an
already-installed PBS CLI and a valid subscription status before package
changes. The play reads `proxmox-backup-manager subscription get --output-format
json` under `no_log` and emits only `valid` or fails. It never configures a
subscription key through command arguments: safe key provisioning is a separate
dependency. Since a fresh Debian guest does not yet have that CLI, first
bootstrap must use the explicitly accepted no-subscription channel until a
safe enterprise-key mechanism is implemented.

Before package or datastore changes, the play re-derives the expected
`ac-pbs-data-<vm_vmid>` serial from `lsblk`, then checks that the same whole-disk
ext4 UUID is mounted at `/srv/pbs-data` with `noatime` and that fstab names
`UUID=<uuid>` with the same option at that exact mountpoint. It repeats the
proof immediately before datastore writes. It also compares the installed
guest disk helper's SHA-256 with the helper in the exact reviewed checkout,
then invokes its read-only containment validation before any datastore-path
inspection or write. This refuses a symlinked or redirected datastore path,
an existing path on another device, and nested mounts beneath the data mount;
it applies to both unregistered and already registered datastores.
The helper emits only the device path, filesystem UUID, mountpoint, and type.
An unclaimed datastore directory is inspected with `file_type: any`, so direct
child directories and symlinks count as content; it is preserved and causes a
refusal. Check mode reads back existing datastore/job objects and reports
planned creates for absent ones without attempting to show objects that do not
exist yet.

The play installs `proxmox-backup-server` from the selected official Trixie
repository after installing the archive keyring using Proxmox's published
SHA-256. It starts both PBS services and checks the local HTTPS API over port
8007. This validates the endpoint's health and TLS response; the default
self-signed certificate is not treated as a trusted remote identity. PVE
certificate pinning belongs to task 4.2. The default PBS certificate is left
intact to avoid rotating identity on reruns.

The `o11y` datastore is fixed at `/srv/pbs-data/datastore`. The play reads the
configured datastore and verification jobs before changing them, refuses path
or maintenance-mode conflicts, and reads them back. It configures daily garbage
collection, verify-new, and a weekly verification job. It does not configure a
prune schedule or delete backups. Reruns preserve the datastore and any
existing artifacts.

This local privileged Ansible CLI path does not need a PBS API credential or
subscription key for the no-subscription channel. Enterprise subscription
state must already be active and available to APT on the guest. The subscription
status read can contain credential fields and is `no_log`; only a sanitized
status is reported. Task 4.1 does not generate or set a Linux root password,
PBS API token, or any other unused secret. The datastore-scoped PVE backup
identity and its OpenBao secret are task 4.2, not created here.

The reviewed integration follow-up corrects the package executable path to
`/usr/sbin/proxmox-backup-manager` and adds a separate `--tags verify` play.
That play rechecks the installed shared-helper checksum, serial-pinned UUID
mount, PBS API and service health, datastore path/maintenance/GC/verify-new
state, and the weekly verification job. It performs only `stat`, `command`,
`script`, `uri`, and assertion reads; it refuses missing or drifted state and
does not inherit the package, service-start, or datastore-reconciliation tasks.
The verify tag also selects the target-group preflight.

## Source checks

- Proxmox documents Debian installation using `apt install proxmox-backup-server`,
  Trixie repository components, repository channel suitability, and the archive
  key checksum in [PBS installation](https://pbs.proxmox.com/docs/installation.html).
- Proxmox documents datastore creation, ext4 support, and scheduled garbage
  collection in [PBS storage](https://pbs.proxmox.com/docs/storage.html).
- The exact CLI options for datastore and verification-job reconciliation are
  listed in [proxmox-backup-manager](https://pbs.proxmox.com/docs/proxmox-backup-manager/man1.html).
- PBS serves its web interface over HTTPS on port 8007; a self-signed default
  certificate requires fingerprint pinning by clients. See
  [PBS system administration](https://pbs.proxmox.com/docs/sysadmin.html).
