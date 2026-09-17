# agentgateway as the inference edge gateway in front of the DGX Spark vLLM API

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Explored 2026-09-14 from the dgx-spark
document `docs/dgx-inference-telemetry-benchmark-gateway-ecosystem.md`; gateway choice
by Joe, 2026-09-14.

Boundary (Joe, 2026-09-14): the two DGX Spark machines and the vLLM API are dgx-spark's
for now; everything outside them is agent-cloud's. The gateway is outside. Companion
changes: agent-cloud `inference-telemetry-production` (ordered before this one; the
gateway's signals land in that stack) and dgx-spark
`node-telemetry-and-placement-benchmark` (the upstream's exporters and the roadmap
record for moving the vLLM API into this estate later). Related, already proposed:
`inference-edge-cloudflare-controls` (edge rate limit and origin lockdown, unchanged by
this).

## Why

Caddy proxies `inference.uhstray.io/v1` straight to vLLM on the head node. Everything
the team needs between the edge and the model is therefore either missing or improvised:

- **One shared key for everyone.** vLLM's `--api-key` is a single credential; a leak
  rotates it for the whole team, and no per-client limit or budget is possible. The
  edge rate limit in `inference-edge-cloudflare-controls` is keyed on source address
  because that is all Cloudflare can see.
- **No request-level telemetry outside vLLM.** First-token latency, inter-token gaps,
  failures and cancellations as the client sees them are measured today by hand-run
  load tests. The ecosystem document lists this layer as its own row.
- **One upstream, hard-wired.** The placement benchmark's two-replica cell, a second
  served model, or the future move of the vLLM API into this estate all need a routing
  layer that Caddy's single `reverse_proxy` line does not give.
- **Agent traffic is arriving.** The platform's agents (skynet's sub-agent roles,
  WisBot, OpenCode and pi sessions) call the endpoint through OpenAI-compatible SDKs,
  and MCP tool traffic is on the roadmap; a gateway that speaks those protocols
  natively is the shape the ecosystem document asks the shortlist to be judged by.

The ecosystem document listed four candidates and required the skynet boundary to be
settled first. Joe settled it on 2026-09-14: **skynet is not a general inference
gateway.** It is the platform's own model-serving gateway, purpose-built to interface
with agent-cloud and orchestrate placement and policy across the platform. agentgateway
is the inference edge in front of the DGX Spark vLLM API: authentication, per-key
limits, routing to one or more upstreams, and request telemetry. The two are distinct
authorities with distinct concerns and do not replace each other. The architecture
record in this change writes that down so plan 06 is amended, not superseded.

## What Changes

- **agentgateway v1.5.0 as a platform service.** Image
  `cr.agentgateway.dev/agentgateway:v1.5.0` (Apache-2.0, Linux Foundation), config-as-
  code YAML passed with `-f`, gateway port 4000, admin on the container loopback only.
  Onboarded through the service checklist as an Infrastructure-tier service on its own
  VM: compose, `deploy.sh`, `env.j2`, playbook, BATS, inventory group, Semaphore
  templates. **Amended 2026-09-17:** the service carries its OWN internal-only Postgres
  (`agentgateway-db`), because v1.5.0 charges per-API-key token budgets only when
  `config.database` is set (verified against the binary); the database holds budget usage
  and request METADATA rows, no prompt payloads. The rendered config carries no plaintext
  credential — the upstream key and the database URL are environment references, client
  keys are enrolled as sha256 hashes.
- **Operator UI behind SSO (Joe, 2026-09-17).** The gateway's built-in UI has no login of
  its own and dumps its config unauthenticated, so it is served on its own listener (:4001,
  `ui.gateways`) and reached ONLY through Caddy with an admin-tier Authentik forward_auth
  gate at `admin.inference.uhstray.io` (prod) / `admin.inference.agent-cloud.test`
  (local-dev). The admin interface itself stays on the container loopback. A nested
  hostname, so the local internal wildcard cert gains a `*.inference.<zone>` SAN, derived
  from the route table by `deploy-caddy.yml`; prod gets a proxied Cloudflare A record in
  `dns.tf`.
- **vLLM as a `custom` provider backend.** The Qwen served name maps to
  `baseUrl: http://<head>:8000/v1` with the vLLM API key injected from OpenBao; the
  gateway's own `/v1` speaks the same OpenAI shape the team already uses, so client
  base URLs do not change.
- **Caddy routes the inference hostname to the gateway.** The `inference_api` route's
  upstream becomes the gateway host and port; its Bearer 401 stays as defence in depth
  (a shape check on the header, so any enrolled client key passes it and identity is
  decided at the gateway); the path allowlist stays. The `/metrics` and control routes on vLLM that were
  LAN-readable become gateway-host-only once the node firewall's API CIDR narrows to the
  gateway (dgx-spark side, coordinated).
