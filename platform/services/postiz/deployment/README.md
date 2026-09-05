# postiz — social-media scheduling and publishing

> **Validation status — 2026-09-05:** local Semaphore task 288 restored all six containers.
> Fresh logout → Authentik → calendar sign-in passed; backend TLS returned 200,
> the retained API key returned 200, and absent/wrong keys returned 401.
> Scheduled-publish verification remains open. The production
> host's SSH hardening/firewall checks passed (Semaphore tasks 393/395/396),
> which does not establish application deployment. The production secret check
> reported no Postiz record (task 391); production deploy, sign-in, API-key
> capture and publishing verification remain pending. Recheck current state
> before operations; this is a dated result, not continuous monitoring.

Self-hosted [Postiz](https://postiz.com). Composes and schedules social posts through
an Authentik-authenticated web interface, and exposes an API-key endpoint that n8n
drives to automate post creation, media upload, and scheduling.

- **Public URL (prod):** `https://postiz.uhstray.io`
- **Local:** `https://postiz.agent-cloud.test:8443`
- **Runtime:** rootless podman, five containers (six with the required search-node overlay — see below)
- **Plan:** [`plan/development/14-postiz-social-publishing.md`](../../../../plan/development/14-postiz-social-publishing.md)
- **Change:** `plan/development/openspec/changes/deploy-postiz-social-publishing/`

## The stack

| Container | Role | Published? |
|---|---|---|
| `postiz` | the app — frontend, backend, and the workflow worker | **:5000**, to Caddy only |
| `postiz-postgres` | its own datastore | no |
| `postiz-redis` | queue + cache | no |
| `temporal` | workflow engine that executes scheduled publishing | no |
| `temporal-postgresql` | the engine's own datastore | no |

Only the app publishes a port, so the host firewall needs exactly one service rule —
`apply-firewall.yml` detects published ports, and there is nothing else to detect.

Upstream's reference deployment runs eight containers, adding Elasticsearch, a workflow
UI, and admin tooling. The UI and admin tooling stay out. Elasticsearch was originally
trimmed too, but the gate scoped for that decision fired: as of v2.23.0 the backend
registers more than 3 `Text` search attributes at startup, SQL visibility refuses that,
and the backend never binds (measured 2026-08-30). The search node is therefore added
back as `compose.search.yml`, applied when the inventory sets `postiz_temporal_search:
true` — which every working deployment now needs. The base compose stays five containers
with `ENABLE_ES=false` so the trim remains inspectable and the gate stays in inventory.

## Configuration — two files, two jobs

| File | Read by | Holds |
|---|---|---|
| `.env` | compose (substitution) | image pins, published bind/port, the two Postgres passwords |
| `config/postiz.env` | the app, bind-mounted at `/config/postiz.env` | everything else — URLs, signing secret, OIDC, ~60 social provider slots |

This split is upstream's "Option B" and it is load-bearing, not cosmetic. Compose
`$`-interpolates values it reads; a `$` inside any social client secret would be
silently mangled if credentials went through `.env`. A mounted file is never
interpolated.

Both files are rendered by Ansible from OpenBao on every deploy, are gitignored, and
are **not** the source of truth. Never edit them on the host.

## Deploying

Through Semaphore only — never by SSH-ing in and running `deploy.sh`.

| Template | Purpose |
|---|---|
| `Seed Postiz Secrets` | one-time: write the social platform credentials into OpenBao |
| `Deploy Postiz` | secrets → render both env files → `deploy.sh` → verify health |
| `Clean Deploy Postiz` | **destructive**: wipe containers + volumes, then redeploy |

`deploy.sh` is container lifecycle only — pull, up, wait healthy. It generates no
secrets and talks to no vault. It refuses to start if either rendered file is missing,
deliberately: a missing bind source would otherwise be created as a *directory* and the
app would boot with no configuration at all.

## Auth

Authentik OIDC **in the app**, not a Caddy `forward_auth` gate. That choice is what
keeps the automation endpoint usable — an edge gate would redirect n8n's API-key calls
into a browser sign-in flow.

Three upstream behaviours worth knowing before you debug a sign-in failure:

1. The redirect URI is hardcoded to `<public-url>/settings`. There is no configurable
   callback path, so Authentik's Strict redirect must match that exactly.
2. The scope is hardcoded to `openid profile email`. Upstream ships a
   `POSTIZ_OAUTH_SCOPE` variable that its own provider code never reads.
3. Identity comes from the **userinfo** endpoint (`email` + `sub`), not an ID token. So
   the signing key and PKCE are irrelevant — but the `email` scope mapping is
   load-bearing. A missing email claim fails sign-in.

**Registration lockdown** is configuration, not a manual step: deploy with
`postiz_disable_registration: false`, sign in once, set it `true`, redeploy.

## Operator steps automation cannot do

- ~~Create the public DNS record.~~ Not needed — the zone is config-as-code via
  OpenTofu and `postiz.uhstray.io` is already declared and live. A DNS change means
  editing `platform/infra/cloudflare/` and running `Apply Cloudflare Tofu`.
- Update each social platform's OAuth redirect URI to the public host. **Connecting an
  account fails until this is done** — the developer consoles are not reachable from here.

## n8n integration

Base `https://postiz.uhstray.io/api/public/v1`, API key from the app's settings, with
`API_LIMIT` (default 90/hr on post creation) as the budget workflows must respect. The
contract lives in [`../context/use-cases.md`](../context/use-cases.md).

Automation goes over the public host rather than the LAN on purpose — Caddy is already
the only source the firewall permits, so there is no second access path to maintain.
