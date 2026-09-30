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

## Read-only diagnostics after task 2103

Task 2103 checked the exact merged revision from a clean Semaphore checkout, then became unreachable before the diagnostic command could run because Ansible could not create its default remote temporary directory on a full guest root filesystem. Independent Proxmox filesystem information confirmed the guest's ext4 root was full while its virtual disk was larger than the root logical volume. No service or storage changes were made.

Both read-only diagnostics verify `/dev/shm` is tmpfs before creating or using `/dev/shm/ansible-tmp`; they also require the exact staging path to remain on writable tmpfs with at least 1 MiB available before transferring any Ansible module. A full persistent root filesystem therefore does not become a prerequisite for the read-only check, and the preflight never creates a fallback directory on persistent storage. The host storage diagnostic also reports sanitized guest-root filesystem capacity, root logical-volume size, and root volume-group free space from read-only LVM reports; device and volume-group names are omitted. These measurements are diagnostic evidence only. Any LVM or filesystem growth remains gated on reviewing the live readback and an explicit, separately reviewed change.

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
