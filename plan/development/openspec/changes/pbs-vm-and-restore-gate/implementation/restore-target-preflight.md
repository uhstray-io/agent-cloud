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
and verified Dev merge commit `c4eee56be4860702df537d40b71d8d85ad229a33`
by normal merge after PR #726 completed its required checks and merged.
The source guard protects the declared target VMID clusterwide and refuses
ambiguous OpenBao state before managed writers proceed. It cannot establish
that the injected Semaphore role has the required effective permissions or
that every older writer invocation has drained.

This source integration does not reserve the target, authenticate restore
task output, establish authoritative VMID absence, execute a restore, or
authorize cleanup. OpenSpec task 5.2 remains open; the guard and its writer
coverage are a source prerequisite only.

Every assigned mutator imports `preflight-o11y-restore-target-inputs.yml` after
the internal-extra-var refusal and before any AppRole/PVE credential lookup.
That standalone preflight is credential-free: it pins the destination to the
sole private `openbao_svc.service_url`, requires the existing downstream
`openbao_addr` value to match it, rejects launch aliases for the endpoint,
protected AppRole environment names, and private target declaration, then
checks the existing transport rule. The full GET guard repeats those checks
before login. Its URI calls explicitly replace inherited headers, require TLS
certificate validation, disable proxy and redirects, and clear inherited
client-certificate and URL-authentication parameters. A rendered-request test
uses the installed Ansible URI module-default merge function to prove PVE
Authorization defaults cannot reach OpenBao.

Reviewed-checkout manifests include the standalone input preflight and its task
dependency. The bounded payload-path maximum is 16, with regression coverage
for the exact PBS attachment dependency list and rejection above the bound.
