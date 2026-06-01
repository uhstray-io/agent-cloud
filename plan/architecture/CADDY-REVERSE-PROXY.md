# Caddy Reverse Proxy Architecture

**Date:** 2026-05-06
**Status:** ACTIVE
**Contributors:** Network architecture review

**References:**
- [SERVICE-INTEGRATION-PLAN.md](SERVICE-INTEGRATION-PLAN.md) -- Service onboarding checklist
- [AUTOMATION-COMPOSABILITY.md](AUTOMATION-COMPOSABILITY.md) -- Composable task library and deploy patterns
- [CREDENTIAL-LIFECYCLE-PLAN.md](CREDENTIAL-LIFECYCLE-PLAN.md) -- Secret generation, storage, rotation
- `platform/services/caddy/deployment/` -- Templatized Caddyfile and compose stack

---

## Purpose

Caddy is the sole HTTPS ingress point for the uhstray-io platform. Every external request to a platform service passes through Caddy for TLS termination, routing, and security header enforcement. This document defines the architecture, integration patterns, and automation gaps for the Caddy reverse proxy.

---

## Role in the 4-Layer Model

Caddy operates at the **Platform Layer** -- it is infrastructure, not automation or AI. It has no business logic; its sole responsibility is accepting inbound HTTPS connections, terminating TLS, and forwarding requests to internal service VMs over HTTP.

```text
AI Layer         NemoClaw, NetClaw, WisBot, Claude Cowork
                 (consumers of services behind Caddy)

Guardrail Layer  OpenBao, Kyverno, OPA
                 (Caddy does not enforce policy -- it routes)

Automation Layer Ansible, Semaphore
                 (Caddy deployment should be orchestrated here -- GAP)

Platform Layer   Caddy (HTTPS ingress) -> Docker/Podman services on VMs
                 Proxmox VMs, container runtimes
```

Caddy is classified as **Infrastructure tier** per the SERVICE-INTEGRATION-PLAN.md taxonomy -- it requires a dedicated VM, has a critical-tier credential (CloudFlare API key), and is a single point of failure for all external access.

---

## Traffic Flow

```mermaid
flowchart LR
    Internet["Internet\n(Browser / API Client)"]
    CF["CloudFlare DNS\n(DNS-01 TLS Challenge)"]
    Caddy["Caddy VM\n(TLS Termination)"]
    SvcA["Service A VM\n(HTTP)"]
    SvcB["Service B VM\n(HTTP)"]
    SvcC["Service C VM\n(HTTP)"]

    Internet -->|"HTTPS :443"| Caddy
    CF -.->|"DNS-01 challenge\n(certificate issuance)"| Caddy
    Caddy -->|"HTTP :port"| SvcA
    Caddy -->|"HTTP :port"| SvcB
    Caddy -->|"HTTP :port"| SvcC
```

Key properties:
- **All external traffic enters through Caddy on port 443.** Port 80 redirects to 443 (Caddy default behavior).
- **TLS terminates at Caddy.** Backend services receive plain HTTP. No double encryption overhead.
- **CloudFlare is DNS only.** Traffic does not proxy through CloudFlare's network -- Caddy handles TLS directly using certificates obtained via DNS-01 challenge.
- **Internal services are not exposed to the internet.** Only Caddy's ports 80/443 are forwarded through the router.

---

## Service Routing Pattern

The Caddyfile uses environment variable substitution for all domains, IPs, and ports. No hardcoded values appear in the committed template.

### Standard Caddyfile Block

```caddyfile
{$SERVICE_DOMAIN} {
    tls {
        dns cloudflare {$CLOUDFLARE_API_KEY}
        resolvers 1.1.1.1 1.0.0.1
    }

    header {
        Strict-Transport-Security max-age=15552000;
    }

    reverse_proxy {$SERVICE_IP}:{$SERVICE_PORT}
}
```

### WebSocket-Aware Block

For services that require WebSocket support (e.g., collaborative editing, real-time dashboards):

