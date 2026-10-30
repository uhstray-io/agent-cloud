# CLAUDE.md — platform/services/console/deployment

The Dispatch Console: a Rust/Dioxus app (source in `../app`, image built from `Containerfile` in this directory) served behind central Caddy. It signs users in through Authentik (native OIDC, client `console`), keeps non-secret preferences in SQLite, shows the tenant, org and site tree declared in the inventory, and (Phase 3) reads NetBox and Proxmox read-only for the tenant inventory. Plan: `plan/development/17-dispatch-console.md`.

## What this deploy does

Composable pattern, copied from `deploy-tududi.yml`:

```text
Semaphore "Deploy console" (or "Deploy console (Local)")
  └─ platform/playbooks/deploy-console.yml
     ├─ Phase 1  transport guard → place-monorepo → mkdir config/ → manage-secrets
     │           (env.j2 → .env, scope.json.j2 → config/scope.json,
     │            workflow.json.j2 → config/workflow.json from the service-onboarding registry)
     │           → distribute-ca-root (local only) → ~/console symlink
     ├─ Phase 2  deploy.sh: build the image on this host (skipped when it matches its inputs)
     │           → compose_up_if_changed → wait for the exec probe (step 4)
     └─ Phase 3  exec probe /healthz 200 → exec probe /v1/me 401 → emit-service-deploy-result
```

`deploy.sh` is the container lifecycle plus the host image build. It generates no secret and reads no OpenBao value (Critical Deployment Rule 2). It reads one key from `.env` (`CONSOLE_IMAGE`) and never sources the file, so the OIDC client secret stays out of its environment.

## Host image build

There is no registry. `deploy.sh` runs `build_console_image`:

- **Build context:** the console service directory (the parent of this directory). The Containerfile is `-f Containerfile` here.
- **Inputs digest:** a sha256 over the per-file sha256 of every file under `../app` (except `../app/target`) plus `Containerfile`, sorted by path. It is stored as the image label `io.agent-cloud.console.build-inputs-sha256`.
- **Skip:** the build is skipped when the image on the host carries a label equal to the current digest. `CONSOLE_FORCE_REBUILD=true` (Semaphore survey `console_force_rebuild`) rebuilds anyway.
- **Cache:** the build keeps the layer cache. Every input is in the digest, so a cached layer is reused only for identical inputs.
- **Local seccomp:** `CONSOLE_BUILD_SECCOMP` (local inventory `console_build_seccomp`) passes `--security-opt seccomp=<path>` to the build. The local podman client sends its own default profile path to the podman-machine engine. Without this, RUN steps fail with "opening seccomp profile failed". NetBox hit the same failure on 2026-09-22 (`platform/services/netbox/deployment/lib/common.sh`). Production leaves it unset.
- **Recreate:** a rebuilt image has a new ID. `compose_up_if_changed` sees that and recreates the container.

## Configuration

`.env` (rendered from `templates/env.j2`, mode 0600):

