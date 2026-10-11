## Why

Semaphore validates an explicitly declared template JWT TTL against the controller's effective `max_ttl`, which the publisher cannot authoritatively read before publication. Supporting explicit values can therefore cause a config-as-code publication to be rejected after an earlier declaration has already been applied.

## What Changes

- Reject any `jwt_params.ttl` field before publisher writes; support native default TTL only.
- Keep publication of `enabled` and `audience`, preserve live scoped JWT settings including TTL, and document the supported subset and the requirement to bind future explicit TTL support to effective `max_ttl`.
- Remove the custom duration parser and its tests; omit TTL from publication fixtures.

## Capabilities

### New Capabilities

- `semaphore-jwt-publication`: Safe native task-JWT publication using server-default TTL.

### Modified Capabilities

None.

## Impact

Affects the Semaphore publication filter, setup playbook fixtures/tests, and Semaphore publisher documentation. No runtime template changes or live Semaphore operations are included.

## Rollback Plan

Revert this change to restore the previous duration parser and explicit TTL publication support. Do not publish explicit TTL declarations until a future change reads or otherwise safely binds effective `max_ttl` before any writes.
