# Podman vs Docker Compose Compatibility Guide

**Date:** 2026-05-06
**Status:** ACTIVE
**Applies to:** All services in `platform/services/` and `agents/`
**Context:** Phase 2 infrastructure architecture review

---

## Overview

This document captures the behavioral differences between Docker Compose (Go-based `docker compose` plugin) and podman-compose (Python CLI wrapper, `pip install podman-compose`) as they affect agent-cloud service deployments. It serves as the single reference for writing compose files that work correctly under both runtimes.

The platform uses a split-runtime strategy: NetBox runs on Docker (due to privileged container requirements), while all other services run on Podman with podman-compose. This guide ensures compose files are portable and deploy scripts handle runtime-specific quirks.

---

## 1. Runtime Strategy

```mermaid
flowchart TD
    START["Service Deployment"] --> CHECK{"Which service?"}
    CHECK -->|"NetBox"| DOCKER["Docker CE<br/>docker compose (Go plugin)<br/>Privileged orb-agent via sudo"]
    CHECK -->|"All other services"| PODMAN["Podman (rootless)<br/>podman-compose (Python CLI)<br/>No sudo required"]
    DOCKER --> INV_D["container_engine: docker<br/>(site-config inventory)"]
    PODMAN --> INV_P["container_engine: podman<br/>(site-config inventory)"]
    INV_D --> ANSIBLE["Ansible sets CONTAINER_ENGINE<br/>on target VM"]
    INV_P --> ANSIBLE
    ANSIBLE --> DEPLOY["deploy.sh reads CONTAINER_ENGINE<br/>sources lib/common.sh"]
```

**Runtime selection rules:**

| Service | Runtime | Reason |
|---------|---------|--------|
| NetBox (+ Diode pipeline) | Docker | Privileged orb-agent needs `CAP_NET_RAW` via `sudo docker run --privileged`. Compose healthcheck dependency chains are critical for the 12-container stack. `lib/common.sh` (NetBox-specific) hardcodes Docker. |
| OpenBao | Podman | Simple single-container service. `cap_add: IPC_LOCK` works in rootless Podman. |
| Semaphore | Podman | Two-container stack (app + postgres). |
| NocoDB | Podman | Two-container stack (app + postgres). |
| n8n | Podman | Four-container stack (app + worker + postgres + redis). |
| Caddy | Podman | Single container, binds ports 80/443. |
| Postiz | Podman | Three-container stack (app + postgres + redis). |

**Runtime is controlled per-host** via the `container_engine` variable in the site-config inventory. Ansible passes this to deploy scripts as the `CONTAINER_ENGINE` environment variable. The platform-level `lib/common.sh` auto-detects if not set (prefers Podman), while the NetBox-specific `lib/common.sh` requires Docker and errors if it is not found.

---

## 2. Container Naming

Docker Compose and podman-compose use different naming conventions for containers when `container_name` is not set:

| Runtime | Default Pattern | Example |
|---------|----------------|---------|
| Docker Compose | `{project}-{service}-{replica}` | `netbox-postgres-1` |
| podman-compose | `{project}_{service}_{replica}` | `netbox_postgres_1` |

The separator difference (`-` vs `_`) breaks any script that constructs container names dynamically.

**Best practice:** Always set explicit `container_name` in compose files.

```yaml
# CORRECT: explicit names, runtime-agnostic
services:
  nocodb-postgres:
    container_name: workflow-nocodb-postgres
    image: docker.io/postgres:16.6

# INCORRECT: relies on auto-generated names
services:
  postgres:
    image: docker.io/postgres:16.6
    # Name will be "project-postgres-1" (Docker) or "project_postgres_1" (Podman)
```

All agent-cloud compose files use a `workflow-` prefix for container names (e.g., `workflow-nocodb`, `workflow-semaphore-db`) or a service-specific prefix (e.g., `postiz-postgres`). This convention prevents naming collisions across services on the same host and makes container names deterministic regardless of runtime.

