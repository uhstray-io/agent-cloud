Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Phase 1 checklist

This is the Phase 1 checklist of `plan/development/17-dispatch-console.md`. Every task is safe to rerun after an interruption: the deploy tasks go through the idempotent playbooks, and no task makes a one-off write. Each phase below ends with a validation gate whose final task names the spec scenarios that prove the phase.

## 1. Crate and container build

Only the crates in the approved dependency list may be added. A crate outside that list stops the work and is raised with Joseph A. Wisneski IV.

- [ ] 1.1 Create the crate `platform/services/console/app/`, package name `console`, edition 2021, with the approved dependencies only. Add `platform/services/console/app/.gitignore` with `target/`.
- [ ] 1.2 Add the health route `GET /healthz`, which returns 200 with the text `ok` and needs no session. Add the `probe` subcommand, `server probe <path> <expected_status>`, which runs before the server starts, sends an HTTP/1.0 GET to `127.0.0.1` on `$PORT` (default 8080), prints the status code, and exits 0 only when it equals the expected status.
- [ ] 1.3 Write the deployment directory `platform/services/console/deployment/`: the Containerfile (builder `rust` bookworm and runtime `distroless` cc-debian12 nonroot, both digest-pinned), `compose.yml`, `compose.local.yml`, `deploy.sh`, `templates/env.j2`, `templates/scope.json.j2`, `README.md`, `CLAUDE.md` with `AGENTS.md` as a symlink to it, and `.gitignore`.
- [ ] 1.4 Validation gate: scenario "Health answers without a session" holds. The probe exits 0 for `/healthz` with expected status 200 and exits 1 for a wrong expected status.

## 2. Sign-in and the API session gate

The Authentik client is created first, because the sign-in routes need its secret.

- [ ] 2.1 Write the Authentik client blueprint for `console`. It is confidential, uses the RS256 signing key, has a strict redirect URI for `<BASE_URL>/auth/callback`, and has the launch URL `console_launch`. Store `console_oidc_client_secret` under `secret/services/authentik` and apply it through `deploy-authentik.yml`.
- [ ] 2.2 Implement `GET /auth/login`. It answers 302 to Authentik and keeps the PKCE S256 verifier, the `state` and the `nonce` in the session. The OIDC HTTP client has redirects turned off, and it adds the certificates in `CONSOLE_EXTRA_CA_FILE` when that variable is set.
- [ ] 2.3 Implement `GET /auth/callback`. It exchanges the code, verifies the ID token and its `nonce`, stores the user in the session, and answers 302 to `/`. Use the cookie `console_session` (Secure, HttpOnly, SameSite=Lax) and an 8-hour inactivity expiry.
- [ ] 2.4 Implement `POST /auth/logout`. It clears the session and answers 302 to `/`.
- [ ] 2.5 Require a session on every `/v1/*` route. Without one, the route answers 401 with the JSON body `{"error":"unauthenticated"}`.
- [ ] 2.6 Validation gate: scenarios "Sign-in redirects to Authentik", "Callback stores the verified user", "Callback with a wrong nonce is refused" and "Unauthenticated API call answers 401" hold.

## 3. Tenants and scope tree

- [ ] 3.1 Render `templates/scope.json.j2` from the inventory variable `console_tenants`. The `example` flag comes from `console_scope_is_example`, with a default of false. Example tenants in the inventory are labelled as example. Real values stay in site-config.
- [ ] 3.2 Implement `GET /v1/tenants`, which lists the scope-file tenants the caller is a member of (a shared Authentik group with the tenant's `groups`; `platform-admins` see every tenant), and `GET /v1/tenants/{t}/tree`, which returns the tenant's organizations, sites and environments. An unknown tenant, or one the caller cannot see, answers 404.
- [ ] 3.3 Validation gate: scenarios "Tenant list reads the inventory declaration", "Scope tree for a tenant", "Unknown tenant is not found", "Example data is labelled" and "Every /v1 route answers 401 without a session" hold.

## 4. Profile, preferences and SPA shell

- [ ] 4.1 Create the preferences table in SQLite (`rusqlite`, bundled) at `/data/prefs.db` on the `console-data` volume. Its columns are `sub`, `theme` (default `system`), `timezone` (default `UTC`), `default_scope` and `updated_at`. Validate `theme` as one of `system`, `light`, `dark`; `timezone` as 1 to 64 characters from `A-Z a-z 0-9 _ + - /`; and `default_scope` as null or 1 to 128 characters.
- [ ] 4.2 Implement `GET /v1/me`, which returns the OIDC claims (`sub`, `email`, `name`, `username`, `groups`) and the preferences. Implement `PUT /v1/me`, which takes `{"preferences":{...}}`, answers 422 on invalid input, and returns the same shape as the GET.
- [ ] 4.3 Build the SPA shell: the sidebar with the tenant switcher, navigation, and the account entry that opens the profile; the scope bar; empty states; and the tokens from `plan/product/dispatch-console/DESIGN.md`. The profile page shows the claims locked and the preferences editable.
- [ ] 4.4 Validation gate: scenarios "Profile shows the signed-in claims", "Preference update is saved" and "Invalid preference is refused" hold.

## 5. Deployment through Semaphore

Every step runs through the Semaphore template. Nothing is deployed by hand over SSH.

- [ ] 5.1 Write `platform/services/console/deployment/deploy.sh` for container lifecycle only. It builds the image on the host with `$CONTAINER_ENGINE build -f deployment/Containerfile platform/services/console`, and it reads the secrets it needs from the rendered `.env` file.
- [ ] 5.2 Write `platform/playbooks/deploy-console.yml` as the composable chain: `place-monorepo.yml`, then `manage-secrets.yml`, then `deploy.sh`, then verify. Verify runs `<engine> exec console` against `/healthz` (expecting 200) and `/v1/me` (expecting 401).
- [ ] 5.3 Provision the console's own AppRole with `manage-approle.yml`, and store its credentials under `secret/services/approles/console`.
- [ ] 5.4 Add the `console_svc` group and the `console-local` host to the inventory placeholders, with `service_name` `console`, `container_engine` `podman`, `console_bind` `127.0.0.1` and `console_port` `3410`. Real values stay in site-config.
- [ ] 5.5 Add the route `console.agent-cloud.test` to `console:8080` through `Manage Caddy Sites` in local-dev.
- [ ] 5.6 Add the templates `Deploy console` (with `dev_variant`, and the survey fields `service_branch` and `deploy_force_recreate`), `Clean Deploy console`, `Deploy console (Local)` and `Clean Deploy console (Local)` to `platform/semaphore/templates.yml`. Publish them with `setup-templates.yml`.
- [ ] 5.7 Add the console's sub-directory line to `AGENTS.md`, linking `plan/development/17-dispatch-console.md` and the console `README.md`.
- [ ] 5.8 Validation gate on local-dev through the Semaphore template: scenarios "Deploy builds the image on the host", "Verify checks health and the API gate" and "Deploy converges on re-run" hold.