| Key | Source |
|-----|--------|
| `CONSOLE_IMAGE`, `CONSOLE_BIND`, `CONSOLE_PORT` | compose substitution; `console_image`, `console_bind`, `console_port` |
| `IP=0.0.0.0`, `PORT=8080` | the server's listen address inside the container |
| `CONSOLE_BASE_URL` | `console_base_url`. The redirect URI is this plus `/auth/callback` |
| `CONSOLE_OIDC_ISSUER`, `CONSOLE_OIDC_CLIENT_ID=console` | `console_oidc_issuer`; the client id is fixed |
| `CONSOLE_OIDC_CLIENT_SECRET` | `secrets.console_oidc_client_secret`, a shared read from `secret/services/authentik` |
| `CONSOLE_DATA_DIR=/data`, `CONSOLE_SCOPE_FILE=/etc/console/scope.json` | fixed paths in the container |
| `CONSOLE_SEMAPHORE_API_URL`, `CONSOLE_SEMAPHORE_UI_URL`, `CONSOLE_SEMAPHORE_PROJECT` | `console_semaphore_api_url` (default `https://semaphore.agent-cloud.test`), `console_semaphore_ui_url` (default `https://semaphore.agent-cloud.test:8443`), `console_semaphore_project` (default `1`) |
| `CONSOLE_SEMAPHORE_TOKEN` | `secrets.semaphore_api_token`, read from `secret/services/console` (see "Semaphore access") |
| `CONSOLE_WORKFLOW_FILE=/etc/console/workflow.json` | fixed path; the file is `config/workflow.json` |
| `CONSOLE_FLOWS_FILE=/etc/console/flows.json` | fixed path; the file is `config/flows.json` (always rendered) |
| `CONSOLE_NETBOX_URL`, `CONSOLE_NETBOX_TOKEN` | `console_netbox_url`; the token is `secrets.console_api_token`, a shared read of `secret/services/netbox`. The group renders only when the URL is set (Phase 3, see below) |
| `CONSOLE_PROXMOX_URL`, `CONSOLE_PROXMOX_TOKEN_ID`, `CONSOLE_PROXMOX_TOKEN` | `console_proxmox_url`; id and secret are shared reads of `secret/services/proxmox` (`console_token_id`, `console_token`). All-or-none, like the NetBox group |
| `CONSOLE_PROXMOX_CA_FILE=/etc/console/proxmox-ca.pem` | set only when `console_proxmox_ca_pem` is set; the file is always mounted (empty when unset) |
| `CONSOLE_GRAFANA_URL` | `console_grafana_url`, the base for Grafana deep links. Not a credential; rendered only when set. The URL alone reads no token |
| `CONSOLE_GRAFANA_TOKEN` | `secrets.console_grafana_token`, a shared read of `secret/services/o11y`. Rendered only when `console_grafana_api_enabled` is true AND `console_grafana_url` is set |

`config/scope.json` (rendered from `templates/scope.json.j2`, mode 0644) is the tenant tree: `console_tenants` (a list of tenants, each with the Authentik `groups` allowed to see it, and orgs, each org with sites and their `envs`; a tenant with no `groups` is visible to `platform-admins` only) and `console_scope_is_example`. It is not a secret.

`config/workflow.json` (rendered from `templates/workflow.json.j2`, mode 0644) is the service-onboarding workflow's steps, `[{id, order, owner, label}]` sorted by `order`, read from `platform/workflows/service-onboarding/registry.yml` on the controller (`label` is the registry's `diagram_label`). It is rendered, never hand-written, so the console has no second copy of the step list. It is not a secret.

**OpenBao.** The console's only secret is its Semaphore API token, at `secret/services/console:semaphore_api_token`. Deploy declares it `type: existing` (manage-secrets never generates it) and lists it in `_required_existing_secrets`, so a missing token stops the play before any write. manage-secrets words that failure generically (`Required OpenBao secret console:semaphore_api_token is missing.`); the play that creates it is **Provision Console Semaphore Access**. The store step merges the token back unchanged; the merge keeps its siblings (`url`, from an earlier revision, and any future key).

**Order.** Deploy Authentik before Deploy console. Authentik must hold `console_oidc_client_secret` in `secret/services/authentik`. Without it, the shared read stops Phase 1 and names the missing key. Run **Provision Console Semaphore Access** before Deploy console, for the token. (Declaring `svc-console` in `semaphore_project_members` is a later step, after the account exists; see below.)

## Semaphore access (Phase 2)

The console's `/v1/work` lists Semaphore tasks, joined to the workflow registry (plan 17, D17-D20). It needs a credential, and that credential must not be able to launch a task: launch rights are runner rights (`manage-semaphore-access.yml`). So it is a dedicated Semaphore identity, never the controller's token (`secret/services/semaphore`).

`platform/playbooks/provision-console-semaphore-access.yml` (Semaphore **Provision Console Semaphore Access**; **(Dev)** and **(Local)** twins) creates it, idempotently:

| Piece | What | Why |
|-------|------|-----|
| User `svc-console` | non-admin, not external, password `secret/services/console:semaphore_password` (generated once, stored before the account is created) | Non-admin: `semaphore_admin_roster` never derives it. Not external: it logs in with that password to mint its own token. |
| Project role | `guest` in project `console_semaphore_project` | Upstream gives guest no run permission (`db/ProjectUser.go`, `ProjectGuest: 0`), and task creation needs `CanRunProjectTasks`. |
| Token | `secret/services/console:semaphore_api_token` | Reused only when `GET /api/user` with it answers `svc-console`, non-admin. Otherwise svc-console logs in and mints one. |

