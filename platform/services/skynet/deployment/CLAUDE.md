# CLAUDE.md — platform/services/skynet/deployment

skynet — the Tier 2 orchestration API (`skynet`, :8080 on the container network) and its own
Postgres for LangGraph checkpoints (`skynet-db`, internal only). Two podman containers, no host
port published. Its design is DESIGN-DECISIONS §17 in the skynet
repository; its variables are listed in `deploy/tier2/README.md` there.

## What this service is

1. `skynet` serves the orchestration API. It runs `service_onboarding`: a run takes a declared
   service through the registry steps, asks OPA for each decision, calls the served model
   through agentgateway, and runs Ansible through Semaphore.
2. `skynet-db` holds the checkpoints. It is not agentgateway's budget database and shares
   nothing with it.
3. The image runs as uid 10001 and listens on `SKYNET_BIND=0.0.0.0`, `SKYNET_PORT=8080`
   (set by the image).

## Auth — two layers, both required

- **Gateway key.** agentgateway's `/skynet/` route admits only enrolled identities.
- **skynet bearer** (`SKYNET_API_TOKEN`). agentgateway's route presents it as `backendAuth`, so
  every route except `/skynet/healthz` answers 401 without it. Reaching skynet directly on the
  network does not bypass the gateway.

## Secrets and the render contract

Ansible renders `.env` (gitignored). `deploy.sh` only reads it.

| Source | Lands in `.env` as |
|---|---|
| `secret/services/skynet:db_password` | `SKYNET_DB_PASSWORD` (compose hands it to both containers) |
| `secret/services/skynet:api_token` | `SKYNET_API_TOKEN` |
| `secret/services/semaphore:api_token`, read by the playbook as the fact `skynet_semaphore_token` | `SKYNET_SEMAPHORE_TOKEN` |
| shared read `secret/services/agentgateway:client_<role>` (`secrets['client_<role>']`) | `SKYNET_GATEWAY_KEY_<ROLE>`, written only when non-empty |
| inventory `skynet_gateway_url`, `skynet_model`, `skynet_opa_url`, `skynet_semaphore_url`, `skynet_semaphore_project`, `skynet_semaphore_catalog`, `skynet_image` | the matching `SKYNET_*` variables |

- The six roles are `infra-agent`, `security-agent`, `o11y-agent`, `service-agent`,
  `network-agent` and `skynet-eval`. A missing role key fails that role's model calls closed.
- The Semaphore token is deliberately NOT shared-read under the name `api_token`. That name
  is skynet's own bearer, and a shared key of the same name would shadow it.
- **Stopgap:** the Semaphore token is the operator's token. A scoped Semaphore role for skynet
  is **unverified** and is a follow-up.
- `skynet_image` has no default. Nothing in this directory names a registry.

## Image

The image ref comes from the inventory (`skynet_image`), pinned by digest. It is not built on
the VM.

**Open:** skynet's `.github/workflows/ci.yml` at `origin/main` (29ecab5) runs the tests against a
podman Postgres and has no registry push step. `deploy/tier2/docker-compose.yml` builds
`localhost/skynet-tier2:latest` locally. No publisher for a GHCR image exists in that repository
at that commit, so a pinned digest has nothing to pin yet.

## Trust (local-dev only)

skynet reaches the gateway, OPA and Semaphore over HTTPS through local Caddy, whose
certificates come from step-ca. `compose.local.yml` mounts `./certs` and sets
`SSL_CERT_FILE=/certs/step-ca-bundle.crt`. `SSL_CERT_FILE` replaces the process's whole trust
store, so the file must hold the step-ca root and intermediate. The render places that bundle
with `tasks/distribute-ca-root.yml`, which the playbook must call with `_ca_root_dest_dir` = the
deploy dir. Do not pass
`_ca_root_combined_dest`: that is agentgateway's upstream case, and skynet needs no public roots.
The base `compose.yml` never sets `SSL_CERT_FILE`.

## Networks and reachability

- Production: no published port and no shared network. Nothing reaches skynet from the production
  gateway yet. Attaching a network both share is a decision for the lead.
- Local-dev: `compose.local.yml` attaches `skynet` to the `local-dev` network (created by
  `platform/playbooks/bootstrap-local-dev.yml`). The local gateway reaches it there by container
  name as `skynet:8080`.
- **Open:** `platform/inventory/local-dev.yml.example` sets `agw_skynet_backend` to
  `host.containers.internal:18556`, a host port this stack does not publish. Over `local-dev` the
  backend would be `skynet:8080`. That value belongs to the gateway route, not this directory.

## Conventions

- **deploy.sh is lifecycle only** (Critical Deployment Rule #2). It pulls, runs
  `compose_up_if_changed` (`platform/lib/common.sh`), and waits for `skynet` to be healthy. It
  prints `DEPLOY_CHANGED=true|false`. `.env` is hashed, so a re-rendered `.env` recreates the
  containers. An unchanged re-run recreates nothing. The local bring-up on a locally built image
  is tolerated by `step_pull_images`, as honcho does.
- **`restart: always`** on both services. Podman's boot unit starts only `always` containers
  (enforced by `platform/tests/test_restart_policy.bats`).
- **Healthchecks use `CMD-SHELL`**, the same form honcho uses, because podman-compose mis-quotes
  exec-form `CMD` lists.
- **`skynet-db` gets no `env_file`.** It takes only `POSTGRES_*` from the compose environment, so
  it never sees the Semaphore token or the gateway keys.

## What not to do

- Don't publish a port for `skynet` or `skynet-db` in the base file. The local route reaches
  skynet over `local-dev`.
- Don't put `SSL_CERT_FILE` in the base file. It belongs to the local overlay.
- Don't copy a secret into a template as a default. Required values have none.
- Don't commit `.env` or `certs/`. The `.gitignore` here and the root `*.env` rule cover them.
- Don't read a token for the Semaphore fact from a workstation file. It comes from OpenBao through
  the playbook.

## Related

- skynet repository, `deploy/tier2/README.md`: the variables and the API surface.
- Platform plan: `plan/development/15-service-deployment-workflow-agents.md`.
- Gateway route and its key policy: `platform/services/agentgateway/deployment/`.
- Trust bundle task: `platform/playbooks/tasks/distribute-ca-root.yml`.