```caddyfile
{$SERVICE_DOMAIN} {
    tls {
        dns cloudflare {$CLOUDFLARE_API_KEY}
        resolvers 1.1.1.1 1.0.0.1
    }

    header {
        Strict-Transport-Security max-age=15552000;
    }

    @ws {
        header Connection *Upgrade*
        header Upgrade websocket
    }

    reverse_proxy {$SERVICE_IP}:{$SERVICE_PORT_MAIN}
    reverse_proxy @ws {$SERVICE_IP}:{$SERVICE_PORT_WS}
}
```

### Variable Resolution

Environment variables are resolved at container startup. The current deployment uses `start-caddy.sh` to parse CLI arguments and export variables before running `docker compose up`. The target state replaces this with Ansible-templated `.env` files following the composable pattern.

### Per-Site Fragment (Composable Pattern)

The two patterns above use the central `Caddyfile` with `{$VAR}`-driven substitution and are appropriate for legacy services. **All new services should use the per-site fragment pattern** instead — it keeps the main Caddyfile small, gives each service its own rollout/rollback unit, and lets each service template arbitrary Caddy directives (CSP, route handlers, asset proxies) without touching shared infrastructure.

**Mechanism:**

```text
1. Each service ships templates/caddy-site.j2 (Jinja2)
       platform/services/<svc>/deployment/templates/caddy-site.j2

2. The service's deploy playbook renders the fragment on the service VM
       platform/playbooks/deploy-<svc>.yml  (Phase 5)

3. tasks/distribute-caddy-site.yml delegates to the central Caddy host,
   copies the rendered fragment into the mounted sites/ directory, and
   reloads Caddy in-place
       platform/playbooks/tasks/distribute-caddy-site.yml
```

The central `Caddyfile` enables this with one line:

```caddyfile
import sites/*.caddy
```

Each fragment is a standalone Caddy site block. Example shape (from `platform/services/uhhcraft/deployment/templates/caddy-site.j2`):

```caddyfile
{{ uhhcraft_domain }} {
    encode brotli gzip
    header { ... HSTS, CSP, ... }

    # Routed asset paths — cross-MinIO proxies
    handle_path /generated/img/* { reverse_proxy {{ comfyui_minio_upstream }} ... }
    handle_path /generated/3d/*  { reverse_proxy {{ hunyuan_minio_upstream }} ... }

    # Static + app
    handle /static/*  { reverse_proxy {{ uhhcraft_upstream }} ... }
    reverse_proxy {{ uhhcraft_upstream }} { ... }
}
```

**Reload safety:**

`caddy reload --config /etc/caddy/Caddyfile` is zero-downtime — inflight requests complete; new requests use the new config; cert state is preserved. If a fragment is malformed, Caddy refuses to reload and keeps serving the previous config, so a broken deploy degrades to "no new fragment" rather than an outage.

**File ownership:**

The `sites/` directory is mounted read-only into the Caddy container (`./sites:/etc/caddy/sites:ro`). The only writer is `tasks/distribute-caddy-site.yml`, which runs as `ansible_user` on the Caddy host. Humans don't edit files here.

**Conflict resolution:**

Fragments are namespaced by filename (`sites/<svc>.caddy`). Two services cannot occupy the same domain — Caddy will fail to start if a domain is declared twice. Coordinate domain allocations in inventory, not in fragments.

**When to keep using the legacy `{$VAR}` pattern instead:**

- Existing services already in the main Caddyfile. Migrate when their deploy is rewritten to the composable pattern.
- One-off / temporary routes (e.g., a maintenance redirect) where a fragment would be overkill.

| Variable | Source | Example Placeholder |
|----------|--------|-------------------|
| `{$SERVICE_DOMAIN}` | FQDN from site-config | `svc.example.com` |
| `{$SERVICE_IP}` | Internal IP from site-config | `10.0.0.x` |
| `{$SERVICE_PORT}` | Service listening port | `8080` |
| `{$CLOUDFLARE_API_KEY}` | OpenBao secret | (API token) |

---

## TLS and DNS Integration

### Why DNS-01 Challenge

Caddy uses the **CloudFlare DNS-01 ACME challenge** for certificate issuance and renewal. This is required because:

