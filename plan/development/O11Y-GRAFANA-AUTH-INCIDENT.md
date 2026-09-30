# Grafana Authentik sign-in incident

**Date:** 2026-09-30  
**Status:** PROPOSED  
**Context:** Production Grafana returns to its login page after the Authentik OAuth round trip. This plan records the evidence and the controlled repair path.

## Problem

The public Grafana login links to `/login/generic_oauth`. A live browser check reached Authentik's login flow with `client_id=grafana` and the `https://o11y.uhstray.io/login/generic_oauth` callback. An existing Authentik `stray` session listed Grafana, yet repeating the OAuth flow returned to Grafana's login page without signing in. The server-side rejection reason is not yet observed. A separate report says direct Authentik sign-in fails; its exact error remains unknown.

## Design principles

- Diagnose the actual rejection before changing the OAuth client, group rules, or credentials.
- Keep OpenBao authoritative for the shared client secret and apply any repair through reviewed `dev` and Semaphore.
- Preserve Grafana state and all observability data; use the normal non-destructive deploy path only if needed.
- Report only sanitized error categories, never authorization codes, tokens, cookies, email addresses, or raw logs.

## Architecture

```mermaid
flowchart LR
  Browser -->|authorize| Authentik
  Authentik -->|callback| Grafana
  Grafana -->|token and userinfo over Caddy| Authentik
  Semaphore -->|read-only diagnostic| Grafana
  OpenBao -->|shared client secret via Ansible| Authentik
  OpenBao -->|shared client secret via Ansible| Grafana
```

The current public template declares the production root URL, Authentik endpoints, group gate, and a LAN Caddy origin. The current private `site-config` main branch declares Grafana as an enabled Authentik application with the matching strict callback. A read-only TLS check from this workstation trusted the inventory-declared Caddy origin certificate; this does not prove the Grafana container's TLS path.

## Implementation phases

1. Add a Dev-bound, read-only Semaphore diagnostic for a bounded recent Grafana OAuth failure. Restrict output to a fixed allowlist of error categories, with no raw log lines. **Acceptance:** a launched task prints an actionable category plus exact reviewed revision; no matching OAuth failure or an unavailable diagnostic is reported as a fixed category and fails the task.
2. Use the resulting category and the operator's direct-login error to select the smallest config-as-code correction. **Acceptance:** source and live evidence agree on the root cause; the change is reviewed, CI green, and merged to `dev`.
3. Apply only the required normal Semaphore deployment(s), then repeat an existing-session browser sign-in and direct Authentik sign-in. **Acceptance:** Grafana opens an authenticated page, and the operator confirms direct login. No volumes are removed.

## Validation criteria

| Check | Pass condition |
| --- | --- |
| Diagnostic safety | No secrets, identities, or raw URLs in task output |
| Revision | Semaphore checks out the reviewed `dev` SHA |
| OAuth | Authentik session reaches an authenticated Grafana page |
| Direct login | Operator confirms Authentik login works |
| Preservation | Existing Grafana and telemetry volumes remain intact |

## Security considerations

OAuth logs can include authorization codes and personal identifiers. The diagnostic must classify locally and print fixed strings only. It must not print environment variables, provider secrets, request headers, or raw log excerpts. Do not disable TLS verification or weaken the group gate to pass the test.

## Cross-references

- [Platform principles](../../PRINCIPLES.md)
- [Credential and access governance](../architecture/04-credentials-access.md)
- [Caddy and container runtime](../architecture/05-platform-infra.md)
- [Observability implementation](05-observability.md)
- [Canonical agent instructions](../../AGENTS.md)
