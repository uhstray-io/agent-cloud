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

## Phase 3 checklist: scope and inventory (read-only)

Decisions D22–D25. Every source is all-or-none in the environment. A source that isn't configured answers `"source":"unconfigured"`, the same contract as Phase 2.

- [ ] 6.1 Credentials:
  - Add a view-only `console` profile to `_profiles` in `provision-netbox-automation-token.yml` and to the closed survey list in `templates.yml`. It views dcim sites and devices, virtualization clusters and virtual machines, ipam prefixes and IP addresses, and extras tags, and stores `secret/services/netbox:console_api_token`.
  - Write `provision-console-proxmox-token.yml`. With the existing Proxmox admin credential, it grants `PVEAuditor` on `/` to `console@pve` and to its privilege-separated token `console`, and stores `secret/services/proxmox:console_token_id` and `console_token`.
  - That playbook never rotates. It refuses a token that exists in Proxmox but is not stored, unless a replace flag is set. Its template uses `dev_variant`.
- [ ] 6.2 Deploy wiring:
  - `deploy-console.yml` shared-reads both tokens and asserts that each `netbox_site` slug is unique across tenants.
  - `env.j2` gains the URL-gated groups `CONSOLE_NETBOX_*` and `CONSOLE_PROXMOX_*`. `CONSOLE_PROXMOX_CA_FILE` comes from `console_proxmox_ca_pem`.
  - `flows.json.j2` renders the D24 flows, names only. `compose.yml` mounts `config/flows.json`, and `deploy.sh` hashes it as an input.
  - Tests pin the shared reads, the slug assertion and the no-address flows rendering.
- [ ] 6.3 Reachability: a read-only probe from `console_svc` to the NetBox, Proxmox and OpenBao endpoints. Missing allow rules are added in site-config before the sources are enabled.
- [ ] 6.4 Server:
  - Write `netbox.rs` and `proxmox.rs` read clients. Each refuses a cleartext URL unless the host is loopback or RFC1918, and Proxmox TLS verifies against the CA file.
  - Write `inventory.rs`, an aggregator with a 60 s cache.
  - Routes: `GET /v1/tenants/{t}/inventory`, `/services` and `/services/{name}` (tenant-scoped, D25), and `GET /v1/flows` (admin-only).
  - Unit tests against mock authorities, including the unconfigured, unreachable, stale and partial cases.
- [ ] 6.5 UI:
  - The Topology page: sites → clusters → nodes → VMs, with click and shift-click selection and red/amber/green status from Proxmox state and the workflow fields.
  - The network view (prefixes and addresses) and the admin-only data-flows view.
  - Compute levels, the Services table, service detail, the resource panel and business-domain tiles.
  - Every status shows an icon and a label. Every per-asset view links to Grafana and draws no chart.
- [ ] 6.6 Validation gate: on local-dev and in production, every host, VM and service shown exists in NetBox or Proxmox, and computer-use browser tests pass (D30).

## Phase 4 checklist: proposals

Waits for OPA and skynet in production (D29).

- [ ] 7.1 To be detailed when OPA and skynet are deployed in production. See the operator handoffs `opa-production.md` and `skynet-production.md` (2026-10-29).

## Shared credentials (before Phase 5)

Decisions D26–D28. This replaces Phase 1 task 5.3, which never ran.

- [ ] 8.0 Console AppRole via `manage-approle.yml`, with the D27 policy file under `platform/services/openbao/deployment/config/policies/`.
  - The console logs in at run time and again on demand. Its credentials go in `secret/services/approles/console`.
  - A scheduled playbook rotates the secret ID before its 90-day expiry and redeploys the console.
- [ ] 8.0a Grafana Viewer service-account token `secret/services/o11y:console_grafana_token`, provisioned like `provision-o11y-mcp-grafana-token.yml`. `deploy-console.yml` shared-reads it.
- [ ] 8.0b `platform/lib/github_app_token.py` accepts a downscoping `permissions` body, with tests.
  - `refresh-console-github-token.yml`, scheduled every 45 minutes, writes a read-only installation token to `secret/services/console/github:token`.

## Phase 5 checklist: governance pages

Decision D28. These are platform-wide views, so they answer `platform-admins` only (D25).

- [ ] 8.1 The Secrets page: OpenBao metadata only (path, versions, timestamps). A canary test proves no value ever leaves the API.
- [ ] 8.2 The Access page: Authentik users and groups through a view-only token declared by blueprint, and the role map.
- [ ] 8.3 The AI Gateway and LLM access pages: `agw_clients` rendered at deploy into `config/gateway.json`, and usage through Grafana (D26). Role pages list only that role's work.
- [ ] 8.4 The Policy page lists the policy files through the GitHub API. The Tenant and Organization settings pages read business-domain tags as NetBox tags. Request a service shows the catalog.
- [ ] 8.5 Change controls are disabled, with the reason shown, until Phase 4. Validation gate: the canary test and computer-use tests pass.

## Phase 6 checklist: delivery and telemetry

Decision D26. Admin-only (D25).

- [ ] 9.1 The CI/CD page: reviews, required checks and the promotion path. The required-check names come from `.github/rulesets/protect-main.json` through the GitHub API (D27 token).
- [ ] 9.2 The Telemetry page: rollups through the Grafana data-source proxy, with Grafana deep links. No per-asset chart.
- [ ] 9.3 Validation gate: the check names match the ruleset (test), and computer-use tests pass.

## Phase 7 checklist: harden and promote

- [ ] 10.1 Accessibility: a keyboard-only path through Work, the composer, the stepper and topology selection.
- [ ] 10.2 Error, permission-denied and unconfigured states on every page.
- [ ] 10.3 A load test of the API.
- [ ] 10.4 The plan 17 validation table passes on `dev`. Write the operator test plan `plan/product/dispatch-console/TEST-PLAN.md`.
- [ ] 10.5 Open the dev → main promotion PR. The merge is the operator's call.
