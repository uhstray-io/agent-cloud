## Why

The PBS thick-LVM store has preliminary capacity and API-reported disk identity evidence, but no repeatable check that binds those facts to the operator's private backing-disk pin before considering a 512-GiB guest data disk. A GET-only preflight makes that evidence reproducible while preserving the rule that identity and reported headroom do not authorize writes or prove physical origin.

## What Changes

- Add a separate Dev-bound Semaphore playbook that reuses the protected physical-storage survey and compares its exact selected disk with a private serial or WWN pin.
- Require exact declared storage/config/status/VG/PV linkage, one direct disk-path join with literal `used=LVM`, healthy SMART status, and 30% residual reported VG headroom after a proposed 512-GiB allocation.
- Emit only fixed status and booleans; keep backing identity, paths, storage IDs, exact capacities, API responses, and credentials private.
- Keep allocation, device safety, PBS readiness, filesystem readiness, and write authorization false. Existing surveys are unchanged unless this separate preflight is invoked.
- Record the production readback and its remaining trace-sender review gate in the observability plan.

## Capabilities

### New Capabilities

- `pbs-thick-lvm-allocation-preflight`: read-only, pinned-identity and reported-headroom evidence for a proposed PBS guest disk.

### Modified Capabilities

None.

## Impact

The change adds a Python inspector operation, one Ansible playbook and Dev Semaphore template, focused tests, and observability planning records. The exact pin remains a private `site-config` declaration; no private value or production inventory is added here. No Proxmox API write, disk creation, filesystem creation, PBS deployment, or retention change is included.

## Rollback Plan

Revert the new inspector operation, preflight playbook/template, tests, and planning record, then publish the previous reviewed Dev revision through the normal branch workflow. The workflow performs only GET requests and makes no runtime changes to undo.
