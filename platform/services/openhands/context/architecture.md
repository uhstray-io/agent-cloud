# OpenHands (Agent Canvas) — architecture

Self-hosted OpenHands GUI server, deployed on its own VM, fronted by the central
Caddy and gated by Authentik forward_auth. Hosted at **canvas.uhstray.io**.

## Why Docker (not the platform-default podman)

OpenHands runs the agent in a **per-session runtime container** that the GUI
server launches through the **host Docker socket** (`/var/run/docker.sock`). It
needs a real Docker daemon, so this is a deliberate Docker service — the same
exception NetBox makes. The socket mount is root-equivalent on the VM; the
containment story is:

- **Dedicated VM** = the blast radius. Nothing else runs there.
- **Authentik forward_auth** at the edge — only `platform-admins`/`platform-developers`
  reach the canvas at all (gate in `authentik .../blueprints/zz-sso-bindings.yaml`).
- **Not published to the internet** — `tls internal` + LAN only; the published
  `:3000` should be firewalled to the Caddy host.

## Request path

```
browser --HTTPS--> Caddy VM                canvas.uhstray.io
                     | tls internal (Caddy internal CA, trusted once)
                     | forward_auth --> Authentik outpost (Authentik VM :9000)
                     v  (X-authentik-* headers on success)
                   OpenHands GUI (OpenHands VM :3000)
                     | docker.sock
                     v
                   per-session agent-server runtime container
```

(Concrete host IPs live only in the private site-config inventory.)

## Image facts (current, v1.x scheme)

Pinned in `templates/env.j2`, overridable via inventory:

| Var | Default | What |
|-----|---------|------|
| `OPENHANDS_IMAGE` | `docker.openhands.dev/openhands/openhands:1.8` | GUI server |
| `AGENT_SERVER_IMAGE_REPOSITORY` | `ghcr.io/openhands/agent-server` | runtime sandbox repo |
| `AGENT_SERVER_IMAGE_TAG` | `1.26.0-python` | runtime sandbox tag |

LLM provider keys are **not** templated — they're entered in the UI and persisted
in the `openhands-state` volume.

> The original hand-off doc described an `ghcr.io/openhands/agent-canvas:latest`
> image on port 8000 with no Docker socket. That image/port do not exist and the
> socket IS required — this deployment follows the official OpenHands v1.x docs
> (image `docker.openhands.dev/openhands/openhands`, port 3000, socket-mounted).

## Operate

- Deploy: Semaphore template **Deploy OpenHands** (`deploy-openhands.yml`).
  Requires the **Install Docker** template to have run on the host first.
- Rebuild: **Clean Deploy OpenHands** (`clean-deploy-openhands.yml`) — destroys
  the `openhands-state` volume.
- Pin to a digest for production reproducibility (set `OPENHANDS_IMAGE` to
  `...@sha256:...` in inventory once a release is chosen).
