# PBS Dev publication and bootstrap runbook

This runbook records the reviewed source workflow and its live acceptance
sequence. Source readiness does not establish that the PBS VM, disk, service,
identity, or storage exists.

## Source integration status and current blockers

- PR #726 merged to `dev` at verified merge commit
  `c4eee56be4860702df537d40b71d8d85ad229a33`, containing reviewed PBS source
  head `e149fe641cf9f2a9034969dc9b3c2daf51032b09`. The source-only restore
  coordination worktree normally merged that exact Dev commit; no PBS files
  were copied between branches. This does not publish this worktree's source,
  verify the effective Semaphore role, or authorize a restore.
- Dev catalog readback confirms all five reviewed task definitions point to
  the expected repository, playbook, inventory, and environment and expose
  only the exact `expected_repository_sha` survey field. Four publication task
  runs succeeded at `c4eee`; a fifth invocation at reviewed PR #727 merge
  `391a4f7` stopped after creating the template. Catalog readback proves the
  definitions, not success of every publication invocation or acceptance of
  the created Proxmox template.
- Private site-config PR #110 is approval-blocked. This blocks target allocation
  and guest configuration until the PBS inventory declaration is approved,
  merged, synchronized, and read back. It does not block publishing the Dev
  catalog entries. Do not substitute launch-time identity values or
  public-repository inventory.
- OpenSpec tasks 1.3 and 2.3 remain open: VMID/address authority and the exact
  Debian 13 cloud-init template/provisioning result have not been accepted.
  If the supported Debian template is unavailable, task 2.1 requires a separate
  reviewed template-publication workflow first.
- Task 4.1 requires an explicit private repository channel. The enterprise
  bootstrap path requires a PBS CLI and an already active subscription; safe
  subscription-key provisioning remains a separate dependency. The
  no-subscription path is allowed only with explicit private
  `pbs_accept_no_subscription_risk: true` acceptance and is not an implicit
  production default.
- The seven-day capacity/retention gate remains blocked by
  `source_series_ambiguous`. That does not prove capacity, authorize VM growth,
  or authorize retention changes. Task 4.4's second-PBS destination remains a
  planning candidate only.

## Preconditions before allocation or guest configuration

1. Confirm PR #726 is merged to `dev`, all required final-head checks are green,
   and record the exact 40-character Dev commit SHA. Use that SHA as the sole
   survey value `expected_repository_sha` for each catalog task. Do not pass
   private target identity, endpoints, credentials, or repository acceptance
   through surveys or extra vars.
2. Confirm the private declaration has been approved and read back from
   site-config. It must identify exactly one PBS guest, the authorized node and
   VMID, Debian 13 template, static guest network declaration, selected OS
   storage, the 512-GiB PBS data disk declaration, and the VMID-derived disk
   serial contract `ac-pbs-data-<vm_vmid>`. Resolve IP and VMID through the
   existing NetBox/IPAM and pfSense checks. Physical-origin/BMC/chassis
   attestation is not a prerequisite (operator decision, 2026-10-29); retain
   the declared API serial/WWN identity pin and exact storage/VG/PV lineage.
3. Confirm the declared repository channel and its prerequisite before task
   launch. Do not add a subscription key through argv, Semaphore survey, or a
   new credential path. Enterprise cannot be treated as ready until the
   guest-side supported mechanism proves an active subscription.
4. Confirm the existing PBS/OpenBao/PVE services and their approved credentials
   are available through the existing Semaphore environment. This source does
   not create root passwords or bootstrap credentials outside OpenBao.

## Dev catalog publication and readback

The five Dev entries below have been published and their definitions read back.
For future catalog changes, use the same reviewed publication mechanism:

1. Use the existing **Publish Semaphore Template Surveys (Dev)** entry point
   (`platform/playbooks/publish-semaphore-templates.yml`) with the exact five
   `(Dev)` names below. It updates only selected declared surveys. If an entry
   is absent, create one at a time with `semaphore_allow_scoped_create=true`
   and verified project, inventory, and environment bindings from an existing
   Dev template; otherwise leave the create flag false. Do not run full-catalog
   `setup-templates.yml` or edit the Semaphore API directly.
2. Read the Dev catalog back and confirm these entries target repository
   `agent-cloud dev`, use the expected playbook paths, and expose only the
   required `expected_repository_sha` survey field:
   - `Attach PBS Data Disk (Dev)` — `attach-pbs-data-disk.yml`
   - `Prepare PBS Guest Data Disk (Dev)` — `prepare-pbs-guest-data-disk.yml`
   - `Configure PBS Service (Dev)` — `configure-pbs.yml`
   - `Configure PBS Backup Access (Dev)` — `configure-pbs-backup-access.yml`
   - `Preflight o11y Restore Target (Dev)` — `preflight-o11y-restore-target.yml`
