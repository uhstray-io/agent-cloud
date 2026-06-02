# WisBot — Deployment

WisBot is a Discord voice/chat + LLM agent ([uhstray-io/WisBot](https://github.com/uhstray-io/WisBot), C#/.NET 10). It is built and published from its own repo to `ghcr.io/uhstray-io/wisbot`; this directory pulls and runs that image.

## Deploy

Deploys run through Semaphore (template: **Deploy WisBot** → `deploy-wisbot.yml`):

1. Ansible renders `templates/wisbot.env.j2` → `config/wisbot.env` (Discord token from OpenBao, guild id / WisAI endpoint from site-config inventory).
2. `deploy.sh` pulls the image and starts the container.
3. Health is verified at `GET /health` (200 once the Discord gateway is connected).

Never SSH to the VM and run `deploy.sh` directly — Semaphore injects the credentials.

## Configuration

| Source | Provides |
|--------|----------|
| OpenBao `secret/services/wisbot` | `discord_token` |
| site-config inventory | `wisbot_guild_id`, `wisbot_ollama_endpoint`, `container_engine`, `service_url` |
| `compose.yml` env / image tag | `WISBOT_LISTEN`, `WISBOT_IMAGE_TAG` |

No secrets or site-specific IDs are committed here — see `.env.example` for the shape (placeholders only).

## Prerequisites

- Target VM with a container runtime (Docker or Podman) — set `container_engine` per host in site-config.
- OpenBao reachable, `secret/services/wisbot` seeded with the bot token.
- The image published at `ghcr.io/uhstray-io/wisbot` (CI in the WisBot repo).
