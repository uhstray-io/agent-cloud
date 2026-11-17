Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Context

Activepieces is the platform's workflow-automation service, replacing the retired n8n. Plan 26 (`plan/development/26-activepieces.md`) holds the decisions, the upstream facts with their source links, the repository facts, the validation criteria and the open items. This design does not repeat them.

## Goals and Non-Goals

Goals are the ones in plan 26, Problem and Design Principles. Non-goals are plan 26 D13.

## Decisions

The decisions are D1 to D13 in plan 26, Decisions. Each is recorded there with its rationale. The alternatives that lost are recorded below for the four decisions where a choice was made against a real option.

- **D2.** Rejected: a paid Team-tier SAML SSO. The platform uses Authentik forward-auth instead, with the accepted cost of a second, local login per administrator.
- **D3.** Rejected: `SANDBOX_PROCESS`. It needs `CAP_SYS_ADMIN`, which means a privileged container (plan 26, F12).
- **D4.** Rejected: S3 storage now. The platform has no object store, so storage stays in the database until one exists.
- **D10.** Rejected: relying on firewall state alone. The publish bind is the gate, and the firewall is the backstop (plan 26, D10 and the publish-bind section).

## Risks and Trade-offs

The accepted trade-offs are plan 26 D2 (two sign-ins) and D4 (database file storage). Every other risk is an open item in plan 26, and is tracked there.

## Migration Plan

None. The service is greenfield (plan 26, D7).

## Open Questions

The open questions are plan 26, Open items (O1 to O14). The API-key question for the Community edition is plan 26, Risk: no API key on Community edition.
