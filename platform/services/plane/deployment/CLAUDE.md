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
3. Qualifies the Compose file's Docker Hub short image names with their registry
   (`makeplane/x` -> `docker.io/makeplane/x`, `postgres:15` -> `docker.io/library/postgres:15`),
   after the digest check. Ubuntu 24.04's podman defines no search registry and refuses short
   names (production deploy, Semaphore task 11139); the platform qualifies rather than adding one.
   An image line it cannot rewrite refuses the fetch. Guarded by `test_plane_fetch_setup.py`
   (the platform's `test_compose_image_qualified.py` sees only committed files).
4. Places the Compose file here as `compose.yml` (gitignored), and renders `.env` from
   `variables.env` with every agent-cloud value replaced (`templates/env.j2`).

Moving to another release means pinning that release's two digests in the playbook: a
reviewed change. The pinned release is `v3.3.3` (Joseph, 2026-10-27).

## What agent-cloud changes — `compose.agentcloud.yml`

`deploy.sh` always applies it, and refuses a Plane file that brings a service it does not name.

| Change | Why |
|---|---|
| `restart: always` everywhere; one-shots `"no"` + `agent-cloud.one-shot` | Plane ships `unless-stopped`, which podman's boot unit does not restart |
| `profiles: ["disabled"]` on Plane AI (`pi-*`), Argus, email and the importer worker | Plane switches them off with `deploy.replicas: 0`, which **podman-compose 1.6.0 ignores** |
| `profiles: ["disabled"]` on Plane Runner (`runner`), which Plane ships **on** | Its image needs x86-64-v3 and crash-loops on every production node that can hold Plane (node CPU survey, Semaphore task 12135; `docs/MISTAKES.md` 2.34). Plane Runner scripts are unavailable until Plane moves to a v3-capable node; then set it back to `*always` and declare `vm_cpu: x86-64-v3` |
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

The provider registers two strict redirect URIs: the web callback `/auth/oidc/callback/` and the
mobile app's `/auth/mobile/oidc/callback/`. The mobile app signs in through `/auth/mobile/oidc/`,
which Plane's OIDC documentation does not list, and without that callback Authentik answers
"Redirect URI Error" (AC-711).

The Pro licence key is the operator's, at `plane_license_key`, and the deploy never generates it.
Seed it through the isolated seed environment, with the value in a file, never in argv:

```bash
scripts/semaphore-seed-input.py --template "Seed OpenBao Key" --variant dev \
  --set bao_path=services/plane --set bao_key=plane_license_key \
  --input BAO_VALUE=<key-file> --inventory <id> --url <semaphore> [--apply] < <token-file>
```

Phase 3c of `deploy-plane.yml` (`activate_license.py`) then signs in as the workspace owner (the
automation account; billing needs owner or Admin, and Admin needs the licence, so no person needs
Admin first), reads the plan,
and only when it is not `plane_license_plan` (default `PRO`) posts the key to
`/api/payments/workspaces/<slug>/licenses/` — the route the Billing page calls; Plane documents
no other — and reads the plan back. No key stored: the phase reports that and skips. A refusal
reports the HTTP status only, because Plane's error body can echo the key.

### GitHub integration

On when the inventory declares `plane_github_app_id` (with `plane_github_app_name`, the App's
slug, and `plane_github_client_id`; none of the three is secret). Removing `plane_github_app_id`
switches it off on the next deploy. The App's OAuth client secret (`plane_github_client_secret`)
and private key (`plane_github_private_key`, the base64 of the PEM on one line, which is what
Plane reads in `GITHUB_PRIVATE_KEY` and what the seed CLI accepts) are the operator's: seed each
with Seed OpenBao Key as above, and the deploy refuses to run without them while the App is
declared. `plane_github_webhook_secret` is generated once.

`env.j2` renders the six `GITHUB_*` values together or fails. Phase 3d then sets the App's own
webhook (URL `<plane_public_url>/silo/api/github/github-webhook`, JSON, and the same secret)
through `github_app_token.py set-hook-secret` on the controller; GitHub never returns the secret,
so it is set on every deploy. Verify sends an unsigned delivery through the proxy and requires
401: spike 0.16 showed silo answers 403 when it has no App configured and 401 to an unsigned
delivery once it has the App and the secret. The Cloudflare skip for that one path
(`plane-github-webhook-bypass-challenge` in `platform/infra/cloudflare/waf.tf`) depends on that
check. It skips the bot challenge and the managed rules, which blocked signed deliveries whose
issue bodies discussed security topics (AC-687), and leaves the rate limits on. It is safe to apply at any time: with no App configured silo answers every delivery 403, so
it is not gated on the deploy; after both are live, redeliver one App delivery and expect 202.

Integrations lists GitHub only once Plane's marketplace application for it exists. Plane creates
those applications from a migration that skips on an empty database, from the add-admin endpoint,
or from `setup_instance`. Our first admin signs up through the sign-up endpoint, which takes none
of those paths. Phase 3e (`ensure_marketplace_apps.py`, inside plane-api) therefore runs Plane's own
`create_applications`, which skips any application already present, and fails unless all of them
exist (MISTAKES 1.33).

Connecting the organization is a one-time step in Plane: Workspace settings > Integrations >
GitHub > Connect. GitHub sends the browser back to Plane only when the App has the Setup URL
`<plane_public_url>/silo/api/github/auth/callback` and "Redirect on update" turned on. With the App
already installed, GitHub otherwise stops on its own installation page. Each person connects their
own GitHub account on the same page ("Connect Personal Account"); that needs the callback URL
`<plane_public_url>/silo/api/github/auth/user/callback` on the App.

Sync Plane Projects Play 3 (`link_github_repos.py`) then links every declared project that names a
repository to it, by creating the two records Plane's own GitHub settings create: the "Project
Issue Sync" link, and the "Pull Request State Mapping". The states match the choices Joseph made by
hand for the first eight projects. Open issues go to Todo and closed ones to Done. Pull requests go
Backlog (draft), then Todo (opened), then In Progress (review requested or ready for merge), then Done (merged);
a closed pull request goes to Cancelled. Silo
authenticates by session, so the script signs in as the automation account.

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
`plane_github_app_id`, `plane_github_app_name` and `plane_github_client_id` turn the GitHub
integration on (see Credentials).

## Operate

| Task | Template |
|---|---|
| Deploy or update | `Deploy Plane` (`Deploy Plane (Local)` locally) |
| Destroy everything and redeploy | `Clean Deploy Plane` (`-e confirm_reset=<host>`) |
| Invite and reconcile members from Authentik groups | `Reconcile Plane Members` (plan only; `-e apply=true` applies) |
| Turn open OpenSpec tasks into work items (create-only) | `Sync Plane Projects` (dry run with `--check`) |
| Run the MCP server for harnesses (after Deploy Plane; re-run after a clean Plane deploy, which removes it) | `Deploy Plane MCP` |
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
