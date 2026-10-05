# CLAUDE.md — platform/services/o11y/deployment

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

o11y is the platform's observability receiver: one rootless-Podman compose stack on its
own VM in production (`o11y_svc`), and on the local-dev VM. `README.md` in this directory
is the full operating record (dated receipts, drills, gates); this file is the short
orientation an agent needs before editing here.

## What runs

`compose.yml` defines seven containers, each image pinned by an env-overridable default:
`o11y-prometheus`, `o11y-loki`, `o11y-alloy`, `o11y-tempo`, `o11y-pyroscope`,
`o11y-grafana` and `o11y-node-exporter` (the receiver host's own exporter).
`deploy.sh` is container lifecycle only: it reads the rendered `.env`, pulls, starts the
stack and waits for Grafana to report healthy. Outside local mode it adds
`compose.prod.yml`, which gives Grafana a route to Authentik through the LAN Caddy host.

## How it is deployed

- **Only through Semaphore:** `platform/playbooks/deploy-o11y.yml` (production runs the
  Dev-bound `Deploy o11y (Dev)` template). When a candidate SHA is required it refuses any
  other checkout, renders the Alloy and Prometheus configs and the
  Grafana alert provisioning, then runs `deploy.sh` and reads the live state back
  (health endpoints, datasources, three named dashboards, the `o11y_` alert rules and
  the Discord contact point).
- **Secrets** come from OpenBao `secret/services/o11y` through `tasks/manage-secrets.yml`:
  the Grafana admin password, and `alert_discord_webhook_url` (read only when alerts are
  enabled). The synthetic probe's key is a shared read rendered to its own 0600
  `probe/inference-probe.env`, never to `.env`, which Grafana loads.
- **Private values** (addresses, binds, enable flags) live in site-config inventory. The
  production Semaphore inventory is a copy of it; sync it before a deploy relies on a new
  value (README, "production Semaphore inventory").

## Switches that change what renders

All default off, so declaring a value alone never starts a scrape or pages anyone:

| Variable | Effect |
|---|---|
| `dgx_spark_scrape_enabled` | Renders `config/scrape.d/dgx-spark.yml` and the `inference-failing`, `telemetry-missing`, `memory-thermal` and `benchmark-gate` alert groups; off lists those rules under `deleteRules` |
| `o11y_inference_probe_enabled` | Installs the five-minute probe timer, its textfile collector and the two probe rules; off removes any leftover artefacts |
| `o11y_inference_probe_client_leaf` | With the probe enabled, sends it straight to the gateway's mutual-TLS listener as a gateway client identity, presenting this declared internal-CA client leaf; places the CA bundle beside the leaf and maps the gateway's name on this host (README, "Gateway client identity") |
| `o11y_alerts_enabled` | Unpauses the rendered rules and routes them to the Discord contact point, except the `benchmark-gate` placeholder, which stays paused and unrouted (`templates/alerts.yml.j2:340-349,428-432`) |
| `o11y_gateway_span_logs_enabled` | Also writes one Loki line per gateway span |

## Conventions specific to this service

- Binds default to loopback (`templates/env.j2`). Grafana, Loki and the OTLP receiver
  (`o11y_otlp_bind`, ports 4317/4318) are bound to the VM address in production by
  inventory, each admitted by a source-scoped firewall rule; a production deploy
  refuses a loopback or non-IPv4 OTLP bind when gateway tracing is enabled.
- Retention defaults are Prometheus `15d`, Loki `7d`, Tempo `168h`; the Prometheus size
  cap defaults to `1GB` in local mode and `0B` (no cap) otherwise (`templates/env.j2:26-30`).
  In production, any retention other than that baseline is refused unless the run has a
  nonzero Prometheus cap and a numeric `o11y_capacity_receipt_id`
  (`deploy-o11y.yml:132-146`), the named volumes resolve with at least 30% headroom
  (`:156-178`), and the capacity readback reports an acceptable state (`:180-186`). The
  retention gate does not check a backup or restore receipt; that receipt is required only
  for Tempo-derived metrics (`:306-310`).
- `config/scrape.d/host-<group>.yml` (rendered from `templates/scrape-host-node.yml.j2`) is
  written by `instrument-host-o11y.yml`, the service deployment workflow's instrument-host
  step, never by `deploy-o11y.yml`, which neither writes nor removes it.
- Every `vllm:` and `node_` metric name used by a dashboard or alert rule must appear in
  the vendored fixtures under `platform/tests/fixtures/`; the o11y BATS suite enforces it.
- The deploy readback requires the live rules in the provisioned folders and groups to
  equal the rendered alert file, so it covers the `inference_` rules as well as the `o11y_`
  ones; the drills check only `o11y_` rules (README, "Inference dashboards and alerts").

## Tests

`platform/tests/test_service_o11y.bats` (render, dashboards, alert rules, metric names)
and `platform/tests/test_inference_probe.py` (probe script).
