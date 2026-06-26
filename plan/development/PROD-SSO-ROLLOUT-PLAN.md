# Production SSO Rollout Plan — Authentik for Semaphore, Proxmox, NetBox (+ Grafana)

Status: IN PROGRESS (2026-06-25). Owner: platform. Supersedes nothing; extends
`plan/development/AUTH-SSO-DEPLOYMENT.md` with the prod promotion + composability.

## Goal

Authenticate into and access the key production services via Authentik SSO at
`https://auth.uhstray.io`: **Semaphore, Proxmox (pve), NetBox** now; **Grafana
(o11y.uhstray.io)** next. Production Authentik must show **only services that are
actually in production**, and promoting a service from local-dev → prod must be a
one-line, composable change.

## Hard constraints (NON-NEGOTIABLE)

- **No destructive actions on production machines.** Additive, reversible only.
- **No changes to existing user credentials.**
- **Proxmox per-user TOTP must keep working.** OIDC is added as a *separate
  realm*; the existing PAM/PVE realms + their TOTP are never touched.
- Production deploys go through Semaphore from a repo branch (branch-deploy to
  test, then `feat → dev → main`). Gate each prod step explicitly.

## Decisions (confirmed 2026-06-25)

1. **Composable prod-tag = manifest list.** `authentik_apps` (per-environment
   list of enabled app slugs). Local-dev = all; prod (site-config) = the prod
   subset. Promote = add a slug. (Chosen over per-blueprint labels / split dirs.)
2. **Rollout order:** composable mechanism → **Semaphore** (already OIDC-proven
   locally, lowest risk) → NetBox → Proxmox (riskiest last) → Grafana/o11y later.
3. **Split-horizon DNS: DEFERRED.** Keep the current Cloudflare path. `*.uhstray.io`
   resolves to Cloudflare proxy IPs today; SSO works over that path (DNS-01 LE
   certs are independent of resolution). Revisit pfSense Unbound overrides
   (`*.uhstray.io → the Caddy host`) only if internal latency / real-client-IP
   audit becomes a concern. (Researched: recommended-but-not-required.)

## The composable mechanism (manifest-driven blueprint selection)

Problem today: all 14 committed Authentik blueprints apply unconditionally in
BOTH local and prod, so prod shows apps for services not in prod (erpnext, n8n,
grafana, openbao…) + local test users. No environment gating exists.

Design:

- **App catalog** (`deployment/app-catalog.yml`, committed): every possible app
  → `{ file, type: oidc|forward_auth, tier: member|admin }`.
- **`authentik_apps`** (inventory list): which catalog slugs are enabled in this
  environment. Local-dev inventory = all; site-config (prod) = the prod subset.
  Unset ⇒ defaults to all (local-dev unchanged).
- **`deploy-authentik` assembles `blueprints-active/`** (gitignored) per deploy:
  the always-shared blueprints (`agent-cloud`, `platform-groups`,
  `agent-cloud-admin`, `stray-admin`, `service-account`) + each enabled app's
  blueprint file from the `blueprints/` library, and renders
  `zz-sso-bindings.yaml` from the enabled set (outpost provider list = enabled
  forward_auth apps; one PolicyBinding per enabled app at its tier). compose
  mounts `blueprints-active/` (not the full `blueprints/` library).
- **Promote** = add the slug to prod `authentik_apps`; next deploy includes it.
  The rendered bindings never reference a non-existent provider/app (fixes the
  `!Find` failure that listing-all would cause in prod).

Per-OIDC-app **redirect URIs are parameterized** (`!Env [<SVC>_REDIRECT_URI,
"<local default>"]`), set per-environment from `env.j2` (local `*.agent-cloud.test`
vs prod `*.uhstray.io`) — same pattern already used for `AUTHENTIK_BROWSER_HOST`.

## Per-service auth (all native OIDC; never forward_auth — keeps APIs/CLIs working)

- **Semaphore** (`semaphore.uhstray.io`): native OIDC via `SEMAPHORE_OIDC_PROVIDERS`
  / `config.json` `oidc_providers`. Redirect
  `https://semaphore.uhstray.io/api/auth/oidc/authentik/redirect/`. Local admin
  fallback retained; admin promoted once. Blueprint `semaphore-oidc.yaml` exists.
