# o11y service — architecture (context for agents)

The platform's **observability stack** — metrics, logs, dashboards. Read with
the root [`AGENTS.md`](../../../../../AGENTS.md) and the plan it implements:
[`plan/development/05-observability.md`](../../../../../plan/development/05-observability.md).

## What it is

- **Grafana** (viz) + **Prometheus** (metrics scrape + TSDB) + **Loki** (logs) +
  **Grafana Alloy** (logs, metrics, profiles, and OTLP ingress) + **Tempo**
  (bounded trace storage) + **Pyroscope** (private continuous-profile storage).
  The same Compose stack runs locally and on the production receiver.
- Long-term metrics (**Mimir**), object-store backends (**MinIO**), and a
  separate **Alertmanager** remain deferred; Grafana manages the current alerts.

## How it runs

- **Behind central Caddy.** Grafana serves HTTP on container `:3000`; Caddy
  terminates TLS and reaches it by name on `local-dev` (`grafana:3000`). Host
  debug ports: Grafana `127.0.0.1:3002`, Prometheus `9090`, Loki `3100`.
- **Composable, no fork.** `compose.yml` is env-parameterized; `compose.local.yml`
  is a slim overlay (caps, `label=disable`, joins `local-dev` so Caddy reaches
  Grafana; mounts the podman socket so Alloy can discover container logs).
  `deploy.sh` is container-lifecycle-only. Prometheus scrapes six o11y
  components. Production inventory renders DGX Spark and agentgateway scrape
  jobs; Alloy receives sampled traces and exports them to Tempo. A separate
  node_exporter service collects only receiver CPU, memory, root filesystem,
  and load metrics on the private o11y network, with host PID and a read-only
  host-root mount; it has no published host port and Alloy receives no host-root
  mount. Runtime isolation and exact root-filesystem parity remain unverified
  until a non-destructive Semaphore deploy passes its readbacks.
- **Config is code.** `config/` (Prometheus scrape, Loki, Alloy, Grafana
  datasource + dashboard provisioning) is committed and mounted read-only —
  provisioned on boot, reproducible on a wipe+redeploy. The ONLY secret is the
  Grafana admin password (`secret/services/o11y`, via `manage-secrets`).

## Consumers (why this exists)

- Production DGX Spark and agentgateway metrics; receiver-side Grafana alerting
  and an inventory-gated Alloy self-profile pilot.
- Sampled agentgateway traces → Alloy OTLP → Tempo, with Grafana trace-to-log
  correlation configured. Loki's `traceid` derived field links matching logs
  back to Tempo. A 2026-09-28 operator click-through verified a same-span Loki
  log; trace-to-metrics and log-to-trace still need functional UI receipts after
  the receiver deploys this revision.
- OpenBao audit ingestion, orb-agent OpenTelemetry, and future
  Reliability/NetClaw consumers remain separately gated.

## Bounded signal identity

Use `service` for metrics and Loki, `service.name` for OTLP resources, and
`service_name` for profiles. Prometheus and Loki carry bounded `cluster` and
`environment`; Grafana alerts add bounded `owner` and `severity` and group on
service, environment, cluster, and alert name. Container and instance labels
are drill-down context. Request/user IDs, trace IDs, timestamps, raw paths,
addresses, prompts, and secrets are dropped from scraped metric labels and
never promoted to Loki stream labels.

Pyroscope is private, persistent, and retention-bounded. Alloy's self-profile
scrape stays disabled until private inventory carries the config, privacy, and
resource proof receipts. No other producer is enabled by this pilot.

The receiver-host exporter is a measurement source, not capacity acceptance.
Production deploys first resolve each existing data volume from its running
container mount, compare the container and volume-inspect mountpoints, and
require at least 30% free space on every backing filesystem. A budget receipt
reports each volume's allocated bytes and backing filesystem total/free bytes
without exposing mount paths. Seven-day history, per-backend growth, and the
30%/25%/CPU-p95 forecast still gate retention or VM changes. Remote metric
sources continue to require source-scoped firewall proof.

## Files

| File | Role |
|---|---|
| `deployment/compose.yml` | grafana + prometheus + loki + alloy + tempo + Pyroscope + private receiver-host node_exporter; pinned images; healthchecks |
| `deployment/compose.local.yml` | slim overlay (caps, `label=disable`, `local-dev`, podman socket for Alloy) |
| `deployment/deploy.sh` | container lifecycle only (verify .env, pull, up, wait Grafana healthy) |
| `deployment/templates/env.j2` | image/port vars + Grafana admin pw (from OpenBao) |
| `deployment/config/*` | committed config-as-code (Prometheus/Loki/Alloy/Grafana provisioning) |
| `deployment/config/pyroscope-config.yml` | private v2 filesystem storage, persistent mount, and seven-day retention |
| `platform/playbooks/drill-o11y-active-alert-delivery.yml` | Dev-bound production delivery proof against active rules; fixed scrape cleanup in `always` |
| `platform/playbooks/recover-o11y-active-alert-drill.yml` | separate idempotent recovery after an interrupted active delivery drill |
| `platform/playbooks/verify-o11y-production-budgets.yml` | read-only retention, sample-limit, and active-series receipt |
| `platform/playbooks/files/inspect-o11y-volume-capacity.py` | read-only exact container-volume mount resolution and sanitized backing-filesystem/stored-byte receipt |

`deployment/.env` is rendered per-deploy and gitignored.
Semaphore's production inventory is a static copy of the private site-config
file. After a reviewed private inventory change, run the versioned
`platform/semaphore/sync-inventory.yml` and verify its readback before relying
on a Dev-bound production task. Runtime Loki/Tempo config endpoints may emit
multiple YAML documents, so the budget verifier selects the document carrying
the setting it needs.
