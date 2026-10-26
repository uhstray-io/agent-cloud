Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Why

The Dispatch Console is the agent-cloud administration UI. Its design and phases are recorded in `plan/development/17-dispatch-console.md`. Phase 0 of that plan settled the two open framework and code-location questions on 2026-10-25, which lets the first buildable slice start. Phase 1 gives the console its skeleton: sign-in through Authentik, a read-only tenant and scope tree from the inventory declaration, a profile page that stores preferences, and a deployment through the same Semaphore path the other platform services use.

## What Changes

- **New platform service `console`** under `platform/services/console/`. The Rust crate in `app/` builds with Dioxus 0.7.10 fullstack, so the WASM client and the console API ship as one binary. The directory `deployment/` holds the Containerfile, the Compose files, `deploy.sh` and the Jinja2 templates. `deploy.sh` builds the image on the host, as the NetBox deployment does.
- **Sign-in through Authentik.** The console signs in through a confidential Authentik OIDC client named `console`, using the authorization code flow with PKCE. The session is an in-memory store, so a restart signs everyone out until a persistent store is chosen.
- **A small HTTP surface.** `GET /healthz` answers without a session. `/auth/login`, `/auth/callback` and `POST /auth/logout` run the sign-in. `GET` and `PUT /v1/me`, `GET /v1/tenants` and `GET /v1/tenants/{t}/tree` are the Phase 1 API routes. Every `/v1/*` route answers 401 without a session.
- **Tenants and scope from the inventory.** The tenant list and the scope tree come from the inventory variable `console_tenants`, which renders to `scope.json`. Example data is labelled as example. Real tenant values stay in site-config. NetBox and Proxmox reads wait for Phase 3.
- **Preferences in SQLite.** The profile shows the signed-in OIDC claims read-only. The user's theme, timezone and default scope are stored in `/data/prefs.db` on the `console-data` named volume. This is the only state the console keeps.
- **Deployment through Semaphore.** A `deploy-console.yml` playbook runs the composable chain: `place-monorepo.yml`, `manage-secrets.yml`, `deploy.sh`, then verify. `manage-approle.yml` provisions the console's own scoped AppRole. The route goes through `Manage Caddy Sites`. The Semaphore templates are `Deploy console` and `Clean Deploy console`, with local variants.
- **Documentation.** Plan 17 and `PRINCIPLES.md` are updated in the Phase 0 commit that adds this change.

## Capabilities

### New Capabilities

- `platform/dispatch-console`: the console as a platform capability. It covers sign-in through Authentik, the API session gate, the tenant and scope tree read from the inventory, the profile and its preferences, and deployment through a Semaphore template with a verify step.

The capability is grouped under the `platform/` domain with a kebab-case leaf name, following the convention the other changes in this store use.

### Modified Capabilities

None. The existing specs in this store are `platform/inference-edge` and `platform/n8n-automation`. Neither one covers the console, so this change alters no existing requirement.

## Impact

- **New files.** The crate `platform/services/console/app/` with its `.gitignore` for `target/`. The directory `platform/services/console/deployment/` with the Containerfile, `compose.yml`, `compose.local.yml`, `deploy.sh`, the templates `env.j2` and `scope.json.j2`, `CLAUDE.md` with an `AGENTS.md` symlink to it, and `README.md`. The playbook `platform/playbooks/deploy-console.yml`, a clean-deploy playbook, and tests for the console.
- **Modified files.** `platform/semaphore/templates.yml`, for the Semaphore templates. An Authentik client blueprint for `console`. The inventory placeholders, which gain a `console_svc` group, and the Caddy route for `console.agent-cloud.test` in local-dev, while production routes stay in site-config. `AGENTS.md`, for the sub-directory documentation line.
- **Secret store.** The console's OIDC client secret is generated under `secret/services/authentik` as `console_oidc_client_secret` and read by the console through the shared-read pattern. The console's AppRole credentials go to `secret/services/approles/console`. No other new path is created.
- **Dependencies, approved by Joseph A. Wisneski IV on 2026-10-25.** `dioxus` 0.7.10 with the `fullstack` and `router` features, `openidconnect` 4.0.1, `tower-sessions` 0.15 with the `memory-store` feature, `rusqlite` 0.41 with the `bundled` feature, `serde`, `serde_json` and `tokio`. `reqwest` 0.12 is needed by the OIDC client's `oauth2` version. It is used with default features off and the `rustls-tls` and `json` features. The build also needs the `wasm32-unknown-unknown` Rust target, and `dioxus-cli` 0.7.10 and `wasm-bindgen-cli` inside the image build only. No other dependency is added without a new approval.
- **Not affected.** The NetBox, Authentik, Caddy and Semaphore services keep their current behaviour. The console adds its own route, client and template, and changes none of theirs.

## Rollback Plan

Phase 0 changes documentation only. Reverting the Phase 0 commit is the whole rollback for this change.

Once the Phase 1 deployment exists, rollback is staged:

1. Retire the console route through `Manage Caddy Sites`, so that the hostname is no longer served.
2. Delete the console client blueprint and re-run `deploy-authentik.yml`. Each client is its own blueprint, so other clients are not affected.
3. Run the console clean-deploy template. The clean-deploy playbook is built on `tasks/clean-service.yml`, which destroys containers and volumes (`AGENTS.md`, composable task library). Running it deletes the saved preferences in `/data/prefs.db`. The console holds no other state, and no authority system is changed by it.
4. Revert the commits that added the console directories and templates once no template or route references them.