The play proves the token with itself, at the end: `GET /api/project/{p}/tasks/last` answers 200, and `POST /api/project/{p}/tasks` with template `-1` answers 403. The POST cannot launch anything whatever the gate does: `-1` names no template, and a guest is refused before any template is read. Under `--check`, reads run and every write is skipped, and the POST is never sent.

Two facts the play depends on, both at upstream v2.19.11:

- **Listing tokens cannot reuse one.** `GET /api/user/tokens` cuts every id to 8 characters (`api/user.go:51-64`), so a listed id does not authenticate. The stored token is the only reuse path.
- **An admin cannot mint for svc-console.** The mint uses the calling user (`api/user.go:70-104`). The play logs in as svc-console itself.

**Ordering with Manage Semaphore Access.** `filter_plugins/semaphore_access.py` refuses the whole run when a declared team member has no Semaphore account. So: provision the account first, then declare `svc-console: guest` in `semaphore_project_members`. Production declares that in site-config, which this repo does not hold. Until it does, the next real **Manage Semaphore Access** run removes `svc-console` from the team, and the console's read fails.

**Unverified.** Whether production Semaphore accepts password login for `svc-console` is not yet proven by a live run. The code path is read (`api/login.go:286-382`).

## Read-only inventory (Phase 3, plan 17 D22-D24)

Each authority is read with the smallest credential, and only when its URL var is set. A deploy without the URL stays "unconfigured" and reads no token.

