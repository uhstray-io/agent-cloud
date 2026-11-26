# console — deployment

The Dispatch Console (`platform/services/console`) deployed as one container: the image is built on the host from the committed sources, the container runs behind central Caddy, and login goes through Authentik (native OIDC, client `console`). Non-secret preferences live in SQLite on a named volume.

Deploy with the Semaphore template **Deploy console** (or **Deploy console (Local)** on a local-dev instance). Clean redeploy: **Clean Deploy console**. Both run `platform/playbooks/deploy-console.yml` / `clean-deploy-console.yml`.

The console reads Semaphore tasks with its own read-only identity (`svc-console`, guest). Run **Provision Console Semaphore Access** (or **(Local)**) once before the first deploy; the deploy stops, naming the missing key, until its token exists in OpenBao.

From Phase 3 the console also reads NetBox and Proxmox, read-only, for the tenant inventory. The variables and the routes' scoping are in [`CLAUDE.md`](CLAUDE.md#inventory-netbox-and-proxmox-read-only-phase-3); the response contract is `../app/docs/phase3-api.md`. The deploy does not provision those credentials yet.

Operator details, configuration, the host build, and open items are in [`CLAUDE.md`](CLAUDE.md) (also reachable as `AGENTS.md`).
