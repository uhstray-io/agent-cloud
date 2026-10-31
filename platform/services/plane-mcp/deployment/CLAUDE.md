# CLAUDE.md — platform/services/plane-mcp/deployment

Author: Joseph A. Wisneski IV

plane-mcp — Plane's MCP server (`makeplane/plane-mcp-server`, pinned tag) run as its own compose project, beside Plane. One container, `plane-mcp`, on Plane's stack network. Container lifecycle only: the OAuth credentials come from OpenBao through Ansible, never from this directory.

## Why it is a separate project

1. **It cannot start with Plane.** In HTTP mode `plane_mcp/__main__.py` builds the OAuth provider, and `plane_mcp/auth/plane_oauth_provider.py` raises `ValueError` when `PLANE_OAUTH_PROVIDER_CLIENT_ID` or `PLANE_OAUTH_PROVIDER_CLIENT_SECRET` is empty. Those values exist only after Plane is up and the MCP app is registered in it.
2. **Plane's compose file is not ours to edit.** `platform/services/plane/deployment/CLAUDE.md`: Plane's Compose file is fetched per deploy and never committed. The MCP server cannot be a service in it.
3. **Each workflow is independent** (`AGENTS.md`, Critical Deployment Rule 3). The MCP server is redeployed, rotated and retired without touching Plane.

## Networks

| Network | Declared as | Why |
|---|---|---|
| `plane_default` | `external: true`, `name: plane_default` in `compose.yml` | Plane runs as compose project `plane`, so this is its stack network. The server reaches `api:8000` and `plane-redis:6379` by service name. This project never creates or removes it. |
| `local-dev` | `external: true` in `compose.local.yml` | Local mode only. The local Caddy reaches `plane-mcp:8211` by name. |

`deploy.sh` refuses to start when `plane_default` is absent (`deploy Plane first`).

## Files

| File | Purpose |
|---|---|
| `compose.yml` | The `mcp` service on `plane_default`. No ports. |
| `compose.local.yml` | Local mode: also joins `local-dev`. Publishes nothing. |
| `compose.publish.yml` | Outside local mode only: the one host publish, `PLANE_MCP_BIND:PLANE_MCP_PORT` to container port 8211. |
| `deploy.sh` | Container lifecycle: checks `.env` and `plane_default`, pulls, runs `compose_up_if_changed`, waits until running and stable. |
| `templates/env.j2` | Renders `.env`. Refuses to render when the OAuth client id or secret is empty. |

## How the overlays are applied

`deploy.sh` sets `COMPOSE_OVERLAYS="compose.publish.yml ..."` unless `LOCAL_MODE=true` and `compose.local.yml` exists. `platform/lib/common.sh:compose()` applies `compose.local.yml` in local mode itself.

## Environment (names only; rendered into `.env` by `templates/env.j2`)

| Variable | Source | Note |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | fixed `plane-mcp` | |
| `PLANE_MCP_VERSION` | `plane_mcp_version`, default `v0.3.5` | Image tag. Compose requires it (`${PLANE_MCP_VERSION:?...}`). |
| `PLANE_MCP_BIND`, `PLANE_MCP_PORT` | `plane_mcp_bind` (default `127.0.0.1`), `plane_mcp_port` (default `8211`) | Used only by `compose.publish.yml`. |
| `PLANE_BASE_URL` | `plane_public_url` | Browser-facing Plane URL. The authorize redirect goes here. |
| `PLANE_INTERNAL_BASE_URL` | fixed `http://api:8000` | Server-to-server Plane API, on `plane_default`. |
| `PLANE_OAUTH_PROVIDER_BASE_URL` | `plane_mcp_public_url` | This server's public origin. No trailing slash, no path. |
| `PLANE_OAUTH_PROVIDER_CLIENT_ID` | `_plane_mcp_client_id` (OpenBao) | Required. The render fails when empty. |
| `PLANE_OAUTH_PROVIDER_CLIENT_SECRET` | `_plane_mcp_client_secret` (OpenBao) | Required. The render fails when empty. |
| `PLANE_OAUTH_ALLOWED_REDIRECT_URIS` | `plane_mcp_allowed_redirect_uris`, default empty | Comma-separated string, extra to the built-in allowlist (`plane_mcp/server.py`). |
| `REDIS_URL` | fixed `redis://plane-redis:6379/1` | OAuth token store. DB 1 of the shared `plane-redis`. |
| `LOG_PAYLOADS` | fixed `false` | See Logging. |
| `LOG_USER_INFO` | fixed `false` | Display names are PII (`plane_mcp/__main__.py`). |

The variable names are the ones the upstream reads: `PLANE_OAUTH_ALLOWED_REDIRECT_URIS` by `os.getenv` in `plane_mcp/server.py`, `REDIS_URL` in `plane_mcp/storage.py`, the rest in `plane_mcp/server.py` and `plane_mcp/auth/plane_oauth_provider.py`. Checked against tag `v0.3.5`.

## Endpoints

Container port 8211 (hardcoded in `plane_mcp/__main__.py`, bound to `0.0.0.0`). Mounts are the ones in `plane_mcp/__main__.py` with `MCP_PATH_PREFIX` unset, which this service does not set:

| Path | Transport | Auth |
|---|---|---|
| `/http/mcp` | Streamable HTTP | Plane OAuth |
| `/http/api-key/mcp` | Streamable HTTP | API key in a header |
| `/sse` | SSE, mounted at `/` | Plane OAuth |

The `/mcp` suffix inside the header mount is FastMCP's default path. It is not checked against the installed package.

## Logging

The upstream default is `LOG_PAYLOADS=true` (`plane_mcp/server.py`). That logs every tool call's arguments and results, which is work-item content. Here it is `false`, so that content does not reach the container log or Loki. Turning it on in any environment is a decision for Joseph, not a tuning knob.

## Deploy

- Semaphore template `Deploy Plane MCP`, playbook `platform/playbooks/deploy-plane-mcp.yml`. The playbook renders `.env` from `templates/env.j2`, reads the OAuth credentials from OpenBao, runs `deploy.sh`, and records the result.
- `deploy.sh` prints `DEPLOY_CHANGED=true|false` (from `compose_up_if_changed`). The playbook reads it.
- Changing `.env` alone recreates the container, because `.env` is hashed into the inputs label.

## What not to do

- Don't put the OAuth client secret in `compose.yml`, `deploy.sh` or a command line. It flows OpenBao → `.env` only.
- Don't add a `ports:` to `compose.yml`. The host publish belongs in `compose.publish.yml`, which is prod only.
- Don't set `LOG_PAYLOADS=true` without Joseph's decision.
- Don't append `/http` or a trailing slash to `PLANE_OAUTH_PROVIDER_BASE_URL`. The server appends `/http` itself.
- Don't generate or read secrets in `deploy.sh`. Critical Deployment Rule 2.

## Related

- Plane: `platform/services/plane/deployment/CLAUDE.md`
- Plane's plan: `plan/development/16-plane-work-management.md`
- Upstream: `makeplane/plane-mcp-server`, tag `v0.3.5`
- Root conventions: `CLAUDE.md` (repository root, symlinked as `AGENTS.md`)
