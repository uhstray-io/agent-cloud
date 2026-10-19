## Why

The pinned, GET-only PBS allocation preflight passed, but the o11y VM still has no protected local PBS copy or proven isolated restore. Longer retention and VM growth remain unsafe until a reproducible backup and restore gate closes.

## What Changes

- Discover visible Debian cloud-init templates, VM records, and store metadata through a Dev-bound read-only Semaphore workflow. The survey reports sanitized visible counts and marks inventory visibility unverified because Proxmox may filter these lists by ACL. Provisioning must independently check the exact declared VMID and store immediately before any write.
- Declare the PBS target and its node/storage placement only in private site-config. Reuse the existing Semaphore/OpenBao VM provisioning path, then attach a distinct 512-GiB virtual data disk on the inventory-declared thick-LVM image store. Recheck target identity, disk lineage, health, headroom, and conflicts immediately before a write. No host physical disk is initialized.
- Configure a supported Debian-based PBS guest, its data filesystem and datastore, and scoped PVE-to-PBS credentials through idempotent Semaphore playbooks. Read back the data path and access rights. Keep the existing backup destination until the new path passes verification.
- Produce an o11y backup on PBS and prove an isolated restore into a dedicated, preflighted target. Verify guest filesystem and telemetry volumes; clean up only the exact restore target created by this workflow after retaining a sanitized receipt.
- Keep o11y volumes intact and Prometheus/Loki/Tempo retention at 15d/7d/168h until immutable-copy policy, isolated restore, and seven-day capacity evidence pass the separate growth gate.

## Capabilities

### New Capabilities

- `pbs-vm-restore-gate`: Dev-bound discovery, guarded PBS VM/data-disk bootstrap, scoped backup access, and isolated o11y restore verification.

### Modified Capabilities

None.

## Impact

Public agent-cloud: Proxmox survey and provisioning playbooks, PBS guest/datastore and restore workflows, Semaphore declarations, tests, and observability plan. Private site-config: the reviewed PBS host, static address, VM/storage declarations, and non-secret operator attestation. OpenBao: PBS credentials and least-privilege API token. Production Proxmox/PBS state changes occur only through reviewed Dev-bound Semaphore tasks; no destructive o11y redeploy or retention expansion is part of this change.
