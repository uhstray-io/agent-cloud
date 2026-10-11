## ADDED Requirements

### Requirement: Native default JWT TTL publication

The publisher SHALL support declared task JWT `enabled` and `audience` fields while relying on Semaphore's native default TTL. It SHALL reject a declaration containing `jwt_params.ttl` before performing any environment or template writes.

#### Scenario: Explicit TTL is refused before writes

- **WHEN** a selected declaration has an explicit `jwt_params.ttl` value
- **THEN** publication refuses before writing any environment or template

### Requirement: Scoped live JWT settings are preserved

A scoped survey publication SHALL preserve the existing live JWT settings, including the server-managed TTL, when the declaration does not publish JWT settings for that scoped update.

#### Scenario: Survey-only update keeps live TTL

- **WHEN** a scoped survey update targets a template with live JWT settings and the declaration omits `jwt_params`
- **THEN** the publisher sends and verifies the existing live JWT settings unchanged
