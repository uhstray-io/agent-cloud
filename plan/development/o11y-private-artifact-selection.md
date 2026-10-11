# O11y private backup artifact selection implementation plan

## Scope

Implement the bounded source workflow that lets an operator privately preserve a
sanitized backup-artifact catalogue, choose one row explicitly, re-enumerate it
through GET-only Proxmox calls, and push a private site-config inventory branch
containing one exact artifact selector. The workflow does not merge site-config,
activate the selector, restore an artifact, alter backup configuration, or change
retention or capacity.

The OpenSpec `4.6e` task remains unchecked. A selector proposal does not prove
immutability, backup contents, isolated restore, cleanup, or restore success.

## Workflow

1. `capture`: use the existing artifact inspector and the GET-only VM, node
   storage, backup-content, and VM-config census inputs; validate all visible
   backup-capable stores and rows;
   retain the exact volid-to-sanitized-row mapping only in a private site-config
   capture branch. Print numbered sanitized rows and the private commit reference.
2. `propose`: require the private capture commit, an explicit row number, and
   reviewed agent-cloud and site-config base revisions. Read the private capture,
   reject malformed, future-dated, or older-than-30-minute capture data, then
   repeat the GET-only census and refuse any identity, scope, or row-set drift.
3. Resolve only the explicitly selected row to the existing five-field private
   selector. Validate the proposed inventory change and push a site-config branch
   whose diff contains only that selector addition. Leave PR creation and merge
   to private review; do not activate or merge automatically.

Capture permits any non-empty validated candidate set. Proposal compares the
complete private row set independent of Proxmox response ordering, so a fixed
candidate count is unnecessary and would make future captures brittle.

Capture and proposal are separate branch operations. Capture stores the fixed
private path `catalogs/o11y/backup-artifacts.json` in a fresh branch based on the
reviewed site-config integration tip. Version 1 records the runner observation
epoch, reviewed public/private base commits, target host/node/VMID, sorted storage
IDs with raw backend types and normalized content, and exact sorted artifact
rows. Rows are numbered by their committed array position; no duplicate row
number is stored. Duplicate JSON keys, unknown fields, incorrect types, duplicate
identities, broken storage linkage, symlink path components, and invalid existing
catalog content refuse.

Proposal starts from that same reviewed private base, reads the exact capture
commit separately, validates its single-parent catalog-only provenance, and
repeats the complete GET-only census. Freshness is 0–1,800 seconds and the full
normalized storage/artifact sets must match independent of API order. The
operator supplies a one-based row. Its deterministic branch is
`proposal/o11y-artifact-<capture-commit>-row-<row>`. An existing branch is
accepted only when provenance, parent, one-file inventory diff, remote tip, and
reconstructed bytes all match; this path returns a receipt without making a new
commit and makes no freshness claim. Differing branch content is preserved and
refused.

Inventory insertion is restricted to the literal
`agent_cloud.children.o11y_svc.hosts.<sole-host>` structure, direct matching
`vm_node`/`vm_vmid` fields, and one new host-level selector. Duplicate keys,
aliases, anchors, merge keys, custom tags, flow mappings, selectors elsewhere,
ambiguous layout, or any semantic change beyond that selector refuse. The actual
private layout remains unverified and is a runtime gate.

The shared site-config Git tasks add optional reviewed-base mode; existing
callers retain their fresh-branch behavior. The proposal push rechecks the
integration tip before and after the branch push and requires the proposal ref to
be absent before pushing. These separate ref checks are not an atomic cross-ref
transaction. If the base moves after the push, the workflow reports refusal and
preserves the unmerged branch for review; no merge or force push is attempted.

## Safety and output

- Keep Proxmox tokens, API responses, volids, private inventory values, and
  private paths inside `no_log` tasks or helper stdin. Visible output is limited
  to fixed refusals, sanitized row numbers/metadata, reviewed revisions, and
  branch/commit references.
- Use the existing OpenBao secret and pinned-host-key site-config Git patterns.
  Stage only the capture catalogue or production inventory path for its phase.
- Use a runner-generated UTC timestamp and compare candidates as complete
  identity-plus-metadata records; future, stale, duplicate, missing, added, or
  changed rows refuse without echoing the row.
- Require the operator's row number. Do not infer newest, largest, or protected.
- Do not mark OpenSpec `4.6e` complete. Restore proof remains an independent gate.

## Files and checks

- Add a focused private-catalogue/inventory helper and tests for sanitization,
  explicit selection, freshness, reordering, drift, and safe refusal.
- Add the Dev-bound capture/propose playbook and Semaphore template, reusing the
  existing artifact inspector and site-config clone/push tasks.
- Update the estate OpenSpec design/spec/task notes without checking `4.6e`.
- Run the focused Python tests and Ansible syntax check for the new playbook.

## Constraints

No private site-config checkout or live Semaphore task is required for this
source-only slice. No direct Proxmox writes, laptop OpenBao HTTP, inventory
activation, restore, VM growth, retention changes, destructive deploy, commit,
push, PR, merge, or deployment is in scope.

## Live transport follow-up (2026-10-11)

After the source change merged, Dev-bound Semaphore capture task 4351 stopped at
its first Proxmox VM-list GET, before writing a private catalog. That request
used Ansible's default certificate validation. A separate credential-free TLS
probe to the inventory-declared Proxmox endpoint failed certificate validation;
the protected task's exact exception and endpoint equivalence remain unverified.

The shared four-request Proxmox census must use the existing inventory-controlled
`proxmox_validate_certs | default(false) | bool` setting and refuse redirects,
matching the backup-readiness survey. Keep this setting on those Proxmox GETs,
not on unrelated OpenBao or GitHub requests. Preserve HTTPS, GET-only access,
`no_log`, and failure before a catalog write. A focused test must require both
transport options on every census request. After independent review and green
CI, rerun capture from the reviewed Dev revision with fresh private-base pins.
Capture is a private Git write; it is not a TLS diagnostic. Re-enable certificate
validation after the Proxmox CA and hostname are made trustworthy to Semaphore.
