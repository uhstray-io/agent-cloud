# agentgateway — agent practices

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Status: written 2026-10-02 from the files
cited in each section, for the service deployment workflow (change
`service-deployment-workflow`, task 7.7).

This is the service-specific half of the service agent's context (plan
`plan/development/15-service-deployment-workflow-agents.md`, "Agents"): ports, auth mode,
health signals, known quirks, undo path and what "healthy" means. The snapshot templates
ship this file verbatim into the reasoning step (`snapshot-service-assessment.yml`,
`snapshot-firewall.yml`, `snapshot-access.yml`, each reading
`platform/services/<service_name>/context/agent-practices.md`). Every statement cites the
file it comes from; where this file and a cited file disagree, the cited file wins and this
one is the bug. `file:line` citations were read on `origin/dev` at `99bbf5e0`.

Operational reference: `context/architecture.md`. Deploy reference: `deployment/README.md`.

## What the service is

The authenticated, metered `/v1` in front of the OpenAI-compatible model API. Clients keep
their base URL and request shape and change only the key they hold; the gateway holds the
upstream credential and decides identity itself (`context/architecture.md`, "Role").

Two containers: `agentgateway` (image `cr.agentgateway.dev/agentgateway:v1.5.0`, no shell,
no curl) and `agentgateway-db` (`postgres:16-alpine`, compose network only, volume
`agentgateway-pg-data`), which exists for per-key token budgets (`context/architecture.md`,
"Shape").

## Ports

| Port (container) | Purpose | Published | Source |
|---|---|---|---|
| 4000 | gateway `default` listener, the `/v1` API Caddy proxies | `AGW_BIND:AGW_PORT`, default `127.0.0.1:4000` | `deployment/compose.yml`, `deployment/templates/env.j2` |
| 4001 | operator UI listener (only with `agw_ui_enabled`, default true) | `AGW_UI_BIND:AGW_UI_PORT`, default `127.0.0.1:4001`; removed locally by `compose.local.yml` | `context/architecture.md` "Operator UI", `deployment/compose.yml` |
| 19001 | readiness, `/healthz/ready` | `AGW_READY_BIND:AGW_READY_PORT`, default `127.0.0.1:19001`; the deploy and verify still probe it over the compose network | `deployment/compose.yml:44`, `deployment/templates/env.j2:11-12`; `context/architecture.md` "Readiness" |
| 19002 | Prometheus text on `/metrics` | `AGW_STATS_BIND:AGW_STATS_PORT`, default `127.0.0.1:19002` | `context/architecture.md` "Stats", `deployment/compose.yml` |
| 15000 | admin | never published | `context/architecture.md` "Admin" |
| 5432 (db) | budget Postgres | never published; compose network only | `context/architecture.md` "Postgres" |

Local-dev replaces the publishes with fixed loopback ports: `127.0.0.1:4400` → 4000,
`127.0.0.1:4401` → 19001, `127.0.0.1:4402` → 19002, and no UI publish
(`deployment/compose.local.yml:34-37`).

A firewall proposal must give every allowed port a consumer. The private bind addresses and
the production firewall allow set are not in this repository; `context/architecture.md`
(status line) lists the production firewall step as pending.

## Auth mode

Two surfaces, two mechanisms:

- **`/v1` (model API): `api_key`, enforced by the gateway.** Policy `apiKey`, `mode: strict`,
  header `Authorization: Bearer <key>`; one identity per name in inventory `agw_clients`
  (`context/architecture.md`, "Identity and limits"). Caddy in front keeps TLS, the path
  allowlist and a header-shape Bearer check only (`context/architecture.md`, "Role"). This
  surface has no Authentik application.
- **Operator UI: `oidc`, run by the gateway itself.** The Authentik app catalog declares
  `agentgateway` as `type: oidc`, `tier: admin`, blueprint `agentgateway-oidc.yaml`
  (`platform/services/authentik/deployment/app-catalog.yml`). The gateway's own
  `authorization` rule requires the admin group (inventory `agw_ui_admin_group`, default
  `platform-admins`) in the token's `groups` claim (`context/architecture.md`, "Operator UI";
  `platform/playbooks/deploy-agentgateway.yml`, the safe-charset assert). Caddy at
  `admin.inference.<zone>` is a plain TLS proxy. It is **not** `forward_auth`: a Caddy
  forward_auth gate was tried first and left the UI reporting itself unauthenticated
  (`context/architecture.md`, "Operator UI").

An access proposal that changes either mechanism is a change to declared state and lands
through a pull request, never an executor (OPA rule "the proposal's verdict is not converge",
`platform/services/opa/deployment/policies/agentcloud/agent_actions.rego`).

