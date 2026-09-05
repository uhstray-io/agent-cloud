# OpenBao Deployment

Secrets management backbone for the agent-cloud platform. Provides KV v2 secrets, AppRole auth, and database credential rotation.

## Deploy

Use the Semaphore **Deploy OpenBao** template for an existing platform. Fresh local
genesis uses `make local-bootstrap` from the repository root. `deploy.sh` is the
internal bootstrap implementation, not a workstation deployment entrypoint.

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
| `OPENBAO_LISTEN` | `0.0.0.0` through `deploy.sh`; `127.0.0.1` in Compose alone | Host bind address for port 8200 |
| `NOCODB_URL` | placeholder | NocoDB service URL for seed secrets |
| `N8N_URL` | placeholder | n8n service URL for seed secrets |
| `SEMAPHORE_URL` | placeholder | Semaphore service URL for seed secrets |
| `PROXMOX_URL` | placeholder | Proxmox API URL for seed secrets |
| `PROXMOX_TOKEN_ID` | placeholder | Proxmox API token ID for seed secrets |

`deploy.sh` exports the all-interface default before starting Compose. Without
that export, Compose publishes only on loopback, so remote clients cannot reach
port 8200. Declare an explicit reachable bind address through the Semaphore
configuration when remote access is required (`0.0.0.0` binds all interfaces),
with the platform firewall and transport rules applied. This distinction does
not authorize a direct Compose deployment.

## Secrets

Generated files in `secrets/` are gitignored. Back them up to `site-config/secrets/openbao/` for disaster recovery.

- `init.json` — root token + unseal key (CRITICAL)
- `*-role-id.txt` / `*-secret-id.txt` — AppRole credentials per service

## Policies

Policy scopes are documented in [config/policies/README.md](config/policies/README.md).
Use the declared policy files and policy-application playbooks; do not maintain a
second policy inventory here or edit live policies through ad-hoc API calls.