- **NetBox.** Token profile `console` in `provision-netbox-automation-token.yml`: view only, on `dcim.site`, `dcim.device`, `virtualization.cluster`, `virtualization.virtualmachine`, `ipam.prefix`, `extras.tag`. No add, change or delete. The token is created with `write_enabled=False` (NetBox's Token model has that field). D24 names prefixes, addresses and interfaces; v1 reads prefixes only, so the profile has no address or interface access. The console user must hold no superuser or group rights, checked before any write. Stored at `secret/services/netbox:console_api_token`.
- **Proxmox.** `provision-console-proxmox-token.yml` mints `console@pve!console`, a privilege-separated token, with PVEAuditor on `/` for both the user and the token. It stores `console_token_id` and `console_token` at `secret/services/proxmox`. It never rotates a live token. A stored token that Proxmox no longer has needs `console_proxmox_token_recreate=true`. A live token Proxmox has but the store lacks is refused by name unless `console_proxmox_token_replace=true`, which revokes exactly that one token and mints a replacement, because the secret is returned only once and cannot be recovered otherwise. The ACLs are granted before the store, so a stored token always has its grants.
- **Scope to NetBox (D23).** A site may declare `netbox_site`, a NetBox site slug. `deploy-console.yml` refuses a slug declared on two sites, naming it. A site without `netbox_site` is "not mapped".
- **Flows (D24).** `templates/flows.json.j2` renders `config/flows.json` from the declarations, names only, never an address or a port. Edges: `authentik` apps typed by their catalog `type` (`oidc`, `forward_auth`), from the effective `authentik_apps` (undefined means the whole catalog, as in `deploy-authentik.yml`); `caddy` to each managed site's hostname (never the upstream); `service` (or `job`) to `o11y` from `o11y_expected_metrics_targets`. A missing source gives no edges of that kind.
- **Proxmox CA.** `console_proxmox_ca_pem` is a public certificate only; a private key is refused by name. The CA file is always mounted read-only and set through `CONSOLE_PROXMOX_CA_FILE` only when the PEM is set.

Inputs are bind-mounted read-only like `scope.json` and `workflow.json`, and `deploy.sh` recreates the container when any of them changes.

Not yet done here: `config.rs` does not read the `CONSOLE_NETBOX_*`, `CONSOLE_PROXMOX_*`, `CONSOLE_FLOWS_FILE` or `CONSOLE_GRAFANA_URL` variables. The server work reads them, so this wiring is inert until that lands.

## Active Work (Semaphore, read-only)

The server reads Semaphore's task list and serves it at `/v1/work` (plan 17 D17-D20). Read it with the code, not this section: `app/src/server/work.rs` (joins, lanes) and `app/src/server/semaphore.rs` (the one read).

Environment, read by the server (`app/src/server/config.rs`):

| Variable | Meaning |
|----------|---------|
| `CONSOLE_SEMAPHORE_API_URL` | Server-side Semaphore origin. Only the poller calls it |
| `CONSOLE_SEMAPHORE_UI_URL` | Browser-facing origin. Receipt links are built from it |
| `CONSOLE_SEMAPHORE_PROJECT` | Project id, a positive integer |
| `CONSOLE_SEMAPHORE_TOKEN` | API token of the Semaphore user `svc-console` (plan 17 D20). Never logged, never printed by `Debug` |
| `CONSOLE_WORKFLOW_FILE` | Registry steps JSON (`[{id, order, owner, label}]`), rendered from `registry.yml`. Optional: without it, roles and run steps are empty |

The four Semaphore variables are all-or-none. A partial set fails startup and names the first missing one. None set means `source: "unconfigured"`.

The console answers `unconfigured` only when none of the four Semaphore variables is set.

Routes (platform admins only, plan 17 D19):

- `GET /v1/work` returns `{source, error, updated_at, items}`, newest task first. Every task in the window is an item, each with `stream`, `latest_in_stream` and `stream_runs` (see Streams below). `source` is `ok`, `unconfigured` or `error`. Under `error`, `items` are the last good poll and `error` gives the reason. Before the first poll, `source` is `ok` with no items and `updated_at: null`.
- `GET /v1/work/{key}` returns `{item, run, stream_history}`. `run` is `null` unless the task was launched by skynet, in which case it lists the registry steps in order with each step's latest lane or `not_started`. `stream_history` is up to 10 runs of the item's stream, newest first, the item's own run included, each with `key, lane, status, launched_by, created, started, ended, receipt_url`. An unknown key is `404 {"error":"unknown_work"}`.
- No session is `401`. A session without `platform-admins` is `403 {"error":"forbidden"}`, the same for an existing key and a missing one.

The poller reads every 10 s. It sends the token only in the `Authorization` header. Its errors name a status code or a failure class, never a URL, body or header. Tasks are ordered by Semaphore id, not by `created`, because Semaphore's timestamp strings are not safely comparable as text. `launched_by` is `skynet` when the task message is exactly `skynet <run_id> <step_id>`, else the Semaphore user name, else `schedule` when the task carries a `schedule_id`, else `integration` when it carries an `integration_id`, else `unknown`. The only field read from a task's environment is `target_service`.

**Streams (one strip per job).** A stream is one job: `tpl-<template_id>`, plus `:<service>` when the task names a `target_service`. A task with no `template_id` is a stream of its own (`task-<id>`), so unrelated tasks never merge. Within the fetched window, `latest_in_stream` is true on the highest-id task of each stream, and `stream_runs` counts the stream's tasks (the same value on each of them). The Active Work board draws only the latest run of each job, the lane shown is that run's lane, and the sidebar badge, the running count and the rail counts count only those. A badge therefore counts a job once, and only while its newest run is blocked or failed. A strip shows `×N runs` when `N > 1`. The details panel lists the job's recent runs from `stream_history`.

The window is Semaphore's newest 200 tasks of the project (`tasks/last`). A run older than the window is not counted, and a job whose runs are all older than it is not shown. `/v1/work` still returns every task in the window, so the API stays complete for other readers.

## Inventory (NetBox and Proxmox, read-only, Phase 3)

The server reads NetBox (sites, clusters, VMs, devices, prefixes) and Proxmox (nodes and guests) and joins them for the tenant views (plan 17 D22-D25). The response contract is `app/docs/phase3-api.md`. The code is `app/src/server/inventory.rs` (join, RAG, routes), `netbox.rs` and `proxmox.rs` (the two reads), and `grafana.rs` (deep links).

| Source | Credential | Read |
|--------|------------|------|
| NetBox | `CONSOLE_NETBOX_TOKEN`, view-only. An `nbt_` token is sent as `Bearer`, a legacy token as `Token` | sites by declared slug, their clusters and VMs, devices; every prefix, with the site it is scoped to |
| Proxmox | `CONSOLE_PROXMOX_TOKEN_ID` and `CONSOLE_PROXMOX_TOKEN`, a PVEAuditor token | `cluster/resources`: nodes and guests, joined to a VM by name |

Environment, read by `app/src/server/config.rs`. The deploy does not set these yet (see Open items):

| Variable | Meaning |
|----------|---------|
| `CONSOLE_NETBOX_URL`, `CONSOLE_NETBOX_TOKEN` | all-or-none. Unset: `source.netbox` is `unconfigured` |
| `CONSOLE_PROXMOX_URL`, `CONSOLE_PROXMOX_TOKEN_ID`, `CONSOLE_PROXMOX_TOKEN` | all-or-none. `CONSOLE_PROXMOX_CA_FILE` (optional PEM roots for a self-signed API) counts as a member, so alone it names the missing URL |
| `CONSOLE_FLOWS_FILE` | optional JSON array of `{kind, from, to, label}`, rendered from declarations. Names only: an extra key fails startup |
| `CONSOLE_GRAFANA_URL` | optional Grafana origin for deep links. Unset: every link is `null` |

Infrastructure URLs take `https://` always, and `http://` only to loopback or RFC1918. Anything else fails startup and names the variable. Certificate verification is never turned off. Without a CA file, Proxmox is verified against reqwest's bundled roots, so a self-signed API shows as `source.proxmox: "error"`.

A site joins NetBox only through its declared `netbox_site` slug in `console_tenants`. `scope.json.j2` renders that list unchanged. A site with no slug is unmapped. The console never guesses a site from a name. A slug is declared by one site only, across every tenant: the scope file fails at load if two sites name the same slug, or a slug has whitespace.

Routes and scoping:

| Route | Who | Carries |
|-------|-----|---------|
| `GET /v1/tenants/{t}/inventory` | members of `t` | sites, with clusters, nodes, VMs (with RAG) and devices. Node CPU, memory and uptime only for platform admins |
| `GET /v1/tenants/{t}/services`, `/services/{name}` | members of `t` | VMs that carry workflow fields; one VM |
| `GET /v1/network` | `platform-admins` only; 403 otherwise | every prefix, global ones included |
| `GET /v1/flows` | `platform-admins` only; 403 otherwise | the flow catalogue |

A tenant the caller may not see answers `404 unknown_tenant`, byte-identical to an unknown one. Prefixes never appear in a tenant answer: a prefix with no site names no tenant (D16). Devices are site-scoped, so they do.

Unconfigured and stale:

- An unconfigured authority reports `unconfigured`, and its sites are `mapped: false`. A configured authority before its first read reports `ok` with `updated_at: null`.
- One background task reads each configured authority every 60 s into memory. A failed read keeps the last good data, sets `stale: true`, and fills `error.<authority>`. A list that reports a next page fails rather than truncating. The NetBox read is all-or-nothing per refresh.

A VM takes live state only when its name is unique across every declared NetBox site and unique among the Proxmox guests. Otherwise its live status is `null`.

`diagnostics.dropped_mismatched` counts NetBox rows that did not name the site or cluster the console asked for. They are dropped, not shown. The count is for platform admins only: a tenant member's envelope omits it. A cluster scoped to a location or region is dropped and counted the same way, even when its legacy site field names the site, until the console resolves those scopes to a site.

RAG (`inventory::rag`), first match wins: `red` when a workflow step failed, or Proxmox says `stopped` while NetBox says `active`; `amber` when the data is stale, NetBox says `planned` or `staged`, a step is pending or unknown, or the live state is neither running nor stopped-while-active; `grey` when there is no live state; `green` when the VM is running and nothing above applies.

Deep links: each VM and service row carries `grafana_url`, the Service Overview dashboard with `var-service` and `var-log_service` set to the VM name. `links.grafana_conformance` is the conformance dashboard. Both use the dashboard uids in `platform/services/o11y/deployment/config/grafana/dashboards/`.

## Shared credentials (plan 17, D26-D27)

Three credentials serve the console. Each has its own provisioning playbook, and the deploy reads the first two into `.env`. The GitHub token is read by the console at run time (Phase 5), not by the deploy. (1) The console AppRole: `secret/services/approles/console` holds `role_id` and `secret_id`. `provision-console-approle.yml` writes it, and `rotate-console-credentials.yml` rotates `secret_id` monthly. (2) The Grafana Viewer token: `secret/services/o11y:console_grafana_token`, from `provision-console-grafana-token.yml`, renewed by the rotation. (3) The read-only GitHub token: `secret/services/console/github`, refreshed every 45 minutes by `refresh-console-github-token.yml` on a Dev schedule. Setting `console_openbao_url` in the inventory reads (1) into `CONSOLE_OPENBAO_*`. Setting `console_grafana_api_enabled: true` together with `console_grafana_url` reads (2) into `CONSOLE_GRAFANA_TOKEN`; `console_grafana_url` alone is only the deep-link base. Each read fails by name when its source is missing. Production order: Provision Console AppRole, Provision Console Grafana Token, set the URLs and `console_grafana_api_enabled` in site-config, then Deploy console. The rotation renders `.env` from the controller checkout (`manage-secrets.yml:205`) while the image stays at the commit it pins; its drift check refuses a rotation whose controller inputs differ from that commit.

## Runtime notes

- **No compose healthcheck; readiness is an exec probe.** The compose file declares no `healthcheck:`. On prod, podman-compose 1.0.6 (tag `v1.0.6`, `podman_compose.py`, the healthcheck translation) wraps every `healthcheck.test` in `/bin/sh -c`, and the runtime image is distroless (`cc-debian12`, no `/bin/sh`). Prod Semaphore task 9892 built and started the container, which logged `OIDC discovery ready`, yet podman reported it `unhealthy`, and `wait_for_healthy` failed the deploy. So `deploy.sh` step 4 runs `podman exec console /usr/local/app/server probe /healthz 200` every 5 s until a 300 s wall-clock deadline, then dumps the container's diagnostics and fails. Each exec is bounded by `timeout 15` where coreutils `timeout` exists, because a stalled `podman exec` would otherwise hold the loop past its deadline. Phase 3 of `deploy-console.yml` repeats the same probe. The probe is the server binary's own subcommand, so no curl or wget is assumed. The shell-less gateway in `agentgateway` is probed the same way.
- **Preferences volume.** `console-data` mounts at `/data` (SQLite `prefs.db`: preferences and login sessions). The image's nonroot uid 65532 must own `/data` (the Containerfile's job; unverified until the image is built).
- **Sessions** are rows in the same `prefs.db` (table `sessions`, one connection shared with preferences). A restart keeps every unexpired session. Expired rows are swept at startup and at most hourly. The cookie `console_session` expires after 8h of inactivity.
- **Local-dev overlay** (`compose.local.yml`, `LOCAL_MODE=true`): joins the `local-dev` network so central Caddy reaches `console:8080` by name, mounts `certs/step-ca-bundle.crt`, and sets `CONSOLE_EXTRA_CA_FILE` to it for OIDC calls to the internal IdP. Prod's IdP uses a public chain, so the overlay is not applied there.
- **Probes** run inside the container with `podman exec`, so they do not depend on the published port or on Caddy.
- **Read-only upstream access.** The console calls NetBox and Proxmox with a view-only token and an auditor token. It never calls a write path on either.