When scripts must reference containers without knowing the name, use the `CONTAINER_SEP` variable from `lib/common.sh`:
- Docker: `CONTAINER_SEP="-"`
- Podman: `CONTAINER_SEP="_"`

---

## 3. Volume Naming

### The `name:` property problem

Docker Compose supports the `name:` property in the top-level `volumes:` section to set explicit volume names:

```yaml
volumes:
  db_data:
    name: my-explicit-volume-name  # Docker: works. podman-compose 1.0.6: IGNORED.
```

In podman-compose 1.0.6, the `name:` property is silently ignored. The volume gets the default auto-generated name (`{project}_{volume}`), which can cause data loss on redeployment if the project name changes.

**Best practice:** Always declare volumes in the top-level `volumes:` section but do NOT use the `name:` property. Instead, control the project name via `--project-name` flag in the compose wrapper.

```yaml
# CORRECT: no name: property, project name controls prefix
volumes:
  db_data:       # Becomes "{project}_db_data"
  redis_data:    # Becomes "{project}_redis_data"

# INCORRECT: name: property (incompatible with podman-compose < 1.3.0)
volumes:
  db_data:
    name: nocodb-db-data
```

The compose wrapper in `lib/common.sh` uses `--project-name` to ensure consistent naming:

```bash
compose() {
  $COMPOSE_CMD -f compose.yml "$@"
  # For NetBox: $CONTAINER_ENGINE compose --project-name "netbox" -f docker-compose.yml "$@"
}
```

### Volume name resolution

| Runtime | Auto-generated Name | With `--project-name foo` |
|---------|-------------------|--------------------------|
| Docker Compose | `{directory}_db_data` | `foo_db_data` |
| podman-compose | `{directory}_db_data` | `foo_db_data` |

Both runtimes use the same pattern when `--project-name` is set. The difference only matters when relying on directory-based inference, which varies by cwd.

---

## 4. depends_on with service_healthy

**This is the most critical compatibility issue.**

The `depends_on` condition `service_healthy` tells the compose engine to wait until a dependency's healthcheck reports healthy before starting the dependent service:

```yaml
services:
  app:
    depends_on:
      postgres:
        condition: service_healthy  # Docker: waits. podman-compose < 1.3.0: IGNORED.
```

**podman-compose 1.0.6 behavior:** The `condition: service_healthy` directive is parsed but not enforced. Containers start in dependency order but without waiting for health. This means application containers start before their database is ready, causing connection errors or crashes.

**podman-compose >= 1.3.0 behavior:** The `condition: service_healthy` directive is properly enforced, matching Docker Compose behavior.

### Current workaround

All deploy scripts that run on Podman VMs use explicit health-wait functions from `lib/common.sh` instead of relying on compose dependency conditions:

```bash
# From deploy.sh — start backing services, wait, then start app
compose up -d postgres redis
wait_for_healthy "workflow-nocodb-postgres" 120
compose up -d nocodb
wait_for_http "${NOCODB_URL}/api/v1/health" "NocoDB" 120
```

The `wait_for_healthy()` function polls `$CONTAINER_ENGINE inspect --format='{{.State.Health.Status}}'` until the container reports `healthy` or times out. The `wait_for_http()` function polls an HTTP endpoint with curl.

### Staged startup pattern

