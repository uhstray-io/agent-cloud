# ERPNext — architecture (deployment + how it fits the platform)

ERPNext is the platform's first **stateful financial system of record** — the
books for UhhCraft revenue, marketplace payouts, and Zord license sales. It runs
Frappe/ERPNext (frontend + backend + workers + scheduler + websocket) over
MariaDB and a Redis cache/queue pair, deployed by the standard composable
pattern (OpenBao → manage-secrets → `.env` → deploy.sh).

## Stack

- `erpnext-db` — MariaDB 10.6 (utf8mb4); holds every site DB.
- `erpnext-redis-cache` / `erpnext-redis-queue` — Redis 7 (unauthenticated on the
  internal `erpnext` network only; neither publishes a host port).
- `erpnext-configurator` — one-shot; writes `common_site_config.json` (DB/Redis
  endpoints). Run via `compose run --rm configurator`, never long-lived.
- `erpnext-backend` — gunicorn app server.
- `erpnext-frontend` — nginx; the only published port (`8080`); Caddy upstream.
- `erpnext-websocket` — Frappe socket.io (realtime).
- `erpnext-queue` — a SINGLE background worker on `long,default,short` (slim
  tier). Prod splits this into `queue-short` + `queue-long`.
- `erpnext-scheduler` — `bench schedule` (cron-equivalent background jobs).

## Deploy contract

- **deploy.sh is container-lifecycle only** and stages startup explicitly
  (podman-compose 1.0.6 ignores `depends_on:` conditions): backing tier → wait
  healthy → one-shot configurator → app tier. It reads `.env` (templated by
  `deploy-erpnext.yml` from OpenBao) and never touches OpenBao.
- **post-deploy.sh** creates the site idempotently (`bench new-site` guarded by
  a `test -d sites/<site>` check). Until it runs, the frontend healthcheck
  (`/api/method/ping`) is expected to report unhealthy. It reads `.env` only.
- `deploy-erpnext.yml` runs both as separate phases, then verifies
  `/api/method/ping → 200`.

## Local-dev specifics (slim tier)

This service ships **slim-first** (LOCAL-DEV-DEPLOYMENT.md P4, ratified
2026-06-12): the base compose is already the laptop shape and `compose.local.yml`
adds only memory caps + the `local-dev` network. Deltas vs the prod plan (§7):

- **Single `queue` worker** on `long,default,short` (covers every queue) instead
  of the prod `queue-short` + `queue-long` split.
- **No MinIO / backup / cross-mirror.** Backups and the peer-VM cross-mirror are
  prod-only DR (plan §8.3); the local tier has no `erpnext-minio`, no
  `MINIO_*`/`PEER_*` secrets, and no `backup.sh`.
- **Frontend on `[erpnext, local-dev]`** so central Caddy reaches it as
  `erpnext-frontend:8080`; DB + Redis stay on the internal `erpnext` network.
- Frontend publishes `${ERPNEXT_BIND:-127.0.0.1}:${ERPNEXT_PORT:-8080}:8080`
  (loopback by default; `deploy-erpnext.yml` passes the inventory values).
- Secrets are the `LOCAL_FAKE_` set at `secret/services/erpnext` — the env
  template references `{{ secrets.* }}` only, so local and prod share one file.
- Measured budget target: **≤3.5 GB** total (caps sum to ~3.4 GB).

## Privacy posture

ERPNext is the highest-stakes service on the platform: it holds customer PII,
payment detail, and raw ledger lines (all **Sensitive** — never reach a frontier
API). The LLM integration (read-only MCP profile, the llm-gate redaction
service, the k-threshold rule) is layered on in later phases and is NOT part of
this deployment.

Full design + phased rollout: `plan/development/ERPNEXT-DEPLOYMENT.md`;
local tier: `plan/development/LOCAL-DEV-DEPLOYMENT.md` (Phase 4).
