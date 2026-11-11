# CLAUDE.md — platform/services/skynet/deployment

skynet — the Tier 2 orchestration API (`skynet`, :8080 on the container network) and its own
Postgres for LangGraph checkpoints (`skynet-db`, internal only). Two podman containers. `skynet`
publishes its API on the declared host bind (`skynet_publish_bind`:`skynet_publish_port`, loopback
`127.0.0.1:8380` by default); `skynet-db` publishes nothing. Its design is DESIGN-DECISIONS §17 in the skynet
repository; its variables are listed in the skynet repository's `deploy/tier2/README.md`.

## What this service is

1. `skynet` serves the orchestration API. It runs `service_onboarding`: a run takes a declared
   service through the registry steps, asks OPA for each decision, calls the served model
   through agentgateway, and runs Ansible through Semaphore (launch steps are refused while svc-skynet is
   guest; they return through the planned narrow launcher).
2. `skynet-db` holds the checkpoints. It is not agentgateway's budget database and shares
   nothing with it.
3. The image runs as uid 10001 and listens on `SKYNET_BIND=0.0.0.0`, `SKYNET_PORT=8080`
   (set by the image).

## Auth — two layers, both required

- **Gateway key.** agentgateway's `/skynet/` route admits only enrolled identities.
- **skynet bearer** (`SKYNET_API_TOKEN`). agentgateway's route presents it as `backendAuth`, so
  every route except `/skynet/healthz` answers 401 without it. Reaching skynet directly on the
  network does not bypass the gateway.
- **Read bearer** (`SKYNET_READ_TOKEN`), for the Dispatch Console. Accepted on `GET /skynet/runs`
  and `GET /skynet/runs/<id>` only; `POST /skynet/runs` answers 401 to it. The server side of that
  contract is in the skynet repository (a separate change); this repository's verify probes it
  inside the container on every deploy. The console's shared read of it is not wired here.
- A published API is reachable without the gateway. The bearers are the control, and the host
  firewall must admit only the Dispatch Console VM and the gateway VM (site-config declaration).

## Secrets and the render contract

Ansible renders `.env` (gitignored). `deploy.sh` only reads it.

| Source | Lands in `.env` as |
|---|---|
| `secret/services/skynet:db_password` | `SKYNET_DB_PASSWORD` (compose hands it to both containers) |
| `secret/services/skynet:api_token` | `SKYNET_API_TOKEN` |
| `secret/services/skynet:read_token` | `SKYNET_READ_TOKEN` |
| `secret/services/skynet:semaphore_api_token` (existing; written by `Provision skynet Semaphore Access`) | `SKYNET_SEMAPHORE_TOKEN` |
| inventory `skynet_publish_bind`, `skynet_publish_port` (optional; defaults `127.0.0.1`, `8380`) | `SKYNET_PUBLISH_BIND`, `SKYNET_PUBLISH_PORT`, substituted into compose `ports:` |
| inventory `skynet_opa_url` (host part) and `skynet_opa_ip` (production only; required, IPv4) | `SKYNET_OPA_HOSTNAME`, `SKYNET_OPA_IP`, substituted into `compose.prod.yml` `extra_hosts` |
| shared read `secret/services/agentgateway:client_<role>` (`secrets['client_<role>']`) | `SKYNET_GATEWAY_KEY_<ROLE>`, omitted when the key exists but is empty. A missing key stops the deploy (the shared read is all-or-nothing) |
| shared read `secret/services/launch-gate:caller_skynet_token`, only when `skynet_launch_gate_url` is set | `SKYNET_LAUNCH_GATE_TOKEN` (skynet's bearer at the launch gate; the gate's deploy generates it) |
| inventory `skynet_launch_gate_url` (optional) | `SKYNET_LAUNCH_GATE_URL`. Production also renders its host part and `skynet_launch_gate_ip` (required then, IPv4) as `SKYNET_LAUNCH_GATE_HOSTNAME`, `SKYNET_LAUNCH_GATE_IP`, substituted into `compose.launch-gate.yml` `extra_hosts` |
| inventory `skynet_tls` (optional, default off) with `skynet_bind`, `skynet_port` (default 8443), `skynet_server_leaf` (default `skynet-server`), `skynet_tls_server_name` (default `skynet.<dns_site>.<dns_zone>`) | `SKYNET_TLS_CERT_FILE`, `SKYNET_TLS_KEY_FILE` (the leaf under `/certs/<leaf>/current/`), `SKYNET_TLS_PUBLISH_BIND`, `SKYNET_TLS_PUBLISH_PORT` (substituted into `compose.tls.yml` `ports:`), `SKYNET_TLS_NAME`. Rendered only when `skynet_tls` is on |
| inventory `skynet_gateway_url`, `skynet_model`, `skynet_opa_url`, `skynet_semaphore_url`, `skynet_semaphore_project`, `skynet_semaphore_catalog`, `skynet_image` | the matching `SKYNET_*` variables |