3. Record the Dev task IDs and readback evidence in the OpenSpec and
   `plan/development/05-observability.md`. Catalog publication alone does not
   establish live acceptance of a VM, disk, credential, ACL, PVE storage,
   backup, or restore.

## Ordered bootstrap sequence

Run each step only after its preceding live acceptance receipt is reviewed.
Use the same recorded Dev SHA for each exact-checkout guard.

1. Run the existing reviewed Semaphore VM provisioner for only the declared PBS
   guest. Verify the Proxmox task result, exact VM config, template, node,
   network identity, and guest readiness. Refuse conflicts; do not allocate a
   different VMID or address ad hoc.
2. Run `Attach PBS Data Disk (Dev)`. It rechecks the declared VM and thick-LVM
   source before attaching only the 512-GiB guest disk with
   `ac-pbs-data-<vm_vmid>` serial. Verify task/config readback before continuing.
3. Run `Prepare PBS Guest Data Disk (Dev)` only after attachment readback. It
   must prove the serial-selected whole guest disk, create ext4 only when the
   disk is proven blank, mount by UUID at `/srv/pbs-data`, and validate the
   datastore child path without following symlinks. Run its read-only verify
   path afterward and retain the second-run data-preservation receipt.
4. Run `Configure PBS Service (Dev)` only after the guest filesystem receipt.
   Verify repository/subscription gate, package/service health, UUID mount,
   datastore path, maintenance state, garbage-collection policy, and verify job.
   Keep this step on the declared guest; it creates no Linux root password or
   PBS API token.
5. Run `Configure PBS Backup Access (Dev)` only after the service is accepted.
   It owns the scoped OpenBao/PBS/PVE identity, ACL, and storage registration
   lifecycle. Verify the token identity, datastore-scoped backup-only grants,
   effective permissions, PVE storage readback, and unchanged existing backup
   jobs. Do not move the existing backup job here.
6. Treat first backup and artifact verification as task 4.3, a separate open
   acceptance step. Keep restore preflight separate until a private disconnected
   target and one exact artifact are declared; the preflight is GET-only and
   does not reserve or create a VM.

Each command or API result must be recorded as a sanitized receipt with Dev
commit, Semaphore task ID, changed/readback state, and the relevant OpenSpec
acceptance item. A source check, catalog entry, check mode, or `--tags verify`
run does not establish the corresponding live acceptance evidence.

## Stop conditions

Stop before allocation, package, identity, ACL, storage, backup, or restore
writes when the private declaration or exact repository SHA is missing; the
Dev catalog does not match the five reviewed entries; the PBS guest, VMID,
address, template, disk serial, selected storage, or required API/storage
identity is ambiguous; repository/subscription acceptance is absent; the mount UUID
or datastore containment check differs; an existing identity, ACL, storage or
backup job conflicts; or the service/readback is unhealthy. Preserve existing
data and destinations for investigation. Do not bypass a refusal with direct
Proxmox/PBS API calls, manual guest commands, or alternate credentials.

The protected-copy candidate (task 4.4), isolated restore (task 5.2), capacity
forecast, retention changes, and VM growth have separate acceptance evidence;
successful bootstrap or backup-access configuration does not satisfy them.

## Restore coordination source prerequisite

OpenSpec task 5.2 remains open. Source preparation adds a fixed-path OpenBao
coordination record, read-only reserved-target checks, and guard calls before
all assigned managed QEMU mutators, including legacy direct-token operator
modes. It uses the sole private `openbao_svc` inventory endpoint and protected
Semaphore AppRole environment. The source addition to `semaphore-read.hcl`
does not prove the effective policy of the role Semaphore injects; policy
publication, capability readback, and review of the actual role binding are
required before any guarded writer is activated.

Each writer's credential-free destination preflight runs after the reserved-name
guard and before any AppRole or Proxmox credential lookup. Its OpenBao endpoint
comes from the sole private `openbao_svc.service_url`; the existing
`openbao_addr` must match it, and launch-supplied endpoint,
AppRole, or restore-target aliases are refused. OpenBao URI tasks clear inherited
PVE headers and URL authentication, require certificate validation, disable
proxy use, and reject redirects. Focused source tests verify the effective URI
arguments after installed Ansible module defaults are merged and pin the exact
reviewed-checkout dependency list within the bounded 16-path limit.

Source guard tests do not establish target availability or successful restore.
Before task 5.2 can be accepted, every cooperating writer/template variant
must contain the guard and older invocations must be drained. The future restore
workflow must journal each PVE write before execution, authenticate the completed
prior restore task output, prove stopped/no-pending-action state, and prove
cleanup task success plus authoritative exact-VMID absence. No restore target
has been reserved or used, and no restore or cleanup has been performed.