## Commands

| What | How |
|------|-----|
| Deploy (prod) | Semaphore **Deploy console**. Until promotion, use **Deploy console (Dev)**: `dev_variant` binds it to the `agent-cloud dev` repository record (`setup-templates.yml`) |
| Deploy (local) | `make local-deploy-console` (Semaphore **Deploy console (Local)**) |
| Force a container recreate | survey `deploy_force_recreate=true` |
| Force an image rebuild | survey `console_force_rebuild=true` |
| Destroy data and redeploy | **Clean Deploy console** (or **(Dev)**; `confirm_reset` = host name); local: `make local-clean-deploy-console CONFIRM_RESET=console-local` |
| Health | `validate-all.yml` ("Validate console"), or `podman exec console /usr/local/app/server probe /healthz 200` |

The clean deploy removes the containers and the `console-data` volume (the preferences). It does not remove the image, so the fresh deploy rebuilds only when the inputs changed.

## File structure

```text
deployment/
├── deploy.sh               container lifecycle + host image build (lib/common.sh)
├── compose.yml             single container, env-parameterized image/bind/port, console-data volume
├── compose.local.yml       local overlay: local-dev network, step-ca bundle, CONSOLE_EXTRA_CA_FILE
├── templates/
│   ├── env.j2              .env (non-secret config + shared OIDC secret + Semaphore token)
│   ├── scope.json.j2       config/scope.json from console_tenants
│   ├── workflow.json.j2    config/workflow.json from the service-onboarding registry
│   ├── flows.json.j2       config/flows.json from the authentik, caddy and o11y declarations (names only)
│   └── proxmox-ca.pem.j2   config/proxmox-ca.pem from console_proxmox_ca_pem (empty when unset)
├── .gitignore              .env, config/, certs/ (rendered per deploy)
├── AGENTS.md -> CLAUDE.md
└── README.md
```