- **NetBox** (`netbox.uhstray.io`): move forward_auth → **native OIDC**
  (community NetBox bundles `python-social-auth` generic OIDC). `configuration.py`:
  `REMOTE_AUTH_BACKEND = social_core.backends.open_id_connect.OpenIdConnectAuth`,
  `SOCIAL_AUTH_OIDC_OIDC_ENDPOINT = https://auth.uhstray.io/application/o/netbox`,
  key/secret, `SOCIAL_AUTH_REDIRECT_IS_HTTPS=True`. Redirect
  `https://netbox.uhstray.io/oauth/complete/oidc/`. Preserves REST API/token auth
  (Diode/orb-agent). New `netbox-oidc.yaml` blueprint (OIDC provider, not proxy).
- **Proxmox** (`pve.uhstray.io`): **add an OIDC realm** —
  `pveum realm add authentik --type openid --issuer-url
  https://auth.uhstray.io/application/o/proxmox/ --client-id … --client-key …
  --username-claim username --autocreate 1`. Additive + reversible
  (`pveum realm delete authentik`). **PAM/PVE realms + TOTP untouched** (OIDC is a
  separate realm; 2FA on the OIDC path is delegated to Authentik). Caddy =
  **TLS reverse-proxy only** (`tls_insecure_skip_verify` to the self-signed :8006,
  forward WS for noVNC); **NO forward_auth** (would break the API + console).
  New `proxmox-oidc.yaml` blueprint. Group→PVE-role ACLs mapped from a
  `proxmox-admins` Authentik group.
- **Grafana** (`o11y.uhstray.io`, later): generic OAuth (`GF_AUTH_GENERIC_OAUTH_*`),
  `role_attribute_path` mapping `platform-admins→Admin / platform-developers→Viewer`,
  `allow_assign_grafana_admin`. Blueprint `grafana-oidc.yaml` exists.

## o11y / monitoring (future phase)

Keep the existing composable `platform/services/o11y` (Grafana+Prometheus+Loki+
Alloy; OIDC-ready, OpenBao, Caddy) as the chassis. **Harvest** components/configs
from `github.com/uhstray-io/o11y` (do NOT adopt its deploy model — hardcoded
creds, `:latest`, a committed Discord webhook). Phases:
- 2a infra metrics: node-exporter + cAdvisor + Caddy metrics scrape + alert rules.
- 2b homelab breadth: `prometheus-pve-exporter` (Proxmox; token already in
  OpenBao `secret/services/proxmox`), `snmp_exporter` (pfSense/switches),
  `blackbox_exporter` (HTTP/ICMP/cert probes of service URLs from inventory),
  per-VM node-exporter/Alloy agents via Ansible.
- 2c tracing/profiling (Tempo/Pyroscope/MinIO) only when an app needs it.
Then Grafana OIDC at `o11y.uhstray.io` via the mechanism above.

## Rollout sequence + gates

1. **Mechanism** (this branch): catalog + `authentik_apps` + `blueprints-active/`
   assembly + templated `zz-sso-bindings`. Validate locally (render local-vs-prod;
   lint). PR `feat → dev`.
2. **Prod Authentik prune + Semaphore SSO**: set prod `authentik_apps` to the prod
   set; parameterize semaphore redirect; branch-deploy Authentik to `.186`
   (verify it shows ONLY prod apps + `ak healthcheck` + outpost), then wire
   Semaphore prod OIDC; verify SSO login (local admin fallback intact). Gate.
3. **NetBox OIDC** (forward_auth → OIDC), verify API/token auth + SSO. Gate.
4. **Proxmox OIDC realm** (additive), verify: existing PAM/PVE login + **TOTP**
   still work, API tokens work, noVNC console works, OIDC login works. Gate.
5. **Grafana/o11y** monitoring expansion + OIDC. Later.

Each prod step: branch-deploy → verify → `feat → dev → main` PR → revert Semaphore
repo to `main`. No credential changes. Nothing destructive.