- **Per-client keys and limits at the gateway.** `apiKey` policy (`mode: strict`) with
  one key per team member or agent role, generated once into OpenBao by the deploy
  (`client_<name>` fields on the service's secret path, reused on every run; rotation is
  an explicit step) and enrolled as sha256 hashes. **Amended 2026-09-17:** v1.5.0 has no
  per-identity REQUEST bucket under the `llm` shortcut (the bucket-key field is
  unreleased, the `conditional` form is rejected there), so per-identity control is a
  per-key hourly TOKEN budget (`Block` on exceed, backed by the service's Postgres) and
  one GLOBAL request bucket protects the upstream's measured ceiling. The shared vLLM
  key becomes an internal credential the gateway holds.
- **Gateway telemetry into the o11y stack.** `config.tracing.otlpEndpoint` to Alloy's
  OTLP receiver (added on the o11y host by this change, the first trace producer);
  metrics from the gateway's stats endpoint scraped by Prometheus; the inference
  dashboard gains the client-view latency row.
- **Architecture record: agentgateway alongside skynet.** Written as an accepted record
  once Joe confirms the text; plan 06 gains a dated amendment line.

**BREAKING** for direct LAN callers of vLLM only once the node firewall narrows (a
dgx-spark change, sequenced after this one proves the gateway path). Public clients
see no change of base URL.

## Capabilities

### New Capabilities
- `platform/inference-gateway`: the authenticated, metered, observable inference edge
  in front of the DGX Spark vLLM API, alongside skynet.

### Modified Capabilities
- `platform/inference-edge` (from `inference-edge-cloudflare-controls`): the Caddy
  route's upstream becomes the gateway; the origin lockdown and rate limit are unchanged.

## Impact

- Files: `platform/services/agentgateway/deployment/{compose.yml, compose.local.yml,
  deploy.sh, templates/env.j2, templates/config.yaml.j2, .gitignore, README.md}`,
  `platform/services/agentgateway/context/architecture.md`,
  `platform/playbooks/deploy-agentgateway.yml`, `clean-deploy-agentgateway.yml`,
  `platform/tests/test_service_agentgateway.bats`, `platform/inventory/local-dev.yml.example`
  (service group + the `admin.inference` Caddy route), `bootstrap-local-dev.yml` (the
  control plane's static inventory + its Caddy route table), `templates.yml` +
  `templates-local.yml`, Authentik `app-catalog.yml` + `agentgateway-forward-auth.yaml` +
  `env.j2`, `deploy-caddy.yml` + `tasks/mint-internal-cert.yml` (nested-host SANs),
  `platform/infra/cloudflare/dns.tf` (`admin.inference` record),
  `Caddyfile.local.j2` (`inference_api` upstream from inventory, unchanged shape),
  o11y `config.alloy` (OTLP receiver) and `scrape.d`, `plan/development/06-inference-skynet.md`
  amendment, `plan/architecture/` record, `docs/MISTAKES.md` 2.21,
  `plan/architecture/02-service-onboarding.md` Known Gaps (IPAM lookup).
- site-config (branch `feat/agentgateway-host`): vm-specs vmid 216; `agentgateway_svc` group
  (upstream, identities, limit figures, firewall vars, UI bind); `admin.inference.uhstray.io`
  forward_auth block in `caddy_managed_sites`; `agentgateway` in `authentik_apps` +
  `agentgateway_external_host`; later, the production inference block's upstream.
- OpenBao: `secret/services/agentgateway` with the vLLM upstream key (`existing`, seeded
  by a separate playbook), the budget database password and the minted `client_<name>` keys.
- Live: one new VM in the request path (two containers); one Caddy redeploy (plus the
  new UI block); one Cloudflare apply (new record); one Authentik redeploy (new app); one client-key rollout to
  team members and agents (the old shared key keeps working at the gateway for a
  dated grace period, then is retired from client use).
- Out of scope, recorded: MCP and A2A routing (the gateway supports them; no consumer
  yet); the skynet-to-gateway relationship beyond "distinct authorities" (open question
  below); moving the vLLM API into this estate (dgx-spark roadmap record).

## Rollback Plan

- Route back (during the grace period): set the production Caddy block's upstream to the
  head node and redeploy Caddy through Semaphore; clients keep working with the shared
  key, which remains valid at vLLM throughout the grace period.
- Route back (after the shared key is retired and rotated): clients hold gateway keys
  that vLLM does not know, so a direct route is not one value. It is encoded as
  `rollback-inference-route.yml`, idempotent and re-runnable, with three modes:
  `-e mode=gateway-config` redeploys the previous rendered gateway config (default, the
  gateway stays in the path); `-e mode=direct` publishes the current vLLM key into each
  enrolled client's OpenBao secret path (the same channel the client keys use), sets the
  Caddy upstream to the head node and redeploys Caddy; `-e mode=restore` sets the upstream
  back to the gateway, redeploys Caddy, rotates the vLLM key at vLLM and in OpenBao, and
  removes the published copies. Each mode converges when re-run; none is a manual step.
- Stop the gateway: Semaphore stop template; nothing on the nodes changes.
- Remove: `clean-deploy-agentgateway.yml` (destroys both containers and the budget
  volume — the only state lost is every identity's current budget window); OpenBao client
  keys revoked; the architecture record stays with status `Rejected` if the approach is
  abandoned.
- The node firewall narrowing is a separate dgx-spark change and is not sequenced until
  the gateway path has served for a review period, so no rollback here touches ufw.
