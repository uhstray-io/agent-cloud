# postiz deployment — agent notes

Operator documentation is in [`README.md`](README.md); read it first. This file records
only what an agent working here is likely to get wrong.

## Do not "fix" these — they are deliberate

**Two env files, not one.** `.env` is compose-substitution only; `config/postiz.env` is
the app's config, bind-mounted read-only. Consolidating them looks tidier and breaks the
service: compose `$`-interpolates what it reads, and a `$` inside any of the ~60 social
client secrets would be silently corrupted. Upstream calls the mounted-file style
"Option B" and marks the single-file style not-recommended.

**`ports:` is env-driven in the base, not overridden in the overlay.** Compose merges
`ports` lists by *appending*, so an overlay can never remove a base publish. The local
loopback bind comes from `POSTIZ_BIND`/`POSTIZ_PORT` in `.env`. Adding a `ports:` entry
to `compose.local.yml` publishes a *second* port; it does not replace the first.

**`--force-recreate` in `deploy.sh` is required.** The app reads config from a
bind-mounted file. Changing that file's *content* is not a compose-spec change, so a
plain `up -d` leaves the old container running with stale config — a redeploy that
appears to succeed and changes nothing.

**No Elasticsearch, no workflow UI, no admin tooling.** Five containers, not upstream's
eight. The engine runs standard visibility on its own Postgres. Do not add the search
node back because upstream's compose has it — add it back only if a scheduled post
demonstrably fails to fire, and then via the inventory-gated path in `compose.yml`'s
header.

**`temporal-postgresql` is pinned to Postgres 16 while `postiz-postgres` is 17.** Not an
oversight — Temporal 1.28 supports up to 16. Do not "align" them.

**`NOT_SECURED` is absent on purpose.** Upstream documents it as dev-only; it disables
security checks. The prior developer-machine config set it. It must never reach a host
reachable from the internet.

**`POSTIZ_OAUTH_SCOPE` is not templated.** It exists in upstream's compose and
`.env.example`, but upstream's provider code never reads it — the scope is hardcoded to
`openid profile email`. Templating it would imply control that does not exist.

## Debugging sign-in

Nearly every failure is one of three things, in this order of likelihood:

1. **Redirect mismatch.** Upstream hardcodes the redirect to `${FRONTEND_URL}/settings`.
   If `postiz_public_url` and the URL the browser actually uses differ by even a port,
   Authentik rejects the redirect. Locally the browse URL includes `:8443` unless
   `make local-https` is running.
2. **Missing `email` claim.** Identity comes from the userinfo endpoint, reading `email`
   and `sub`. If the Authentik provider lacks the `email` scope mapping, sign-in fails
   with no account created.
3. **Local TLS trust.** The backend calls the IdP server-side. Locally the IdP presents
   a step-ca leaf, so `certs/step-ca-bundle.crt` must be present (distributed by
   `tasks/distribute-ca-root.yml`, gated on `local_mode`) and `NODE_EXTRA_CA_CERTS` set.
   Symptom is a TLS verification error in the app's logs, not an auth error.

The signing key and PKCE are *not* on this list — the flow never validates an ID token.

## Secrets

All at `secret/services/postiz`. Three are generated once and reused —
`postiz_jwt_secret`, `postiz_db_password`, `postiz_temporal_db_password`. Regenerating
the first invalidates every session **and every API key n8n holds**; regenerating the
second locks the app out of its existing volume. The nine social credentials are seeded
by `seed-postiz-secrets.yml`. `postiz_oidc_client_secret` is a `_shared_read` from
`authentik`, which owns it — never store a copy under this service's path.

Unseeded provider slots render empty, so enabling a new platform is a seed plus a
redeploy with no code change. Do not add a provider by editing the template.

## Never

- Run `deploy.sh` over SSH. Deploys go through Semaphore, which injects the OpenBao
  credentials.
- Put `no_log: true` on the deploy, health-check, or verification tasks. It belongs on
  credential-handling steps only — a past failure in this repo was censored exactly that
  way and made a Semaphore run undiagnosable.
- Commit a rendered `.env`, `config/postiz.env`, or anything under `certs/`.