Open point for review, not decided here: `access-policy.json` takes ONE `auth_mode`
(`platform/workflows/service-onboarding/schemas/access-policy.json`), while this service has
two surfaces. The Authentik catalog records `oidc`.

## What healthy means

A deploy is healthy when Phase 3 of `deploy-agentgateway.yml` passes. Conditions 1-3 are
proved on every real run; 4 and 5 (the keyed round-trip) only when a verifying identity
resolves, which is `agw_verify_client`, else the first `agw_clients` entry
(`deploy-agentgateway.yml:594-596`). Each keyed task is conditioned on that identity being
non-empty (`deploy-agentgateway.yml:610-722`), so with `agw_clients` empty and no
`agw_verify_client` the keyed probes are skipped and the report says "No enrolled identity
(agw_clients is empty): the keyed round-trip was not run" (`deploy-agentgateway.yml:740`).
Such a run has not proved the upstream is reachable. Conditions:

1. Readiness answers on `:19001/healthz/ready`, probed from the sibling `agentgateway-db`
   container to the gateway's compose-network address (`gateway-addr.sh`).
2. When `agw_otlp_host` is declared: the o11y Prometheus holds an
   `agentgateway_config_synchronized == 1` sample newer than this run's readiness time.
3. A keyless `GET /v1/models` answers **401** at the gateway (never reaches the upstream).
4. One enrolled identity (`agw_verify_client`, default the first `agw_clients` entry) lists
   every model it may use on `/v1/models` (200).
5. The same identity completes one chat request, and the reply is a `chat.completion` with
   at least one choice and `usage.completion_tokens > 0`, which the gateway can only produce
   by reaching `agw_upstream_base_url`.

