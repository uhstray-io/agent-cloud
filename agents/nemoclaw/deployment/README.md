# NemoClaw Deployment

> **Legacy standalone reference:** the scripts and examples below predate the
> platform credential/orchestration boundary. They describe that interface, not
> an authorized deployment procedure. Use the declared Semaphore **Deploy NemoClaw**
> workflow; verify its inventory and legacy wrapper wiring before rollout. Any
> missing integration must be fixed in code. Do not copy secret files or run a
> server-local deploy as a workaround for Semaphore.


Deploy [NemoClaw](https://github.com/uhstray-io/NemoClaw) — an AI agent sandbox powered by [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) and [OpenClaw](https://docs.openclaw.ai). This directory manages deployment scripts and general config for running NemoClaw locally (macOS via Colima) or on a remote server.

## Prerequisites

| Dependency | Notes |
|---|---|
| **Node.js 20+** | Installed automatically by `install.sh` via nvm if missing |
| **Docker** | Linux: native Docker. macOS: [Colima](https://github.com/abiosoft/colima) + Docker CLI (`brew install colima docker`) |
| **Python 3.11+** | For the NemoClaw blueprint runner |
| **NVIDIA API Key** | Free from [build.nvidia.com](https://build.nvidia.com) — required for cloud inference |

> **macOS note**: Podman is not yet supported by OpenShell. Use Colima as the container runtime.

## Platform deployment

Use Semaphore **Deploy NemoClaw**, backed by
[`deploy-nemoclaw.yml`](../../../platform/playbooks/deploy-nemoclaw.yml).
Declare the target and non-secret configuration in private inventory; credentials
remain owned by OpenBao. Verify the legacy wrapper's inventory and credential
wiring before rollout. Missing integration must be fixed in the playbook/task
path before deployment.

Routine updates must preserve conversations, paired devices and agent history.
The legacy `--onboard` mode rebuilds the sandbox and requires explicit destructive
operation authorization through the platform workflow.

## Configuration

### Historical standalone file layout

```
config/
  sandboxes.json         # Sandbox name + policy presets (site-config)
  credentials.json       # NVIDIA_API_KEY for credential store (site-config)
  discord.json           # Discord guild allowlist and user IDs (site-config)
  presets/
    google.yaml          # Custom network policy presets (public)
```

This table records inputs expected by the legacy wrapper. It is not an instruction
to copy credential files; the supported credential boundary is OpenBao → Ansible
rendering. Non-secret site configuration belongs in private inventory.

### Historical secret-file interface

The legacy deploy.sh loads secrets from `$NEMOCLAW_SECRETS_DIR` (default: `./secrets/`) as environment variables:

| File | Env Var | Purpose |
|---|---|---|
| `nvidia-api-key.txt` | `NVIDIA_API_KEY` | NVIDIA Nemotron inference |
| `gemini-api-key.txt` | `GEMINI_API_KEY` | Google Search grounding for web_search |
| `discord-bot-token.txt` | `DISCORD_BOT_TOKEN` | Discord bot integration |

OpenBao at `secret/services/nemoclaw` remains authoritative. The legacy local-file
interface above is an integration gap, not an approved credential distribution path.

### Adding Integrations

Declare the channel's non-secret configuration and policy preset, extend the
OpenBao-backed Ansible credential/rendering path, and deploy through Semaphore.
Do not add another local secret-file loader or use direct onboard commands to
bypass that integration work.

Channel configs are baked into `openclaw.json` at build time. Tokens activate via env vars at runtime — never stored in the image.

## Legacy wrapper architecture

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
| **Remote server** | Native Docker (Ubuntu) | Historical gateway crash on image push; resolve through the declared deployment mechanism |

## Validation

The legacy wrapper invokes `validate.sh` after deployment. Its checks belong in
the declared Semaphore workflow; do not use a workstation invocation as proof of
a successful platform deploy.

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
