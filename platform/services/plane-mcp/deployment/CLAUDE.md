# CLAUDE.md — platform/services/plane-mcp/deployment

Author: Joseph A. Wisneski IV

plane-mcp — Plane's MCP server (`makeplane/plane-mcp-server`, pinned tag) run as its own compose project, beside Plane. One container, `plane-mcp`, on Plane's stack network. Container lifecycle only: the OAuth credentials come from OpenBao through Ansible, never from this directory.

## Why it is a separate project

1. **It cannot start with Plane.** In HTTP mode `plane_mcp/__main__.py` builds the OAuth provider, and `plane_mcp/auth/plane_oauth_provider.py` raises `ValueError` when `PLANE_OAUTH_PROVIDER_CLIENT_ID` or `PLANE_OAUTH_PROVIDER_CLIENT_SECRET` is empty. Those values exist only after Plane is up and the MCP app is registered in it.
2. **Plane's compose file is not ours to edit.** `platform/services/plane/deployment/CLAUDE.md`: Plane's Compose file is fetched per deploy and never committed. The MCP server cannot be a service in it.
3. **Each workflow is independent** (`AGENTS.md`, Critical Deployment Rule 3). The MCP server is redeployed, rotated and retired without touching Plane.
   The reverse does not hold: `Clean Deploy Plane` removes `plane-mcp` too, because `tasks/clean-service.yml` removes every
   container whose name starts with the service name `plane`. Re-run `Deploy Plane MCP` after a clean Plane deploy; it
   re-creates the OAuth application and stores the new credentials.

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

The token endpoint `/http/api-key/mcp` was exercised by the deploy's verify step (local Semaphore task 10275). A GET on
either MCP endpoint answers 405; health is the OAuth protected-resource document at
`/.well-known/oauth-protected-resource/http/mcp`, whose `resource` names the public URL (what `validate-all.yml` checks).

## Logging

The upstream default is `LOG_PAYLOADS=true` (`plane_mcp/server.py`). That logs every tool call's arguments and results, which is work-item content. Here it is `false`, so that content does not reach the container log or Loki. Turning it on in any environment is a decision for Joseph, not a tuning knob.

## Deploy

- Semaphore template `Deploy Plane MCP`, playbook `platform/playbooks/deploy-plane-mcp.yml`. The playbook registers or repairs Plane's OAuth application (`platform/services/plane/deployment/register_mcp_app.py`), keeps
  the credentials in OpenBao (`plane_mcp_client_id`, `plane_mcp_client_secret`), renders `.env` from `templates/env.j2`, runs
  `deploy.sh`, and verifies the token endpoint. It records no service-deploy step result (that is keyed by the host's service,
  `plane`).
- The playbook also installs the application into the workspace on every pass, as the automation account (the workspace
  admin). Plane refuses a member's authorization until an admin has installed the app ("This app can only be installed after
  a workspace admin installs it"); a repeat install only sets the installation back to installed.
- Plane shows the client secret only at create or regenerate. The playbook reuses the stored one only when OpenBao's
  `plane_mcp_client_id` is the id Plane lists; otherwise it regenerates, so a new id is never paired with an old secret.
