# postiz — automation contract

How platform automation and agents drive Postiz to create, upload media for, and
schedule social posts. **This is a contract document only** — it describes the surface
so clients can be built against something stable. No automation client is deployed or
configured by the Postiz onboarding.

## Endpoint and auth

| | |
|---|---|
| Base | `https://postiz.uhstray.io/api/public/v1` |
| Auth | API key, issued from the Postiz UI (Settings) |
| Rate ceiling | `API_LIMIT`, **90/hour** on post creation (`postiz_api_limit` in inventory) |

The API key is **separate from interactive sign-in**. Human users authenticate
through Authentik OIDC; automation authenticates with the key. That separation is
the reason Postiz is *not* gated by `forward_auth` at Caddy — an edge gate would
redirect a key-bearing API call into a browser sign-in flow and the automation would
break. See `deployment/CLAUDE.md`.

The key is captured by `store-postiz-api-key.yml` (Semaphore: `Store Postiz API
Key`) into `secret/services/postiz:postiz_api_key` — postiz owns it, the same
ownership convention as the OIDC client secret. A client that needs the key reads it
from OpenBao at that path. It is never copied under another service's path, and never
written into a client's literal configuration.

## Why the public host and not the LAN

Automation calls `https://postiz.uhstray.io`, not `http://<postiz-vm>:5000`.

This looks like an unnecessary hop and is deliberate. The host firewall permits
`:5000` from the **central Caddy only** (`firewall_upstream_source`), so a direct
call would require adding each calling host as a second permitted source — a second
access path to maintain, in cleartext on the LAN, diverging from how any other
caller reaches the service. The public path is already TLS-terminated, already
permitted, and identical to what an external integrator would use.

If someone later "optimizes" this into a direct call, they are widening the
firewall boundary that the `platform/service-vm-hardening` spec deliberately
narrowed.

## The flow

```
automation client
  │
  ├─ 1. compose content            (LLM / template / RSS — the client's own logic)
  ├─ 2. POST  /api/public/v1/upload         → media id      (only if attaching media)
  ├─ 3. POST  /api/public/v1/posts          → post id
  │        body: content, channel/integration ids, publishDate, media ids
  └─ 4. GET   /api/public/v1/posts          → verify it is scheduled

        ...then the client's job is DONE.

Postiz's Temporal workflow engine owns execution from here:
  scheduled time arrives → orchestrator publishes → per-channel result recorded
```

**The client does not publish.** It schedules. Execution belongs to the workflow engine
inside the Postiz stack, which is why that engine is a hard dependency of this
service rather than an optional extra (see `deployment/compose.yml`). A client call
that "succeeds" only proves the post was *accepted*, not that it went out — check
the post's state for that.

## Consequences worth designing around

- **Channels must be connected by a human first.** Each social account is
  OAuth-authorized interactively in the Postiz UI. Automation can only post to
  channels that already exist, and it must reference them by their integration id.
- **Re-authorization is manual and eventual.** Platform tokens expire or get
  revoked; publication then fails against that channel. A workflow should surface
  that as an alert rather than retrying blindly.
- **The rate ceiling is per hour, on creation.** A backfill that schedules hundreds
  of posts in a burst will hit it. Space the creation calls, not the publish times.
- **A clean redeploy keeps API keys but loses accounts.** The signing secret is
  reused across deploys, so an existing key keeps authenticating — against a
  database that no longer has the connected channels. After any clean deploy,
  re-connect channels and re-issue the key.

## Not in scope here

The Postiz onboarding creates no automation clients, workflows, or credentials. A
client built against this surface reads its key from OpenBao at
`secret/services/postiz:postiz_api_key` and is written against this document.
