## Context

The publisher sends template declarations to Semaphore. Semaphore validates a provided template JWT TTL against its effective JWT maximum TTL. The publisher cannot authoritatively read that server setting before publication, so a valid-looking duration can still be rejected by Semaphore. The publisher already validates the full selection before writes and preserves live `jwt_params` during scoped survey updates.

## Goals / Non-Goals

**Goals:**
- Restrict declarative JWT publication to `enabled` and `audience`, with native default TTL.
- Reject a TTL field during preflight, before environment or template mutations.
- Preserve existing scoped live JWT settings, including their TTL.
- Document the supported subset and the prerequisite for future explicit TTL support.

**Non-Goals:**
- Discovering or changing Semaphore's effective `max_ttl`.
- Changing any live template or performing a live publication.

## Decisions

- Treat the presence of `jwt_params.ttl` as unsupported, regardless of its value. This removes a local duration parser that cannot establish server compatibility.
- Keep the existing `jwt_params` object readback and scoped mutation refusal behavior intact.
- Remove TTL from all publisher fixtures; add zero-write cases for explicit `5m` and `25h` declarations, including one later declaration after an earlier environment-bearing declaration.
- Permit future explicit TTL support only after a pre-publication binding proves the declared TTL does not exceed Semaphore's effective `max_ttl`.

## Risks / Trade-offs

Templates that explicitly request TTL must remove that declaration before publication. Existing live JWT settings remain untouched by scoped survey updates.
