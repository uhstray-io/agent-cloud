## Purpose

Provides repeatable, sanitized read-only evidence that a privately pinned PBS thick-LVM backing disk matches Proxmox's reported disk identity and has reported headroom for a proposed guest data disk.

## ADDED Requirements

### Requirement: PBS allocation preflight remains read-only and non-authorizing
The platform SHALL evaluate the privately declared PBS storage against protected Proxmox GET responses and SHALL emit only fixed, sanitized status when every required identity, lineage, health, and capacity check passes. The preflight SHALL NOT authorize allocation or writes.

#### Scenario: Pinned backing disk passes the proposed allocation preflight
- **WHEN** the declared store has exact matching configuration and status, one complete VG/PV mapping, a direct top-level disk path with literal `used=LVM`, a matching private serial or supported well-formed WWN pin, healthy SMART status, and 30% residual reported VG headroom after a proposed 512-GiB disk
- **THEN** the report states that the preflight passed without exposing identity, paths, storage identifiers, exact capacities, addresses, or credentials
- **AND** allocation, device safety, filesystem readiness, PBS readiness, and write authorization remain false

#### Scenario: Incomplete, changed, or unhealthy backing evidence fails closed
- **WHEN** the private identity pin is absent or differs, identity is malformed or ambiguous, storage/VG/PV linkage is incomplete, the PV is not a direct disk with literal `used=LVM`, SMART health is not passed, or reported residual headroom is below 30%
- **THEN** the task refuses with fixed sanitized output and does not issue any Proxmox write request

#### Scenario: Existing surveys are run without the new preflight
- **WHEN** an operator runs the existing physical-storage survey or identity-capture workflow
- **THEN** their requests and outputs retain their current behavior and no allocation preflight is implicitly invoked
