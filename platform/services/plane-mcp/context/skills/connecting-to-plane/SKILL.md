---
name: connecting-to-plane
description: Use when a teammate on an agent-cloud platform wants Claude Code to read or update Plane work items, when the `plane` MCP server is missing, disconnected or asking to authenticate, or when someone asks how to connect Claude Code to their Plane instance
---

# Connecting Claude Code to Plane

Author: Joseph A. Wisneski IV

## Overview

An agent-cloud platform's Plane is reached from Claude Code through one HTTP MCP server, `plane`, at
`https://<plane-mcp-host>/http/mcp`. Sign-in is OAuth through your own Plane account, so every edit
is attributed to you. **There is no API token to create, paste or store.**

This skill is a template. It holds no instance details. Your platform admin gives you four values,
kept with the platform's private configuration:

| Placeholder | What it is |
|---|---|
| `<plane-url>` | The Plane web app, e.g. `https://plane.example.org` |
| `<plane-mcp-host>` | The MCP server's hostname, e.g. `plane-mcp.example.org` |
| `<workspace>` | The workspace name you were invited to |
| `<platform-admin>` | Who fixes access when something is missing |

No values? Stop and ask your platform admin. Do not guess a hostname.

## Before you start

Your Plane account must exist and be in the workspace. Do this once, in a browser on your own
computer (also when Claude Code runs on a remote box):

1. Open `<plane-url>`.
2. Choose the single sign-on option and sign in with your platform account.
3. Accept the waiting invitation to `<workspace>` during onboarding.

No invitation shown? Stop here and ask `<platform-admin>`. Invitations follow your identity-provider
group, so the fix is on the platform side, not in Claude Code.

## Setup (once per machine)

```bash
# 0. Already set up? `add` refuses an existing name and keeps its old URL.
claude mcp get plane
#    URL already https://<plane-mcp-host>/http/mcp: skip step 1.
#    A different URL: remove it first (from the scope `get` shows), then do step 1.
claude mcp remove plane -s user

# 1. Add the server for every project on this machine (user scope)
claude mcp add --transport http --scope user plane https://<plane-mcp-host>/http/mcp

# 2. Sign in: opens a browser, approve access to the workspace
claude mcp login plane
#    Over SSH or without a browser: it prints the authorization URL. Open it in your local
#    browser, approve, then paste the URL the browser lands on back into the prompt.
claude mcp login plane --no-browser

# 3. Check it
claude mcp get plane        # expect: Status: ✔ Connected
```

Then start a new Claude Code session and ask: "Use the plane tools: who am I, and which projects can I see?"
Claude should call the `member` tool (`me`) and the `project` tool and answer with your name and the workspace's projects.

## Quick reference

| Need | Command |
|---|---|
| Re-authenticate (expired, wrong account) | `claude mcp login plane` |
| See status | `claude mcp get plane` |
| Remove | `claude mcp remove plane -s user` |

## Common mistakes

| Mistake | Fix |
|---|---|
| Creating a Plane personal API token and putting it in a header | Not needed and not supported for people. The server runs its own OAuth sign-in. |
| Using `https://<plane-mcp-host>/http/api-key/mcp` | That endpoint is for headless bots. People use `/http/mcp`. |
| Using `<plane-url>` as the MCP URL | That is the web app. The MCP server is `<plane-mcp-host>`. |
| Running `claude mcp login` before joining the workspace | Do "Before you start" first. Joining first is the tested order. |
| Leaving out `--scope user` | The default scope is `local`, so `plane` only works in the directory you added it from. From that directory run `claude mcp remove plane -s local`, then re-add with `--scope user`. |
| `claude mcp login` is not a known command | Checked on Claude Code 2.1.318 and 2.1.321. Update Claude Code and retry. |

## Using it

- Work items always belong to a project; name the project in your request.
- An epic is a work item whose type is "Epic"; there are no separate epic tools.
- Your role comes from your identity-provider group (admins are Owners or Admins, developers and business users are Members). Ask `<platform-admin>` for a change; do not change roles in Plane by hand, because the hourly reconcile sets them back.
