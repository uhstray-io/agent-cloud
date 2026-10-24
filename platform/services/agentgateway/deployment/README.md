# agentgateway deployment

Deploy through Semaphore: template **Deploy agentgateway** (prod) or
`make local-deploy-agentgateway` (local-dev). Destructive rebuild: **Clean Deploy
agentgateway** — removes both containers (gateway + its own Postgres) and the budget
volume, then redeploys. Keys come back from OpenBao unchanged; the only state lost is
every identity's current token-budget window.

Every real deploy checks that rendered `AGW_IMAGE` equals the reviewed
`cr.agentgateway.dev/agentgateway:v1.5.0` pin, then asks `deploy.sh --pull-only`
to resolve the effective Compose images. It captures the resulting local image
ID and runs that ID with `--validate-only -f /config.yaml` before `deploy.sh`
can recreate containers. The validator uses `--pull=never`, and the actual
lifecycle uses `deploy.sh --no-pull`; a second image-ID check immediately before
deployment refuses a moved tag. The pull-only mode uses the same Compose
file/overlay selection as deployment and exits before any container decision or
change. Before invoking Compose, `deploy.sh` replaces any inherited `AGW_IMAGE`
with the single value rendered in `.env`, because Compose otherwise gives the
process environment precedence. The validator receives `.env` by file path.
Production uses Podman's default network so issuer-only OIDC discovery can run;
local-dev joins `local-dev`, where the Authentik hostname alias is declared.
This checks config acceptance but does not reproduce the runtime Compose network
topology. When listener TLS is enabled, it also receives the same read-only
`/certs` mount and trust settings as Compose; local-dev always receives them;
the production TLS check uses the same keep-id mapping. Its output is suppressed
because parser errors may quote configuration. The invocation follows the
[standalone validation command](https://agentgateway.dev/docs/standalone/latest/documentation/setup/update/)
and the image comes from the [v1.5.0 release](https://github.com/agentgateway/agentgateway/releases/tag/v1.5.0).
`Verify agentgateway Runtime (Dev)` is a separate,
read-only receipt that requires the checkout to match the requested reviewed SHA,
the running input label to match the current rendered files, readiness, and the
running image ID to match the resolved v1.5.0 image ID. It reports the checkout
revision, two sampling values, and bounded metadata describing whether the rendered
`frontendPolicies.accessLog.otlp` block is present and whether its fixed service and
signal fields match. The access-log metadata reports config shape only; it does not
prove receiver delivery or Loki records. A checkout mismatch emits only bounded
observed/expected SHA metadata marked unverified, then still fails the exact-revision
guard.
Because `.env` and `config.yaml` are gitignored render outputs without a source
revision stamp, this receipt does not prove which checkout produced them. It
reports only metadata; hashes and rendered file contents are not displayed.
It does not claim callback-marker absence from stored logs or spans. That runtime
correlation gate remains incomplete until stdout and OTLP records and an exact
callback span can be joined to each bounded synthetic request. Validator success
is necessary config evidence, not proof of runtime callback behavior. OTLP access
record separation with `signal: "access-log"` remains pending in gateway-owned
configuration; this source-side verifier does not claim that field has landed.

`deploy.sh` is container lifecycle only. Both files the container reads (`.env`,
`config.yaml`) are rendered by `deploy-agentgateway.yml` from OpenBao + inventory and
are gitignored. Operational reference: `../context/architecture.md`. When a deploy's render changes `config.yaml`, the file it replaced is kept
beside it as `config.yaml.previous` (an unchanged render leaves that copy alone; never under
local-dev's `agw_plaintext_keys`), which `rollback-inference-route.yml` gateway-config mode puts back. The
rollback refuses, before any write, a kept copy that enrols an identity the live config does not
(a rotated or revoked key) or `legacy-shared` past `legacy_shared_expires`, naming the identities
only; and `manage-agentgateway-client-key.yml` removes the copy once its deploy succeeds, so a
rotation or revocation leaves no rollback path to the old key.

## MCP and the declared admin list: variables

All are inventory values (site-config in production, `platform/inventory/local-dev.yml.example` locally);
none is a survey variable. The reference for what each does is `../context/architecture.md` ("Identity and
limits", "MCP (change `agentgateway-mcp`, gateway side)", "Operator operations").

| Variable | Default | Meaning |
|---|---|---|
| `platform_admins` | the shared default in `platform/playbooks/vars/platform-admins.yml` | The ONE declared admin list: each entry is `key`, `username`, `name`, `email` (not for a service account), optional `type` and `agw_identity`. An inventory that sets it replaces the default entirely. Authentik renders its admin accounts from it; the gateway derives one personal identity per entry |
| `agw_admin_identities_enabled` | `false` | Gate for the derivation. Off, the gateway's identities are exactly `agw_clients` and the list is not read. On, `platform_admins` must be declared for the gateway host (the default list is built from per-user variables that exist only on the Authentik host, so the deploy refuses the run) |
| `platform_admins_exceptions` | empty | Names of `platform-admins` members deliberately outside the list; read by the Authentik drift guard only |
| `platform_retired_accounts` | empty | Usernames Deploy Authentik deletes. Retiring an administrator: remove it from `platform_admins` in the same change, deploy Authentik, then revoke its gateway key with `Manage agentgateway Client Key` (`action=revoke`) |
| `agw_mcp_enabled` | `false` | Serve the key-gated `/mcp` endpoint. False renders no `mcp` block and no loopback gateway, and the verify asserts `/mcp` is not served |
| `agw_mcp_hostname` | none | The hostname Caddy fronts `/mcp` on. Declared for the record: no gateway playbook reads it, the Caddy block and `platform/infra/cloudflare` carry the name |
| `agw_loopback_port` | `4002` | The loopback gateway's container port, never published; the MCP target calls `/v1` here |
| `agw_mcp_openapi_targets` | none | List of `{name, schema}`; `schema` is a `*.json` file in `deployment/mcp-schemas/`. Anything else on an entry is refused |
| `agw_client_policies.mcp-self.tokens_per_hour` | none | The aggregate hourly token cap on every model request made through MCP; required while `agw_mcp_enabled` is true, with `mcp-self` in `agw_clients` |
| `agw_openrouter_identities`, `agw_openrouter_admins` | empty, `false` | Who may call `provider: openrouter` models (`agentgateway-openrouter`); the union must not be empty while one is declared |

Declared admin list, as of this change: the list itself lives in site-config (private); this repository
holds only the local-dev placeholders. Declared retired list: `platform_retired_accounts`, also in
site-config, empty in this repository.

## A deploy recreates the gateway only when its inputs changed

A recreate drops every in-flight stream, and scheduled or imported runs call this deploy, so
`deploy.sh` leaves a running gateway alone unless something it runs on changed (gateway task
1.12, design decision 11). It hashes `config.yaml`, `.env`, the compose files in use (the
base, `compose.local.yml` in local-dev, every `COMPOSE_OVERLAYS` entry such as
`compose.tls.yml`) and every file under `./certs`, and compares that digest with the
`io.agent-cloud.inputs-sha256` label the running container was started with. It recreates
both containers (`up -d --force-recreate`) when the digest differs, when no gateway
container exists or it is stopped, when any of the project's containers (the gateway and its
Postgres, both on tags a pull can move) runs a different image than its tag now names, or when
readiness does not answer; otherwise it runs no `compose up` at all. An input it cannot list
or read fails the deploy by name rather than hashing what happened to be readable.
The label, not this run's render, is the record, so a run that fails after rendering is
converged by the next one. The last output line is `deploy-result: recreated (<reason>)` or
`deploy-result: unchanged`, and the playbook reports a change only for the first.

## The operator UI is read-only

The rendered `.env` sets `UI_READ_ONLY=true` whether or not the UI is enabled. In agentgateway
v1.5.0 that switches the config store to read-only (`crates/agentgateway/src/config.rs:390-392`):
the five UI write handlers answer 403 and the UI shows its read-only banner, while logs, budgets
and the playground keep working. `config.yaml` is rendered by the deploy and mounted `:ro`, so
it is the only source of gateway configuration; change it through inventory and a deploy.
