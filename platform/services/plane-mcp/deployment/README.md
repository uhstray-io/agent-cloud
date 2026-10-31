# plane-mcp — deployment

Author: Joseph A. Wisneski IV

Plane's MCP server, run as its own compose project beside Plane. The design, the environment and the operating rules are in [CLAUDE.md](CLAUDE.md).

## Files

| File | Purpose |
|---|---|
| `compose.yml` | The `mcp` service on Plane's network (`plane_default`). No ports. |
| `compose.local.yml` | Local mode: also joins `local-dev`. Publishes nothing. |
| `compose.publish.yml` | Outside local mode: the one host publish. |
| `deploy.sh` | Container lifecycle: check `.env` and `plane_default`, pull, start, wait until running. |
| `templates/env.j2` | `.env`, rendered by Ansible from OpenBao and inventory. |

## Deploy

Semaphore template `Deploy Plane MCP`, playbook `platform/playbooks/deploy-plane-mcp.yml`. Run it after `Deploy Plane`: it registers (or repairs) Plane's OAuth application for the server itself, so there is no manual step.
