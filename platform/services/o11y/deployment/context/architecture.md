# o11y service — architecture (context for agents)

The platform's **observability stack** — metrics, logs, dashboards. Read with
the root [`CLAUDE.md`](../../../../CLAUDE.md) and the plan it implements:
[`plan/development/O11Y-DEPLOYMENT.md`](../../../../plan/development/O11Y-DEPLOYMENT.md).

## What it is

- **Grafana** (viz) + **Prometheus** (metrics scrape + TSDB) + **Loki** (logs) +
  **Grafana Alloy** (collector: ships container logs to Loki; OTLP receiver for
  agent telemetry is a Phase-2 add). A minimal local "LGTM-lite" stack.
- Long-term metrics (**Mimir**), traces (**Tempo**), object-store backends
  (**MinIO**), and **Alertmanager** are **prod** additions, out of local scope.

## How it runs

- **Behind central Caddy.** Grafana serves HTTP on container `:3000`; Caddy
  terminates TLS and reaches it by name on `local-dev` (`grafana:3000`). Host
  debug ports: Grafana `127.0.0.1:3002`, Prometheus `9090`, Loki `3100`.
- **Composable, no fork.** `compose.yml` is env-parameterized; `compose.local.yml`
  is a slim overlay (caps, `label=disable`, joins `local-dev` so Caddy reaches
  Grafana; mounts the podman socket so Alloy can discover container logs).
  `deploy.sh` is container-lifecycle-only. (Prometheus scrapes only itself
  today; Caddy/cAdvisor/agent targets are Phase 2 — see `config/prometheus.yml`.)
- **Config is code.** `config/` (Prometheus scrape, Loki, Alloy, Grafana
  datasource + dashboard provisioning) is committed and mounted read-only —
  provisioned on boot, reproducible on a wipe+redeploy. The ONLY secret is the
  Grafana admin password (`secret/services/o11y`, via `manage-secrets`).

## Consumers (why this exists)

- **OpenBao audit → Loki** with alerting (AUTOMATION-COMPOSABILITY §audit) —
  Phase 2.
- **orb-agent OpenTelemetry** export → Alloy OTLP receiver — Phase 2.
- Caddy metrics; future Reliability/NetClaw agents (IMPLEMENTATION_PLAN).

## Files

| File | Role |
|---|---|
| `deployment/compose.yml` | grafana + prometheus + loki + alloy; pinned images; healthchecks |
| `deployment/compose.local.yml` | slim overlay (caps, `label=disable`, `local-dev`, podman socket for Alloy) |
| `deployment/deploy.sh` | container lifecycle only (verify .env, pull, up, wait Grafana healthy) |
| `deployment/templates/env.j2` | image/port vars + Grafana admin pw (from OpenBao) |
| `deployment/config/*` | committed config-as-code (Prometheus/Loki/Alloy/Grafana provisioning) |

`deployment/.env` is rendered per-deploy and gitignored.
