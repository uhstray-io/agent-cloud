# authentik service — architecture (context for agents)

Authentik as the platform's central **IdP / SSO**. Read with the root
[`AGENTS.md`](../../../../../AGENTS.md) and the plan it implements:
[`plan/development/02-sso-auth.md`](../../../../../plan/development/02-sso-auth.md).

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

## Platform administrators — one declared list

Every member of `platform-admins` (the Authentik superuser group, the top role in every
SSO'd app) is declared in ONE list, `platform_admins`, and rendered from it: the account
blueprint (`templates/platform-admins.yaml.j2` → `blueprints-active/platform-admins.yaml`),
the worker env's password lines (`templates/env.j2`) and the OpenBao password fields
(`platform/playbooks/vars/secret-declarations/authentik.yml`) are all looped over the same
entries. The gateway deploy derives a per-person identity from the same list
(`platform/playbooks/vars/platform-admins.yml` is the shared reader). There are no fixed
admin blocks any more (`stray-admin.yaml`, `agent-cloud-admin.yaml`, `service-account.yaml`
and the admin entries of `platform-users.yaml` were replaced by the render).

Each entry (a mapping):

| Field | Meaning |
|---|---|
| `key` | lowercase slug; upper-cased it is the worker env prefix and `<key>_password` is the existing OpenBao field, so it MUST equal today's name (`stray_admin`, `agent_cloud_admin`, `wisward`, `jacob`, `svc_automation`) — changing one re-keys a stored password |
| `username` | the Authentik username; changing it declares a SECOND account, it does not rename the first |
| `name`, `email` | display name and address; `email` is required except for a service account |
| `type` | optional, Authentik only: `user` (default) or `service_account` (path `users/service-accounts`, no email, no password; its API token entry is rendered with it from `SERVICE_ACCOUNT_TOKEN`; at most one) |
| `agw_identity` | optional, gateway only; Authentik ignores it |

Where the list comes from: an inventory that sets `platform_admins` REPLACES the default
entirely (it is a whole list, not a merge). With none set, the default in
`platform/playbooks/vars/platform-admins.yml` is built from the per-user vars site-config
already declares (`wisward_*`, `jacob_*`, `stray_admin_email`) with the same defaults the old
fixed blocks used, so an inventory that has not adopted the list renders exactly what it
rendered before. Real values live in site-config; this repo carries placeholders only
(`.test` addresses). The shape is documented in `platform/inventory/local-dev.yml.example`.

The bootstrap `akadmin` (`AUTHENTIK_BOOTSTRAP_*`) is not a blueprint user and is not in
`platform-admins`; it stays as the first-boot break-glass superuser and is outside the list.

`platform_admins_exceptions` (names only, default empty) names `platform-admins` members that
are deliberately NOT in the list. It exists for the drift guard below and nothing else.

The list is validated before anything acts on it, by `platform/playbooks/tasks/validate-platform-admins.yml`
(the deploy includes it before it reads the usernames): a lowercase slug key, a username, a name, an email
unless a service account, no duplicate key or username, at most one service account, and no key whose
`<key>_password` field or own name is a non-user secret (database, bootstrap, secret key, OIDC client
secrets, the service-account token).

**What the credential print may print is not derived from the list.** `platform_admins` can arrive from an
inventory or an extra var, so anything built from it would let a launcher declare an entry and print its
password. `print-platform-user-credentials.yml` instead allows the users blueprint's own `!Env X_PASSWORD`
entries plus the committed constant `_platform_admins_printable_keys` in `vars/platform-admins.yml` (`wisward`
and `jacob`, the two administrators that were printable before the list existed). The break-glass and operator
admins are not in it. The constant's underscore name cannot be overridden as an extra var
(`refuse-internal-extra-vars.yml`), and each key must still be a plain slug that names no static secret.

Identity (username, name, email) is baked into the rendered blueprint; a password never is
(`!Env <KEY>_PASSWORD`), because that file is world-readable on the host.

## Retiring an account

A retired account is declared, not deleted by hand. Name its username in the site-config
inventory list `platform_retired_accounts` (default empty) and run Deploy Authentik:

1. If the account is a platform administrator, remove it from `platform_admins` in the same
   change. The deploy refuses a username that is both retired and active (an administrator, or a
   user the users blueprint creates, including env.j2 defaults), and refuses `akadmin`, before it
   applies anything.
2. The deploy renders `templates/platform-retired.yaml.j2` into `blueprints-active/platform-retired.yaml`:
   one `state: absent` `authentik_core.user` per name. With an empty list nothing is rendered and a
   stale copy is pruned, so a platform that retires nobody carries no extra blueprint.
3. The verify step expects that blueprint applied and every listed name gone from the live API;
   a name that still resolves fails the deploy.
4. For a gateway admin identity, also revoke its key: `Manage agentgateway Client Key` with
   `action=revoke` (the identity must already be out of `platform_admins`).

The list joins the set the legacy `*_LEGACY_USERNAME` variables (the users blueprint's `state: absent`
entry) feed, so one guard and one verify cover both. New retirements use the list; no blueprint or
`env.j2` line is added per account. The initial password in OpenBao (`<key>_password`) of a retired
administrator is not deleted by this.

## Verify — the deploy reads the live state back

Blueprint application is **asynchronous** in the worker: a deploy that placed the files
and saw a healthy server has proven only that the files landed. So the last step of
`deploy-authentik.yml` renders `templates/verify-users.py.j2` and runs it inside
`authentik-server` (bootstrap token from the container env, same as the OIDC check).
It fails the deploy unless:

- every blueprint **this deploy assembled** (shared + enabled apps + `zz-sso-bindings` + `platform-admins`, plus `platform-retired` when the retirement list is non-empty)
  has an API record under `custom/` **and** its status is `successful` — a file the
  worker never discovered has no record at all, which is why the expected set is
  derived from what was placed, not from what the API lists;
- every account the users blueprint declares present exists, is active, and is in at
  least one group (a `!Find` that matched nothing leaves a user every gate refuses);
- every account declared `state: absent` (the users blueprint's legacy entry and `platform_retired_accounts`) is gone;
- **drift guard:** the members of `platform-admins`, read from the group itself, equal the
  declared administrator list plus `platform_admins_exceptions` in both directions. A member
  added by hand in the UI, or by a blueprint block outside the list, fails the deploy
  naming the account — it would otherwise hold the admin tier with no record and no
  gateway identity.

It prints usernames, group names and blueprint statuses — never a credential. Handing
a new user their first login waits on this step passing.

## Files

| File | Role |
|---|---|
| `deployment/compose.yml` | server + worker + postgres + redis; `ak healthcheck`; HTTP :9000 |
| `deployment/compose.local.yml` | slim overlay (caps, `label=disable`, joins `local-dev` so Caddy reaches it) |
| `deployment/deploy.sh` | container lifecycle only (verify .env, pull, up, wait healthy — long first boot). Recreates only on change: `compose_up_if_changed` (`platform/lib/common.sh`) hashes the compose files in effect, `.env`, `env/*.env` and `blueprints-active/` into a label on every container, and a re-run with identical inputs and images is a true no-op. Prints `DEPLOY_CHANGED=true\|false`, which sets the playbook task's changed status. Operator lever: the `Deploy Authentik` survey input `deploy_force_recreate=true` (default `false`; only the literal `true`, any case, forces — `yes` or `1` do not) passes `FORCE_RECREATE=true` and recreates every container regardless of the digest — the fix for a container still holding a stale `blueprints-active/` bind mount (the directory deleted and recreated under it, 2026-10-05: the worker saw an empty `/blueprints/custom`). Deploys converge that directory in place; `platform/tests/test_no_bind_mount_dir_delete.py` fails if a deploy task deletes it |
| `deployment/templates/env.j2` | compose-subst vars + authentik runtime config + secrets from OpenBao |
| `deployment/blueprints/*.yaml` | config-as-code applied by the worker (committed; non-secret) |
| `deployment/templates/platform-admins.yaml.j2` | the administrator accounts, rendered from the declared list at deploy |
| `deployment/templates/platform-retired.yaml.j2` | the retired accounts (`state: absent`), rendered from `platform_retired_accounts`; not rendered when empty |
| `deployment/templates/verify-users.py.j2` | post-apply live-state check (above); rendered per deploy, run inside the server container |

`deployment/.env` is rendered per-deploy and gitignored. Local issuance/TLS is
handled by Caddy + step-ca (`*.agent-cloud.test`); prod uses Caddy + Let's
Encrypt on the real domain.
