# Dispatch Console — Product Brief

<!-- impeccable:product-schema 1 -->

**Date:** 2026-10-20
**Status:** ACTIVE
**Author:** Joseph A. Wisneski IV
**Context:** Product truth for the agent-cloud administration console, captured during the
2026-10-20 design session. Three layout prototypes were compared and the **Dispatch**
(work-board) layout was chosen and refined through review comments. The build plan is
[`plan/development/17-dispatch-console.md`](../../development/17-dispatch-console.md); the
UI principles are [`plan/architecture/09-console-ui-principles.md`](../../architecture/09-console-ui-principles.md).

## Platform

web

## Users

- **Platform operators.** Joseph A. Wisneski IV and a small group of engineers running
  agent-cloud. Long desktop sessions. They queue work, approve agent proposals, drill into
  failures, and manage credentials, policy and access for every tenant.
- **Tenant admins.** Customer-side administrators who manage their own organization, sites
  and environments. They need short flows: ask for a change, watch it happen, see what is red.

## Product Purpose

One central administration console for agent-cloud. It shows what is deployed for a tenant,
what state it is in, what work is queued or running, which skynet role is doing it, and lets a
user request changes in natural language. Those requests flow through the platform's
guardrails (OPA) and automation (Semaphore, skynet) instead of around them.

Success: a user goes from "something is wrong" or "I need X" to a queued, policy-checked work
item in the fewest possible steps, and can drill from any work item down to
role → workflow → tasks → Semaphore receipt.

## Positioning

The console is not a new control plane. It is a single pane over the authorities agent-cloud
already has: Authentik (identity), OpenBao (secrets), OPA (policy), Semaphore (automation),
skynet (agent orchestration), Proxmox (virtualization), NetBox (inventory and IPAM),
agentgateway (LLM access), GitHub Actions (CI/CD) and the o11y stack (telemetry). Every change
it makes routes through those systems. It never writes around them. Natural-language requests
become proposals that pass guardrails before automation executes them.

## Operating Context

- Hierarchy for access: **Tenant → Organization → Site → Environment**. For infrastructure:
  **Tenant → Organization → Site → Cluster → Host**. Services cut across that tree.
- Work can be queued against any node of the hierarchy, against a service, against a
  business domain, or against a hand-picked group of hosts, VMs and services.
- Telemetry comes from the existing o11y deployment (Prometheus, Loki, Alloy, Grafana).
  Tempo (traces) is a planned production addition.
- Agile-compatible: work items behave like tickets (key, state, owner role, history,
  iteration).

## Capabilities and Constraints

- Written entirely in Rust; client-side WASM SPA. The UI framework is not chosen yet.
- API-first; domain-driven design with one bounded context per authority.
- Authentication through the existing Authentik OIDC flow; RBAC derived from Authentik
  groups (PRINCIPLES.md identity model; `plan/development/12-rbac-user-provisioning.md`).
- Red / amber / green status on every managed asset, always with a shape and a label.
- **Open:** PRINCIPLES.md says Organization is the tenant boundary ("tenants are orgs"). The
  console adopts a separate **Tenant tier above Organization**. PRINCIPLES.md needs amending
  to match.

## Decisions made in review (2026-10-20)

| Area | Decision |
|---|---|
| Layout | Dispatch (work-board home) chosen over Ask (request-first) and Atlas (map-first); Ask's dashboard and Atlas's scope tree were folded in |
| Tenant | Shown and switched in exactly one place, the sidebar brand block; every page is scoped to the selected tenant |
| Work | Sidebar entry "Active Work". Lanes: Awaiting approval, Queued, Running, Blocked (paused items keep a Paused label), Failed, Done (collapsed by default; denied items land here); filters by asset, group or business domain; red badges for stuck work |
| Domains | Tiles above Work are business domains (Products, Professional Services, Automation, Administration, IT), not org/site |
| Chat | The Work composer asks about current work; "New request" is a secondary mode. The Topology composer is aimed at the selection |
| Plans | Execution and plans are vertical steppers; every step names its owning role |
| Telemetry | Per-service, per-node, per-host and per-VM charts link to Grafana; the console draws tenant / org / site / cluster rollups only |
| Agents | skynet roles `infra-agent`, `security-agent`, `service-agent`, `o11y-agent` (plan 15 D7); `net-agent` is planned; NetClaw and NemoClaw are not used |
| AI Gateway | "Agents" page renamed AI Gateway; production gateway (→ vLLM on spark-pair) and development gateway (local-dev → LM Studio) |
| LLM access | Stays its own page; key requests pick target scopes (gateway groups) then agents; default 250,000,000 tokens/hour; user identity comes from Access |
| Policy | Proposing a change drafts a plan and creates a work item that needs approval |
| Proposals | The Dashboard lists proposals awaiting approval; each expands to review the plan, comment, edit the plan (OPA re-checks), approve or deny |
| Services | "Ask about services" chat; "Request a service": an approved catalog service into the current scope, or a new service through the onboarding checklist |
| Profile | Fields Authentik provides over OIDC are locked ("Managed by Authentik"); display name, avatar, pronouns, timezone, theme, notifications and default scope are editable |
| Settings | Platform admins get Tenant settings and Organization settings above their account; saving creates a proposal (configuration is code); Organization settings owns the service → business-domain mapping |
| Sites | site-hq, site-edge, DigitalOcean NYC1 and Cloudflare (edge only) |

## Evidence on Hand

- Reference prototype: [`prototype/dispatch.html`](prototype/dispatch.html) (published artifact:
  https://claude.ai/artifact/J8vrskNK9xbzSYfMMDhXVg).
- Superseded alternatives kept for reference: `prototype/ask.html`, `prototype/atlas.html`.
- All tenants other than Uhstray.io, all people, work items, PR numbers, VM IDs, addresses,
  capacity figures and business-domain mappings in the prototype are **example data**.
  Future work must not present them as facts.

## Product Principles

1. Through the platform, never around it. Every action becomes a Semaphore/skynet job, an
   OpenBao/OPA change as code, or a proposal.
2. Fewest steps from intent to queued work. Natural language first, one-row forms as the
   fallback.
3. Status is always visible and always explained. Red means a link to why and a next action.
4. Every work item is traceable to a role, its workflows and their receipts.
5. Scope is explicit and single-sourced. The user always knows which tenant, org, site,
   environment or group they are acting on.

## Accessibility & Inclusion

Status is never conveyed by color alone (shape + label). Keyboard-operable composers,
navigation, steppers and selection. Respects reduced motion.
