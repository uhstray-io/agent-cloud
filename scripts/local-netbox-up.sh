#!/usr/bin/env bash
# local-netbox-up.sh — bring up the NetBox APP TIER locally under podman.
#
# NetBox is the platform's one Docker-required service, and the local Semaphore
# (in the podman VM) can't reach Docker Desktop's daemon — so per
# plan/development/NETBOX-LOCAL-ENGINE.md we run the app tier under PODMAN,
# discovery/orb-agent EXCLUDED. Driven from the Mac so the deployment dir's
# bind-mounts resolve via the /Users virtiofs share at identical paths.
#
# Idempotent: clones the upstream context if absent, writes fake env only if
# absent, builds the image only if absent, then (re)starts the app tier.
# The full Semaphore-wired composable path is the plan's remaining work.
#
# After this: `make local-netbox-discover` feeds the running containers in.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="${REPO_ROOT}/platform/services/netbox/deployment"
VERSION="v4.5-4.0.0"
cd "$DEPLOY_DIR"

log() { printf '[netbox-up] %s\n' "$*"; }

# 1. Upstream netbox-docker context (provides the runtime /etc/netbox/config mount).
if [ ! -d netbox-docker/.git ]; then
  log "cloning netbox-docker (release)..."
  git clone --depth 1 --branch release https://github.com/netbox-community/netbox-docker.git netbox-docker
fi

# 2. Fake LOCAL_FAKE_ env (gitignored). Written only if absent — never clobbers.
#    SECRET_KEY and API_TOKEN_PEPPER_1 must be >=50 chars (NetBox requirement).
mkdir -p env secrets
if [ ! -f env/netbox.env ]; then
  log "writing fake local env files..."
  cat > env/netbox.env <<'EOF'
DB_HOST=postgres
DB_NAME=netbox
DB_USER=netbox
DB_PASSWORD=LOCAL_FAKE_netbox_db
REDIS_HOST=redis
REDIS_DATABASE=0
REDIS_PASSWORD=LOCAL_FAKE_redis
REDIS_SSL=false
REDIS_INSECURE_SKIP_TLS_VERIFY=false
REDIS_CACHE_HOST=redis-cache
REDIS_CACHE_DATABASE=1
REDIS_CACHE_PASSWORD=LOCAL_FAKE_rediscache
REDIS_CACHE_SSL=false
REDIS_CACHE_INSECURE_SKIP_TLS_VERIFY=false
SECRET_KEY=LOCAL_FAKE_netbox_secret_key_0000000000000000000000000000
API_TOKEN_PEPPER_1=LOCAL_FAKE_api_token_pepper_00000000000000000000000000
SKIP_SUPERUSER=false
GRAPHQL_ENABLED=true
METRICS_ENABLED=false
WEBHOOKS_ENABLED=true
MEDIA_ROOT=/opt/netbox/netbox/media
CORS_ORIGIN_ALLOW_ALL=true
EOF
  cat > env/postgres.env <<'EOF'
POSTGRES_DB=netbox
POSTGRES_USER=netbox
POSTGRES_PASSWORD=LOCAL_FAKE_netbox_db
DIODE_POSTGRES_DB_NAME=diode
DIODE_POSTGRES_USER=diode
DIODE_POSTGRES_PASSWORD=LOCAL_FAKE_diode_db
HYDRA_POSTGRES_DB_NAME=hydra
HYDRA_POSTGRES_USER=hydra
HYDRA_POSTGRES_PASSWORD=LOCAL_FAKE_hydra_db
EOF
  : > env/discovery.env   # must exist (compose env_file); app-tier doesn't use it
  cat > .env <<'EOF'
REDIS_PASSWORD=LOCAL_FAKE_redis
REDIS_CACHE_PASSWORD=LOCAL_FAKE_rediscache
SUPERUSER_PASSWORD=LOCAL_FAKE_admin
EOF
  printf 'LOCAL_FAKE_n2d_secret' > secrets/netbox_to_diode_client_secret.txt
  chmod 600 secrets/netbox_to_diode_client_secret.txt
fi

# 3. Custom plugins image (built once; reused).
if ! podman image exists netbox:latest-plugins 2>/dev/null && \
   ! podman image exists localhost/netbox:latest-plugins 2>/dev/null; then
  log "building netbox:latest-plugins (one-time, several minutes)..."
  podman build -t netbox:latest-plugins -f Dockerfile-Plugins --build-arg VERSION="$VERSION" .
fi

compose() { podman compose --project-name netbox -f docker-compose.yml "$@"; }

# 4. App tier only — backing services first, then netbox + worker (--no-deps so
#    the discovery pipeline is never pulled in).
log "starting backing services (postgres, redis, redis-cache)..."
compose up -d postgres redis redis-cache
log "waiting for postgres..."
for _ in $(seq 1 30); do
  [ "$(podman inspect -f '{{.State.Health.Status}}' netbox-postgres-1 2>/dev/null)" = healthy ] && break
  sleep 3
done
log "starting netbox + worker..."
compose up -d --no-deps netbox netbox-worker

log "waiting for netbox to become healthy (first boot runs migrations — up to 10 min)..."
for _ in $(seq 1 120); do
  s=$(podman inspect -f '{{.State.Health.Status}}' netbox-netbox-1 2>/dev/null || echo none)
  [ "$s" = healthy ] && { log "NetBox healthy at http://127.0.0.1:8000 (admin / LOCAL_FAKE_admin)"; exit 0; }
  sleep 5
done
log "ERROR: NetBox did not become healthy in time — check: podman logs netbox-netbox-1" >&2
exit 1
