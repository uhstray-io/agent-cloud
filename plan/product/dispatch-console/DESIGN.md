# Dispatch Console — Design System

**Date:** 2026-10-20
**Status:** ACTIVE
**Author:** Joseph A. Wisneski IV
**Context:** The visual system as built in the reference prototype
([`prototype/shared.css`](prototype/shared.css), [`prototype/kit.js`](prototype/kit.js)).
This file describes what exists; when the Rust build diverges, update this file in the same
change. Principles live in [`plan/architecture/09-console-ui-principles.md`](../../architecture/09-console-ui-principles.md).

## World

Control-room HMI ("high-performance" display). Grey carries the surface. Color is reserved
for state, so red and amber always mean "look here". One accent, process blue, marks only what
you can act on. Work items are flight-progress strips. Plans are vertical steppers. Clusters
are dashed containers. Both composers look like chat.

## Color tokens

Defined once on `:root` and redefined for dark mode (both `prefers-color-scheme` and an
explicit `data-theme`).

| Token | Light | Dark | Role |
|---|---|---|---|
| `--bg` | `#eef1f4` | `#101317` | Page ground |
| `--surface` | `#f7f8fa` | `#161a1f` | Bars, lanes, quiet panels |
| `--raised` | `#ffffff` | `#1c2127` | Cards, strips, inputs |
| `--panel` | `#e4e8ed` | `#0c0f12` | Sidebar, logs |
| `--line` / `--line-strong` | `#d3d9e0` / `#b9c1cb` | `#272d35` / `#3a434e` | Borders, connectors |
| `--fg` / `--fg2` / `--fg3` | `#15191e` / `#4a535e` / `#6b7480` | `#e6e9ed` / `#aeb6c0` / `#8a939e` | Text tiers |
| `--accent` / `--accent-weak` | `#2448b8` / `#dce4fa` | `#8ea8ff` / `#1e2a4d` | Actions, selection, focus |
| `--ok` / `--warn` / `--crit` | `#0c8f2e` / `#c98500` / `#d03b3b` | `#3fbf5f` / `#f2b33d` / `#f06a63` | State only |
| `--ok-bg` / `--warn-bg` / `--crit-bg` | tints | tints | State surfaces |
| `--idle` | `#8a929c` | `#6f7884` | No signal / not started |

Integration-edge palette (validated for color-vision deficiency in both themes, with dash
patterns as secondary encoding): `--e-api` blue, `--e-oidc` aqua (dashed), `--e-mcp` violet
(dotted), `--e-data` magenta. Logs, metrics and traces are subsets of data flow: one hue,
told apart by dash pattern; planned traces are drawn faded.

## Type

System stacks only: `ui-sans-serif, system-ui, …` for UI and `ui-monospace, "SF Mono", …` for
keys, hostnames, paths, template names and numbers. Base 14px; scale steps 11 / 12 / 13 / 14 /
16 / 20 / 22–24px. Uppercase labels at 11px with 0.06em tracking. Tabular numerals wherever
digits align.

## Shape, spacing, depth

Radius 10px (cards), 6px (controls), 99px (pills and chips only). Gaps from flex/grid, not
margins. Elevation is either a border or a soft offset shadow, never both on one element.

## Status vocabulary

| State | Icon | Label |
|---|---|---|
| ok | circle-check | Healthy |
| warn | triangle-alert | Degraded |
| crit | octagon-x | Down / Unavailable |
| run | loader-circle (spins) | Running |
| idle | circle-dashed | Idle / No signal |

Status is always icon + word. Dots appear only beside a name that also carries a status
label nearby or in its tooltip.

## Components (prototype ids)

Every component in the prototype carries a stable id, shown as a dashed tag in review mode.

| Id | Component |
|---|---|
| `C.board` | Work lanes; Done collapses to a narrow column |
| `C.domain-ribbon` | Business-domain tiles: health, open and stuck counts; click filters Work |
| `C.quick-add` | Work composer: "Ask about current work" (default) / "New request" |
| `K.drill` | Execution / plan detail with the vertical stepper (`vsteps`) |
| `K.scope-banner` | "N workflows running in this scope" → Work filtered to that scope |
| `K.hier` | Hierarchy tree with rollups and "+" (queue / filter here) per row |
| `C.infra-map` | Compute topology: site → cluster → node; click selects, shift-click groups |
| `C.network-map` | Network topology: ingress path → gateway → segments → attached resources |
| `K.topology` | Data flows: services by guardrail layer, typed edges with toggles |
| `B.context-ask` | Topology composer aimed at the current selection or group |
| `K.service-detail` | Service drill-down: placement, integrations, credentials (names only), work |
| `K.assess` | Site / cluster / node resource panel (tabs, rollup chart, connected resources) and its Assess resource action |
| (verify plan) | Verify health: read-only plan run by `o11y-agent`, opened from any unhealthy resource |
| `K.compute-levels` | Capacity by site / cluster / node / VM |
| `K.telemetry-rollup` | Telemetry rollups by tenant / org / site / cluster |
| `K.gateway-groups` / `K.gateway-dev` | AI Gateway groups for production and development |
| `K.provision-key` | LLM key request: scopes → agents, identity from Access, admin access |
| `K.delivery` | CI/CD delivery context: review, checks, promotion path |
| `K.policy-propose` | Policy change: draft plan → work item needing approval |
| `K.role-view` | One skynet role and only its workloads |

## Layout grammar

Sidebar (tenant switcher, nav with red issue badges, agent roles) · scope bar (org / site /
env) + iteration · page. Work page: domain tiles, collapsible hierarchy, composer, lanes, and a
details panel that minimizes to a 44px summary rail. Drawers (380px) hold details opened from
anywhere. Below 1100px the details panel stacks under the board; below 760px the sidebar
becomes a horizontal strip and lanes stack.

## Motion

150–250ms state transitions. Data-flow edges animate their dash offset. A spinner marks only
running work. Everything collapses to near-instant under `prefers-reduced-motion`.

## Rules that are part of the design

- No per-service, per-node, per-host or per-VM charts in the console. Link to Grafana.
- Secret values never render; OpenBao appears as paths, field counts and rotation age.
- Planned capabilities (net-agent, Tempo traces, OPA deploy template) are labelled "planned".
- Example data is labelled wherever it could be mistaken for real.
