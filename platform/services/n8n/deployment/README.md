# n8n — composable deployment

Workflow automation (queue mode): `workflow-n8n` (app) + `workflow-n8n-worker`
+ `workflow-n8n-postgres` (16) + `workflow-n8n-redis`, rootless podman. The
image is pinned (`docker.n8n.io/n8nio/n8n:2.25.7` default, `n8n_image`
inventory override) — never `:latest`, which froze prod on 2.8.3 for six
months. Deployed **only** via Semaphore: `deploy-n8n.yml` (secrets → guarded
cutover diff → `deploy.sh` → verify + owner seed).

## Stateful secrets (OpenBao: `secret/services/n8n`)

| Field | Why it is stateful |
|-------|--------------------|
| `encryption_key` | Re-keying makes every stored workflow credential permanently undecryptable |
| `db_admin_password`, `db_user_password` | Postgres was initialised with them; new values fail auth against the existing volume |
| `owner_password`, `owner_totp_secret` | The real owner's login + TOTP seed — the API-key mint logs in with them (`store-n8n-api-key.yml`; TOTP computed by `playbooks/files/totp.py`, RFC 6238) |
| `n8n_api_key` | The captured `agent-cloud-automation` public-API key |

The single manifest `platform/playbooks/vars/n8n-stateful-keys.yml` names the
stateful env keys, their legacy live-name aliases (`ENCRYPTION_KEY`), and their
OpenBao fields — consumed by both the cutover guard and the pre-seed so they
can never disagree.

## Startup ordering (do not undo)

Two n8n processes racing one boot-migration chain half-apply it (seen live,
prod cutover 2026-09-01). The worker therefore starts only after the app is
READY, enforced at three layers: the compose healthcheck probes
`/healthz/readiness` (**not** `/healthz`, which answers 200 during
migrations), the worker's `depends_on` requires `service_healthy`, and
`deploy.sh` serializes `up` app-first regardless of whether the compose shim
honours conditions (prod runs podman-compose 1.0.6, which does not, and whose
recreate also cannot replace containers that have dependents — remove the
containers first when the config hash changes; volumes carry the data).

## Upgrade runbook (designed for, not aspirational)

1. `Back Up n8n DB` — timestamped `pg_dump` into the owner-only backup dir;
   the report names the artifact.
2. Bump `n8n_image` in inventory; run the guarded `Deploy n8n`.
3. n8n migrates the schema at boot (one-way). If it goes wrong:
   `Restore n8n DB` with the dump — it loads into a staging database and
   swaps only on success, keeping the prior live DB as `n8n_prev`, so a bad
   dump can never destroy the only copy.

## Local dev

`compose.local.yml` overlay; `./scripts/local-dev.sh deploy n8n` (or
`clean-deploy`, or `run <playbook>` for the operational playbooks). Community
node `n8n-nodes-postiz` is env-managed and pinned (name+version+integrity) in
`templates/n8n.env.j2`; UI installs are disabled — declare, don't click.
