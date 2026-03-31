# NemoClaw Deployment — Claude Guidance

Deployment configuration for [NemoClaw](https://github.com/uhstray-io/NemoClaw) (our fork) — an AI agent sandbox powered by NVIDIA OpenShell and OpenClaw.

## Rules

- **NemoClaw uses Docker, not Podman** — OpenShell requires Docker. All other agent-cloud services use Podman.
- **Never hardcode IPs or credentials** — deploy.sh reads from environment variables. Site-specific values live in `site-config`, secrets in OpenBao.
- **Default to update, not reinstall** — `./deploy.sh` preserves sandbox state. Only `--onboard` is destructive.
- **Fork is the source** — always use `uhstray-io/NemoClaw`, not upstream NVIDIA.

## Directory Structure

```
agents/nemoclaw/deployment/
├── deploy.sh               # Main deploy script (parameterized, no hardcoded values)
├── update.sh               # Non-destructive update wrapper
├── validate.sh             # 14-point validation suite
├── config/
│   └── presets/            # Network policy presets (google.yaml, etc.)
├── .env.example            # Template for required environment variables
├── CLAUDE.md               # This file
└── README.md               # Full deployment guide
```

Site-specific files (NOT in this repo — stored in site-config):
- `config/credentials.json` — NVIDIA API key for inference
- `config/sandboxes.json` — Sandbox name and policy assignments
- `config/discord.json` — Discord guild and user IDs
- `secrets/*.txt` — API keys and tokens (NVIDIA, Gemini, Discord, Google Search)

## Environment Variables

deploy.sh requires these (set via `.env` or export):

| Variable | Purpose |
|----------|---------|
| `NEMOCLAW_HOST` | Target VM IP address (required) |
| `NEMOCLAW_USER` | SSH username (required) |
| `NEMOCLAW_SSH_KEY` | Path to SSH private key (default: `~/.ssh/nemoclaw`) |
| `NEMOCLAW_SECRETS_DIR` | Path to secrets directory (default: `./secrets`) |
| `NEMOCLAW_REPO` | Fork URL (default: `https://github.com/uhstray-io/NemoClaw`) |

## Deploy Modes

```bash
# Update existing deployment (preserves sandbox)
NEMOCLAW_HOST=<ip> NEMOCLAW_USER=<user> ./deploy.sh

# Fresh install (destructive — rebuilds sandbox)
NEMOCLAW_HOST=<ip> NEMOCLAW_USER=<user> ./deploy.sh --onboard

# Already on the target machine
./deploy.sh --local
```

## Known Issues

- **Gateway crash on image push** — Colima (macOS) has intermittent gateway crashes during Docker image builds. Workaround: deploy locally on the server with `--local`.
- **DNS proxy required** — sandbox DNS resolution requires the DNS proxy script after onboard.
- **OpenShell sandbox ssh** — uses `openshell ssh-proxy` ProxyCommand, not standard SSH.

## References

- Fork: https://github.com/uhstray-io/NemoClaw
- Upstream: https://github.com/NVIDIA/NemoClaw
- OpenShell: https://github.com/NVIDIA/OpenShell
- OpenClaw: https://docs.openclaw.ai
