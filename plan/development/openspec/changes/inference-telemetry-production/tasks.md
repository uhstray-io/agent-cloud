# Tasks: inference telemetry in production

## 0. Branch and inventory
- [ ] 0.1 Feature branch from `dev`: `feat/inference-telemetry-production`. Pull requests
      only when Joe asks for them (repo rule)
- [ ] 0.2 Read-only inventory through Semaphore: confirm no o11y containers run on any
      production host today; record the result in `design.md` Context
- [ ] 0.3 Allocate the o11y VM in site-config `proxmox/vm-specs.yml` (Auxiliary tier,
      Podman) and provision it through the onboarding checklist phases 1 to 2
- [ ] 0.4 Validation gate: `openspec validate inference-telemetry-production --store
      agent-cloud` passes; the VM answers SSH via the distributed key; proves nothing in
      the spec yet and unblocks section 1

## 1. Promote o11y to production
- [ ] 1.1 `o11y_svc` group in site-config `inventory/production.yml` with
      `monorepo_deploy_path: platform/services/o11y/deployment`; `templates-prod` entry for
      **Deploy o11y** and **Clean Deploy o11y**
- [ ] 1.2 `templates/env.j2`: `O11Y_PROM_RETENTION` default `15d`, `O11Y_LOKI_RETENTION`
      default `7d`, binds loopback for Prometheus and Alloy, Loki and Grafana bound to the
      VM address; compose reads the retention vars
- [ ] 1.3 Caddy route `o11y.uhstray.io` to the Grafana port in site-config
      `caddy_managed_sites`, `forward_auth` to Authentik per the existing route shape,
      with `/api/health` exempted from `forward_auth` (unauthenticated liveness, no data);
      `manage-caddy-sites.yml` through Semaphore
- [ ] 1.4 `firewall_allow_rules` on the o11y host: Grafana port from the Caddy host; Loki
      push port from the two node addresses; `apply-firewall.yml` through Semaphore
- [ ] 1.5 Deploy; record resident memory and disk after 24 hours in `design.md`
- [ ] 1.6 Validation gate: a second deploy run reports no changes and the three health
      endpoints return 200, proving scenario "Deploy converges and verifies"; `curl` to
      the Grafana port from a LAN host is refused while `https://o11y.uhstray.io` serves
      Grafana, proving scenario "Grafana reachable only through the front door"

## 2. Scrape the nodes, receive their logs
- [ ] 2.1 `config/prometheus.yml`: `scrape_config_files: [scrape.d/*.yml]`;
      `templates/scrape-dgx-spark.yml.j2` rendered from inventory (`dgx_spark_nodes`,
      exporter ports, head API port) with `labels: {cluster: dgx-spark, env: prod}` and
      per-target `node`; local profile renders no file
- [ ] 2.2 `deploy-o11y.yml`: Prometheus `--web.enable-lifecycle` and a `POST /-/reload`
      step after the scrape file changes
- [ ] 2.3 Coordinate with dgx-spark task 1.3: hand over the o11y host address as
      `telemetry_scrape_src` and the Loki push URL as `telemetry_loki_url`
- [ ] 2.4 Confirm every `dgx-spark` target `up == 1`; confirm
      `{cluster="dgx-spark", service="vllm"}` returns lines after a rank restart on the
      nodes (dgx-spark window)
- [ ] 2.5 Fault drill, encoded: `o11y-fault-drill.yml -e drill=exporter` stops one node
      exporter, waits twelve minutes (longer than the alert's five-minute pending window
      plus scrape and evaluation delay), asserts the target is `up == 0` and the
      `telemetry-missing` rule is firing in Grafana, then restores the exporter in an
      `always:` block so an interrupted run never leaves it stopped; re-running after a
      restore is a no-op. Run in a dgx-spark window; the overview panel shows a gap, not zero