1. **Wildcard certificates** -- DNS-01 is the only ACME challenge type that supports wildcard certs (`*.example.com`).
2. **No port 80 dependency** -- HTTP-01 requires port 80 to be publicly reachable. DNS-01 works entirely via DNS record creation, making it compatible with NAT and firewall configurations.
3. **Automatic renewal** -- Caddy handles certificate renewal automatically before expiry. The DNS-01 plugin creates and cleans up TXT records via the CloudFlare API.

### Certificate Lifecycle

```mermaid
sequenceDiagram
    participant Caddy
    participant ACME as Let's Encrypt ACME
    participant CF as CloudFlare DNS API

    Caddy->>CF: Create TXT record (_acme-challenge)
    CF-->>Caddy: TXT record propagated
    Caddy->>ACME: Request certificate (DNS-01 proof)
    ACME->>CF: Verify TXT record exists
    CF-->>ACME: Record confirmed
    ACME-->>Caddy: Issue certificate
    Caddy->>CF: Delete TXT record (cleanup)
    Note over Caddy: Certificate stored in caddy-data volume
    Note over Caddy: Auto-renews before expiry (~30 days)
```

### CloudFlare API Key Scope

The CloudFlare API key needs **Zone:DNS:Edit** permission for the target zone. It does not need account-level access or any permissions beyond DNS record management. A scoped API token (not a Global API Key) is the recommended credential type.

---

## Adding a New Service to the Proxy

When a new platform service needs external HTTPS access, follow these steps in coordination with the SERVICE-INTEGRATION-PLAN.md onboarding checklist.

**New services should use the per-site fragment pattern** (recommended path below). The legacy `{$VAR}`-in-main-Caddyfile path is preserved only for the existing services that haven't been migrated yet.

### Recommended path — per-site fragment

#### Step 1: Allocate DNS Record

In CloudFlare (or via Terraform/API), create an A record pointing the service's FQDN to the Caddy VM's public IP. Record the FQDN in site-config inventory.

#### Step 2: Write `templates/caddy-site.j2` in your service

Inside `platform/services/<svc>/deployment/templates/`, create `caddy-site.j2`. It's a full Caddy site block with Jinja2 variables for everything dynamic (domain, upstream IP, ports). UhhCraft's is the reference shape — read `platform/services/uhhcraft/deployment/templates/caddy-site.j2`.

Minimum useful skeleton:

```jinja
{{ '{{' }} svc_domain {{ '}}' }} {
    encode brotli gzip
    header {
        Strict-Transport-Security "max-age=15552000;"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
    }
    reverse_proxy {{ '{{' }} svc_upstream {{ '}}' }} {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

#### Step 3: Wire distribution into your deploy playbook

In `platform/playbooks/deploy-<svc>.yml`, add a phase that renders the fragment locally and then includes `tasks/distribute-caddy-site.yml`:

```yaml
- name: "Distribute Caddy fragment"
  hosts: <svc>_svc
  tasks:
    - name: "Render"
      ansible.builtin.template:
        # src resolves on the CONTROLLER (the checked-out monorepo), never the
        # remote clone path — ansible.builtin.template always reads src locally.
        src: "{{ playbook_dir }}/../services/<svc>/deployment/templates/caddy-site.j2"
        dest: "/tmp/<svc>-caddy-site.caddy"
      vars:
        svc_domain: "{{ service_url | regex_replace('^https?://', '') }}"
        svc_upstream: "{{ ansible_default_ipv4.address }}:<port>"

    - name: "Push + reload"
      ansible.builtin.include_tasks: tasks/distribute-caddy-site.yml
      vars:
        _fragment_src: "/tmp/<svc>-caddy-site.caddy"
        _fragment_name: "<svc>.caddy"
```

#### Step 4: Deploy

Run `deploy-<svc>.yml` via Semaphore. The fragment is rendered, copied to the central Caddy host's `sites/` directory, and Caddy is reloaded — all zero-downtime.

#### Step 5: Verify

- Confirm HTTPS access at `https://svc.example.com`.
- Check the Let's Encrypt certificate is issued.
- Confirm the security headers and CSP are what the service expects.

