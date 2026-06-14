# authentik service — architecture (context for agents)

Authentik as the platform's central **IdP / SSO**. Read with the root
[`CLAUDE.md`](../../../../CLAUDE.md) and the plan it implements:
[`plan/development/AUTH-SSO-DEPLOYMENT.md`](../../../../plan/development/AUTH-SSO-DEPLOYMENT.md).

## What it is

- **Central identity + SSO** for every agent-cloud service. OIDC provider +
  Caddy `forward_auth` gate the apps; one login, one user store.
- **Four containers:** `server` + `worker` (same image, different command) on
  **Postgres** + **Redis/valkey** (the worker's broker — matches NetBox's
  valkey choice). The worker applies **blueprints** (config-as-code) on boot.

## How it runs

- **Behind central Caddy.** Authentik serves plain **HTTP on container :9000**
  (`AUTHENTIK_LISTEN__HTTP=0.0.0.0:9000`); Caddy terminates TLS in front and
  reaches it by name on the `local-dev` network (`authentik-server:9000`). The
  published loopback port is for host debugging only — `127.0.0.1:9300` locally
  (step-ca owns `:9000`), mapping to the container's `:9000`.
- **Composable, no fork.** `compose.yml` is env-parameterized (image/ports);
  `compose.local.yml` is a slim overlay (caps, `label=disable`, joins
  `local-dev`). `deploy.sh` is container-lifecycle-only — no secret generation.
- **Secrets from OpenBao** (`secret/services/authentik`): `secret_key`,
  `bootstrap_password` (initial `akadmin`), `bootstrap_token` (initial API
  token), `db_password`. Rendered into `.env` (gitignored, 0600) by
  `deploy-authentik.yml` → `manage-secrets.yml`. Keys never live in the repo.

## Config-as-code (blueprints)

`blueprints/*.yaml` are committed and mounted read-only at `/blueprints/custom`;
the worker applies them idempotently on boot. The seed creates an `agent-cloud`
group. Flows, OIDC providers, and per-app gating are added here as services are
onboarded — never click-configured in the UI (that would drift from code).

## Files
| File | Role |
|---|---|
| `deployment/compose.yml` | server + worker + postgres + redis; `ak healthcheck`; HTTP :9000 |
| `deployment/compose.local.yml` | slim overlay (caps, `label=disable`, joins `local-dev` so Caddy reaches it) |
| `deployment/deploy.sh` | container lifecycle only (verify .env, pull, up, wait healthy — long first boot) |
| `deployment/templates/env.j2` | compose-subst vars + authentik runtime config + secrets from OpenBao |
| `deployment/blueprints/*.yaml` | config-as-code applied by the worker (committed; non-secret) |

`deployment/.env` is rendered per-deploy and gitignored. Local issuance/TLS is
handled by Caddy + step-ca (`*.agent-cloud.test`); prod uses Caddy + Let's
Encrypt on the real domain.
