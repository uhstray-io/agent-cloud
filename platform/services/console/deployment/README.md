# console — deployment

The Dispatch Console (`platform/services/console`) deployed as one container: the image is built on the host from the committed sources, the container runs behind central Caddy, and login goes through Authentik (native OIDC, client `console`). Non-secret preferences live in SQLite on a named volume.

Deploy with the Semaphore template **Deploy console** (or **Deploy console (Local)** on a local-dev instance). Clean redeploy: **Clean Deploy console**. Both run `platform/playbooks/deploy-console.yml` / `clean-deploy-console.yml`.

Operator details, configuration, the host build, and open items are in [`CLAUDE.md`](CLAUDE.md) (also reachable as `AGENTS.md`).