The `Containerfile` is part of this change. It sits next to this file and builds the crate in `../app`.

## What not to do

- Don't generate or read secrets in `deploy.sh`. Ansible and OpenBao own credentials.
- Don't add a `healthcheck:` to `compose.yml`. podman-compose 1.0.6 runs it through `/bin/sh`, and the image has no shell. Readiness belongs to the exec probe in `deploy.sh` step 4.
- Don't add `--force-recreate` to `deploy.sh`. Recreate only on a change, through `compose_up_if_changed`.
- Don't put a real tenant, site or address in `console_tenants` in this repo. Production's scope lives in site-config. The local example is fictional.
- Don't commit `.env` or `config/`. Both are gitignored because they are rendered per deploy.
- Don't give the console a write-capable NetBox or Proxmox token. The inventory is read-only: a view-only NetBox token and a PVEAuditor token.
- Don't generate the Semaphore token in `deploy-console.yml`, and don't give the console the controller's token. The token belongs to the svc-console account and is provisioned by `provision-console-semaphore-access.yml`.

## Open items

- **Phase 3 wiring (not in this change).** The server reads the Phase 3 variables above, but nothing sets them. `templates/env.j2` and `deploy-console.yml` do not render them. No playbook provisions the NetBox token: `provision-netbox-automation-token.yml` has no `console` entry yet. `provision-console-proxmox-token.yml` does not exist. Until these land, a deploy answers `unconfigured` for both sources.