- The six roles are `infra-agent`, `security-agent`, `o11y-agent`, `service-agent`,
  `network-agent` and `skynet-eval`. The shared read is all-or-nothing: a missing role key stops the
  deploy in manage-secrets. An absent `SKYNET_GATEWAY_KEY_<ROLE>` line fails that role's model calls
  closed in skynet.
- `semaphore_api_token` is the svc-skynet account's Semaphore token, an `existing` secret in
  skynet's own path. The deploy never generates it, and manage-secrets refuses the run by name,
  before any OpenBao write, when it is absent. It is never the operator's token.
- The svc-skynet account and its project role come from `Provision skynet Semaphore Access`
  (`platform/playbooks/provision-skynet-semaphore-access.yml`): project role `guest`, read-only. This is
  interim, from 2026-10-31 (the role was `task_runner` until then): launch rights return only through the
  planned narrow launcher, a separate change. Run it before this deploy.
- `read_token` is generated once, like `api_token`, and never shared-read here.
- The launch gate (`skynet-launch-containment`) is optional until production deploys it. With
  `skynet_launch_gate_url` unset, the deploy reads no gate token, renders no gate variable and loads
  no gate overlay. With it set, the gate must be deployed first: its deploy generates
  `caller_skynet_token`, and the shared read stops this deploy by name when the key is absent.
- `skynet_image` has no default. Nothing in this directory names a registry.
- The inventory publish values are optional. A declared `skynet_publish_bind` must be one
  non-wildcard IPv4 address (`0.0.0.0` is refused), and `skynet_publish_port` a whole number from
  1024 to 65535. Both are refused before anything changes.

## Image

The image ref comes from the inventory (`skynet_image`), pinned by digest. It is not built on
the VM.

The skynet repository publishes the image from its `.github/workflows/publish-tier2.yml` (merged 45fd869, skynet
PR #10) to GHCR as `ghcr.io/uhstray-io/skynet-tier2`. The inventory pins that digest in
`skynet_image`.

## Trust

skynet calls OPA, the gateway and Semaphore over HTTPS. `SSL_CERT_FILE` replaces the process's
whole trust store, so each overlay names a bundle that holds everything skynet must verify. The
base `compose.yml` never sets `SSL_CERT_FILE`.

- **Local-dev** (`compose.local.yml`, loaded when LOCAL_MODE=true): every local endpoint is a
  step-ca leaf. The bundle is `./certs/step-ca-bundle.crt`, the root and intermediate, written by
  `tasks/distribute-ca-root.yml` with `_ca_root_dest_dir` = the deploy dir. No public roots.
- **Production** (`compose.prod.yml`, appended by `deploy.sh` outside LOCAL_MODE): OPA's leaf is
  an internal-CA leaf for a name with no production DNS record. The playbook calls
  `tasks/distribute-ca-root.yml` with `_ca_host` = the `step_ca_svc` host and
  `_ca_root_combined_dest` = `certs/ca-bundle-combined.crt`. That bundle holds the root and
  intermediate plus this VM's public roots, because the gateway and Semaphore are public-chain
  hosts. `SSL_CERT_FILE` names it, and `extra_hosts` maps the OPA name to `skynet_opa_ip`.
- Refused before anything changes, outside local mode: no `step_ca_svc` host, and a
  `skynet_opa_ip` that is not one non-wildcard IPv4 address.
- The combined bundle is built from the play host's public roots. A host without them is refused
  by `tasks/distribute-ca-root.yml`, not left with a bundle that lacks them.
- The deploy's verify proves this whole path: inside the container, skynet's own
  `skynet.policy.opa.OPAClient` asks `SKYNET_OPA_URL` about an input no policy knows and must get
  OPA's default deny. So OPA must be running and must admit skynet (production: OPA's
  `firewall_allow_groups` names `skynet_svc`; local-dev: the OPA stack is up) before a skynet deploy
  can pass. A failure's message names TLS, resolution, the firewall or the decision contract.
  The verify also sends skynet's transport a `PUT` to `/v1/data/agentcloud_write_probe` on OPA, which must get 401: OPA's authorization policy admits only decisions and health.
- **Launch gate** (when `skynet_launch_gate_url` is set): the gate's leaf is an internal-CA leaf too,
  so the bundles above already trust it. In production its name has no DNS record either:
  `deploy.sh` adds `compose.launch-gate.yml` ahead of `compose.prod.yml` outside LOCAL_MODE when `.env` carries
  `SKYNET_LAUNCH_GATE_HOSTNAME`, and that overlay maps the name to `skynet_launch_gate_ip`. It is a
  separate file because `compose.prod.yml` requires its variables, which would refuse every production
  deploy without the gate. Local-dev reaches the gate by its network alias on `local-dev`. The verify
  sends two `POST /v1/launch` requests from inside the container: without a token it must get
  `401 unauthorized`, and with skynet's token and an empty body `400 bad_request`, which proves the
  rendered token is the one the gate holds without creating a task. In production the gate's firewall
  must admit `skynet_svc`.

## HTTPS listener (`skynet_tls`)