### Legacy path — `{$VAR}`-driven block

For services already on the legacy pattern (or for one-off routes):

1. Allocate the DNS record (same as Step 1 above).
2. Add a new block to `platform/services/caddy/deployment/Caddyfile` using `{$NEWSERVICE_DOMAIN}`, `{$NEWSERVICE_IP}`, `{$NEWSERVICE_PORT}`.
3. Add the variables to the `.env` template (or to `start-caddy.sh` CLI flags).
4. Reload Caddy: `<engine> exec caddy caddy reload --config /etc/caddy/Caddyfile` (`<engine>` is `podman` by default on this platform; `docker` only on the NetBox host).
5. Verify the same way.

The legacy path is being phased out as each service migrates to the composable deploy pattern. Don't add new services here unless there's a strong reason.

---

## Credential Management

### Current State

The CloudFlare API key is currently managed outside the composable pattern:
- The standalone `caddy` repo has the key hardcoded in its Caddyfile
- The agent-cloud template uses `{$CLOUDFLARE_API_KEY}` substitution
- The `start-caddy.sh` script accepts the key as a CLI argument (`-k`)
- site-config has a placeholder Caddyfile (0 bytes)

### Target State

The CloudFlare API key should follow the standard credential flow:

```mermaid
flowchart TD
    OB["OpenBao\nsecret/services/caddy\ncloudflare_api_key"]
    AN["Ansible manage-secrets.yml\nfetch key from OpenBao"]
    ENV[".env on Caddy VM\nCLOUDFLARE_API_KEY=..."]
    CADDY["Caddy container\nreads {$CLOUDFLARE_API_KEY}"]

    OB --> AN
    AN -->|"Jinja2 template"| ENV
    ENV -->|"compose env_file"| CADDY
```

### OpenBao Secret Path

| Path | Key | Type | Rotation |
|------|-----|------|----------|
| `secret/services/caddy` | `cloudflare_api_key` | `user` (externally managed) | 90-day rotation cycle |

