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
- [ ] ~~5.3~~ **Superseded by Shared credentials task 8.0 (D27).** Was: Provision the console's own AppRole with `manage-approle.yml`, and store its credentials under `secret/services/approles/console`.
- [ ] 5.4 Add the `console_svc` group and the `console-local` host to the inventory placeholders, with `service_name` `console`, `container_engine` `podman`, `console_bind` `127.0.0.1` and `console_port` `3410`. Real values stay in site-config.
- [ ] 5.5 Add the route `console.agent-cloud.test` to `console:8080` through `Manage Caddy Sites` in local-dev.
- [ ] 5.6 Add the templates `Deploy console` (with `dev_variant`, and the survey fields `service_branch` and `deploy_force_recreate`), `Clean Deploy console`, `Deploy console (Local)` and `Clean Deploy console (Local)` to `platform/semaphore/templates.yml`. Publish them with `setup-templates.yml`.
- [ ] 5.7 Add the console's sub-directory line to `AGENTS.md`, linking `plan/development/17-dispatch-console.md` and the console `README.md`.
- [ ] 5.8 Validation gate on local-dev through the Semaphore template: scenarios "Deploy builds the image on the host", "Verify checks health and the API gate" and "Deploy converges on re-run" hold.

## Phase 3 checklist: scope and inventory (read-only)

Decisions D22–D25. Every source is all-or-none in the environment. A source that isn't configured answers `"source":"unconfigured"`, the same contract as Phase 2.

- [x] 6.1 Credentials:
  - Add a view-only `console` profile to `_profiles` in `provision-netbox-automation-token.yml` and to the closed survey list in `templates.yml`. It views dcim sites and devices, virtualization clusters and virtual machines, ipam prefixes, and extras tags, with `token_write_enabled: false` (`platform/playbooks/provision-netbox-automation-token.yml:69-76`). It has no IP address or interface access (D24), and it stores `secret/services/netbox:console_api_token`.
  - Write `provision-console-proxmox-token.yml`. With the existing Proxmox admin credential, it grants `PVEAuditor` on `/` to `console@pve` and to its privilege-separated token `console`, and stores `secret/services/proxmox:console_token_id` and `console_token`.
  - That playbook never rotates. It refuses a token that exists in Proxmox but is not stored, unless a replace flag is set. Its template uses `dev_variant`.
- [x] 6.2 Deploy wiring:
  - `deploy-console.yml` shared-reads both tokens and asserts that each `netbox_site` slug is unique across tenants.
  - `env.j2` gains the URL-gated groups `CONSOLE_NETBOX_*` and `CONSOLE_PROXMOX_*`. `CONSOLE_PROXMOX_CA_FILE` comes from `console_proxmox_ca_pem`.
  - `flows.json.j2` renders the D24 flows, names only. `compose.yml` mounts `config/flows.json`, and `deploy.sh` hashes it as an input.
  - Tests pin the shared reads, the slug assertion and the no-address flows rendering.
- [x] 6.3 Reachability: a read-only probe from `console_svc` to the NetBox, Proxmox and OpenBao endpoints. Missing allow rules are added in site-config before the sources are enabled.
- [x] 6.4 Server:
  - Write `netbox.rs` and `proxmox.rs` read clients. Each refuses a cleartext URL unless the host is loopback or RFC1918, and Proxmox TLS verifies against the CA file.
  - Write `inventory.rs`, an aggregator with a 60 s cache.
  - Routes: `GET /v1/tenants/{t}/inventory`, `/services` and `/services/{name}` (tenant-scoped, D25). `GET /v1/flows` and `GET /v1/network` (prefixes) are admin-only.
  - Unit tests against mock authorities, including the unconfigured, unreachable, stale and partial cases.
- [x] 6.5 UI:
  - The Topology page: sites → clusters → nodes → VMs, with click and shift-click selection and red/amber/green status from Proxmox state and the workflow fields.
  - The network view (prefixes) and the admin-only data-flows view.
  - Compute levels, the Services table, service detail, the resource panel and business-domain tiles.
  - Every status shows an icon and a label. Every per-asset view links to Grafana and draws no chart.
