# NemoClaw Deployment

Deploy [NemoClaw](https://github.com/uhstray-io/NemoClaw) — an AI agent sandbox powered by [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) and [OpenClaw](https://docs.openclaw.ai). This directory manages deployment scripts and general config for running NemoClaw locally (macOS via Colima) or on a remote server.

## Prerequisites

| Dependency | Notes |
|---|---|
| **Node.js 20+** | Installed automatically by `install.sh` via nvm if missing |
| **Docker** | Linux: native Docker. macOS: [Colima](https://github.com/abiosoft/colima) + Docker CLI (`brew install colima docker`) |
| **Python 3.11+** | For the NemoClaw blueprint runner |
| **NVIDIA API Key** | Free from [build.nvidia.com](https://build.nvidia.com) — required for cloud inference |

> **macOS note**: Podman is not yet supported by OpenShell. Use Colima as the container runtime.

## Quick Start

```bash
# 1. Clone the agent-cloud monorepo
git clone https://github.com/uhstray-io/agent-cloud.git
cd agent-cloud/agents/nemoclaw/deployment

# 2. Copy site-specific config from site-config (private repo)
#    - config/credentials.json, sandboxes.json, discord.json
#    - secrets/*.txt (NVIDIA, Gemini, Discord API keys)

# 3. Set environment variables (or create .env from .env.example)
export NEMOCLAW_HOST=<target-vm-ip>
export NEMOCLAW_USER=<ssh-user>

# 4. Start Colima (macOS only)
colima start --cpu 6 --memory 12 --disk 40

# 5. Deploy
./deploy.sh --local --onboard
```

Once complete:

```bash
nemoclaw <sandbox-name> connect    # SSH into the sandbox
openclaw tui                       # Start the chat interface
```

## Deploying

| Command | What it does |
|---|---|
| `./deploy.sh --local --onboard` | Fresh install — builds everything from scratch (destructive) |
| `./deploy.sh --local` | Update — syncs config, injects env vars, preserves sandbox state |
| `./deploy.sh --onboard` | Fresh install on remote server (rsyncs config, SSHs once) |
| `./deploy.sh` | Update remote server |
| `./update.sh` | Check if fork is behind upstream + update |

**Default is update/migrate** — preserves conversations, paired devices, and agent history. Use `--onboard` only for fresh installs or when the Dockerfile changes.

## Configuration

### Config Files

```
config/
  sandboxes.json         # Sandbox name + policy presets (site-config)
  credentials.json       # NVIDIA_API_KEY for credential store (site-config)
  discord.json           # Discord guild allowlist and user IDs (site-config)
  presets/
    google.yaml          # Custom network policy presets (public)
```

Files marked **(site-config)** are not in this repo — copy them from the private `site-config` repository.

### Secrets

deploy.sh loads secrets from `$NEMOCLAW_SECRETS_DIR` (default: `./secrets/`) as environment variables:

| File | Env Var | Purpose |
|---|---|---|
| `nvidia-api-key.txt` | `NVIDIA_API_KEY` | NVIDIA Nemotron inference |
| `gemini-api-key.txt` | `GEMINI_API_KEY` | Google Search grounding for web_search |
| `discord-bot-token.txt` | `DISCORD_BOT_TOKEN` | Discord bot integration |

Secrets are stored in OpenBao at `secret/services/nemoclaw` and backed up in the private `site-config` repo.

### Adding Integrations

To add a new channel (e.g., Slack, Telegram):
1. Create a secret file in site-config: `nemoclaw/secrets/slack-bot-token.txt`
2. Add the env var to `deploy.sh`'s `build_env_file()` function
3. Add a channel config: `config/slack.json`
4. Add the policy preset to `sandboxes.json`
5. Run `./deploy.sh --local --onboard` (Dockerfile change requires rebuild)

Channel configs are baked into `openclaw.json` at build time. Tokens activate via env vars at runtime — never stored in the image.

## Architecture

deploy.sh is a thin wrapper. For `--onboard`, it delegates to NemoClaw's own [`install.sh`](https://github.com/uhstray-io/NemoClaw/blob/main/install.sh) which handles:
- Node.js installation (via nvm)
- NemoClaw CLI build + link
- OpenShell gateway setup
- Sandbox image build from Dockerfile
- Inference provider configuration
- Policy preset application

deploy.sh adds on top:
- Config syncing (presets, channel configs, credentials)
- Secret injection into `/sandbox/.env`
- DNS proxy setup (fixes sandbox DNS resolution)
- Post-deploy validation (14 automated checks)

### Environments

| Environment | Runtime | Notes |
|---|---|---|
| **Local (macOS)** | Colima + Docker CLI | Working |
| **Remote server** | Native Docker (Ubuntu) | Gateway crash on image push — use `--local` on server |

## Validation

deploy.sh runs `validate.sh` automatically after every deploy:

```bash
./validate.sh --local
```

Checks 14 conditions: Docker running, gateway healthy, sandbox ready, DNS resolution, inference provider, agent responds, web search works, API keys present, policies enabled.

## Useful Commands

```bash
# Sandbox management
nemoclaw <name> connect           # SSH into sandbox
nemoclaw <name> policy-list       # Show enabled presets
nemoclaw status                   # List sandboxes

# Inside the sandbox
openclaw tui                      # Interactive chat
openclaw agent --agent main --local -m "hello" --session-id test

# Infrastructure
openshell status                  # Gateway health
openshell term                    # Monitoring TUI
colima status                     # Colima VM status (macOS)
```

## Resources

| Resource | Link |
|---|---|
| Our Fork | https://github.com/uhstray-io/NemoClaw |
| Upstream | https://github.com/NVIDIA/NemoClaw |
| NemoClaw Docs | https://docs.nvidia.com/nemoclaw/latest/ |
| OpenShell Docs | https://docs.nvidia.com/openshell/latest/ |
| OpenClaw Docs | https://docs.openclaw.ai |
| Discord Setup | https://docs.openclaw.ai/channels/discord |
