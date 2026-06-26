# Credential Lifecycle Implementation Plan

**Date:** 2026-04-05 (extracted from governance doc 2026-05-07)
**Status:** PROPOSED
**Context:** Implementation phases for the credential lifecycle governance defined in `plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md`. No phases have been started. Phase 3 (AppRole TTL enforcement) has a dedicated plan at `plan/development/APPROLE-TTL-ENFORCEMENT-PLAN.md`.

---

## Current Problems

1. **Credential accumulation** — Diode OAuth2 clients created every deploy, never deleted (no `delete_client` in plugin API)
2. **No expiry** — AppRole secret_ids have TTL=0, static KV secrets never expire
3. **No audit trail** — No tracking of creation time, creator, last use, or purpose
4. **No revocation workflow** — Decommissioned VMs leave orphaned credentials forever
5. **No multi-site path strategy** — `secret/services/*` has no mechanism for per-site isolation when scaling beyond one site
6. **Static database passwords** — Postgres credentials persist indefinitely (highest risk)

---

## Implementation Phases

| Phase | What | Effort | Impact | Depends On |
|-------|------|--------|--------|------------|
| 1. Composable vault paths | Add `vault_secret_prefix` to site-config inventory, update `manage-secrets.yml` to use it | Low | Foundation for multi-site | — |
| 2. Credential metadata | Implement `write-secret-metadata.yml` task | Low | Audit visibility | Phase 1 |
| 3. AppRole TTL enforcement | secret_id_ttl=90d, token_num_uses=25 | Low | Limits blast radius | — |
| 4. Diode rotation playbook | Create→Verify→Retire with Hydra admin delete | Medium | Stops credential accumulation | Phase 2 |
| 5. Audit playbook + logging | `audit-credentials.yml` + OpenBao audit backend | Medium | Compliance, detection | Phase 2, o11y integrated |
| 6. Dynamic DB secrets | Configure database engine for Postgres | High | Eliminates static DB passwords | Phase 1 |
| 7. Site lifecycle playbooks | `provision-site.yml`, `decommission-site.yml` | Medium | Multi-site readiness | Phases 1-5 |

---

## Phase 1: Composable Vault Paths

Add `vault_secret_prefix: "services"` to site-config inventory. Update `manage-secrets.yml` to construct paths from `{{ vault_secret_prefix }}/{{ service_name }}`. No migration needed — current paths continue to work.

When adding a second site: create a new inventory with `vault_secret_prefix: "sites/<site_id>/services"`.

---

## Phase 2: Credential Metadata

Implement `write-secret-metadata.yml` task that writes KV v2 custom metadata after every secret store operation. Required metadata fields defined in the governance doc.

---

## Phase 3: AppRole TTL Enforcement

See dedicated plan: `plan/development/APPROLE-TTL-ENFORCEMENT-PLAN.md`.

Summary: Update `manage-approle.yml` to use `secret_id_ttl: 2160h` (90 days) and `token_num_uses: 25` with `| default()` patterns. Semaphore orchestrator AppRole explicitly overrides to unlimited (documented exception in governance).

---

## Phase 4: Diode Client Rotation

**Problem:** `netbox_diode_plugin.client` has `create_client()` and `list_clients()` but NO `delete_client()`.

**Solution:** Use Hydra admin API directly for deletion:

```bash
docker exec netbox-hydra-1 hydra admin clients delete <client_id>
```

**Rotation playbook: `rotate-diode-credentials.yml`**
1. List current clients via `list_clients()` in NetBox manage.py shell
2. Create new client via `create_client()`
3. Verify new client: `POST /diode/auth/oauth2/token` with new credentials
4. If verified: delete old clients via `hydra admin clients delete`
5. Store new credentials in OpenBao with `created_at` timestamp
6. Update `.env` on the VM

**Schedule:** Monthly, independent of deploy-orb-agent.yml

---

## Phase 5: Audit Playbook + Logging

**OpenBao audit backend:**
```bash
bao audit enable file file_path=/openbao/audit/audit.log
```

Pipe to observability stack (Loki) for alerting per governance requirements.

**Credential inventory playbook: `audit-credentials.yml`**

Scheduled weekly via Semaphore:
1. List all credentials under `{{ vault_secret_prefix }}` with creation dates
2. Compare against site-config inventory for expected services
3. List all Hydra OAuth2 clients with ages
4. List all AppRoles and their secret_id ages
5. Report: active, stale (>30 days unused), expired, orphaned
6. Flag credentials missing metadata

---

## Phase 6: Dynamic Database Secrets

Configure OpenBao's database secrets engine for Postgres:

```hcl
resource "vault_database_secret_backend_connection" "netbox_pg" {
  backend       = "database"
  name          = "netbox-postgres"
  allowed_roles = ["netbox-app", "netbox-worker"]
  postgresql {
    connection_url = "postgresql://{{username}}:{{password}}@postgres:5432/netbox"
  }
}

resource "vault_database_secret_backend_role" "netbox_app" {
  backend             = "database"
  name                = "netbox-app"
  db_name             = "netbox-postgres"
  creation_statements = ["CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; GRANT ALL ON ALL TABLES IN SCHEMA public TO \"{{name}}\";"]
  default_ttl         = 3600   # 1 hour
  max_ttl             = 86400  # 24 hours
}
```

Requires compose changes to fetch credentials at container startup via entrypoint script.

---

## Phase 7: Site Lifecycle Playbooks

### Adding a New Site (`provision-site.yml`)

1. Create site in NetBox (DCIM > Sites)
2. Add site inventory in site-config with `vault_secret_prefix: "sites/<site_id>/services"`
3. Run `provision-site.yml` — creates metadata, SSH keys, AppRoles, discovers architecture
4. Deploy services using standard playbooks (paths resolve via inventory)

### Decommissioning a Site (`decommission-site.yml`)

Per governance requirements: stop services → revoke credentials → archive for 90 days → permanent deletion.

---

## Cross-Team Review Summary

| Reviewer | Key Finding |
|----------|------------|
| **Security** | secret_id TTL=0 is critical risk. 90-day lifecycle for Diode clients. Per-site AppRole isolation. |
| **Network** | Composable vault path prefix per site via inventory. Central OpenBao, path-based isolation. |
| **Infrastructure** | Dynamic DB secrets highest impact. Token usage limits. Audit backend to Loki. |
| **Automation** | No delete_client in Diode plugin — use Hydra admin API. Create→Verify→Retire pattern. |
| **Architecture** | Composable `vault_secret_prefix` driven from site-config inventory. KV v2 metadata for audit. |

---

## Cross-References

- `plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md` — governance standards this plan implements
- `plan/development/APPROLE-TTL-ENFORCEMENT-PLAN.md` — Phase 3 detailed implementation
- `plan/architecture/ACCESS-BOUNDARIES.md` — access and escalation policies
- `plan/architecture/AUTOMATION-COMPOSABILITY.md` — composable task library used by all phases
