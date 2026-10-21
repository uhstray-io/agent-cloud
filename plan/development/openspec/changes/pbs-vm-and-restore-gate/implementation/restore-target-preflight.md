# Restore target preflight source integration

The independently reviewed, fix-verified source head
`9099a421b01016226cb806560c43a6d6da819d69` is integrated by normal merge
commit `64ed7ae0`. It adds the GET-only
`platform/playbooks/preflight-o11y-restore-target.yml` workflow and the
Dev-bound `Preflight o11y Restore Target (Dev)` catalog definition. The
catalog exposes only the exact reviewed `expected_repository_sha`; target
identity and artifact selector remain private inventory values.

The preflight checks one distinct restore target declared as disconnected,
one exact selected backup artifact, current Proxmox resource and storage state,
and the configured headroom. It does not reserve a VMID, create or modify a
VM, or prove restore success. The playbook's `verify` tag selects the same
read-only checks. The separate implementation notes describe the exact
sanitized failure/status contract.

Source integration does not satisfy OpenSpec 5.1 live acceptance. The private
target and artifact declarations, current VMID availability authority, and
ACL census visibility remain unverified. No restore target has been reserved
or used, and no restore has been attempted.

## Coordination and mutator guard source boundary

The coordination and reserved-target guard source is developed separately in
`plan/development/O11Y-RESTORE-COORDINATION.md`. Its local worktree includes
the exact PBS feature source head `e149fe641cf9f2a9034969dc9b3c2daf51032b09`
by normal merge while PR #726 remains pending its required checks and merge.
The source guard protects the declared target VMID clusterwide and refuses
ambiguous OpenBao state before managed writers proceed. It cannot establish
that the injected Semaphore role has the required effective permissions or
that every older writer invocation has drained.

This source integration does not reserve the target, authenticate restore
task output, establish authoritative VMID absence, execute a restore, or
authorize cleanup. OpenSpec task 5.2 remains open; the guard and its writer
coverage are a source prerequisite only.
