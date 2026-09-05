# Recover NetBox Discovery

**Date:** 2026-09-05

**Status:** PROPOSED

**Context:** Evidence-first recovery contract plus a bounded metadata inspector; live recovery remains unimplemented.

## Why

The operator confirmed on 2026-09-05 that the August outage report is accurate and discovery appears not to have worked since about April; this is operator-reported history, not a new live measurement. The existing checks cannot establish successful collection and ingestion, so recovery must first distinguish credential, authorization, collection, and ingestion failures and leave a repeatable way to detect recurrence.

## What Changes

- Establish a read-only, per-source diagnosis and acceptance contract for Proxmox, pfSense, network discovery, and SNMP, recording evidence and unresolved causes without equating a running container or empty result with success.
- Repair only faults demonstrated by diagnosis through reusable Semaphore playbooks and OpenBao-backed configuration. Credential preflight validates required fields, matching identity and secret, path access, and source authentication before mutation; writes preserve unrelated values and existing working credentials.
- Make the discovery checker strictly read-only and fail on incomplete evidence. Add per-source collection, ingestion, and expected-object gates plus scheduled stale/failed/unknown reporting and a tested notification route.
- Declare source enablement and timing as configuration. Preserve currently enabled SNMP unless the operator explicitly approves a configuration change; missing credentials remain a failed prerequisite, never an implicit opt-out.
- Reconcile the recovery instructions in plan 04 with current schedules and governance plan 03. Bound local Semaphore integration and retirement of the local ORM container writer to separate work; defer SNMPv3, LLDP, production runtime migration, and inventory pruning.

## Capabilities

### New Capabilities

- `platform/netbox-discovery-recovery`: Evidence-based diagnosis, safe recovery, read-only verification, and per-source operational observability for the existing discovery pipeline.

### Modified Capabilities

None. The current main spec inventory contains `platform/n8n-automation`; no existing NetBox recovery capability was found.

## Impact

Expected implementation touchpoints are the existing NetBox discovery workers and agent template, discovery checker and deployment tasks, discovery credential seeding helper, scoped OpenBao policy, and Semaphore declarations. A dedicated read-only NetBox observation path may be added or reused; repair of the broader device-creation token provisioner is conditional on proving it is needed for this recovery and does not expand into Build #1 delivery.

The bounded operator-side Request A metadata inspector and offline tests are included in this change. They do not execute a live diagnosis or close task 1.1; the remaining recovery behavior is proposed.

Dependencies: reachable Semaphore/OpenBao, the current NetBox/Diode stack, source endpoints and authorized credential material, a representative Linux validation host for real network/SNMP scans, and private site-config declarations for enabled sources and expected objects. Public artifacts contain no live addresses, credentials, or topology. See [design](design.md) for source evidence and [tasks](tasks.md) for ordered implementation and validation gates.

Private site-config owns environment declarations and backup/reference material; OpenBao remains the live credential authority. Semaphore retrieves VM access keys from OpenBao and performs diagnosis/recovery on the target hosts. Direct workstation SSH is not a prerequisite, and an absent access mechanism must be repaired as code.

No deploy, live-state change, implementation, new infrastructure service, or destructive cleanup is authorized by this proposal. Later implementation follows feature → dev → main and verifies the exact deployed revision without repointing shared orchestrator records.

## Rollback Plan

Revert the owned code/configuration change and redeploy the previously recorded agent revision/configuration through Semaphore; preserve NetBox data and unrelated services. Keep prior credential versions in OpenBao, verify a candidate against its source before selection, and retain a working credential until replacement and rollback are demonstrated. Never restore an entire stale secret document over sibling keys. Stop only the newly introduced verifier schedule through its declaration if defective; do not stop existing discovery or label an unobserved pipeline healthy. No database migration, delete, prune, SNMP disablement, or runtime conversion is part of this change.
