# WisBot Deployment

Operational reference for deploying WisBot — the Discord voice/chat + LLM agent
([uhstray-io/WisBot](https://github.com/uhstray-io/WisBot), C#/.NET 10).

WisBot is built and published from its own repo to `ghcr.io/uhstray-io/wisbot`.
This directory only pulls and runs that prebuilt image — it does not build source.

## Service stack

- **wisbot** (`agent-wisbot`) — single container, the prebuilt image. Exposes an
  internal HTTP `GET /health` on port 8080 (no public web UI, no Caddy route).
  Connects outbound to Discord and to WisAI/Ollama for `/wisllm`.

## Configuration layers

- `compose.yml` — pulls `ghcr.io/uhstray-io/wisbot:${WISBOT_IMAGE_TAG:-latest}`,
  binds the health port via `${WISBOT_LISTEN:-127.0.0.1}`, mounts named volumes
  for the SQLite DB (`/app/data`) and recordings (`/app/recordings`).
- `config/wisbot.env` — rendered by Ansible (`manage-secrets.yml`) from
  `templates/wisbot.env.j2`. Holds the Discord token (from OpenBao) and site
  config (guild id, WisAI endpoint). Mode 0600, gitignored, never committed.

## Secrets

- OpenBao path `secret/services/wisbot` → `discord_token` (operator-managed,
  `type: user` — never auto-generated).
- No runtime OpenBao access / AppRole needed: the token is supplied at deploy
  time via the env file only. Semaphore's orchestrator AppRole templates it.

## Common commands

Deploys run through Semaphore (never SSH + run deploy.sh by hand). For reference,
the scripts in this directory are container-lifecycle only:

- `deploy.sh` — verify `config/wisbot.env` exists, `compose pull`, `compose up -d`,
  wait for `/health`.
- `validate.sh` — health check `/health` (200 = ready).

## File structure

```text
agents/wisbot/
  deployment/
    compose.yml           pulls the GHCR image, volumes, healthcheck
    deploy.sh             container lifecycle only (no secrets, no OpenBao)
    validate.sh           post-deploy health check
    templates/
      wisbot.env.j2       Ansible renders -> config/wisbot.env
    .env.example          illustrative placeholders
  context/                agent skills / prompts / use-cases / architecture
```

## Notes

- `deploy.sh` resolves `platform/lib/common.sh` via `CLONE_DIR` (set by Ansible)
  and uses the shared `compose`/`wait_for_http` helpers.
- The image installs `libopus0` for Discord voice; `libsodium`/SQLite natives
  ship with the .NET publish. See the WisBot repo for image internals.
- Deployment is wired by `platform/playbooks/deploy-wisbot.yml` (composable
  pattern). Inventory host vars (guild id, WisAI endpoint) live in site-config.