A 429 with body `rate limit exceeded` (the gateway's own request bucket) fails the deploy as
**unproven** unless the run passes `-e agw_verify_allow_unproven=true`; any other 429 fails
like any other status (`deploy-agentgateway.yml`, "Keyed probes: note a policy refusal").

Health signals outside a deploy:

- Readiness URL for `Verify Service Health`: local-dev declares
  `health_url=http://agentgateway:19001/healthz/ready`, verified 200 from inside the local
  Semaphore container (`platform/playbooks/bootstrap-local-dev.yml`, `[agentgateway_svc:vars]`).
- Metrics on `:19002/metrics`; every metric carries `identity`. Request-level series
  (`agentgateway_requests_total`, `agentgateway_gen_ai_client_token_usage_*`,
  `agentgateway_requests_shed_total`, ...) appear only after traffic
  (`context/architecture.md`, "Verification log", 2026-09-17).
- `deploy.sh` prints `deploy-result: recreated (<reason>)` or `deploy-result: unchanged` as
  its last line; only the first is reported as a change (`deployment/README.md`).

## Templates

All Semaphore templates are declared in `platform/semaphore/templates.yml`; each below has a
generated `(Dev)` variant (`dev_variant`).

| Template | Playbook | Does | Survey |
|---|---|---|---|
| Deploy agentgateway | `deploy-agentgateway.yml` | secrets, render `.env` + `config.yaml`, `deploy.sh`, verify (above) | `service_branch` |
| Clean Deploy agentgateway | `clean-deploy-agentgateway.yml` | **destructive**: removes both containers, the budget volume and the rendered files, then imports the deploy | `service_branch` |
| Manage agentgateway Client Key | `manage-agentgateway-client-key.yml` | rotate or revoke ONE client key, then imports the deploy | `client`, `action`, `service_branch` |
| Rollback Inference Route | `rollback-inference-route.yml` | take the gateway out of the public path or put it back (modes below) | `mode` |
| Back Up Credentials to site-config | `backup-credentials-to-site-config.yml` | the hand-out channel for a client key | `credential_service=agentgateway`, `credential_fields=client_<name>` |

Local-dev deploys run `make local-deploy-agentgateway` (`deployment/README.md`; the
`local-deploy-%` target in `Makefile` deploys through the local Semaphore).

## What an agent may do

Decided by OPA on the committed catalog
(`platform/services/opa/deployment/policies/agentcloud/data.json`, rules in
`agent_actions.rego`):

- **`service-agent`** may launch `Deploy agentgateway` — it is on its closed
  `service_deploy_templates` list — only as registry step `service-deploy`, only with a
  `service-assess` proposal whose verdict is `converge`, and only when that proposal's
  `deploy_template` is the template launched. `service-deploy` is `reviewed: false`, so the
  launch must be an off-main variant (`(Dev)` / `(Local)`) on a non-`main` branch: "an
  unreviewed step cannot run from main" (`agent_actions.rego:131-143`). Every agent launch
  must also name `main` or `dev` as its `git_branch`, a missing one meaning `main` ("an agent
  task may run only from main or dev", `agent_actions.rego:258-267`; `launch_branches`
  `["main", "dev"]`, `data.json:166`). Together: the `(Dev)` variant with `git_branch: dev`.
- `service-agent` may also run `Verify Service Health` (`service-validate`),
  `Snapshot Service Assessment` (`service-assess`), `Snapshot Access` (`access-assess`),
  `Check Secrets` (`secrets-approle`) and `Manage Caddy Sites` (`edge-route`) against this
  service, each under the same step and review rules.
- Read-only work needs no launch: reading this file and `context/architecture.md`. The deploy
  playbook emits no workflow step result of its own (no `emit-step-result.yml` include in
  `deploy-agentgateway.yml`).

## What an agent may not do

- **Clean Deploy agentgateway.** Any template starting `Clean Deploy` is destructive and is
  denied without `human_approved` for every agent (`agent_actions.rego`, `_destructive`), and
  it is on no workflow role's `allowed_templates`. A service proposal naming it is denied
  ("service proposal names a destructive template").
- **Manage agentgateway Client Key** and **Rollback Inference Route.** Neither is on any
  workflow role's `allowed_templates` or `service_deploy_templates` (`data.json`), so OPA
  denies them to `infra-agent`, `security-agent`, `o11y-agent` and `service-agent`. Note: the
  legacy `skynet` and `nemoclaw` catalog entries declare no `allowed_templates`
  (`data.json:3`, `data.json:62`), so they are not role-scoped (`agent_actions.rego:74`): OPA
  limits their `run_task` by template only through the destructive rule
  (`agent_actions.rego:68-70`), and by branch through the `main`|`dev` gate that applies to
  every agent launch (`agent_actions.rego:258-267`). Neither stops them launching these two
  templates from `main` or `dev`. This file forbids those two templates to any agent by
  practice; rotation, revocation and rollback are operator actions.
- **Add a client by any route other than inventory.** Adding a client is adding the name to
  `agw_clients` and deploying; that inventory change lands through a pull request
  (`manage-agentgateway-client-key.yml` header; "Require an explicit client and action").
- **Edit gateway configuration in the operator UI.** `UI_READ_ONLY=true` makes the UI's write
  handlers answer 403 and `config.yaml` is mounted `:ro`; configuration is code
  (`deployment/README.md`, "The operator UI is read-only").
- **Set `agw_plaintext_keys` outside local-dev.** The deploy refuses it without
  `local_mode: true` (`deploy-agentgateway.yml`, "Refuse agw_plaintext_keys outside
  local_mode").
- **Generate the upstream key.** `vllm_api_key` is `existing`: seeded by a separate step,
  never generated by the deploy; a deploy with it empty is refused while
  `agw_upstream_requires_key` is true
  (`platform/playbooks/vars/secret-declarations/agentgateway.yml`).
- **Rotate the key at vLLM.** Nothing in this repository can; vLLM reads it from the
  dgx-spark repository (`rollback-inference-route.yml` header, "WHAT THIS DOES NOT DO").

## Secrets boundary

- All values live at `secret/services/agentgateway`: `vllm_api_key` (`existing`),
  `agw_db_password`, `agw_oidc_cookie_seed` and one `client_<name>` (48 chars) per
  `agw_clients` entry, generated once and reused
  (`vars/secret-declarations/agentgateway.yml`). The UI's OIDC client secret is Authentik's,
  shared-read as `agentgateway_oidc_client_secret`, and only when `agw_ui_enabled`
  (`deploy-agentgateway.yml`, `_shared_reads`).
- On the host, `.env` is 0600 and carries the credentials (`deploy-agentgateway.yml:73`).
  `config.yaml` is 0644 (`deploy-agentgateway.yml:78`), and what it holds depends on the
  environment:
  - **Production and any run without the flag:** sha256 key hashes (`keyHash: sha256:<hex>`),
    identity names and env references only (`deployment/templates/config.yaml.j2:7-10`,
    `:169`).
  - **Local-dev with `agw_plaintext_keys: true` and `local_mode: true`:** the raw client key
    values (`- key: <value>`), so the UI playground can offer saved keys
    (`deployment/templates/config.yaml.j2:163-167`). The local inventory sets the flag
    (`platform/playbooks/bootstrap-local-dev.yml`, `[agentgateway_svc:vars]`), so there the
    0644 `config.yaml` is a credential. The deploy refuses the flag outside `local_mode`
    (`deploy-agentgateway.yml:106-109`).
- The keyed verify sends the key as a `uri` module argument from the target, never on any
  process's argv, and the key-bearing probes are `no_log` (`deploy-agentgateway.yml`, comment
  above "Keyed probes"). No playbook here prints a key value; the client-key and rollback
  playbooks report names only (`manage-agentgateway-client-key.yml`, "Report (no values)";
  `rollback-inference-route.yml`, "Report the published copies (names only)").
- Snapshots carry names and metadata only, never values (`snapshot-access.yml`,
  `snapshot-firewall.yml` headers). A proposal must not ask for a value.
- Prompt and completion content is never logged; the db stores request metadata, and
  `request_log_payloads` stayed 0 after a completion (`context/architecture.md`, "Stats",
  "Postgres").

## Undo path

The registry records `undo: none` for every step after the VM exists (`provision-vm` and
`cloud-init` name `Destroy VM`; `platform/workflows/service-onboarding/registry.yml`). This is
what exists for this service:

| Situation | Undo | Source |
|---|---|---|
| A deploy rendered a bad config | Fix inventory and redeploy; `deploy.sh` recreates only when an input changed. The declaration asserts (upstream and models, name charset, plaintext keys, listener TLS) run before anything is rendered or restarted | `deployment/README.md`; `deploy-agentgateway.yml` Phase 1 |
| Put the last live config back, gateway stays in the path | `Rollback Inference Route`, `mode=gateway-config`: copies `config.yaml.previous` into place and recreates the gateway, Caddy untouched. The deploy keeps the config it replaces as `config.yaml.previous`, written only when a render changed `config.yaml` (a first deploy keeps nothing). It keeps nothing under the local-dev `agw_plaintext_keys` flag, and never promotes a staged copy that carries a raw `- key:` entry. With no `.previous`, the mode refuses and says to roll back in `direct` mode or fix forward | `deploy-agentgateway.yml:62-63`, `:248-262`; `rollback-inference-route.yml:14`, `:368-375` |
| Take the gateway out of the public path | `mode=direct`: publishes the live vLLM key as `direct_<name>` beside each enrolled `client_<name>`, then points the inference route at vLLM through `Manage Caddy Sites` | `rollback-inference-route.yml` header |
| Put the gateway back | `mode=restore`: points the route back, then removes the copies only once the key they hold is no longer live and the running gateway holds the new one; otherwise stops naming the steps owed | `rollback-inference-route.yml` header |
| A client key leaked | Operator runs `Manage agentgateway Client Key` `action=rotate`; for a retired client, remove it from `agw_clients` FIRST, then `action=revoke` (old key is 401 after the reload) | `manage-agentgateway-client-key.yml` |
| Budget state corrupted | Operator runs `Clean Deploy agentgateway`; keys return from OpenBao unchanged, every identity's budget window restarts at zero | `clean-deploy-agentgateway.yml` header |

The rollback modes' live drill is still owed (`inference-gateway-agentgateway` tasks.md 4.6
note, 2026-10-02).

