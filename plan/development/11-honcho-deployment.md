# 11 — honcho Deployment (memory.uhstray.io)

> **Depends on:** 00 (local-dev), 01 (secrets), 02 (sso-auth), 06 (skynet — honcho's eventual LLM)
>
> Part of the dependency-ordered `plan/development/` set. Read 00/01/02 first; this plan applies those conventions to a new service.

**Date:** 2026-07-07
**Status:** ACTIVE

**Context:** Onboard a new rootless-podman service through the composable pattern — **honcho** (ambient memory / identity backend for AI agents, `memory.uhstray.io`, prod VM `.190`) — full IPs live in site-config inventory. Local-dev first (`make local-deploy-*`), then promotion to the prod VM with Authentik SSO, a Caddy public subdomain, per-service SSH keys and the UFW firewall. honcho becomes the self-hosted memory backend for the **evolve** skill and team collaboration.

## Decisions (settled 2026-07-07)

| # | Decision | Rationale |
|---|---|---|
| D2 | **honcho = its own JWT auth** (`AUTH_USE_AUTH=true`) for the `/v3` API; **Authentik forward_auth only on the human `/docs`** | honcho's consumers (evolve, skills, agents) are non-interactive — a browser OIDC flow can't gate them; JWT + workspace/peer scoping is the native fit |
| D3 | **honcho LLM = interim external API (Gemini)** now, swap base-URL to skynet later | honcho's deriver/dialectic hard-require a tool-calling LLM; skynet isn't confirmed ready. The Gemini key is honcho-owned at `secret/services/honcho:gemini_api_key`, and Gemini supports function-calling. The base-URL/transport is per-feature config, so the skynet swap is a config change, not a redeploy |
| D4 | **honcho image = CI → GHCR pinned tag**, `deploy.sh` only pulls | honcho has no prebuilt image; building in CI keeps `deploy.sh` container-lifecycle-only (Rule #2) and gives reproducible pins |
| D5 | **VM .190 already provisioned + SSH-password reachable** | go straight to key-hardening; no Proxmox clone step |
| D6 | **honcho runs rootless podman** (validated 2026-07-07) | It needs neither the root socket / privilege / ports <1024; matches the platform default (Authentik is rootless). Rootless keeps aardvark-dns in the user netns (container DNS never crosses host UFW — sidesteps the bridge-DNS issue entirely) and publishes ports on INPUT (plain `ufw allow`, no route rules). Validated: `podman-machine-default` rootless=true resolves a sibling container by name via an in-netns gateway |

## Architecture

honcho sits in the Platform layer and is reached through the central Caddy. It authenticates API callers with its own JWTs and only defers `/docs` to Authentik.

```mermaid
flowchart TD
    U["Browser / API client"] --> CF["Cloudflare (DNS-01 TLS)"]
    CF --> CAD["Central Caddy .112"]

    CAD -->|"memory.uhstray.io /v3 (JWT)"| HAPI["honcho api :8000 (podman, .190)"]
    CAD -->|"memory.uhstray.io /docs (forward_auth)"| AK["Authentik .186"]
    CAD -.->|"/docs allowed"| HAPI

    subgraph HONCHO[".190 honcho stack (self-contained)"]
        HAPI --> HPG[("postgres + pgvector")]
        HDER["deriver worker"] --> HPG
        HAPI --> HRED[("redis cache")]
        HDER -->|"OpenAI-compatible"| LLM["Gemini now -> skynet later"]
    end

    EVOLVE["evolve skill (team)"] -->|"/v3 workspace/peer JWT"| HAPI
```

## Shared approach

Per `02-service-onboarding.md` and `00-foundation-local-dev.md`, honcho uses the same composable mechanism as every service ("one codebase, no forks"):

- **Service dir** `platform/services/<svc>/deployment/`: `deploy.sh` (lifecycle only — pull/up/wait-healthy, no secrets), `compose.yml`, `compose.local.yml` (SELinux `label=disable` + local port shifts), `templates/env.j2`, `CLAUDE.md`, `README.md`.
- **Playbook** `deploy-<svc>.yml` on the composable pattern: `manage-secrets` (define `_secret_definitions` + `_env_templates` + `_shared_reads`) → `deploy.sh` → health verify → (Caddy fragment only if `caddy_composable`; prod is flat, so Caddy is handled via `manage-caddy-sites.yml`). Plus `clean-deploy-<svc>.yml` via `tasks/clean-service.yml`.
- **Inventory in three files**: `platform/inventory/local-dev.yml.example` (local), `platform/inventory/production.yml` (public placeholders), `site-config/inventory/production.yml` (real IPs).
- **Semaphore templates**: add Deploy + Clean-Deploy to `platform/semaphore/templates.yml` (+ `templates-local.yml`), then run `setup-templates.yml`.
- **SSH + firewall order (verify-before-hardening — must not break access):**
  `store-ssh-password` → `generate-service-ssh-key` (ed25519 → OpenBao `secret/services/ssh/<svc>`, backed up to `site-config/secrets/ssh/<svc>/`) → `distribute-ssh-keys` (password still ON) → **verify key auth from Semaphore AND locally** → `harden-ssh` (disable password, NOPASSWD sudo, re-verify) → `apply-firewall` (**rootless**: leave `firewall_rootful` false; `firewall_detect_ports` + `firewall_upstream_source: <central-caddy-host>` allow only the published port from the Caddy host on the INPUT chain). No route rules and no `firewall_allow_bridge_dns` — rootless podman runs aardvark-dns in the *user* netns, so container↔container name resolution (honcho api↔postgres↔redis) never crosses the host UFW, and rootless publishes ports via a host-side proxy on INPUT (a plain `ufw allow` reaches them).
- **Local-dev first**: `make local-deploy-<svc>` brings each up with wildcard DNS (`*.agent-cloud.test`) + the step-ca wildcard cert + a `caddy_routes` entry; validate before promoting.

## Service — honcho

- **Classification:** AI/heavy — 4 containers with its own datastore + a worker → full multi-phase deploy. **Runtime:** podman. **VM .190:** heavier (≈4 vCPU / 8 GB / 40 GB; Postgres+pgvector + deriver).
- **Containers (self-contained stack):** `api` :8000 (GHCR image), `deriver` (same image, worker, no port), `database` `docker.io/pgvector/pgvector:pg15` (internal :5432), `redis` `docker.io/redis:8.2` (internal :6379). honcho brings its **own** Postgres — not the platform Postgres. **Health:** `GET /health` (+ a `POST /v3/workspaces` smoke test for real readiness).
- **Image (D4):** a CI workflow builds honcho from source (`github.com/plastic-labs/honcho`) → `ghcr.io/uhstray-io/honcho:<pinned>`; `deploy.sh` pulls it for `api`+`deriver`. Pin the upstream commit/tag.
- **`templates/env.j2`:**
  - `DB_CONNECTION_URI=postgresql+psycopg://honcho:{{ secrets.honcho_db_password }}@database:5432/honcho`
  - `CACHE_ENABLED=true`, `CACHE_URL=redis://redis:6379/0?suppress=true`
  - `AUTH_USE_AUTH=true`, `AUTH_JWT_SECRET={{ secrets.honcho_jwt_secret }}`
  - **LLM (D3 — Gemini interim):** `LLM_GEMINI_API_KEY={{ secrets.gemini_api_key }}`; set each **text-gen** feature to `..._MODEL_CONFIG__TRANSPORT=gemini` + a `..._MODEL_CONFIG__MODEL=<gemini function-calling model>` (deriver, summary, dream, each `DIALECTIC_LEVELS__*`). **Embeddings stay OFF** — `EMBED_MESSAGES=false` and the embedding transport is left unconfigured (no OpenAI key here); the env template enforces this. **Skynet swap later:** flip transport to `openai` + `..._OVERRIDES__BASE_URL=<skynet /v1>` + `..._OVERRIDES__API_KEY_ENV=SKYNET_API_KEY` (config-only; models must support tool calling), then re-enable embeddings and set `EMBEDDING_VECTOR_DIMENSIONS` to match the served model.
- **Secrets (`_secret_definitions`):** `honcho_jwt_secret` (random), `honcho_db_password` (random, 32). **Existing secret, no shared read:** `gemini_api_key` lives at `secret/services/honcho:gemini_api_key` (type `existing`, required; placed there with Seed OpenBao Key). **Team JWTs** (workspace/peer-scoped) are minted post-deploy (`generate_jwt.py --workspace` or `POST /v3/keys`) and stored under `secret/services/honcho/tokens/<member>` — this is the evolve/team-provisioning step (phase 4), not the deploy.
- **Auth (D2):** `AUTH_USE_AUTH=true`; team access = workspace/peer-scoped JWTs. **Tenancy for the team:** one workspace (e.g. `uhstray`), each teammate = a **peer** (stable id), a **session** per thread — gives shared-yet-isolated memory.
- **Caddy:** `memory.uhstray.io` → `honcho-api:8000`, Cloudflare DNS-01. **Path-scoped forward_auth:** a matcher for `/docs`, `/redoc`, `/openapi.json` (+ `/outpost.goauthentik.io/*`) runs Authentik forward_auth; `/v3/*` proxies straight through (JWT-authenticated). Requires an Authentik `proxyprovider` blueprint (`honcho-docs`, `mode: forward_single`, `external_host: memory.uhstray.io`) in `app-catalog.yml` as `type: forward_auth`, plus the path-matched block in `caddy_managed_sites`.
- **evolve hook (phase 4):** evolve points at `https://memory.uhstray.io/v3` with a workspace-scoped JWT; team members are peers in the shared workspace.

## Phase 1 — local-dev ⬜

For honcho:
1. Create `platform/services/<svc>/deployment/` (`deploy.sh`, `compose.yml`, `compose.local.yml`, `templates/env.j2`, `CLAUDE.md`, `README.md`).
2. Write `deploy-<svc>.yml` + `clean-deploy-<svc>.yml` (composable pattern).
3. Add the `<svc>_svc` group to the working local inventory + `local-dev.yml.example` (`ansible_host: 127.0.0.1`, `ansible_connection: local`, `service_name`, `monorepo_deploy_path`, `container_engine: podman`, `<svc>_port`).
4. Add the `caddy_routes` entry (`/docs` forward_auth vs `/v3` direct) and re-run `make local-deploy-caddy`.
5. Add the `honcho-docs` forward_auth blueprint + catalog entry.
6. Add `Deploy <svc> (Local)` to `templates-local.yml`; `make local-bootstrap` (picks up templates), then `make local-deploy-<svc>` → `make local-validate`.
7. Acceptance: `POST /v3/workspaces` returns an id (DB+migrations OK), a JWT-authenticated `/v3` call works, `/docs` prompts Authentik. **honcho note:** the deriver needs the real Gemini key even locally to actually derive; seed `secret/services/honcho:gemini_api_key` into local OpenBao or accept "messages stored, not derived" for local acceptance.

**Gate:** honcho green in local-dev before any prod action.

## Phase 2 — prod promotion ⬜

Through Semaphore (Rule #1), on a feature branch → dev → main, branch-deploying to the VM:
1. **Inventory (site-config):** add the host under `virtualmachines` (`.190` + `service_port`) and the `<svc>_svc` group with `service_name`, `service_url`, `health_path`, `monorepo_deploy_path`, `container_engine: podman`, plus Caddy + firewall vars (`firewall_ssh_cidrs`, `firewall_upstream_source: <central-caddy-host>`; leave `firewall_rootful` false — rootless).
2. **SSH hardening (verify-before-hardening):** `store-ssh-password` → `generate-service-ssh-key` (→ OpenBao + back up to `site-config/secrets/ssh/<svc>/`) → `distribute-ssh-keys` → **verify key auth (Semaphore + local)** → only then `harden-ssh` → re-verify. Abort lockdown on any verify failure.
3. **Firewall:** `apply-firewall` with the vars above — rootless publishes the port on the host INPUT chain, so `firewall_detect_ports` + the Caddy-host allow is all that's needed (no route rules, no bridge-DNS rule). honcho publishes **only** the api :8000; its postgres/redis stay internal to the rootless network (no host port, no firewall rule).
4. **Authentik:** add `honcho-docs` (forward_auth) to `authentik_apps` in site-config inventory; run Deploy Authentik (renders blueprints + bindings).
5. **Deploy:** run the Deploy honcho template (branch survey var). honcho also needs the GHCR image built+pushed by CI first.
6. **Caddy + public DNS:** create the Cloudflare A record for `memory.uhstray.io` (Cloudflare-side, outside the playbooks); append the site block to `caddy_managed_sites` (path-scoped forward_auth + `/v3` direct) and run `manage-caddy-sites.yml` (validates before load, restarts).
7. **Verify:** JWT `/v3` + gated `/docs` (honcho), health endpoints, and re-confirm SSH key-only + firewall via Validate All.

## Phase 4 — evolve (honcho team memory) — update `huhhb/docs/evolve-plan.md` ⬜

- Point evolve at the **self-hosted** honcho: `base_url=https://memory.uhstray.io`, API `/v3`, auth = workspace/peer-scoped **JWT** (not OIDC).
- **Team collaboration model:** one shared workspace (`uhstray`); each team member is a **peer** (stable peer id); a **session** per conversation/thread. Mint a per-member workspace-scoped JWT (`generate_jwt.py --workspace uhstray` or `POST /v3/keys`), store under `secret/services/honcho/tokens/<member>`, and hand it to that member's evolve config. Cross-session peer representation gives each member durable, shared-yet-scoped memory.
- Note the interim Gemini LLM (D3) and the later skynet swap; note honcho's `/docs` is Authentik-gated while `/v3` is JWT.

## Sequencing, gates, risks

- **Order:** local-dev honcho → (gate) → prod honcho → evolve-plan update (honcho's LLM, CI-built image and 4 containers).
- **Gates:** local-dev green before prod; SSH key-auth verified before password lockdown (do **not** break access); CodeRabbit reviews the `dev → main` promotion; prod deploys run through Semaphore.
- **Risks:** (1) honcho deriver needs a **tool-calling** LLM — verify Gemini function-calling against honcho before relying on derivation. (2) honcho image must be CI-built to GHCR before the prod deploy. (3) VM reachability/size — confirm `.190` responds on the default password and have headroom (honcho). (4) Rootless boot persistence: enable `loginctl enable-linger` for the service user so rootless containers auto-start on reboot (with `restart: always` in compose) — the rootless replacement for the rootful systemd concern. (5) Public `*.uhstray.io` DNS records are a Cloudflare-side step outside the playbooks.

## Open items

- [ ] Confirm skynet readiness + tool-calling; schedule the Gemini→skynet base-URL swap for honcho.
- [ ] Add a CI workflow to build+push `ghcr.io/uhstray-io/honcho`.
- [ ] Decide honcho VM sizing / whether Postgres backups need the o11y/backup path.
- [ ] Reconcile the site-config Semaphore compose drift (separate; from the prior task) so future compose syncs are safe.
- [ ] Ship honcho's prod Caddy block as a repo-side `platform/services/honcho/deployment/templates/caddy-site.j2` (mirroring the path-scoped `forward_auth` from `Caddyfile.local.j2` — the header-strip + `@gated /docs*` block) rather than free-handing that security-sensitive block into site-config `caddy_managed_sites`; wire behind `caddy_composable`.
