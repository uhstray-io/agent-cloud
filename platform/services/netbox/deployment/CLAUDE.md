# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This directory contains a NetBox deployment configuration for the Uhstray.io platform infrastructure. NetBox is an infrastructure resource modeling (IPAM/DCIM) tool that runs as a containerized application.

The deployment supports both **Podman** and **Docker** as container runtimes (auto-detected by `lib/common.sh`; prefers Podman when both are available). It builds on the official [netbox-docker](https://github.com/netbox-community/netbox-docker) project (located in `netbox-docker/`) with local configuration overrides in the root directory.

## Architecture

### Service Stack

The deployment consists of 12 compose-managed containers (11 services + 1 one-shot migration) plus 1 standalone privileged agent:

**NetBox Core:**
- **netbox**: Main application server (port 8000 → container 8080). Custom image built from `Dockerfile-Plugins` with the `netboxlabs-diode-netbox-plugin`. Healthcheck verifies `/login/` responds and no pending migrations exist. The `netbox-to-diode` client secret is mounted at `/run/secrets/netbox_to_diode` for the Diode plugin. The Diode plugin requires three settings in `PLUGINS_CONFIG` (see `configuration/plugins.py`): `diode_target_override` (reads `DIODE_TARGET_OVERRIDE` env var), `diode_username`, and `netbox_to_diode_client_secret` (reads from `/run/secrets/netbox_to_diode`). The plugin derives the OAuth2 token URL from `diode_target_override` — without it, the plugin defaults to `grpc://localhost:8080/diode` and tries to reach `http://localhost:8080/diode/auth`, which hits NetBox's own HTTP server and returns 404.
- **netbox-worker**: Background task processor using RQ (Redis Queue). Depends on netbox being healthy. `restart: unless-stopped` so it recovers if it starts before migrations complete.

**Backing Services:**
- **postgres**: PostgreSQL 18 (Alpine). Shared by NetBox, Diode, and Hydra. Three databases (`netbox`, `diode`, `hydra`) created by `discovery/init-db.sh` on first startup. Data persisted in `netbox-postgres` volume.
- **redis**: Valkey 9.0 (Alpine). Task queue for NetBox. Password via compose variable substitution (`${REDIS_PASSWORD}` from `.env`). Append-only persistence in `netbox-redis-data` volume.
- **redis-cache**: Valkey 9.0 (Alpine). Application cache for NetBox. Password via `${REDIS_CACHE_PASSWORD}` from `.env`. No append-only (cache-only). Data in `netbox-redis-cache-data` volume.

**Discovery (Diode) Pipeline:**
- **ingress-nginx**: Nginx reverse proxy exposing gRPC/HTTP for the Diode pipeline (port 8081 → container 80). Uses `auth_request` to validate OAuth2 tokens on gRPC requests via diode-auth's `/introspect` endpoint. The `/diode/auth` path is unauthenticated (token endpoint).
- **diode-ingester**: Processes incoming discovery data via Redis streams. Distroless image (no shell/healthcheck possible).
- **diode-reconciler**: Reconciles discovered data against NetBox objects. Distroless image. Connects to both diode-redis and postgres.
- **diode-auth**: OAuth2 token management for Diode. Has `sh`/`wget`/`nc` — healthcheck via `nc -z localhost 8080`. OAuth2 clients are registered post-deploy by `deploy.sh` step 13.
- **hydra**: Ory Hydra v25.4.0 OAuth2/OIDC provider. DSN uses compose variable substitution from `.env`. Config mounted from `discovery/hydra.yaml`. **Critical:** Hydra v25 ignores `HYDRA_*` environment variables from `env/discovery.env` for several settings — they must be set directly in `discovery/hydra.yaml` instead (see "Hydra Configuration" section below).
- **hydra-migrate**: One-shot container (`restart: "no"`) that runs Hydra database migrations then exits.
- **diode-redis**: Dedicated Redis Stack for Diode (port 6378). Uses `redis/redis-stack-server` for RedisJSON, RediSearch, and RedisGraph modules required by the reconciler. Separate from NetBox Redis. Password from `env/discovery.env`.
- **orb-agent**: NetBox Labs Orb Agent for network and SNMP discovery. **Not compose-managed** — runs as a standalone privileged container via `sudo $CONTAINER_ENGINE run --privileged --net=host`. This gives it `CAP_NET_RAW` for fast SYN scans and ICMP host discovery, which rootless compose containers cannot provide. Container name is fixed as `netbox-orb-agent`. Runs two discovery backends: `network_discovery` (nmap subnet scans) and `snmp_discovery` (SNMP device enrichment with custom OID lookup via `discovery/snmp-extensions/`). Config from `discovery/agent.yaml` (site-specific, gitignored). Diode credentials created automatically by `deploy.sh` step 14 via the Diode plugin API. SNMP community string is read from `secrets/snmp_community.txt` (user-managed). For environments where sudo is unavailable, see `discovery/agent.yaml.rootless.example` for a TCP connect scan fallback.

### Script Architecture

All shell scripts share a common library to eliminate duplication:

```
lib/common.sh              ← shared functions (sourced by all scripts)
├── deploy.sh              ← sources common.sh
└── lib/generate-secrets.sh ← sources common.sh (via ROOT_DIR)
```

**`lib/common.sh`** provides:
- **Logging**: `info()`, `warn()`, `error()` — consistent output formatting
- **`sedi()`**: Cross-platform in-place sed (handles macOS BSD vs GNU)
- **`compose()`**: Wraps `$CONTAINER_ENGINE compose` with `--project-name netbox` and explicit `-f docker-compose.yml` to prevent auto-discovery of override files
- **`wait_for_healthy()`** / **`wait_for_running()`**: Poll `compose ps --format json` with a Python parser that handles both podman (Labels/Names array) and docker (Service/Name string) JSON formats
- **`wait_for_completed()`**: Polls `$CONTAINER_ENGINE inspect` until a one-shot container exits with code 0 (used for `hydra-migrate`)
- **`verify_service_logs()`**: Checks last 10 log lines for fatal/panic/segfault/killed/oom
- **`register_oauth2_client()`**: Idempotent Hydra client registration via `authmanager`
- **`create_agent_credential()`** / **`get_agent_credentials()`**: Create/list Diode plugin credentials via Django management shell (calls `netbox_diode_plugin.client.create_client` / `list_clients`)
- **Secret helpers**: `gen_secret()`, `gen_django_key()`, `get_secret()`, `put_secret()`, `get_val()`, `needs_gen()`, `read_existing()`, `write_env_val()`
- **Postgres password sync**: `postgres_volume_exists()`, `sync_postgres_passwords()` — detects existing Postgres volume and runs `ALTER USER` to sync passwords
- **Deployment helpers**: `copy_example_templates()`, `register_oauth2_clients()`, `ensure_agent_credentials()`, `restart_discovery_services()`, `verify_services()`
- **Agent lifecycle**: `start_orb_agent()` (resolves config, mounts SNMP extensions, detects OS scan mode, sudo privileged run), `stop_orb_agent()` (stop + remove), `wait_for_agent_running()` (polls container inspect)
- **`build_netbox_image()`**: Extracts VERSION from `docker-compose.yml` and runs `$CONTAINER_ENGINE build`

The library uses a source guard (`_COMMON_SH_LOADED`) to prevent double-loading. Root-level scripts set `SCRIPT_DIR`; lib/ scripts set `ROOT_DIR` before sourcing to get correct directory paths.

### Configuration Layers

1. **Main Compose File** (`docker-compose.yml`): 12 service definitions (orb-agent runs standalone via sudo). Single compose file — no override file.
2. **Secrets Directory** (`secrets/`): Persisted secret files (source of truth across re-runs)
   - Up to 17 individual `.txt` files (e.g., `postgres_password.txt`, `secret_key.txt`, `snmp_community.txt`)
   - 13 created by `lib/generate-secrets.sh` on first run; 2 agent credentials created by `deploy.sh` step 14; up to 2 user-managed (`snmp_community.txt` for SNMP, `pfsense_api_key.txt` for pfSense REST API sync — not auto-generated)
   - Reused on subsequent runs
   - Takes priority over env file values; `chmod 600` on each file
   - Gitignored — never committed
3. **Environment Files** (`env/`): Service-specific configuration (written by generate-secrets.sh from secrets/ values). Live env files are gitignored; committed `.example` templates have empty secret values.
   - `env/netbox.env` (`.example` committed): NetBox application settings (DB credentials, Redis config, secret key, email, etc.). Used by `netbox` and `netbox-worker` services.
   - `env/postgres.env` (`.example` committed): Database initialization variables (POSTGRES_PASSWORD, DIODE/HYDRA passwords). Used by `postgres` service.
   - `env/discovery.env` (`.example` committed): Diode pipeline secrets (Diode Redis password, Diode/Hydra Postgres passwords, OAuth2 client secrets including `NETBOX_TO_DIODE_CLIENT_SECRET`, Hydra config). Used by `diode-ingester`, `diode-reconciler`, `diode-auth`, `hydra`, and `diode-redis` services.
   - `discovery/hydra.yaml` (`.example` committed): Hydra OAuth2 config with system secret, issuer URL, JWT strategy, and default scope grant. Gitignored; template has placeholder secret. See "Hydra Configuration" section for required settings.
4. **Root `.env`** (generated by generate-secrets.sh): Compose variable substitution values consumed by `docker-compose.yml`:
   - `REDIS_PASSWORD` / `REDIS_CACHE_PASSWORD` — injected into redis/redis-cache `environment:` blocks
   - `HYDRA_POSTGRES_USER` / `HYDRA_POSTGRES_PASSWORD` / `HYDRA_POSTGRES_DB_NAME` — used in Hydra DSN
   - `DIODE_POSTGRES_USER` / `DIODE_POSTGRES_PASSWORD` / `DIODE_POSTGRES_DB_NAME` — used by diode-reconciler
   - `SUPERUSER_PASSWORD` — passed to netbox container environment
   - `DIODE_INGEST_CLIENT_SECRET` — used by `diode-ingest` infrastructure client
   - `ORB_AGENT_CLIENT_ID` / `ORB_AGENT_CLIENT_SECRET` — read by `start_orb_agent()` from `secrets/` (also written to `.env` for reference)
   - `SNMP_COMMUNITY` — SNMP community string for orb-agent. Read from `secrets/snmp_community.txt` first (user-managed, not auto-generated), falls back to `.env`, defaults to `public`
5. **Python Configuration** (`netbox-docker/configuration/` + `configuration/`): NetBox-specific Python config files
   - `netbox-docker/configuration/configuration.py`: Main NetBox configuration (upstream). Provides `_read_secret()` helper for reading from `/run/secrets/`.
   - `configuration/plugins.py`: Plugin configuration (local override, mounted at runtime via volume). Configures `PLUGINS_CONFIG` with the three required Diode plugin settings: `diode_target_override` (from `DIODE_TARGET_OVERRIDE` env var), `diode_username` (`"diode"`), and `netbox_to_diode_client_secret` (read from `/run/secrets/netbox_to_diode`). All three are required — an empty `PLUGINS_CONFIG` causes the plugin to fail with "Failed to obtain access token: Not Found".

## Common Commands

### Deployment (First-time or Update)

```bash
# Full automated deployment — all 16 steps are idempotent
./deploy.sh                              # defaults to http://localhost:8000
./deploy.sh http://192.168.1.100:8000    # custom host

# Skip image pull, only rebuild custom image
./deploy.sh --no-pull

# Both options
./deploy.sh --no-pull http://192.168.1.100:8000
```

### Managing the Stack

Commands below use `podman`; substitute `docker` if that is your runtime.

```bash
# Start all compose services (does NOT include orb-agent)
podman compose up -d

# View logs
podman compose logs -f netbox

# Stop all compose services (preserves volumes)
podman compose down

# Stop and destroy all data
podman compose down -v

# Restart NetBox after config changes
podman compose restart netbox netbox-worker

# Pull latest images
podman compose pull

# Orb Agent (standalone, requires sudo)
sudo podman run -d --name netbox-orb-agent --privileged --net=host ...  # see start_orb_agent()
sudo podman logs -f netbox-orb-agent
sudo podman stop netbox-orb-agent && sudo podman rm netbox-orb-agent
```

### NetBox Management

```bash
# Create additional superuser interactively (deploy.sh creates the first admin automatically)
podman compose exec netbox /opt/netbox/netbox/manage.py createsuperuser

# Run Django management commands
podman compose exec netbox /opt/netbox/netbox/manage.py <command>

# Access NetBox shell
podman compose exec netbox /opt/netbox/netbox/manage.py shell

# Run database migrations
podman compose exec netbox /opt/netbox/netbox/manage.py migrate
```

### Custom Image Building (netbox-docker/)

```bash
cd netbox-docker/

# Build custom image from a specific NetBox branch/tag
./build.sh <branch>

# Build and push to registry
./build.sh <branch> --push

# Run tests
IMAGE=netboxcommunity/netbox:latest ./test.sh
```

## File Structure

```
.
├── deploy.sh                    # Unified deployment script (16 idempotent steps)
├── docker-compose.yml           # All service definitions (single compose file)
├── Dockerfile-Plugins           # Custom NetBox image with diode plugin
├── pyproject.toml               # Python project config for uv (pfsense-sync dependencies)
├── requirements.txt             # Python deps reference (managed via pyproject.toml)
├── .env                         # Compose variable substitution (generated by generate-secrets.sh)
├── .gitignore
├── configuration/
│   └── plugins.py               # NetBox plugin configuration (mounted into container)
├── env/
│   ├── netbox.env               # NetBox app config (gitignored — contains secrets)
│   ├── netbox.env.example       # Template — committed with empty secret values
│   ├── postgres.env             # Database settings (gitignored — contains secrets)
│   ├── postgres.env.example     # Template — committed with empty passwords
│   ├── discovery.env            # Diode pipeline secrets (gitignored)
│   └── discovery.env.example    # Template — committed with empty secrets
├── secrets/                     # Persisted secrets — source of truth (gitignored)
│   ├── postgres_password.txt
│   ├── redis_password.txt
│   ├── redis_cache_password.txt
│   ├── secret_key.txt
│   ├── api_token_peppers.txt
│   ├── superuser_password.txt
│   ├── diode_redis_password.txt
│   ├── diode_postgres_password.txt
│   ├── hydra_postgres_password.txt
│   ├── hydra_system_secret.txt
│   ├── diode_to_netbox_client_secret.txt
│   ├── netbox_to_diode_client_secret.txt
│   ├── diode_ingest_client_secret.txt
│   ├── orb_agent_client_id.txt
│   ├── orb_agent_client_secret.txt
│   ├── snmp_community.txt          # User-managed, not auto-generated
│   ├── pfsense_api_key.txt         # User-managed, not auto-generated (pfSense REST API)
│   └── agent-resolved.yaml         # Runtime: resolved agent config (generated by start_orb_agent)
├── lib/
│   ├── common.sh                # Shared library (logging, compose, waiters, secrets, DB sync, deployment helpers, image builder)
│   ├── generate-secrets.sh      # Generates/reuses secrets, writes env files + secrets/
│   └── pfsense-sync.py          # pfSense REST API → Diode ingestion (Approach B)
├── discovery/
│   ├── roles.yaml               # Canonical device role list (source of truth for all discovery)
│   ├── init-db.sh               # Creates Hydra/Diode databases in Postgres on first startup
│   ├── nginx.conf               # Ingress proxy configuration
│   ├── hydra.yaml               # Hydra OAuth2 config (gitignored — contains secret)
│   ├── hydra.yaml.example       # Template — committed with placeholder secret
│   ├── agent.yaml               # Orb Agent config (gitignored — site-specific subnet targets)
│   ├── agent.yaml.example       # Template — OS-aware scan mode (default, includes SNMP + OID lookup)
│   ├── agent.yaml.rootless.example  # Template — static rootless fallback (TCP connect scans)
│   └── snmp-extensions/         # Custom SNMP sysObjectID → device name mappings
│       └── pfsense.yaml         # Maps pfSense OID to Netgate-4200-pfSense
├── README.md                    # Deployment notes and credentials
└── netbox-docker/               # Upstream netbox-docker repository (do not modify)
    ├── docker-compose.yml       # Upstream compose (not used directly)
    ├── configuration/           # NetBox Python configuration (upstream)
    ├── build.sh                 # Custom image builder
    └── test.sh                  # Test runner
```

## Important Notes

- Maintain compatibility with the netbox-docker/ deployment and its nested folders. Don't make changes to the netbox-docker folder and below.
- The NetBox instance is accessible at `http://0.0.0.0:8000/`
- Admin credentials are auto-generated by `lib/generate-secrets.sh` and printed by `deploy.sh`; the superuser password is stored in `.env` as `SUPERUSER_PASSWORD`
- The `netbox-docker/` directory is a clone of the upstream repository (branch: release)
- All secrets are auto-generated at deploy time by `lib/generate-secrets.sh` and persisted to `secrets/`; live env files are gitignored, `.example` templates are committed with empty secret values
- The `secrets/` directory is the source of truth for credentials — `generate-secrets.sh` reads from it first, falls back to env files, and always writes back to it after generation
- When `deploy.sh` re-runs against an existing Postgres volume, `sync_postgres_passwords()` (in `lib/common.sh`) runs `ALTER USER` to sync DB passwords, preventing authentication mismatches
- Redis passwords for the `redis` and `redis-cache` services are passed via compose variable substitution (`${REDIS_PASSWORD}` / `${REDIS_CACHE_PASSWORD}` from `.env`), not via env files. The Diode Redis password comes from `env/discovery.env`.
- NetBox version is controlled by the `VERSION` variable in `docker-compose.yml` (defaults to `v4.5-4.0.0`)
- Configuration changes in `configuration/` directory require service restart
- Persistent data is stored in 7 named volumes: `netbox-media-files`, `netbox-postgres`, `netbox-redis-data`, `netbox-redis-cache-data`, `netbox-reports-files`, `netbox-scripts-files`, `diode-redis-data`
- **Supports both Podman and Docker** — `lib/common.sh` auto-detects the available runtime (`CONTAINER_ENGINE`) and sets `CONTAINER_SEP` (`_` for Podman, `-` for Docker) so container/volume names resolve correctly. Podman is preferred when both are installed.
- `lib/common.sh` is the shared library sourced by all scripts — contains runtime detection, logging, compose wrapper, health waiters, secret helpers, Postgres password sync, deployment helpers, and `build_netbox_image()`. Uses a source guard (`_COMMON_SH_LOADED`) to prevent double-loading.
- There is no `docker-compose.override.yml` — all services are defined in the single `docker-compose.yml`. The `compose()` wrapper uses explicit `-f` to prevent auto-discovery of any override file a user might create.
- The `orb-agent` is **not** in `docker-compose.yml` — it runs as a standalone privileged container via `sudo $CONTAINER_ENGINE run --privileged --net=host`. This is required because rootless compose cannot grant `CAP_NET_RAW` for SYN scans. `deploy.sh` step 16 starts it automatically via `start_orb_agent()`. Use `stop_orb_agent()` or `sudo $CONTAINER_ENGINE stop netbox-orb-agent` to stop it.

## Hydra Configuration

Ory Hydra v25.4.0 ignores several `HYDRA_*` environment variables set in `env/discovery.env`. The following must be set directly in `discovery/hydra.yaml` (not via env vars):

- **`urls.self.issuer: http://hydra:4444`** — Without this, Hydra defaults the token issuer to `http://0.0.0.0:4444/`, which doesn't match what diode-auth expects (`http://hydra:4444`). The mismatch causes "failed to validate token" errors because JWT issuer verification fails.
- **`strategies.access_token: jwt`** — Without this, Hydra issues opaque tokens (`ory_at_*` prefix) instead of JWTs. diode-auth validates tokens as JWTs, so opaque tokens fail validation.
- **`oauth2.client_credentials.default_grant_allowed_scope: true`** — Without this, tokens issued without an explicit `scope` parameter get empty scopes. The NetBox Diode plugin doesn't request scopes in its token requests, so this setting auto-grants the client's full allowed scopes (e.g., `diode:read diode:write`). Without it, diode-auth returns 403 because the token lacks required scopes.

The `HYDRA_STRATEGIES_*` and `HYDRA_URLS_SELF_ISSUER` env vars in `env/discovery.env` are kept for documentation but are not effective — Hydra reads these settings from the YAML config file only.

## Orb Agent Config Structure

The `discovery/agent.yaml` uses a specific YAML nesting that differs from what might seem intuitive. In policy definitions, `scope` (containing `targets`, `ports`) is a **sibling** of `config` (containing `schedule`, `timeout`, `defaults`), not nested inside it:

```yaml
policies:
  network_discovery:
    policy_name:
      config:                    # ← runtime settings
        schedule: "0 */2 * * *"
        timeout: 600
      scope:                     # ← sibling of config, NOT a child
        targets:
          - 192.168.1.0/24
        ports: [22, 80, 443]
```

Nesting `scope` inside `config` causes "400 subnet_scan: no targets found in the policy" because the agent looks for targets at the wrong YAML path. See [upstream config samples](https://github.com/netboxlabs/orb-agent/blob/develop/docs/config_samples.md).

### Scan Modes — OS-Aware Configuration

The agent runs with `sudo --privileged` by default. `start_orb_agent()` auto-detects the OS and adjusts the network scan mode at startup:

- **macOS (Darwin)**: Podman runs inside an Apple Hypervisor VM (`applehv`/`qemu`). Even with `--privileged --net=host`, raw sockets (SYN/ICMP) cannot traverse the VM's NAT layer. `start_orb_agent()` injects `scan_types: [connect]` and `skip_host: true` into the resolved config via `awk` before mounting.
- **Linux**: `--privileged --net=host` gives real `CAP_NET_RAW`. `start_orb_agent()` removes any `scan_types: [connect]` / `skip_host: true` lines, allowing nmap's default SYN scan.

The `agent.yaml` template should **not** hardcode `scan_types` or `skip_host` — the function handles injection. Two example templates are provided:

- **`agent.yaml.example`** (default): No hardcoded scan mode — OS injection handles it. Includes SNMP discovery policies with site/role/tag defaults and `lookup_extensions_dir` for custom OID mappings.
- **`agent.yaml.rootless.example`**: Static rootless fallback — `scan_types: [connect]`, `skip_host: true` hardcoded. Includes `lookup_extensions_dir`. For environments where sudo is unavailable and `start_orb_agent()` is not used.

### SNMP Custom OID Lookup

The `discovery/snmp-extensions/` directory contains custom sysObjectID-to-device-name mappings. These are mounted into the orb-agent container at `/opt/orb/snmp-extensions` and referenced by `lookup_extensions_dir` in the SNMP policy scope.

- **`pfsense.yaml`**: Maps the pfSense/BSNMP sysObjectID (`.1.3.6.1.4.1.12325.1.1.2.1.1`) to `Netgate-4200-pfSense`. Without this, enterprise OID 12325 resolves to "Fraunhofer FOKUS" (the IANA registrant) instead of "Netgate".

To add mappings for additional devices, create new YAML files in `discovery/snmp-extensions/` with the format:
```yaml
devices:
  .1.3.6.1.4.1.<enterprise>.<product_oid>: Manufacturer-Model-Platform
```

### Standardized Device Roles

`discovery/roles.yaml` is the single source of truth for valid device roles across all discovery sources. The canonical list is: `application-server`, `firewall`, `gateway-router`, `hypervisor`, `kubernetes-cluster`, `container`, `nas`, `network`, `server`, `switch`. `lib/pfsense-sync.py` loads and validates its `DEVICE_ROLE` against this file at startup and exits with an error if the role is invalid. Agent config templates reference the file in a comment.

### pfSense REST API Sync

`lib/pfsense-sync.py` supplements SNMP discovery with richer data from the pfSense REST API (pfrest v2 package). It queries device info, interfaces, IPs, gateways, and ARP entries, then pushes them to NetBox via the Diode gRPC pipeline. The device role is `gateway-router` (validated against `discovery/roles.yaml` at module load time). Requires `pyyaml` (declared in `pyproject.toml`).

Prerequisites: install pfrest on the pfSense device, create an API key, and store it in `secrets/pfsense_api_key.txt`. Run manually with `uv run lib/pfsense-sync.py [--dry-run]`. `deploy.sh` runs it automatically if the script, API key, and `uv` are available.

### Scan Timeout and Ports

- **`timeout`** (in `config`): Network discovery timeout is in **minutes** (not seconds). TCP connect scans need ~10 min per /24 subnet; SYN scans need ~5 min. Default: 20 minutes. Without sufficient timeout, scans are killed mid-run ("nmap scan timed out") and no results are reported.
- **`ports`** (in `scope`): Limits which ports nmap scans. Without this, nmap scans its default 1000 ports per IP. Specifying `ports: [22, 80, 443, 8080, ...]` dramatically reduces scan time.
- **`os_detection`**: Not supported by the orb-agent network-discovery backend. Setting `os_detection: true` causes `exit status 1`. Do not use.

## Container Runtime Notes

- **`podman-compose` (Python) vs `docker compose` (Go)**: `podman-compose 1.5.0` does not support all `docker compose` flags. `deploy.sh` handles this with fallbacks — e.g., `compose pull --ignore-buildable` falls back to pulling explicit service names. `compose down` may silently leave stale containers due to pod dependency chains; `deploy.sh` step 6 detects and force-removes them.
- **Batch-start race condition** (Podman-specific): `podman compose up -d` may fail to start one container when launching many at once (`internal libpod error`). `deploy.sh` handles this with an automatic retry since `compose up -d` is idempotent. Not observed with Docker.
- **Container naming**: Podman uses underscores (`netbox_service_1`), Docker uses hyphens (`netbox-service-1`). The `CONTAINER_SEP` variable in `lib/common.sh` handles this difference automatically.
- **One-shot dependency chain** (Podman-specific): `hydra-migrate` is a one-shot container (`restart: "no"`) that exits after running migrations. Its completion is validated by `deploy.sh` (step 9) via `wait_for_completed()` instead of a compose `depends_on`, because podman considers an exited container's state improper and fails `podman restart` on any container that transitively depends on it. With the dependency in `deploy.sh`, step 15 can safely use `$CONTAINER_ENGINE restart` for the discovery services.
- **Distroless images**: `diode-ingester` and `diode-reconciler` use distroless images (no shell, no curl/wget), so they cannot have compose healthchecks. `diode-auth` has `sh`, `wget`, and `nc` and does have a healthcheck. Healthcheck-based `depends_on` conditions (`service_healthy`) are used where possible to enforce startup ordering.
- **Orb Agent requires sudo**: The agent runs outside compose as a standalone `sudo $CONTAINER_ENGINE run --privileged` container. Rootless Podman cannot grant `CAP_NET_RAW` even with `privileged: true` in compose — only `sudo` works. Running `sudo compose` is also not viable because root's container storage is separate from the rootless user's (different images, networks, volumes). The agent is named `netbox-orb-agent` (fixed, not dependent on `CONTAINER_SEP`).
- **macOS Podman VM limitation**: On macOS, Podman runs containers inside an Apple Hypervisor VM (`applehv`). Even with `--privileged --net=host`, raw sockets (SYN scans, ICMP) cannot traverse the VM's NAT layer — nmap exits with `exit status 1`. `start_orb_agent()` auto-detects Darwin and forces TCP connect scans. On a native Linux host, SYN scans work as expected.
- **Agent config env var substitution**: The orb-agent binary does not reliably resolve `${VAR}` placeholders in its YAML config. `start_orb_agent()` pre-resolves credentials with `sed` into `secrets/agent-resolved.yaml` (gitignored, chmod 600) before mounting. It also mounts `discovery/snmp-extensions/` at `/opt/orb/snmp-extensions` for custom OID lookups.

## Git Commit Guidelines

When creating commit messages:
- Do NOT mention Anthropic, Claude, or AI assistance in commit messages
- Do NOT include "Co-Authored-By: Claude" or similar attributions
- Write commits as if they are authored entirely by the repository owner
- Focus on what changed and why, not how the changes were generated

## Updating NetBox

When updating to a new NetBox version:

1. Check [release notes](https://github.com/netbox-community/netbox-docker/releases) for breaking changes
2. Update the `VERSION` variable in `docker-compose.yml`
3. Run `./deploy.sh` (all 16 steps are idempotent — handles upstream update, secrets, image build, migrations, OAuth2, agent credentials, and verification)
4. Or use `./deploy.sh --no-pull` to skip pulling images
5. Optionally pass a custom URL: `./deploy.sh http://192.168.1.100:8000`

For manual updates:

1. Update the `VERSION` variable in `docker-compose.yml`
2. Pull new images: `podman compose pull` (or `docker compose pull`)
3. Rebuild custom image: run `build_netbox_image` or manually `podman build --no-cache -t netbox:latest-plugins -f Dockerfile-Plugins --build-arg VERSION=<version> .`
4. Restart services: `podman compose up -d`
5. Run migrations: `podman compose exec netbox /opt/netbox/netbox/manage.py migrate`

Ensure the netbox-docker repository version stays in sync with the container image version.