The CloudFlare API key is classified as `type: user` in `_secret_definitions` because it is created externally (in CloudFlare's dashboard), not auto-generated. The `manage-secrets.yml` task fetches it from OpenBao but never generates it.

### Rotation Policy

- **Rotation cycle:** 90 days (critical-tier credential per CREDENTIAL-LIFECYCLE-PLAN.md)
- **Rotation method:** Generate new scoped API token in CloudFlare, store in OpenBao, redeploy Caddy, verify certificates still renew, then revoke old token in CloudFlare
- **Dual-valid window:** Both old and new tokens are active during rotation. Caddy uses the new token after redeployment. The old token is revoked only after confirming successful certificate renewal with the new one.

---

## Automation Gap

Caddy is the only infrastructure-tier service that lacks full composable automation. The following items are needed to bring it into compliance with the platform deployment pattern.

### Missing Components

| Component | Status | Required Action |
|-----------|--------|-----------------|
| **Ansible playbook** (`deploy-caddy.yml`) | Missing | Create 3-phase playbook: secrets -> deploy -> verify |
| **Clean deploy playbook** (`clean-deploy-caddy.yml`) | Missing | Create using `tasks/clean-service.yml` |
| **Semaphore templates** | Missing | Add "Deploy Caddy" and "Clean Deploy Caddy" to `platform/semaphore/templates.yml` |
| **OpenBao secret path** | Missing | Store CloudFlare API key at `secret/services/caddy` |
| **Jinja2 env template** | Missing | Create `platform/services/caddy/deployment/templates/caddy.env.j2` |
| **deploy.sh** | Partial (`start-caddy.sh` exists) | Refactor to container-lifecycle-only `deploy.sh` following composable pattern |
| **Health check** | Missing | Add to `validate-all.yml` (check HTTPS response from Caddy) |
| **SSH key pair** | Missing | Generate and store in OpenBao at `secret/services/ssh/caddy` |
| **Inventory entry** | Missing | Add `caddy_svc` host group to site-config inventory |

### Deployment Classification

Per SERVICE-INTEGRATION-PLAN.md, Caddy is an **auxiliary-to-infrastructure** tier service:

- **No database** -- state is in the `caddy-data` volume (certificates) and `caddy-config` volume
- **No runtime OpenBao access** -- credentials injected at deploy time via `.env`
- **Single container** -- simple compose stack
- **3-phase playbook** (simplified pattern): manage-secrets -> deploy -> verify

### Priority

Caddy automation should be implemented as part of the next service onboarding wave (alongside NocoDB and n8n migration). The CloudFlare API key must be stored in OpenBao before Caddy can use the composable credential flow.

---

## Backend Policies

### HTTP vs HTTPS Backend Connections

| Scenario | Backend Protocol | When to Use |
|----------|-----------------|-------------|
| **Plain HTTP** (default) | `reverse_proxy http://IP:PORT` | All internal services on trusted network. Standard pattern. |
| **HTTPS with TLS skip verify** | `reverse_proxy https://IP:PORT { transport http { tls_insecure_skip_verify } }` | Backend has self-signed cert (e.g., Proxmox API). Use sparingly -- only when the backend requires HTTPS and you control the certificate. |
| **HTTPS with trusted CA** | `reverse_proxy https://IP:PORT { transport http { tls_trusted_ca_certs /path/to/ca.pem } }` | Backend uses internal CA. Preferred over skip verify when an internal CA exists. |

**Default policy:** All platform services run plain HTTP on the internal network. Caddy handles TLS termination. Do not configure backends with HTTPS unless the service explicitly requires it (e.g., Proxmox's management API).

### HSTS Policy

All Caddyfile blocks include the `Strict-Transport-Security` header:

```text
Strict-Transport-Security max-age=15552000;
```

This tells browsers to always use HTTPS for the domain for 180 days. Notes:

- **Do not enable `includeSubDomains`** unless all subdomains are also served via HTTPS through Caddy.
- **Do not enable `preload`** until the HSTS policy has been stable for at least 6 months and all services are confirmed working under HTTPS.
- The 180-day `max-age` is a conservative starting point. Increase to 1 year (`31536000`) after confirming no services need HTTP fallback.

### Security Headers

Beyond HSTS, consider adding these headers as the proxy matures:

```caddyfile
header {
    Strict-Transport-Security max-age=15552000;
    X-Content-Type-Options nosniff
    X-Frame-Options DENY
    Referrer-Policy strict-origin-when-cross-origin
}
```

These should be evaluated per-service -- some services (e.g., collaborative editors, iframe-embedded dashboards) may need `X-Frame-Options` set to `SAMEORIGIN` instead of `DENY`.

---

## Caddyfile Management: Public vs Private

The Caddyfile in `agent-cloud/platform/services/caddy/deployment/Caddyfile` is the **public template** -- it contains only environment variable references (`{$VAR}`), no real domains or IPs.

The production Caddyfile with real routing configuration lives in **site-config**. This split follows the public/private repository boundary:

| Repository | File | Contains |
|------------|------|----------|
| agent-cloud | `platform/services/caddy/deployment/Caddyfile` | Template with `{$VAR}` placeholders |
| agent-cloud | `platform/services/caddy/deployment/compose.yml` | Compose stack definition |
| site-config | Caddy configuration files | Real FQDNs, IPs, and routing rules |

When the composable automation is implemented, the production Caddyfile will be rendered from Jinja2 templates using values from site-config inventory, matching the pattern used by all other services.

---

## Kubernetes Migration Path

When the platform migrates to Kubernetes (k0s), Caddy's role shifts:

- **Current (Compose):** Caddy runs as a standalone container on a dedicated VM, routing to other VMs by IP.
- **Future (Kubernetes):** Caddy becomes an Ingress controller, routing to Kubernetes Services by name. The Caddyfile is replaced by Ingress resources or a CRD-based configuration.

The CloudFlare DNS-01 integration remains relevant in Kubernetes via [cert-manager](https://cert-manager.io/) with a CloudFlare DNS01 solver, or by running Caddy as the ingress controller directly.
