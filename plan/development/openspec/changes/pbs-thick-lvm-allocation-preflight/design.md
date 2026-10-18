## Context

See `proposal.md` and the capability contract in `specs/pbs-thick-lvm-allocation-preflight/spec.md`. The existing Dev-bound physical-storage survey already reads the protected disk, storage configuration/status, LVM, and PV inventories and verifies the declared thick-LVM mapping. The identity-capture task reports private identity values for operator review, but does not compare a site-config pin or gate on SMART health.

## Goals / Non-Goals

**Goals:** Reuse the current survey payload, preserve its protected API boundary, and emit a fixed preflight result for the exact private backing identity and 512-GiB proposal.

**Non-Goals:** Proving physical origin, reserving or allocating VG extents, creating a VM or disk, formatting a filesystem, deploying PBS, or changing retention.

## Decisions

- Add a separate playbook and Semaphore template that import the current GET-only survey. This avoids new API surface and keeps existing survey and identity workflows unchanged.
- Read `proxmox_pbs_backing_disk_serial` and/or `proxmox_pbs_backing_disk_wwn` from the private `o11y_svc` host inventory. Require at least one pin; when both are set, both must match. These names are a public contract for a private site-config follow-up, not values stored in this repository.
- Reuse the inspector's exact declared-store/config/status/VG checks and compute whether remaining VG free space after 512 GiB is at least 30% of reported VG size. Require exactly one direct PV-to-disk path join and literal `used=LVM`; require SMART category `passed` for this preflight.
- Keep the private survey payload and pins in `no_log` tasks and emit only a fixed result shape. The reported API pin detects drift against the private declaration; it is not independent proof that the disk came from the operator-attested chassis.

## Risks / Trade-offs

- A changed inventory declaration can pin the wrong API identity → require reviewed private inventory changes; the preflight proves only equality with that pin.
- Proxmox capacity is point-in-time reported state and can change after the GET requests → label it as preflight evidence and keep allocation authorization false.
- The active OpenSpec CLI does not recognize the historical `estate-wide-observability-instrumentation` directory → maintain this work in a new registered change rather than editing historical task history.

## Migration Plan

Publish the new Dev-bound template through the existing Semaphore template workflow. A private site-config follow-up must add and review at least one backing identity pin before the task can pass. Run the preflight only after review on the exact Dev revision. Revert by publishing the prior code and template state; the task has no runtime state to clean up.