For services with deep dependency chains (like NetBox's 12-container stack), compose files declare `depends_on` for documentation and Docker compatibility, but deploy scripts implement staged startup:

```bash
# Stage 1: backing services
compose up -d postgres redis redis-cache diode-redis
sleep 15

# Stage 2: middleware
compose up -d hydra hydra-migrate
wait_for_completed "hydra-migrate" 300

# Stage 3: application
compose up -d
```

### Migration path

Once all VMs are upgraded to podman-compose >= 1.3.0 (see `plan/development/PODMAN-UPGRADE-PLAN.md`), deploy scripts can optionally simplify to `compose up -d` and let compose enforce the dependency chain. The explicit staged startup pattern will remain as a documented fallback and for NetBox, which benefits from the staged approach due to first-boot migration timing.

---

## 5. Healthcheck Behavior

### JSON output format differences

`compose ps --format json` returns different JSON structures between runtimes:

**Docker Compose:**
```json
{
  "Name": "workflow-nocodb",
  "Service": "nocodb",
  "State": "running",
  "Health": "healthy"
}
```

**podman-compose (via `podman ps --format json`):**
```json
{
  "Names": ["workflow-nocodb"],
  "Labels": {
    "io.podman.compose.service": "nocodb"
  },
  "State": "running",
  "Status": "Up 2 minutes (healthy)"
}
```

Key differences:
- Docker uses `Name` (string), Podman uses `Names` (array)
- Docker uses `Service`, Podman uses `Labels["io.podman.compose.service"]`
- Docker has a dedicated `Health` field, Podman embeds health in the `Status` string

### Python parser pattern

The NetBox-specific `lib/common.sh` includes an inline Python parser that handles both formats:

```python
c_svc = c.get('Service', '') or c.get('Labels', {}).get('io.podman.compose.service', '')
c_names = c.get('Names', [c.get('Name', '')])
if not isinstance(c_names, list): c_names = [c_names]
```

### Inspect format

The `inspect` command works identically across both runtimes for health status:

```bash
$CONTAINER_ENGINE inspect --format='{{.State.Health.Status}}' container_name
# Returns: "healthy", "unhealthy", "starting", or "" (no healthcheck)
```

This is the preferred method for health polling in deploy scripts (`wait_for_healthy()`), as it avoids the JSON format differences entirely.

---

## 6. env_file Handling

### Format requirements

podman-compose 1.0.6 requires strict `KEY=VALUE` format in env files. It does not support:
- Quoted values with embedded newlines
- Multi-line values using `\` continuation
- Variable interpolation within env files (`${OTHER_VAR}`)
- Comments after values (`KEY=value # comment`)

**Best practice:** Use simple `KEY=VALUE` format with no quotes, no interpolation, no trailing comments.

```bash
# CORRECT: simple KEY=VALUE
POSTGRES_USER=nocodb
POSTGRES_PASSWORD=s3cur3p4ss
POSTGRES_DB=nocodb

# INCORRECT: features not supported in podman-compose 1.0.6
POSTGRES_PASSWORD="${ADMIN_PASS}"    # Variable interpolation
DATABASE_URL="postgres://..."        # Quotes may cause issues
POSTGRES_DB=nocodb # the database     # Trailing comment
```

### env_file vs environment

Both runtimes support the `environment:` section in compose files for non-secret configuration. Use `env_file:` for secret-containing files (templated by Ansible from OpenBao) and `environment:` for static, non-secret values:

```yaml
services:
  app:
    env_file: ./config/app.env       # Secrets (gitignored, templated)
    environment:                      # Static config (in compose file)
      NODE_ENV: production
      DB_HOST: postgres
```

### YAML anchors in env_file

podman-compose 1.0.6 supports YAML anchors (`&name` / `*name`) for the `environment:` section but does NOT support the merge key (`<<: *anchor`). The n8n compose file uses this pattern:

```yaml
x-n8n-env: &n8n-env
  N8N_HOST: localhost
  NODE_ENV: production

services:
  n8n:
    environment:
      <<: *n8n-env    # Works in Docker Compose, may not work in podman-compose 1.0.6
```

If this causes issues on podman-compose 1.0.6, move shared env vars into the `env_file` instead.

---

## 7. Pull and Build

### The `--ignore-buildable` flag

Docker Compose supports `compose pull --ignore-buildable` to skip pulling images for services that have a `build:` section. podman-compose does not support this flag.

**Workaround:** Fall back to pulling specific service names:

```bash
# Try --ignore-buildable first (Docker), fall back to explicit list (Podman)
compose pull --ignore-buildable 2>/dev/null || \
  compose pull postgres redis redis-cache
```

### Build behavior

Both runtimes support `compose build` and `$CONTAINER_ENGINE build`. For services with a `build:` section and `pull_policy: never` (like NetBox), always build explicitly before `compose up`:

```bash
$CONTAINER_ENGINE build --no-cache -t netbox:latest-plugins \
  -f Dockerfile-Plugins --build-arg VERSION="${VERSION}" .
```

### Image references

Always use fully qualified image references (`docker.io/library/postgres:16`) to avoid differences in default registry resolution between Docker (docker.io) and Podman (configurable via `registries.conf`).

---

## 8. Compose Down Cleanup

### Stale container problem

podman-compose `down` may silently leave containers behind when pod dependency chains block removal. This is particularly common after:
- Changing volume mount paths in the compose file
- Renaming services
- Interrupted previous deployments

**Detection and cleanup pattern:**

```bash
compose down 2>&1 || true

# Detect stale containers by project prefix
leftover=$($CONTAINER_ENGINE ps -a --format '{{.Names}}' 2>/dev/null \
  | grep "^${PROJECT_PREFIX}" || true)

if [ -n "$leftover" ]; then
  warn "Stale containers remain after compose down - force-removing..."
  echo "$leftover" | xargs $CONTAINER_ENGINE rm -f 2>/dev/null || true
  # Remove orphaned pod/network if present (Podman creates pods)
  $CONTAINER_ENGINE pod rm -f "pod_${PROJECT_NAME}" 2>/dev/null || true
  $CONTAINER_ENGINE network rm "${PROJECT_NAME}_default" 2>/dev/null || true
fi
```

This pattern is implemented in the NetBox deploy.sh (step 6) and should be replicated in all deploy scripts.

### Pod cleanup (Podman-specific)

podman-compose creates an implicit pod for each project. When containers are force-removed but the pod remains, the next `compose up` may fail. Always clean up the pod after force-removing containers.

---

## 9. Network Configuration

### DNS resolution

Both runtimes provide DNS resolution between containers on the same compose network, but the underlying mechanisms differ:

| Runtime | DNS Provider | Default Network |
|---------|-------------|-----------------|
| Docker Compose | Built-in DNS server (127.0.0.11) | `{project}_default` bridge |
| Podman (rootless) | netavark + aardvark-dns | `{project}_default` bridge |

**Requirement:** Podman must use netavark (not CNI) as the network backend for DNS resolution to work. Check with:

```bash
podman info --format '{{.Host.NetworkBackend}}'
# Should return: netavark
```

If using the older CNI backend, install the `dnsname` plugin or upgrade to Podman 4.0+ which defaults to netavark.

### Cross-service resolution

Container-to-container DNS uses the service name as defined in the compose file (not the `container_name`). Both runtimes resolve `postgres` to the IP of the container running the `postgres` service:

```yaml
services:
  app:
    environment:
      DB_HOST: postgres     # Resolves via compose DNS in both runtimes
  postgres:
    container_name: workflow-nocodb-postgres  # Not used for DNS resolution
```

### Custom networks

podman-compose 1.0.6 supports the `networks:` section but may not support all properties (like `external: true` in some configurations). Keep network definitions simple:

```yaml
networks:
  app-network:
    external: false    # Works in both runtimes
```

---

## 10. Rootless Considerations

### CAP_NET_RAW limitation

Rootless Podman cannot grant `CAP_NET_RAW` even with `privileged: true` in the compose file. This capability is required for:
- ICMP ping (host discovery)
- Raw socket SYN scans (nmap)
- Network packet capture

**This is why the NetBox orb-agent runs outside compose** as a standalone `sudo $CONTAINER_ENGINE run --privileged --net=host` container.

### Workarounds for rootless

| Capability Need | Rootless Workaround |
|----------------|-------------------|
| `CAP_NET_RAW` (SYN scan) | TCP connect scan fallback (`scan_types: [connect]`) |
| `CAP_NET_RAW` (ICMP ping) | `skip_host: true` (skip ping, scan directly) |
| `IPC_LOCK` (memory locking) | Works in rootless via `cap_add: IPC_LOCK` (OpenBao uses this) |
| Bind port < 1024 | `sysctl net.ipv4.ip_unprivileged_port_start=0` or rootful |

### sudo compose is not viable

Running `sudo podman-compose up` creates containers in root's storage, which is separate from the rootless user's storage (different images, networks, volumes). This breaks the deployment model. Only use `sudo` for individual `podman run` commands that genuinely need privileges (like orb-agent).

---

## 11. Restart Policies After Reboot

### The problem

Docker containers with `restart: always` automatically restart when the Docker daemon starts at boot. Podman has no persistent daemon (daemonless architecture), so containers do not auto-restart after a host reboot.

### systemd integration

Podman containers must be managed by systemd for restart-after-reboot behavior:

```bash
# Generate systemd unit from running container
podman generate systemd --new --name workflow-nocodb > \
  ~/.config/systemd/user/container-workflow-nocodb.service

# Enable with lingering (survives logout)
loginctl enable-linger $USER
systemctl --user enable container-workflow-nocodb.service
```

### podman-compose + systemd

For compose-managed stacks, generate a systemd unit for the entire compose project:

```bash
# Option A: systemd unit that runs compose up/down
cat > ~/.config/systemd/user/nocodb-stack.service << 'EOF'
[Unit]
Description=NocoDB Stack (podman-compose)
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=true
WorkingDirectory=/home/%u/services/nocodb
ExecStart=/usr/bin/podman-compose -f compose.yml up -d
ExecStop=/usr/bin/podman-compose -f compose.yml down
TimeoutStartSec=300

[Install]
WantedBy=default.target
EOF
```

### Current state

systemd integration is not yet automated in agent-cloud playbooks. Services are restarted after reboot by re-running the deploy playbook via Semaphore. A planned playbook (`configure-podman-systemd.yml`) will automate systemd unit generation for all Podman services.

---

## 12. Minimum Version Requirements

### Current state on VMs

| Component | Current Version | Target Version | Status |
|-----------|----------------|---------------|--------|
| Podman | 4.9.3 | >= 4.0 | MEETS TARGET |
| podman-compose | 1.0.6 | >= 1.3.0 | NEEDS UPGRADE |
| Docker CE (NetBox VM only) | Latest stable | Latest stable | MEETS TARGET |

### Why podman-compose >= 1.3.0

podman-compose 1.3.0 (released 2024-08-15) introduced:
- Proper enforcement of `depends_on: condition: service_healthy`
- Support for the `name:` property in top-level `volumes:`
- Improved `--format json` output parsing
- Better handling of compose spec extensions (`x-` prefixes)

### Upgrade path

**Upgrade automation must be built before standardizing on >= 1.3.0.** See `plan/development/PODMAN-UPGRADE-PLAN.md` for the phased upgrade plan.

The upgrade is a pip install since podman-compose is a Python package:

```bash
pip3 install --upgrade podman-compose>=1.3.0
```

**Critical:** The platform uses `podman-compose` (Python CLI wrapper installed via pip), NOT `podman compose` (Go-based native plugin). These are different tools with different behavior. Do not install or use `podman compose`.

---

## 13. Compose Spec Feature Compatibility Matrix

| Feature | Docker Compose | podman-compose 1.0.6 | podman-compose >= 1.3.0 | Notes |
|---------|---------------|---------------------|------------------------|-------|
| `depends_on: condition: service_healthy` | Yes | IGNORED | Yes | Most critical gap |
| `depends_on: condition: service_started` | Yes | Yes | Yes | Basic ordering works |
| `depends_on: condition: service_completed_successfully` | Yes | IGNORED | Partial | Use `wait_for_completed()` |
| Top-level `volumes:` (basic) | Yes | Yes | Yes | |
| Top-level `volumes: name:` | Yes | IGNORED | Yes | Use `--project-name` instead |
| `container_name:` | Yes | Yes | Yes | Always set explicitly |
| `healthcheck:` definition | Yes | Yes | Yes | Runs but not enforced for deps |
| `restart: always` | Yes | Yes | Yes | But no daemon restart (see sec 11) |
| `restart: unless-stopped` | Yes | Yes | Yes | |
| `restart: "no"` | Yes | Yes | Yes | One-shot containers |
| `env_file:` (simple KEY=VALUE) | Yes | Yes | Yes | |
| `env_file:` (quoted values) | Yes | Partial | Partial | Avoid quotes |
| `environment:` | Yes | Yes | Yes | |
| YAML merge key (`<<: *anchor`) | Yes | Partial | Yes | Test before relying |
| `pull_policy: never` | Yes | Yes | Yes | For locally-built images |
| `compose pull --ignore-buildable` | Yes | No | No | Fall back to explicit list |
| `compose down` (clean removal) | Yes | Partial | Partial | May leave stale containers |
| `networks:` (basic) | Yes | Yes | Yes | |
| `networks: external: true` | Yes | Partial | Yes | |
| `cap_add:` | Yes | Yes | Yes | IPC_LOCK works rootless |
| `privileged: true` | Yes | Yes (limited) | Yes (limited) | No CAP_NET_RAW rootless |
| `--format json` output | Structured | Different schema | Different schema | Use inspect instead |
| `compose exec` | Yes | Yes | Yes | |
| `compose logs` | Yes | Yes | Yes | |
| `profiles:` | Yes | No | Partial | Avoid |
| `compose watch` | Yes | No | No | Docker-only feature |

---

## 14. Cross-Runtime Best Practices

These 10 rules ensure compose files and deploy scripts work correctly under both Docker and Podman runtimes.

### Rule 1: Always set explicit `container_name`

Prevents the underscore-vs-hyphen naming divergence. Every service must have a deterministic, predictable container name.

### Rule 2: Never use the `name:` property on volumes

Use `--project-name` to control the volume name prefix. Declare volumes in the top-level section but leave them bare.

### Rule 3: Use fully qualified image references

Always include the registry domain (`docker.io/`, `ghcr.io/`). Podman's default registry resolution differs from Docker's.

```yaml
image: docker.io/postgres:16    # CORRECT
image: postgres:16              # INCORRECT: ambiguous registry
```

### Rule 4: Implement staged startup in deploy scripts

Never rely solely on `depends_on: service_healthy` for startup ordering. Deploy scripts must explicitly start backing services, wait for health, then start application services.

### Rule 5: Use `wait_for_healthy()` or `wait_for_http()` instead of compose dependency conditions

Poll health via `$CONTAINER_ENGINE inspect` or HTTP checks. These work identically across runtimes.

### Rule 6: Keep env files in simple KEY=VALUE format

No quotes, no variable interpolation, no trailing comments. This is the lowest common denominator that works everywhere.

### Rule 7: Clean up stale containers after compose down

Always check for leftover containers after `compose down` and force-remove them. Clean up orphaned pods and networks on Podman.

### Rule 8: Detect runtime via `CONTAINER_ENGINE` variable

Never hardcode `docker` or `podman` in compose files or scripts. Use the `detect_runtime()` function from `lib/common.sh` or accept `CONTAINER_ENGINE` from Ansible.

### Rule 9: Handle `compose pull` flag differences

Use the fallback pattern: try Docker-specific flags first, fall back to explicit service names.

```bash
compose pull --ignore-buildable 2>/dev/null || \
  compose pull service1 service2 service3
```

### Rule 10: Test compose changes on both runtimes

Before merging compose file changes, verify they work on both a Docker host (NetBox VM) and a Podman host (all other VMs). The CI pipeline runs linting but does not currently test runtime behavior.

---

## Appendix: Compose File Naming Convention

The codebase uses two naming patterns for compose files:

| Pattern | Used By | Reason |
|---------|---------|--------|
| `compose.yml` | OpenBao, NocoDB, n8n, Semaphore, Caddy, Postiz | Modern compose spec default filename |
| `docker-compose.yml` | NetBox | Legacy filename; NetBox's compose wrapper uses explicit `-f docker-compose.yml` |

Both are valid. New services should use `compose.yml`. The `compose()` wrapper in `lib/common.sh` uses `compose.yml` by default; the NetBox-specific wrapper overrides this.

## Appendix: Runtime Decision Flowchart

```mermaid
flowchart TD
    NEED{"Does the service need<br/>CAP_NET_RAW or host networking?"}
    NEED -->|"Yes"| STANDALONE["Run outside compose:<br/>sudo CONTAINER_ENGINE run --privileged --net=host"]
    NEED -->|"No"| COMPOSE_Q{"Is the service NetBox<br/>(or its Diode pipeline)?"}
    COMPOSE_Q -->|"Yes"| DOCKER_RT["Use Docker CE<br/>container_engine: docker"]
    COMPOSE_Q -->|"No"| PODMAN_RT["Use Podman + podman-compose<br/>container_engine: podman"]
    PODMAN_RT --> VERSION_Q{"podman-compose version?"}
    VERSION_Q -->|">= 1.3.0"| FULL["Full compose spec support<br/>depends_on + healthchecks enforced"]
    VERSION_Q -->|"< 1.3.0 (1.0.6)"| LIMITED["Limited support<br/>deploy scripts must handle health waits"]
```

## Appendix: References

- [podman-compose GitHub repository](https://github.com/containers/podman-compose)
- [podman-compose man page](https://docs.podman.io/en/latest/markdown/podman-compose.1.html)
- [Docker Compose specification](https://docs.docker.com/compose/compose-file/)
- [Podman networking (netavark)](https://docs.podman.io/en/latest/markdown/podman-network.1.html)
- [Compose spec depends_on reference](https://docs.docker.com/compose/how-tos/startup-order/)

## Appendix: UhhCraft — reference Podman service

[`platform/services/uhhcraft/deployment/`](../../platform/services/uhhcraft/deployment/) is the first agent-cloud service designed Podman-first from the start. Its `compose.yml` illustrates every pattern this document recommends:

| Pattern | UhhCraft applies it as |
|---------|------------------------|
| **Explicit `container_name:`** | `uhhcraft-postgres`, `uhhcraft-redis`, `uhhcraft-minio`, `uhhcraft-app` |
| **Fully-qualified image names** | `docker.io/library/postgres:16-alpine` (not bare `postgres:16-alpine`) — Podman requires the registry prefix; Docker accepts both |
| **No top-level `name:` property on volumes** | Volume keys are short (`postgres_data`, `redis_data`, `minio_data`); the project name `uhhcraft` (set by `name: uhhcraft` at the top of the file) prefixes them |
| **`depends_on` with `condition: service_healthy`** | The `app` service waits for `postgres`, `redis`, and `minio` to report healthy before starting |
| **Healthchecks on every backing service** | Postgres uses `pg_isready`, Redis uses authenticated `redis-cli ping`, MinIO uses its `/minio/health/ready` endpoint |
| **Loopback port binding** | App is published as `127.0.0.1:3000:3000` so only the central Caddy on the same host can reach it; backing services don't expose ports at all |
| **`env_file: [.env]`** | The Ansible-templated `.env` is the single source of compose env-vars; no literal values in `compose.yml` |
| **`nvidia.com/gpu=all` device** | Sister services [`inference-comfyui`](../../platform/services/inference-comfyui/deployment/compose.yml) and [`inference-hunyuan3d`](../../platform/services/inference-hunyuan3d/deployment/compose.yml) use the CDI handoff for GPU passthrough under Podman |

UhhCraft also demonstrates how to handle the Go + templ + sqlc generation lifecycle inside a multi-stage `Dockerfile` (Stage 1 installs the tool-chain, Stage 2 generates + builds, Stage 3 is distroless runtime). See [`platform/services/uhhcraft/deployment/Dockerfile`](../../platform/services/uhhcraft/deployment/Dockerfile) for the pattern any future Go service should mirror.

Compose file: [`platform/services/uhhcraft/deployment/compose.yml`](../../platform/services/uhhcraft/deployment/compose.yml). Use it as a template when starting a new Podman-first service.
