# Semaphore operator API token rotation

## Problem and boundary

The workstation's production Semaphore API token appeared in an agent transcript
after an interpreter-on-stdin command was combined with a secret-file redirect.
`docs/MISTAKES.md` §4.10 records earlier occurrences of the same failure.
Treat the operator token as exposed. Keep production deployment paused until it
is replaced and the old token is proven invalid.

The workstation token and `secret/services/semaphore:api_token` in OpenBao are
separate authorities until a protected comparison proves they contain the same
credential. Do not overwrite the controller's token merely because the
workstation file was exposed. The current backend and its token-deletion
semantics must be verified before revocation.

## Implementation and rollout

1. Add a reusable operator-side rotation command. It reads the old token from
   stdin, uses HTTPS without redirects, verifies the current user and the
   unique literal short token ID, and never prints token values or embeds them
   in process arguments or error text. It records a private recoverable state
   before creating exactly one replacement.
2. Verify the new token and compare the old token with the controller's OpenBao
   value inside a protected, Dev-bound Semaphore playbook. Update OpenBao only
   on an exact match, using a guarded merge that preserves other secret fields.
   An uncertain result blocks revocation and is reconciled before retry.
3. Atomically replace the local operator backup with mode `0600`, verify it,
   then revoke only the old token. Require the new token to authenticate and
   the old token to fail. Reject ambiguous or wildcard-bearing short IDs and
   do not delete while another token change for that user may race this run.
4. Add focused tests for every failure boundary, redaction, and idempotent
   recovery. Add a deterministic hook guard for commands that combine an
   interpreter program from stdin with a secret-file stdin redirect; safe
   script-file plus stdin calls remain allowed.
5. Merge after one independent review and final-head CI. Run the rotation from
   reviewed `dev`, record sanitized receipts, then resume the non-destructive
   o11y deployment and live acceptance checks. Preserve existing data.

The pinned Semaphore v2.19.11 source defines create/list/delete behavior for
`/api/user/tokens`; its SQL deletion path uses a prefix match. The rotation
command must verify the deployed backend and refuse any unproven deletion
condition. See `platform/semaphore/README.md`, `plan/architecture/04-credentials-access.md`,
and `docs/MISTAKES.md` §4.10 for the existing credential and incident rules.

## Dev task 5013 follow-up

Task 5013 failed at the first no-log input assertion, before any OpenBao call.
Both the old and newly created operator tokens still authenticated; the local
backup remained old; private recovery state remained mode `0600` at phase
`verified`; and the isolated Semaphore environment was clean. The root cause is
unknown. Do not infer it from the assertion failure.

The input preflight now checks endpoint and encrypted-token-input presence under
`no_log`, then prints only the three bounded categories and their present or
missing status. A preflight refusal preserves the private recovery state and
returns a fixed safe message. Resume is gated on focused offline evidence that
all missing-input cases refuse before OpenBao, sentinels never appear in output,
and a refusal from `phase=verified` causes no token-creation request. After those
offline tests pass and the Dev seed environment is confirmed clean, retry may
proceed from saved state through the reviewed Dev-bound workflow. At that
point, this follow-up had performed no live retry; task 5013's root cause
remains historically unknown.

## Rotation recovery and production check-mode outcome

PR #583 merged to `dev` as `318c7e84baf5e4bdb24277e50738d452465ac976`. The
saved verified state was resumed without creating another token. Dev-bound
Semaphore task 5026 succeeded: its isolated encrypted inputs were removed, and
the protected controller-token comparison matched exactly and updated OpenBao.
The local backup is mode `0600`; recovery state was removed; replacement-token
authentication succeeded and the old token was rejected.

Production o11y check-mode task 5027 later failed at the existing Alloy health
check after eight retries, with health still `starting`; no apply ran. The cause
remains unknown. Track the separate Alloy diagnostic in task 3.3 of the
[estate-wide observability OpenSpec](openspec/changes/estate-wide-observability-instrumentation/tasks.md).
