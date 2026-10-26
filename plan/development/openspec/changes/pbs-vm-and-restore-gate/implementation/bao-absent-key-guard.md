# Shared OpenBao absent-key guard implementation

## Problem

The PBS identity workflow decides that credential fields are absent during its preflight. A concurrent writer can populate them before `bao-merge-keys.yml` performs its own fresh read. An empty `_bm_guard_values` mapping currently passes without checking any field, allowing the helper to CAS the fresh version and replace those credentials.

## Design

Add optional `_bm_guard_absent_keys` to the reusable KV-v2 merge task. It defaults to an empty list, accepts only a list of unique strings, and checks every named key against the same fresh data and metadata version used by the guarded write. Any present key, including one whose value is `null` or `false`, refuses before a write. A successful guarded update remains a version-CAS full write, preserving unrelated siblings. A missing-path create remains protected by CAS 0 and must fail closed if another writer wins; it never falls back to PATCH when any guard is active. Existing callers that omit this option keep their current behavior.

The caller's earlier preflight selects whether the identity workflow is eligible to create; it is not treated as the concurrency guard. Secret values and request results remain inside `no_log` tasks, with fixed safe assertion text.

## Implementation and validation

1. Validate the optional parameter shape and uniqueness before requests.
2. Evaluate absent-key and existing value guards before either update or create writes.
3. Route absent-key guarded updates through the same-version CAS path and suppress the unguarded create-race fallback.
4. Exercise concurrent population, absent create/no-op, present null/false fields, CAS conflict, existing key guards, and sibling preservation using the focused helper tests.

## Security considerations

The guard only controls writes made through this helper and relies on OpenBao KV-v2 CAS semantics. It does not replace caller preflight or prevent another authorized writer from changing data after a successful CAS. No live OpenBao or production task is part of validation.