- [ ] 2.6 Validation gate: 2.4 proves scenario "All node targets up" and scenario "Boot
      journal is queryable"; 2.5 proves scenario "Missing scrape is a telemetry failure"
      including the alert firing;
      a push to the Loki port from the controller Mac is refused, proving scenario
      "Unlisted source cannot push"

## 3. Dashboards, alerts, synthetic probe
- [ ] 3.1 Import the metric-name list from dgx-spark `results/vllm-metric-names-*.txt`
      into `config/grafana/dashboards/inference-latency-capacity.json`; build
      `inference-fleet-health.json` and `inference-placement-comparison.json`
      (variable `model_alias`, link field to dgx-spark `results/`)
- [ ] 3.2 `config/grafana/provisioning/alerting/inference.yml`: groups
      `inference-failing`, `telemetry-missing`, `memory-thermal`, `benchmark-gate`
      (last one with a single placeholder rule marked disabled until the manifest metric
      exists); contact point Discord webhook from OpenBao `secret/services/o11y:discord_webhook`
- [ ] 3.3 Synthetic probe: `platform/services/o11y/deployment/probe/inference-probe.sh`
      (curl, one short chat completion, effort `none`, through
      `https://inference.uhstray.io/v1`, key from OpenBao at deploy into the gitignored
      `.env`), systemd timer every 5 min on the o11y host, writes
      `inference_probe_success` and `inference_probe_latency_seconds` to node_exporter's
      textfile directory. node_exporter is provisioned on the o11y VM by the same deploy
      (`platform/playbooks/install-node-exporter.yml`, textfile collector directory
      `/var/lib/node_exporter/textfile`) and added as a Prometheus scrape target, so the
      probe metrics exist for the dashboards and alerts
- [ ] 3.4 `platform/tests/test_service_o11y.bats`: dashboards and alerting files are
      valid JSON/YAML, every `vllm:` name in a dashboard appears in the imported list,
      probe script `shellcheck` clean and contains no literal key
- [ ] 3.5 Validation gate: wipe and redeploy o11y; the three dashboards render, proving
      scenario "Dashboards render from provisioning alone"; `o11y-fault-drill.yml -e
      drill=probe` points the probe at a model name the server does not serve for six
      minutes (the completion fails, `/health` stays 200), asserts `/health` returned 200
      throughout and the Discord message arrived, then restores the probe target in an
      `always:` block; `vllm-node` on the nodes was not restarted; proving scenario "Alert
      reaches the contact point" and scenario "Health up, inference down"
- [ ] 3.6 External liveness watcher on a path the firewall permits: a Semaphore schedule
      runs `check-o11y-liveness.yml` from the Semaphore host every 10 min against the
      Caddy front door, not the VM: Grafana `https://o11y.uhstray.io/api/health` (exempt
      from `forward_auth`, task 1.3) and Prometheus readiness through Grafana's datasource
      health API (`/api/datasources/uid/<prometheus>/health`) with a read-only Grafana
      service-account token from OpenBao `secret/services/o11y:watcher_token`; Discord
      webhook on failure. No direct VM port is opened for the watcher. Drill:
      `o11y-fault-drill.yml -e drill=grafana` stops Grafana for 15 minutes, asserts the
      Discord message arrived from the watcher, and restarts Grafana in an `always:` block

## 4. Retention, thresholds, records
- [ ] 4.1 After seven days: read Prometheus TSDB size and Loki ingestion per day; set
      retention against the VM disk with the arithmetic in `env.j2` comments; set alert
      thresholds from the baseline week
- [ ] 4.2 Append dated status lines to `plan/development/05-observability.md` and
      `plan/architecture/06-observability-instrumentation.md`
- [ ] 4.3 `plan/architecture/` record: static node scrape jobs and a dedicated o11y VM,
      with the rejected alternatives from the design
- [ ] 4.4 Validation gate: both plan documents carry the dated lines and `git diff` shows
      only appended text, proving scenario "Status is dated and append-only"; on archive,
      retain the outcome (worked / dead end / corrected) into bank `agent-cloud-750a33b9`
