# Plane — work tracker (Commercial edition)

Plane links GitHub issues, each repository's OpenSpec plans and work cards. agent-cloud runs
the **Commercial** edition (the licence is purchased; local-dev runs its Free plan, decision
D2). Plan: `plan/development/16-plane-work-management.md`. Change, with every spike finding
this file relies on: `plan/development/openspec/changes/deploy-plane-work-management/`.

## Plane's files are fetched, never committed

Plane's EULA (§2.1) forbids redistributing its software, so **no Plane-authored file is in
either repository**. On every run, `deploy-plane.yml`:

1. Fetches the pinned release's `docker-compose.yml` and `variables.env` on the controller
   (`fetch_setup.py`) from `prime.plane.so/api/v2/setup/?version=<tag>`, in memory: nothing is
   written on the controller, so a dry run fetches and verifies for real.
2. Verifies **each file's** sha256 against `_plane_setup_pins` in the playbook. Not the zip's
   digest, which changes on every download because the server stamps entry dates per request.
3. Places the Compose file here as `compose.yml` (gitignored), and renders `.env` from
   `variables.env` with every agent-cloud value replaced (`templates/env.j2`).

Moving to another release means pinning that release's two digests in the playbook: a
reviewed change. The pinned release is `v3.3.3` (Joseph, 2026-10-27).

## What agent-cloud changes — `compose.agentcloud.yml`

`deploy.sh` always applies it, and refuses a Plane file that brings a service it does not name.

| Change | Why |
|---|---|
| `restart: always` everywhere; one-shots `"no"` + `agent-cloud.one-shot` | Plane ships `unless-stopped`, which podman's boot unit does not restart |
| `profiles: ["disabled"]` on Plane AI (`pi-*`), Argus, email and the importer worker | Plane switches them off with `deploy.replicas: 0`, which **podman-compose 1.6.0 ignores** |
| Named volumes for every bind mount | `compose down -v` removes them; the deploy tree's git update and rsync never touch data |
| The proxy publishes plain HTTP on `plane_bind:plane_port` only | TLS is at the platform Caddy; HTTPS and SMTP publishes dropped |
| `container_name` `plane-api` and `plane-proxy` | Stable names for the health wait, the configuration phase and the local Caddy upstream |

The project is named `plane` by `COMPOSE_PROJECT_NAME` in `.env`: Plane's file names no
project, and podman-compose ignores a `name:` set in an overlay.

`compose.local.yml` joins the proxy and the API to the `local-dev` network (the API reaches
Authentik through the local Caddy). `monitor`, the licence agent, runs locally too: the API
resolves feature flags and the seat check behind every invitation through it, so without it no
one can be invited. `compose.local-ca.yml`, which
`deploy.sh` applies LAST in local mode, gives the API the combined step-ca and public trust
bundle (`REQUESTS_CA_BUNDLE`) and restates its full volume list: in `compose.local.yml` the
mount would be replaced by `compose.agentcloud.yml`'s volume `!override`, because `common.sh`
applies the local overlay before the declared ones.

**Local-only exception to the proxy hold.** Because the API joins `local-dev`, other local-dev
containers can reach `plane-api:8000` directly, so on a first or clean LOCAL deploy the window
between the API becoming healthy and the setup step is open to them, not only to the proxy. The
local stack runs only the platform's own services, and production has no such shared network:
there the proxy remains the only way in.

## Credentials

Everything is in OpenBao at `secret/services/plane`, generated once and reused:
`plane_secret_key`, `plane_live_server_secret_key`, `plane_silo_hmac_secret_key`,
`plane_aes_secret_key`, `plane_pi_internal_secret`, `plane_cursor_webhook_secret`, the Postgres,
RabbitMQ and MinIO passwords, `plane_machine_signature` (the licence binds to it, so it is never
rotated), and `plane_automation_password` for the first admin.

The OIDC client secret is Authentik's (`secret/services/authentik:plane_oidc_client_secret`,
injected into the provider by `blueprints/plane-oidc.yaml`). The deploy shared-reads it, only when
`plane_authentik_base` is set, and never stores a copy under `secret/services/plane`.

Plane ships **working default credentials** in `variables.env` and in its Compose fallbacks,
identical on every install. `env.j2` refuses to render if any of them comes out empty or
equal to the shipped value. Note the RabbitMQ trap: Plane's Compose reads `RABBITMQ_PASSWORD`,
not the `RABBITMQ_DEFAULT_PASS` its `variables.env` sets, so both are written.