## Known quirks

- The gateway image has no shell: every probe runs from `agentgateway-db` (busybox `wget`)
  or as a `uri` call from the target (`deploy-agentgateway.yml`, Phase 3 comment).
- Probes address the gateway by compose-network IP, not by name: by name, a VM named
  `agentgateway` resolved to the db container itself (`deploy-agentgateway.yml`, Phase 3
  comment, task 1215).
- With `agw_ui_enabled: true`, v1.5.0 fetches the OIDC discovery document when it loads
  config, so the gateway cannot start before its Authentik provider exists;
  `agw_ui_enabled: false` deploys `/v1` alone (`context/architecture.md`, "Operator UI").
- A recreate drops every in-flight stream, which is why `deploy.sh` leaves a running gateway
  alone unless an input or image changed (`deployment/README.md`).
- `localRateLimit` is ONE global request bucket; the per-identity control is each key's
  hourly token budget, and a per-identity request bucket is unavailable in v1.5.0
  (`context/architecture.md`, "Identity and limits").
- A play whose `hosts:` matches nothing exits 0, so the deploy, client-key and rollback
  playbooks first import `preflight-target-group.yml` for `agentgateway_svc`
  (`deploy-agentgateway.yml`, Pre-flight comment); the clean deploy reaches it through its
  imported deploy, after its destroy play.
- The deploy playbook's header still says the keyed round-trip is not done in the deploy;
  Phase 3 does run it (`deploy-agentgateway.yml` lines 13-15 against the "Keyed probes"
  tasks). Trust the tasks.
- Local-dev verifies through `agw_verify_base_url=http://agentgateway:4000`, because the play
  runs inside the Semaphore container where the published loopback port is not the gateway
  (`bootstrap-local-dev.yml`, `[agentgateway_svc:vars]`).