- Locally, `validate-all.yml` runs inside the Semaphore container, where `*.agent-cloud.test` resolves to the container's own
  loopback, so its public-URL checks (this one and Plane's) report unhealthy there. Check from the workstation instead.
- `deploy.sh` prints `DEPLOY_CHANGED=true|false` (from `compose_up_if_changed`). The playbook reads it.
- Changing `.env` alone recreates the container, because `.env` is hashed into the inputs label.

## What not to do

- Don't put the OAuth client secret in `compose.yml`, `deploy.sh` or a command line. It flows OpenBao → `.env` only.
- Don't add a `ports:` to `compose.yml`. The host publish belongs in `compose.publish.yml`, which is prod only.
- Don't set `LOG_PAYLOADS=true` without Joseph's decision.
- Don't append `/http` or a trailing slash to `PLANE_OAUTH_PROVIDER_BASE_URL`. The server appends `/http` itself.
- Don't generate or read secrets in `deploy.sh`. Critical Deployment Rule 2.

## Harness clients

`scripts/plane-mcp-client.py` writes one secret-free entry, `plane`, into each harness's user-level configuration. The entry carries the URL and nothing else: no header, no token, no helper. Each harness runs Plane's OAuth sign-in itself. The templates are `platform/services/plane-mcp/context/clients/claude-code.json` and `pi-mcp.json`.

```bash
# Production (needs the `plane-mcp-bypass-challenge` WAF rule applied; see Production edge)
python3 scripts/plane-mcp-client.py --host plane-mcp.uhstray.io

# Local (the local Caddy serves Plane MCP on :8443)
python3 scripts/plane-mcp-client.py --host plane-mcp.agent-cloud.test:8443
```

- `--host` takes a bare hostname with an optional `:port`, and reads `PLANE_MCP_HOST` when the flag is absent. A scheme or a path is refused.
- `--harness claude|pi|both` (default `both`), `--dry-run` reports and changes nothing, `--remove` deletes exactly `plane` from each harness. Output names only.
- Claude Code: user scope, through `claude mcp add-json -s user plane` with `{"type": "http", "url": "https://<host>/http/mcp"}`.
- pi: `mcpServers.plane` = `{"url": "https://<host>/http/mcp"}` in `~/.pi/agent/mcp.json`, a merge-only edit. Other servers and keys are kept. An unparseable file is refused and left untouched.
- Why pi needs no helper: the pi-mcp-adapter 5.1.0 package documents that a URL-only HTTP server gets OAuth automatically (its `docs/auth.md`, lines 43-49), and that manual sign-in is the default flow (its `docs/auth.md`, lines 118-126). The config file it reads is `~/.pi/agent/mcp.json` (its `README.md`, line 81).

Sign-in, after the entry exists:

- Claude Code: `claude mcp login plane` (the `login` subcommand is listed in `claude mcp --help` for Claude Code 2.1.318).
- pi: `/mcp-auth plane` in an interactive session (pi-mcp-adapter `docs/auth.md`, lines 120-126).

Notes:

- `/http/api-key/mcp` is a different endpoint with a workspace token. weft's builder entry for it is task 5.4, in the weft repository. This script does not write it.
- Local TLS uses the local step-ca certificate. `make local-tls-trust` trusts that root in the macOS keychain (`LOCAL-DEV-README.md:83`). Whether each harness's TLS stack honours that trust is unverified here.
- Prod sign-in from a harness is untested until the `plane-mcp-bypass-challenge` rule is applied (see Production edge).

## Related

- Plane: `platform/services/plane/deployment/CLAUDE.md`
- Plane's plan: `plan/development/16-plane-work-management.md`
- Upstream: `makeplane/plane-mcp-server`, tag `v0.3.5`
- Root conventions: `CLAUDE.md` (repository root, symlinked as `AGENTS.md`)

## Production edge (not yet in place)

Locally Caddy reaches `plane-mcp:8211` on `local-dev`. Production needs, in site-config and the edge, before the first run:
`plane_mcp_public_url` and a `plane_mcp_bind` the central Caddy can reach (the default `127.0.0.1` is local only), a firewall
allow on the published port from Caddy, a `caddy_managed_sites` route, the Cloudflare DNS record, and a decision on the
Cloudflare challenge for non-browser MCP clients (the `/v1/*` precedent is `inference-api-bypass-challenge` in `waf.tf`).

**Harness client entry (task 5.3) is in this repository** (`scripts/plane-mcp-client.py`, see Harness clients). The zone challenges every `plane-mcp.uhstray.io` path today (observed 2026-11-03: `/http/mcp` and `/http/api-key/mcp` answer 403 `cf-mitigated: challenge`). The skip for the MCP machine paths is the `plane-mcp-bypass-challenge` rule in `platform/infra/cloudflare/waf.tf`, applied with Apply Cloudflare Tofu. It widens the edge, so it was reviewed on its own.
