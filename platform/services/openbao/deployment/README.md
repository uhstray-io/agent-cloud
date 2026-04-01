# OpenBao Deployment

Secrets management backbone for the agent-cloud platform. Provides KV v2 secrets, AppRole auth, and database credential rotation.

## Deploy

```bash
bash deploy.sh
```

The script is idempotent (safe to re-run) and performs 7 steps:
1. Start OpenBao container
2. Initialize (1-of-1 Shamir, Raft storage)
3. Unseal
4. Enable secrets engines (KV v2 + database)
5. Write policies (nemoclaw-read/rotate, nocodb/n8n/semaphore-write, semaphore-read)
6. Create AppRoles (nemoclaw, nocodb, n8n, semaphore)
7. Seed placeholder secrets

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENBAO_LISTEN` | `0.0.0.0` | Bind address for port 8200 |
| `NOCODB_URL` | placeholder | NocoDB service URL for seed secrets |
| `N8N_URL` | placeholder | n8n service URL for seed secrets |
| `SEMAPHORE_URL` | placeholder | Semaphore service URL for seed secrets |
| `PROXMOX_URL` | placeholder | Proxmox API URL for seed secrets |
| `PROXMOX_TOKEN_ID` | placeholder | Proxmox API token ID for seed secrets |

## Secrets

Generated files in `secrets/` are gitignored. Back them up to `site-config/secrets/openbao/` for disaster recovery.

- `init.json` — root token + unseal key (CRITICAL)
- `*-role-id.txt` / `*-secret-id.txt` — AppRole credentials per service

## Policies

All policy files in `config/policies/` define least-privilege access:

| Policy | Scope | Used By |
|--------|-------|---------|
| `nemoclaw-read` | Read `secret/services/*` | NemoClaw agent |
| `nemoclaw-rotate` | Read `database/creds/nemoclaw-role` | NemoClaw (dynamic DB) |
| `nocodb-write` | CRUD `secret/services/nocodb` | NocoDB deploy |
| `n8n-write` | CRUD `secret/services/n8n` | n8n deploy |
| `semaphore-write` | CRUD `secret/services/semaphore` | Semaphore deploy |
| `semaphore-read` | Read `secret/services/*` | Semaphore playbooks |