- [x] 6.6 Validation gate: on local-dev and in production, every host, VM and service shown exists in NetBox or Proxmox, and computer-use browser tests pass (D30).
  - Local-dev (2026-11-03): Deploy console (Local) through the local Semaphore, task 10657; signed in as the local platform admin, Topology, Compute, Services and Network show NetBox and Proxmox as not configured with 0 nodes, VMs and services, and Data flows render from the inventory declarations.
  - Production (2026-11-04, after the node-list fix #820, deploy 11757 and later redeploys): signed in through Brave, Compute lists the 11 nodes of `uhstray-cluster` with both sources `Read ok`, matching the Proxmox cluster's node count.
  - Production hosts, VMs and services (2026-11-07, after the Phase 5 redeploy 12424): signed in through Authentik in Orca's built-in browser, driven by its CLI (an accepted session under D30 as amended 2026-11-07). The Brave session had lost its accessibility grant. Topology, Compute and Services each show NetBox and Proxmox `Read ok`. The Network and Data flows tabs were opened by clicking them.
    - Hosts: Topology lists 13 NetBox devices for the mapped site, the 11 hypervisors that match the 11 Proxmox nodes plus 2 pfSense devices.
    - VMs: Compute lists 40 VM rows under 33 names. 24 rows carry a Proxmox VM ID and live state (12 running, 12 stopped); the other 16 show no live state.
    - Services: the Services table lists 18 services, each the name of a VM row above.
    - Data flows render from the inventory declarations, and Network lists one NetBox prefix.
  - How existence is established: every VM row is built from a NetBox record (`cluster_view`, `server/inventory.rs:574`), and live state comes only from a unique Proxmox guest-name match (`unique_guest`, `server/inventory.rs:498`). So a row shown is a NetBox record, and a VM ID shown is a Proxmox guest. No independent by-name read was available: NetBox's UI needs a local login, and production Prometheus has no `pve_*` metrics.
  - Findings, left for separate work:
    - NetBox holds two VM records under each of 7 platform names: agentgateway, dns, gh-runner-01, gh-runner-02, o11y, openbao and step-ca. The console withholds live state from a name held twice (`duplicate_vm_names`, `server/netbox.rs:282`), so all 14 rows show none, although these services run. Cause not established. `Reconcile NetBox Duplicate VMs` (`reconcile-netbox-duplicate-vms.yml`) removes one known kind of pair, a bare workflow record beside discovery's record. Whether these 7 pairs are that kind has not been checked, and the template has not been run for them.
    - console, opa and skynet are `planned` in NetBox but `running` in Proxmox, so they show amber.

## Phase 4 checklist: proposals

Detailed 2026-11-08 by D48–D52. OPA and skynet are in production; the launch gate's production rollout is the change `skynet-launch-containment`, section 5. Build and prove on local-dev first.

- [ ] 7.1 Decisions D48–D53 recorded (structured proposals first, console as a gate caller acting as the existing roles, the proposal store and routes, the gate's decision-only preview, approval and self-approval, origin checks, atomic approval and the template variant).
- [ ] 7.2 Launch gate: `POST /v1/decide` (D51) with unit tests; the `console` caller with the five roles in `launch_gate_callers` and `caller_console_token` generated by the gate's deploy; structure tests. Coordinate with the gate's own open pull requests (#922, #924).
- [ ] 7.3 Console deploy: shared-read `caller_console_token`; render `CONSOLE_LAUNCH_GATE_URL`, `CONSOLE_LAUNCH_GATE_TOKEN` and `CONSOLE_LAUNCH_TEMPLATE_SUFFIX`; in production the step-ca trust bundle and the gate's name mapping; input refusals and tests.
- [ ] 7.4 Server: the proposal store and routes (D50), the preview through the gate's `/v1/decide` (D51), atomic approval through `/v1/launch` (D52, D53), the origin check on every state-changing route (D53), and the Work board reading `console <proposal id> <step>`; tests with a fake gate and Semaphore, including a cross-origin POST refused and two concurrent approvals launching once.
- [ ] 7.5 UI: the Dashboard proposal queue and proposal detail (preview, comments, edit values, approve, deny, self-approved marker); Assess resource and Verify health on the service detail and the topology resource panel; keyboard path through the queue (the composer's keyboard path waits for the composer, D48).
- [ ] 7.6 Validation gate on local-dev: propose Verify health → preview allowed → self-approve → the task is tagged `console <id> service-validate` and appears on the Work board as launched by the console; a preview OPA denies shows its reason and cannot be approved; a gate refusal records `launch_failed`.
- [ ] 7.7 Production, after the launch gate's rollout: Deploy launch-gate (Dev) with the console caller, Deploy console (Dev), and the 7.6 scenarios signed in.
- [ ] 7.8 Later, with skynet's plan-only runs, approve route and questions route: the natural-language composer, Ask about current work, Request a service and its keyboard path.

## Shared credentials (before Phase 5)

Decisions D26–D28. This replaces Phase 1 task 5.3, which never ran.

- [x] 8.0 Console AppRole and its D27 policy, provisioned by `provision-console-approle.yml` (template `Provision Console AppRole`), which includes `tasks/manage-approle.yml` with `platform/services/openbao/deployment/config/policies/console.hcl`.
  - Its credentials are stored at `secret/services/approles/console`.
  - `Rotate Console Credentials (Dev)` rotates the secret ID monthly, well inside its 90-day TTL, and redeploys the console.
- [x] 8.0a Grafana Viewer service-account token `secret/services/o11y:console_grafana_token`, minted through the Grafana admin API by `provision-console-grafana-token.yml` (D32: 90-day lifetime, renewed under 30 days). `deploy-console.yml` shared-reads it when `console_grafana_api_enabled` is true.
- [x] 8.0b `platform/lib/github_app_token.py` accepts a downscoping `permissions` body, with tests.
  - `refresh-console-github-token.yml`, scheduled every 45 minutes, writes a read-only installation token to `secret/services/console/github:token`.
  - Done 2026-11-04: GitHub App `uhstray-dispatch-console` created from a manifest (read on contents, checks, actions, pull requests and metadata; no webhook events), installed on `agent-cloud` only. Its client id and private key were seeded to `secret/services/github` through Seed OpenBao Key (Dev), the key as base64 (#844), tasks 12250 and 12251. `Refresh Console GitHub Token (Dev)` ran in check mode (12264) and for real (12266), writing a token whose reported grant is agent-cloud with read permissions only; its 45-minute schedule is active.
- [x] 8.0c The console server logs in to OpenBao with its AppRole at run time and again on demand, reading `CONSOLE_OPENBAO_*`. `platform/services/console/deployment/templates/env.j2:55-63` renders those variables and says the server reads them from Phase 5, so they stay unread until this task lands. Needed by 8.1 and the GitHub token read. Done: `server/openbao.rs`; first consumer is 8.1.

## Phase 5 checklist: governance pages

Decision D28. These are platform-wide views, so they answer `platform-admins` only (D25).

- [x] 8.1 The Secrets page: OpenBao metadata only (path, versions, timestamps). A canary test proves no value ever leaves the API.
- [x] 8.2 The Access page: Authentik users and groups through a view-only token declared by blueprint, and the role map.
  - Token (#839): role `console-directory-reader` (view_user, view_group), account `svc-console-directory`. Deploy Authentik (Dev) task 12019 proved it live: users and groups 200, group create 403.
  - Page (#845 server, #840 UI): read over the LAN (site-config #145 declares the console as an allowed source on the Authentik host); deploy 12036; signed in, the page lists Authentik people, service accounts and groups, and the role map pinned to `plan/architecture/04-credentials-access.md`.
- [x] 8.3 The AI Gateway and LLM access pages: `agw_clients` rendered at deploy into `config/gateway.json`, and usage through Grafana (D26). Role pages list only that role's work.
  - 8.3a done 2026-11-04: `config/gateway.json` (#838) and both pages (#837), deploy 12024; signed in, LLM access lists the gateway's clients with tier, hourly budget and allowed models, and AI Gateway lists the models with the clients allowed on each. Open: usage through Grafana (D26) and the role pages.
  - 8.3b done 2026-11-07, signed in through Authentik in Orca's built-in browser after the Phase 5 redeploy 12424 (usage and role pages #860).
    - LLM access shows each client's last-hour usage, read through Grafana's Prometheus proxy (`server/grafana.rs:10-13`). The figures match the same query run against production Prometheus: `o11y-probe` about 220 to 240 tokens, `opencode` 0, and no series for the clients shown `—`.
    - All five role pages list their steps and gateway client. Each shows no active work, because every production job on the board belongs to the `operator` role. The filter itself is proven by `ui::roles::tests::a_role_page_lists_only_its_own_board_strips`, which passed in CI on the last console change (#863 head, run 41961700631).
- [x] 8.4 The Policy page lists the policy files through the GitHub API (D36). Organization settings reads business-domain tags as NetBox tags, and Tenant settings shows the tenant's structure (D37). Request a service shows the catalog derived from the deploy templates (D38).
  - Done 2026-11-07, signed in, after deploy 12424 (#859, #861, #863):
    - Policy lists the OPA and OpenBao policy files as they are on dev, read from GitHub, each with its last commit.
    - Organization settings lists the organization's sites and their NetBox mapping, and the five business-domain tags from NetBox (Ensure NetBox Business Domains (Dev), task 12362).
    - Tenant settings shows the tenant and its one organization, with three sites of which one is mapped to NetBox.
    - Request a service lists the services from the deploy templates, marking the ones running here.
- [x] 8.5 Change controls are disabled, with the reason shown, until Phase 4. Validation gate: the canary test and computer-use tests pass.
  - Done 2026-11-07, signed in, after deploy 12424. Every change control on Policy, Organization settings, Tenant settings, Request a service, Access, Secrets and LLM access is a disabled button, and each names the reason in its tooltip: "… arrive with proposals (Phase 4)". The Settings pages also say so in the page text. No page has an enabled change control. AI Gateway, Services and the role pages have none.
  - The canary test `server::secrets::tests::the_canary_never_leaves_the_api` passed in CI on the last console change (#863 head, run 41961700631: 509 passed, 0 failed).
  - Method: Orca's built-in browser, driven by its CLI, an accepted session under D30 as amended 2026-11-07.

## Phase 6 checklist: delivery and telemetry

Decision D26. Admin-only (D25).

- [x] 9.1 The CI/CD page (D42): `GET /v1/delivery` and the page.
  - Server: required-check names from `.github/rulesets/protect-main.json` through the GitHub API (D34 token), the promotion path (`compare/main...<ref>` and the open ref → `main` pull request), and open pull requests into the ref and into `main` with check runs and review state. Refreshed every 15 minutes into a cache, as the Policy page.
  - UI: required checks, the promotion path, and the pull-request table, each with a GitHub link. Re-run disabled with its Phase 4 reason.
- [x] 9.2 The Telemetry page (D43): `GET /v1/telemetry/rollups` and the page.
  - Server: `up` through the Grafana Prometheus proxy and Grafana alert instances, grouped by environment, cluster and service. Refreshed every 5 minutes into a cache.
  - UI: the rollup tree with targets up of total and firing and pending alerts, and a `service-overview` Grafana link per service. No chart.
- [x] 9.3 Validation gate:
  - A unit test parses the committed `protect-main.json` and gets exactly its required-check names.
  - Deploy console (Dev) in production, then computer-use tests of both pages signed in.
  - Done 2026-11-08. #917 merged (`f740a8d4`). The ruleset test `required_checks_match_the_committed_ruleset` passes in CI. Deploy console (Dev) ran in check mode (Semaphore task 13879), then for real (13880), placing `dev` and rebuilding the image. Signed in through Authentik in Orca's built-in browser (D30 as amended):
    - CI/CD matches GitHub read directly: `dev` 1275 commits ahead of `main` and 0 behind, no open promotion pull request, 7 open pull requests into `dev` and 6 into `main`. Review states and the required checks of a pull request into `main` agree with GitHub. The four required checks are the ruleset file's. Re-run is disabled with its Phase 4 reason.
    - Telemetry matches Prometheus and Grafana read directly: `prod`, then `agent-cloud-prod` (8 of 8 targets up) and `dgx-spark` (3 of 3). Of 43 Grafana alert instances, 0 are firing or pending, and the inactive ones add no rows. The alerts read succeeds with the console's Viewer token. Each service links to `service-overview`, and the page draws no chart.

## Phase 7 checklist: harden and promote

- [ ] 10.1 Accessibility (D44): a keyboard-only path through Work, its details and stepper, topology selection and the tenant menu, with a skip link, and disabled controls whose reason is visible text. Narrowed 2026-11-08 (operator's call): the composer's keyboard path moves to Phase 4.
- [ ] 10.2 Error, permission-denied and unconfigured states on every page (D45): a 401 signs out everywhere, a failed tenant list says so, unknown states say they are unknown, and retry sentences take their seconds from the poll interval.
- [x] 10.3 A load test of the API (D46): the in-process ignored test, its request count and p50, p95 and p99 recorded here.
  - Done 2026-11-08: `load_cached_reads` (`server/load_test.rs`) run on the integrated Phase 7 branch, one Darwin arm64 workstation, 32 workers, 16 `/v1` read routes, 10 s, platform-admin session, unconfigured caches: 152,337 requests (15,166 req/s), every status 200, no transport error, p50 1.24 ms, p95 3.69 ms, p99 26.24 ms, max 85.33 ms. Tail latency varies between runs (an earlier run had p99 41.40 ms), so these figures are indicative, not a baseline.
- [x] 10.3a Status rendering (D47): each status component rendered for every state shows an icon and a word.
  - Done 2026-11-08: `ui/render_tests.rs` renders `RagBadge`, the source line, the step list, every lane of the minimized rail (icon, accessible name and visible word) and the meter for every state, and checks every icon name a status look returns draws a glyph. Renaming one icon turned five render tests red while every data-level test stayed green. `cicd::status_badge`, `telemetry::HealthBadge`, `work_detail::Stepper` and `roles::LaneCell` are private, so their icons are covered by the icon-name test only.
- [ ] 10.4 The plan 17 validation table passes on `dev` **and in production** (every phase gate 6.6, 8.5 and 9.3, run through the (Dev) templates and the computer-use tests). Write the operator test plan `plan/product/dispatch-console/TEST-PLAN.md`.
- [ ] 10.5 Only after 10.4 passes: open the dev → main promotion PR. The merge is the operator's call.