The HTTPS listener lets the Dispatch Console reach skynet without sending its bearer across the LAN
in cleartext (skynet DESIGN-DECISIONS §20). It is added beside the plain port, not in place of it:
the bearer stops crossing in cleartext only once the console calls `skynet_port` and the firewall no
longer admits it to `skynet_publish_port`. The image serves the same routes, with the same token scopes, on
container port 8443 once `SKYNET_TLS_CERT_FILE` and `SKYNET_TLS_KEY_FILE` are set; plain 8080 is
unchanged. The listener checks the leaf's files on every new connection and reloads them when
their identity changes, so a renewal needs no restart.

- **Leaf.** `internal_leaves` declares `skynet-server` for this host: profile `server`, dir
  `<deploy>/certs/skynet-server`, `reload: none`, the TLS name in its SANs. The deploy refuses
  anything else, then issues the leaf through `tasks/mint-internal-cert.yml` (production: signed on
  the `step_ca_svc` host and kept; local-dev: re-issued when under 7 days remain). Production also
  needs the SAN in the CA's name policy, which `Deploy step-ca (Dev)` applies from `internal_leaves`.
- **Publish.** `deploy.sh` loads `compose.tls.yml` when `.env` carries `SKYNET_TLS_CERT_FILE`, and
  publishes `skynet_bind:skynet_port` to 8443. Production refuses an undeclared or loopback
  `skynet_bind`, and a `skynet_port` equal to `skynet_publish_port`. The firewall must admit the
  Dispatch Console VM to `skynet_port`.
- **Key access.** The image runs as uid 10001. Production maps the deploy account to it with
  `userns_mode: keep-id` (`compose.prod-tls.yml`, loaded by `deploy.sh` only outside local mode
  with the listener on), so it can read the 0600 key; a production deploy without the listener
  renders no `user` or `userns_mode`. With the listener, the deploy refuses podman older than 4.3,
  checked after the repo placement that installs podman. Local-dev runs as `1001:0`, the local Semaphore
  container's uid that writes the key (`compose.local.yml`).
- **Renewal.** `Renew Internal Certs` admits the leaf through the `skynet-server` row in
  `vars/renew-listener-servers.yml` (prefix `skynet`, gated on `skynet_tls`) and proves the new
  serial is served on `skynet_bind:skynet_port`. `deploy.sh` hashes only the trust bundles, so a
  renewed leaf does not recreate the container on the next deploy.
- **Verify.** From the host, against `certs/step-ca-bundle.crt`: `GET /skynet/healthz` over TLS
  answers 200, `GET /skynet/runs` with no token answers 401, and plain HTTP to the TLS port does not
  answer 200. Local-dev dials the TLS name on 8443 through its `local-dev` network alias.

## Networks and reachability

- Production: `skynet` publishes `${SKYNET_PUBLISH_BIND}:${SKYNET_PUBLISH_PORT}` to container
  8080, on the address the inventory declares (the VM's LAN address). The host firewall admits only
  the Dispatch Console VM and the gateway VM. The gateway's `/skynet/` route is NOT enabled by this
  deploy; enabling it in production is a follow-up that runs after this deploy has run once.
  `skynet-db` has no published port and no shared network.
- Local-dev: `compose.local.yml` attaches `skynet` to the `local-dev` network (created by
  `platform/playbooks/bootstrap-local-dev.yml`). The local gateway reaches it there by container
  name as `skynet:8080`. The host publish defaults to loopback, `127.0.0.1:8380`.

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

- Don't publish `skynet-db`. Don't publish `skynet` anywhere but the declared bind and port:
  `skynet_publish_bind` refuses `0.0.0.0`, and the compose defaults stay on loopback.
- Don't render `SKYNET_BIND` or `SKYNET_PORT` into `.env`. The image sets `SKYNET_BIND=0.0.0.0`, and
  `env_file` would hand a rendered value to the container. The publish knobs are
  `SKYNET_PUBLISH_BIND` and `SKYNET_PUBLISH_PORT`. The same goes for `SKYNET_TLS_PORT`, the image's
  HTTPS listen port: the host port is `SKYNET_TLS_PUBLISH_PORT`.
- Don't put `SSL_CERT_FILE` in the base file. Each overlay sets it: `compose.local.yml` and `compose.prod.yml`.
- Don't copy a secret into a template as a default. Required values have none.
- Don't commit `.env` or `certs/`. The `.gitignore` here and the root `*.env` rule cover them.
- Don't give skynet the operator's Semaphore token, and don't read one from a workstation file.
  skynet's own token is `secret/services/skynet:semaphore_api_token`, read through manage-secrets.

## Related

- skynet repository's `deploy/tier2/README.md`: the variables and the API surface.
- OpenSpec change `service-deployment-workflow`, task 3.7 (`plan/development/openspec/changes/service-deployment-workflow/tasks.md`).
- Platform plan: `plan/development/15-service-deployment-workflow-agents.md`.
- Gateway route and its key policy: `platform/services/agentgateway/deployment/`.
- Trust bundle task: `platform/playbooks/tasks/distribute-ca-root.yml`.
