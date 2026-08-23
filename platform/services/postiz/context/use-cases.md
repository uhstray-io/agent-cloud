# postiz — automation contract

How platform automation (n8n today, agents later) drives Postiz to create, upload
media for, and schedule social posts. **This is a contract document only** — it
describes the surface so workflows can be built against something stable. No n8n
deployment or configuration is part of the Postiz onboarding.

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

Store the key at `secret/services/n8n` when the workflows are built, and inject it
from there — never in a workflow node's literal fields.

## Why the public host and not the LAN

Automation calls `https://postiz.uhstray.io`, not `http://<postiz-vm>:5000`.

This looks like an unnecessary hop and is deliberate. The host firewall permits
`:5000` from the **central Caddy only** (`firewall_upstream_source`), so a direct
call would require adding the n8n host as a second permitted source — a second
access path to maintain, in cleartext on the LAN, diverging from how any other
caller reaches the service. The public path is already TLS-terminated, already
permitted, and identical to what an external integrator would use.

If someone later "optimizes" this into a direct call, they are widening the
firewall boundary that the `platform/service-vm-hardening` spec deliberately
narrowed.

## The flow

```
n8n workflow
  │
  ├─ 1. compose content            (LLM / template / RSS — n8n's business)
  ├─ 2. POST  /api/public/v1/upload         → media id      (only if attaching media)
  ├─ 3. POST  /api/public/v1/posts          → post id
  │        body: content, channel/integration ids, publishDate, media ids
  └─ 4. GET   /api/public/v1/posts          → verify it is scheduled

        ...then n8n's job is DONE.

Postiz's Temporal workflow engine owns execution from here:
  scheduled time arrives → orchestrator publishes → per-channel result recorded
```

**n8n does not publish.** It schedules. Execution belongs to the workflow engine
inside the Postiz stack, which is why that engine is a hard dependency of this
service rather than an optional extra (see `deployment/compose.yml`). A workflow
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

No n8n workflows, credentials, or nodes are created by the Postiz onboarding. When
they are built, they belong with the n8n service, and this document is the contract
they should be written against.
