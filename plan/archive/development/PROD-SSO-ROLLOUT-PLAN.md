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
- **Snapshot before every prod upgrade.** Standard practice: take a Proxmox
  snapshot of the target VM, **wait for it to finish, and validate it exists**
  *before* the upgrade — the catastrophic-failure rollback point. Reusable
  mechanism: `platform/playbooks/snapshot-vm.yml` (create → wait → validate; via
  Semaphore with OpenBao creds, or operator-side with env creds). The upgrade
  aborts if the snapshot can't be validated.

## Decisions (confirmed 2026-06-25)

1. **Composable prod-tag = manifest list.** `authentik_apps` (per-environment
   list of enabled app slugs). Local-dev = all; prod (site-config) = the prod
   subset. Promote = add a slug. (Chosen over per-blueprint labels / split dirs.)
2. **Rollout order:** composable mechanism → **Semaphore** (already OIDC-proven
   locally, lowest risk) → NetBox → Proxmox (riskiest last) → Grafana/o11y later.
3. **Split-horizon DNS: DEFERRED, with one scoped exception (found in testing).**
   Browsers reach `*.uhstray.io` fine via Cloudflare. But **server-side** calls
   from a service to `auth.uhstray.io` (OIDC discovery + token exchange) get
   Cloudflare's bot-challenge (verified: `cf-mitigated: challenge`). So each
   OIDC *client* must resolve `auth.uhstray.io` to the internal Caddy — done
   per-container via compose `extra_hosts` (Authentik derives the issuer from the
   Host header, so tokens stay `https://auth.uhstray.io/...` and still validate).
   The broader pfSense Unbound split-horizon (`*.uhstray.io → Caddy host`) remains
   deferred — `extra_hosts` covers the server-side need without it.

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

## Per-service auth — target: native OIDC over forward_auth (keeps APIs/CLIs working)

The intended direction is native OIDC per service, not forward_auth, because OIDC
leaves each service's API/CLI/token auth intact. Some services start on forward_auth
and migrate (e.g. NetBox below); the goal state is native OIDC where the service
supports it.

- **Semaphore** (`semaphore.uhstray.io`): native OIDC via the
  `SEMAPHORE_OIDC_PROVIDERS` env var. Redirect
  `https://semaphore.uhstray.io/api/auth/oidc/authentik/redirect` (no trailing
  slash — byte-matches the parameterized `semaphore-oidc.yaml` blueprint). Local
  admin fallback retained (OIDC only ADDS a provider; OIDC-only is never forced).
  **Applied from the operator/genesis layer, NEVER via a Semaphore job** —
  Semaphore is the control plane; a job that restarts its own container is
  circular (same reason `make local-bootstrap` brings it up last from outside).
  Mechanism (site-config `scripts/semaphore-upgrade.sh` — a REUSABLE safe-upgrade
  tool, `CHANGE=oidc|image|restart`): an additive `compose.override.yml` injects
  only the env (+ `extra_hosts` for internal issuer resolution) — `compose.yml`/
  `entrypoint.sh`/`config.json` are untouched, so `access_key_encryption`
  (decrypts Semaphore's stored SSH keys) is never at risk. The envelope is the
  reusable part: pre-flight gates (valid OIDC JSON; the container's internal path
  to the issuer resolves; Semaphore healthy) → **pre-upgrade Proxmox snapshot
  (create/wait/validate)** → stage → recreate → post-flight verify (`/api/ping` +
  provider login route redirects) with **auto-rollback** on failure;
  rollback = restore the previous override + recreate.
  The OIDC map mirrors the local-genesis shape in `bootstrap-local-dev.yml`.
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
   set; parameterize semaphore redirect; branch-deploy Authentik to the Authentik
   VM (verify it shows ONLY prod apps + `ak healthcheck` + outpost — DONE).
   Then wire Semaphore prod OIDC with `semaphore-upgrade.sh apply` (operator-side;
   snapshot+validate → env-override → restart → verify → auto-rollback);
   verify SSO login (local admin fallback intact). Gate.
3. **NetBox OIDC** (forward_auth → OIDC), verify API/token auth + SSO. Gate.
4. **Proxmox OIDC realm** (additive), verify: existing PAM/PVE login + **TOTP**
   still work, API tokens work, noVNC console works, OIDC login works. Gate.
5. **Grafana/o11y** monitoring expansion + OIDC. Later.

Each prod step: branch-deploy → verify → `feat → dev → main` PR → revert Semaphore
repo to `main`. No credential changes. Nothing destructive.

## GitHub Actions path (future, for bootstrapping/upgrading Semaphore)

Semaphore can't safely upgrade itself (a self-restarting job is circular), so today
the operator runs `semaphore-upgrade.sh` from a workstation. A more consistent
future home for that **same envelope** is a GitHub Actions workflow on a
**self-hosted runner** inside the network (the runner reaches the Semaphore
host/Proxmox/OpenBao; secrets via GitHub Environments or OpenBao):
- The job runs `semaphore-upgrade.sh` (or the `snapshot-vm.yml` + override logic
  directly) — identical gates: pre-flight → **snapshot+validate** → apply →
  verify → auto-rollback. No logic forks; the runner just replaces the laptop.
- Gains: consistent environment, audited/approved runs (GH Environments + required
  reviewers), one button, runs even when no operator is at a workstation.
- Constraints unchanged: never via Semaphore itself; additive + reversible;
  snapshot first. Keep the bash tool as the source of truth the workflow calls,
  so local and CI paths stay identical.