- **Authentik side** (in this change): the `console` OAuth2 provider and application are declared in `platform/services/authentik/deployment/blueprints/console-oidc.yaml`, and `console_oidc_client_secret` is a generated secret in `platform/playbooks/vars/secret-declarations/authentik.yml`. Deploy Authentik before the console, otherwise the Phase 1 shared read fails.
- **Containerfile and `../app`** (in this change): the Containerfile must copy only the paths it needs. `deployment/Containerfile.dockerignore` keeps `.env`, `config/`, `certs/` and `app/target` out of the build context; podman 5.8.2 honors it (verified with a scratch build). A Containerfile with `COPY . .` would still bake in whatever remains.
- **AppRole.** `plan/development/17-dispatch-console.md` names `manage-approle.yml` for a scoped AppRole. This deploy does not provision one.
- **Step-result registration.** `platform/tests/test_service_deploy_step_result.py` (`WAVE`, `RECREATE_AWARE`) does not list the console. Its tests pass without it.

## Related

- Model: [`../../../playbooks/deploy-tududi.yml`](../../../playbooks/deploy-tududi.yml). Playbook: [`../../../playbooks/deploy-console.yml`](../../../playbooks/deploy-console.yml), [`../../../playbooks/clean-deploy-console.yml`](../../../playbooks/clean-deploy-console.yml)
- Plan: [`../../../../plan/development/17-dispatch-console.md`](../../../../plan/development/17-dispatch-console.md)
- Onboarding checklist: [`../../../../plan/architecture/02-service-onboarding.md`](../../../../plan/architecture/02-service-onboarding.md)
- Root conventions: [`../../../../AGENTS.md`](../../../../AGENTS.md)
