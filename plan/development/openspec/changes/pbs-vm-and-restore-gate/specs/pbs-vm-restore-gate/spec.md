## Purpose

Provide a reproducible, guarded local Proxmox Backup Server path for o11y backups and an isolated restore while preserving existing telemetry data and retention settings.

## ADDED Requirements

### Requirement: Read-only bootstrap discovery
The system SHALL discover visible PBS-related VM, store, and supported Debian template metadata through Dev-bound read-only workflows at exact reviewed revisions. It SHALL report only fixed categories and counts, mark global inventory visibility unverified, and SHALL require each write workflow to read and verify its exact declared target before mutation.

#### Scenario: Visible discovery evidence
- **WHEN** a supported template is visible in the Proxmox response and a private target declaration is available
- **THEN** the workflow reports fixed visible counts with inventory visibility unverified and creates or modifies no VM

#### Scenario: Incomplete or ambiguous discovery
- **WHEN** a template, address, VM identity, or selected store cannot be uniquely validated
- **THEN** the workflow refuses and no provisioning request is sent

### Requirement: Guarded PBS VM and data disk
The system SHALL create or adopt only the PBS VM declared in private inventory, using a supported Debian guest and static address. Before a new 512-GiB data disk is allocated, it SHALL revalidate the pinned backing identity, direct LVM lineage, SMART health, store and VG linkage, 30% residual reported headroom, target ownership, and the exact VM and disk state. It SHALL never initialize a host physical disk or replace an existing service VM.

#### Scenario: Matching new allocation
- **WHEN** the declaration is unique, the live preflight passes, and the target VM and disk are absent
- **THEN** the workflow creates only the declared VM and virtual data disk and verifies their readback

#### Scenario: Existing state differs
- **WHEN** a VM, disk, node, address, or store differs from the declaration
- **THEN** the workflow refuses without deleting, resizing, or recreating that state

### Requirement: PBS guest and datastore readiness
The system SHALL install PBS through its supported Debian package source, format only the exact newly attached blank guest data disk, mount it persistently, create one declared datastore, and read back filesystem, mount, service, and datastore health. Credentials SHALL originate in OpenBao and stay out of durable task output.

#### Scenario: Guest converges safely
- **WHEN** the attached data disk is uniquely identified, blank, and matches the Proxmox declaration
- **THEN** repeated deployment converges on the same mounted datastore without erasing existing data

#### Scenario: Disk cannot be proven blank
- **WHEN** guest disk identity or existing content is uncertain
- **THEN** no format or datastore creation occurs

### Requirement: Restricted backup and isolated restore
The system SHALL configure a backup credential that cannot prune or delete existing snapshots, run a verified o11y backup to PBS, and restore one selected artifact to a separately preflighted target without modifying the source VM. It SHALL verify the restored guest filesystem and telemetry volumes, record sanitized evidence, and clean up only a target that the workflow created and still owns.

#### Scenario: Verified restore
- **WHEN** a fresh PBS artifact is selected and a distinct restore target is free
- **THEN** the workflow restores it, verifies the expected data paths, and records a reproducible success receipt

#### Scenario: Ownership or restore evidence is missing
- **WHEN** the artifact, target, or restored data cannot be verified
- **THEN** the workflow refuses to mark the restore gate complete and preserves existing services and volumes

#### Scenario: Restore uses authenticated readiness for artifact placement
- **WHEN** restore authenticates the ordered PBS task chain and the copy-readiness receipt
- **THEN** it derives the artifact locator from that receipt, compares it with fresh destination PVE and manifest readback, and verifies the current destination Reader identity and storage configuration fingerprints; a caller or legacy inventory artifact selector is refused

#### Scenario: Restore action response is uncertain
- **WHEN** any restore, isolation, start, guest-exec, stop, or cleanup mutation has an unknown response or mismatched versioned journal readback
- **THEN** the workflow preserves the target and current journal state, does not retry or adopt a nonce, and does not report success or clean the target

#### Scenario: Restore proof requires historical data
- **WHEN** the restored guest exposes expected volume paths but cannot prove rootless volume ownership, exact mounts, Grafana database integrity, or receipt-bound historical backend data
- **THEN** the restore remains unverified; paths alone cannot satisfy the restore gate

#### Scenario: Cleanup selects a prior task
- **WHEN** cleanup is launched with a prior restore task ID, reviewed restore and cleanup code SHAs, and the current private site-config SHA
- **THEN** it authenticates the completed task's project, status, reviewed commit, ordered output, and one fixed-contract receipt, compares the full coordination record/version, and deletes only the stopped target still owned by that record

#### Scenario: Cleanup absence is ambiguous
- **WHEN** the delete task, target config, or exact VMID absence cannot be authoritatively verified
- **THEN** cleanup refuses to mark the record cleaned and preserves the record; arbitrary API errors or filtered list absence are not proof

### Requirement: Protected restore while primary PBS is unavailable
The system SHALL accept the unavailable-primary component of the protected-copy gate only through a separate Dev-bound transaction that proves the declared primary PBS guest stopped before and throughout destination restoration and restored historical data reads, then verifies primary service recovery. This proposed capability SHALL preserve the ordinary target-only restore contract; a schema-2 journal or `restore_receipt/v3` alone SHALL NOT satisfy it. It SHALL preserve live o11y, primary and destination PBS data, and the independent retention and capacity gates.

#### Scenario: Primary outage spans protected destination recovery
- **WHEN** authenticated prerequisite receipts, exact private inventory provenance, installed API compatibility, effective capabilities, sufficient task visibility, maintenance window and assigned-writer exclusion have been verified
- **THEN** the transaction journals a graceful exact-primary shutdown without force-stop, requires authoritative stopped readback and bounded observations spanning fresh destination reads, restore and historical guest checks, verifies primary restart and service recovery, and emits a versioned acceptance receipt binding those proofs and the stopped isolated target

#### Scenario: Busy state or writer exclusion is unproven
- **WHEN** a relevant backup, sync, verification, maintenance or VM mutation is active or queued, task visibility is incomplete, or the closed assigned writer/job census cannot be excluded
- **THEN** the transaction refuses before primary shutdown; a GET-only guard, filtered empty task list or elapsed maintenance window is not exclusion evidence

#### Scenario: Outage or recovery evidence is uncertain
- **WHEN** a primary power response, subject binding, CAS readback, observation interval or recovery readback is missing, ambiguous or inconsistent
- **THEN** the transaction does not emit successful acceptance, retains the target and durable journal, and uses only an explicitly permitted journaled recovery transition; it does not blindly retry a mutation, adopt a nonce, force-stop or delete a service guest

#### Scenario: Ordinary restore succeeds without outage evidence
- **WHEN** an ordinary restore succeeds with its existing schema-2 journal and v3 receipt
- **THEN** it remains ordinary isolated-restore evidence and the unavailable-primary protected-copy gate remains open

### Requirement: Retention gate remains independent
The system SHALL keep Prometheus, Loki, and Tempo at 15d, 7d, and 168h until an immutable or equivalent protected local copy, a successful isolated restore, and the separate seven-day capacity gate are all verified. A PBS protected flag or backup-only token alone SHALL NOT satisfy immutable-copy evidence.

#### Scenario: Backup exists without all gates
- **WHEN** PBS has a backup but copy protection or capacity evidence is incomplete
- **THEN** no o11y VM growth or 90d/45d/45d retention change occurs
