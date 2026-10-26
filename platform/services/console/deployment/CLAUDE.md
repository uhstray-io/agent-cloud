# CLAUDE.md — platform/services/console/deployment

The Dispatch Console: a Rust/Dioxus app (source in `../app`, image built from `Containerfile` in this directory) served behind central Caddy. It signs users in through Authentik (native OIDC, client `console`), keeps non-secret preferences in SQLite, and shows the tenant, org and site tree declared in the inventory. Plan: `plan/development/17-dispatch-console.md`.

## What this deploy does

Composable pattern, copied from `deploy-tududi.yml`:

```text
Semaphore "Deploy console" (or "Deploy console (Local)")
  └─ platform/playbooks/deploy-console.yml
     ├─ Phase 1  transport guard → place-monorepo → mkdir config/ → manage-secrets
     │           (env.j2 → .env, scope.json.j2 → config/scope.json)
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

`config/scope.json` (rendered from `templates/scope.json.j2`, mode 0644) is the tenant tree: `console_tenants` (a list of tenants, each with orgs, each org with sites and their `envs`) and `console_scope_is_example`. It is not a secret.

**OpenBao.** The console owns no secret, so `_secret_definitions` is empty. The deploy still stores one non-secret value: `url` at `secret/services/console`, from `_service_url`. This is required because `bao-merge-keys.yml` refuses an empty merge, and the store step has nothing else to merge. `comfyui` and `hunyuan3d` set `_service_url` the same way.

**Order.** Deploy Authentik before Deploy console. Authentik must hold `console_oidc_client_secret` in `secret/services/authentik`. Without it, the shared read stops Phase 1 and names the missing key.

## Runtime notes

- **Healthcheck is exec form, not CMD-SHELL.** The runtime image is distroless (`cc-debian12`, no `/bin/sh`), so `CMD-SHELL` cannot run. The probe is the server binary's own subcommand: `/usr/local/app/server probe /healthz 200`. Other services' comments say podman-compose mis-quotes `["CMD", ...]` lists. podman-compose 1.6.0 turns the list into a JSON `--health-cmd` (`podman_compose.py`, the `CMD` branch). Whether the engine then runs it as exec is **unverified** on this machine. The live deploy is the proof: `wait_for_healthy` fails if the container never reports healthy.
- **Preferences volume.** `console-data` mounts at `/data` (SQLite `prefs.db`). The image's nonroot uid 65532 must own `/data` (the Containerfile's job; unverified until the image is built).
- **Sessions** are in memory: a restart signs everyone out. This is the contract's accepted trade-off.
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
│   ├── env.j2              .env (non-secret config + shared OIDC secret)
│   └── scope.json.j2       config/scope.json from console_tenants
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