## First run closes the instance

Until the first admin signs up, **anyone who reaches `/god-mode/` claims the instance**. The
proxy is the only way in, so `deploy.sh`'s staged start (`compose_staged_up`) brings every
other service up first and starts the proxy only once the instance is closed: set up AND
self sign-up off, so a run that failed between the two never opens it. Otherwise it
holds the proxy down and prints `PLANE_PROXY_HELD=true`; `deploy-plane.yml` then completes
setup against the API directly (`configure_instance.py`, run inside `plane-api` on
`localhost:8000` with the password on stdin) and runs `deploy.sh` again, which opens the
proxy. A first or clean deploy therefore never has a reachable, unclaimed instance:

1. Instance setup through the admin sign-up form, as `plane_automation_email`.
2. `ENABLE_SIGNUP=0` (invitation-only), PATCHed into the instance configuration. Not through
   `.env`: Plane's Compose file passes no `ENABLE_SIGNUP` to any container (measured on the
   first local deploy, 2026-10-28), so the PATCH is the only control, and the held proxy keeps
   the instance unreachable until it has run.
3. The workspace `plane_workspace_slug`. Its owner is the automation account, never a person.

Never run `manage.py create_instance_admin` before setup: sign-up then refuses because an
admin exists, nothing else completes setup, and the instance is stuck.

The instance configuration read-back returns encrypted settings **decrypted**, so any task
that reads it is `no_log`. The script prints one JSON line and never a credential, so a refusal
reaches the run log through the visible assert after it.

With an `https` public URL Plane marks its cookies `Secure`; the script sends them itself to
`http://localhost:8000`, because a cookie jar would withhold a Secure cookie over plain HTTP and
every POST would fail Plane's CSRF check (a page it serves with status 200).

## Inventory

`plane_svc` hosts need: `plane_public_url`, `plane_trusted_proxies` (the platform Caddy's
address; `private_ranges` locally), `plane_bind`, `plane_port`, `plane_automation_email`,
`plane_company_name`, `plane_workspace_slug`, `plane_workspace_name`. Optional:
`plane_version` (default `v3.3.3`); `plane_authentik_base` (the IdP's base URL) turns sign-in
through Authentik on, and leaving it unset switches it OFF on the next deploy. `plane_admin_role`
(default `admin`) is the role `platform-admins` get from the member reconcile; local-dev sets
`member`, because the Admin role is a plan feature (`WORKSPACE_ADMIN_ROLE`) the Free plan lacks.

## Operate

| Task | Template |
|---|---|
| Deploy or update | `Deploy Plane` (`Deploy Plane (Local)` locally) |
| Destroy everything and redeploy | `Clean Deploy Plane` (`-e confirm_reset=<host>`) |
| Invite and reconcile members from Authentik groups | `Reconcile Plane Members` (plan only; `-e apply=true` applies) |
| Turn open OpenSpec tasks into work items (create-only) | `Sync Plane Projects` (dry run with `--check`) |
| Back up the database | `Back Up Plane DB` (pg_dump of `plane-db` to an owner-only dir on the VM) |
| Restore a named dump | `Restore Plane DB` (`-e plane_dump_file=<path>`; staging first, prior database kept as `plane_prev`) |

**Sync Plane Projects** reads `plane_service_token`, which Deploy Plane mints (get-or-create, never
expires) and stores in OpenBao. Plane offers no route to revoke a service token (its token delete
and list skip them), so revocation is an open follow-up. Which projects, modules
and work items exist, and which module an item is in, follow `tasks.md`: an item deleted in Plane comes
back, so tick or strike the task instead. Task ids and change names are permanent keys; renumbering or
renaming creates duplicates. Everything else about an item (status, assignees, labels, edits) is Plane's.
Each project's states and labels follow `platform/services/plane/schema.yml` (created, or declared
fields patched; never deleted); new work items get its creation labels once. Names are the keys: a
declared state or label renamed in Plane is created again under its declared name.

The dump covers the database only. Uploads live in the MinIO volumes and are covered by a VM
snapshot before an upgrade (change task 1.7).

Logs reach Loki through Alloy's container discovery. `live`, `silo` and `space` can also
export OTLP logs (`OTEL_ENABLED`, `OTLP_ENDPOINT`), proven on 2026-10-27 but not enabled yet.
