# CLAUDE.md — platform/services/console/deployment

The Dispatch Console: a Rust/Dioxus app (source in `../app`, image built from `Containerfile` in this directory) served behind central Caddy. It signs users in through Authentik (native OIDC, client `console`), keeps non-secret preferences in SQLite, and shows the tenant, org and site tree declared in the inventory. Plan: `plan/development/17-dispatch-console.md`.

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
     │           → compose_up_if_changed → wait healthy
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

## Runtime notes

- **Healthcheck is exec form, not CMD-SHELL.** The runtime image is distroless (`cc-debian12`, no `/bin/sh`), so `CMD-SHELL` cannot run. The probe is the server binary's own subcommand: `/usr/local/app/server probe /healthz 200`. Other services' comments say podman-compose mis-quotes `["CMD", ...]` lists. podman-compose 1.6.0 turns the list into a JSON `--health-cmd` (`podman_compose.py`, the `CMD` branch). Whether the engine then runs it as exec is **unverified** on this machine. The live deploy is the proof: `wait_for_healthy` fails if the container never reports healthy.
- **Preferences volume.** `console-data` mounts at `/data` (SQLite `prefs.db`: preferences and login sessions). The image's nonroot uid 65532 must own `/data` (the Containerfile's job; unverified until the image is built).
- **Sessions** are rows in the same `prefs.db` (table `sessions`, one connection shared with preferences). A restart keeps every unexpired session. Expired rows are swept at startup and at most hourly. The cookie `console_session` expires after 8h of inactivity.
- **Local-dev overlay** (`compose.local.yml`, `LOCAL_MODE=true`): joins the `local-dev` network so central Caddy reaches `console:8080` by name, mounts `certs/step-ca-bundle.crt`, and sets `CONSOLE_EXTRA_CA_FILE` to it for OIDC calls to the internal IdP. Prod's IdP uses a public chain, so the overlay is not applied there.
- **Probes** run inside the container with `podman exec`, so they do not depend on the published port or on Caddy.

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
│   └── workflow.json.j2    config/workflow.json from the service-onboarding registry
├── .gitignore              .env, config/, certs/ (rendered per deploy)
├── AGENTS.md -> CLAUDE.md
└── README.md
```

The `Containerfile` is part of this change. It sits next to this file and builds the crate in `../app`.

## What not to do

- Don't generate or read secrets in `deploy.sh`. Ansible and OpenBao own credentials.
- Don't use `CMD-SHELL` in the compose healthcheck. The image has no shell.
- Don't add `--force-recreate` to `deploy.sh`. Recreate only on a change, through `compose_up_if_changed`.
- Don't put a real tenant, site or address in `console_tenants` in this repo. Production's scope lives in site-config. The local example is fictional.
- Don't commit `.env` or `config/`. Both are gitignored because they are rendered per deploy.
- Don't generate the Semaphore token in `deploy-console.yml`, and don't give the console the controller's token. The token belongs to the svc-console account and is provisioned by `provision-console-semaphore-access.yml`.

## Open items

- **Authentik side** (in this change): the `console` OAuth2 provider and application are declared in `platform/services/authentik/deployment/blueprints/console-oidc.yaml`, and `console_oidc_client_secret` is a generated secret in `platform/playbooks/vars/secret-declarations/authentik.yml`. Deploy Authentik before the console, otherwise the Phase 1 shared read fails.
- **Containerfile and `../app`** (in this change): the Containerfile must copy only the paths it needs. `deployment/Containerfile.dockerignore` keeps `.env`, `config/`, `certs/` and `app/target` out of the build context; podman 5.8.2 honors it (verified with a scratch build). A Containerfile with `COPY . .` would still bake in whatever remains.
- **AppRole.** `plan/development/17-dispatch-console.md` names `manage-approle.yml` for a scoped AppRole. This deploy does not provision one.
- **Step-result registration.** `platform/tests/test_service_deploy_step_result.py` (`WAVE`, `RECREATE_AWARE`) does not list the console. Its tests pass without it.

## Related

- Model: [`../../../playbooks/deploy-tududi.yml`](../../../playbooks/deploy-tududi.yml). Playbook: [`../../../playbooks/deploy-console.yml`](../../../playbooks/deploy-console.yml), [`../../../playbooks/clean-deploy-console.yml`](../../../playbooks/clean-deploy-console.yml)
- Plan: [`../../../../plan/development/17-dispatch-console.md`](../../../../plan/development/17-dispatch-console.md)
- Onboarding checklist: [`../../../../plan/architecture/02-service-onboarding.md`](../../../../plan/architecture/02-service-onboarding.md)
- Root conventions: [`../../../../AGENTS.md`](../../../../AGENTS.md)
