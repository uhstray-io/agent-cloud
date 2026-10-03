# Tasks: inference telemetry in production

## 0. Branch and inventory
- [x] 0.1 Feature branch from `dev`: `feat/inference-telemetry-production`. Pull requests
      only when Joe asks for them (repo rule)
- [ ] 0.2 Read-only inventory through Semaphore: confirm no o11y containers run on any
      production host today; record the result in `design.md` Context
      2026-10-02 rescope: PARTIAL — tasks 1105/1134 found no `o11y-*` on every reachable host;
      `grafanapodman` retired by the operator 2026-09-26 (site-config#33). `nemoclaw`,
      `nocodb`, `openhands` were never reached; rerun `Audit o11y Containers (Dev)` or close
      as superseded now that the dedicated receiver runs.
- [ ] 0.3 Allocate the o11y VM in site-config `proxmox/vm-specs.yml` (Auxiliary tier,
      Podman) and provision it through the onboarding checklist phases 1 to 2
      2026-10-02 rescope: PARTIAL — the VM is reserved and provisioned under
      `observability-estate` (DHCP refusal task 1342, reservation 1347, provisioned by 1353,
      05-observability.md "Production receiver receipt, 2026-09-26"; declared in
      site-config#24). Remaining: onboarding phases 1 to 2 also back up and harden the
      per-service SSH key and add a service `CLAUDE.md`; site-config has no `secrets/ssh/o11y`
      entry and the deployment directory has no such document (review of #375).
- [x] 0.4 Validation gate: `openspec validate inference-telemetry-production --store
      agent-cloud` passes; the VM answers SSH via the distributed key; proves nothing in
      the spec yet and unblocks section 1
      2026-10-02 rescope: validate passes (run 2026-10-02); Semaphore deploys 1359, 1822, 1830
      and 1851 reached the VM over SSH.

## 1. Promote o11y to production
- [ ] 1.1 `o11y_svc` group in site-config `inventory/production.yml` with
      `monorepo_deploy_path: platform/services/o11y/deployment`; `templates-prod` entry for
      **Deploy o11y** and **Clean Deploy o11y**
      2026-10-02 rescope: PARTIAL — `o11y_svc` and its deploy path are declared
      (site-config#24); production runs the Dev-bound `Deploy o11y (Dev)` template (template
      224, task 1156). Remaining: a main-bound Deploy and a Clean Deploy o11y template, or a
      decision recording that production stays Dev-bound.
- [ ] 1.2 `templates/env.j2`: `O11Y_PROM_RETENTION` default `15d`, `O11Y_LOKI_RETENTION`
      default `7d`, binds loopback for Prometheus and Alloy, Loki and Grafana bound to the
      VM address; compose reads the retention vars
      2026-10-02 rescope: PARTIAL — the retention defaults are 15d/7d from 884e565b (PR #198),
      read by compose and the Loki config, and the Grafana and Loki VM binds are declared in
      site-config#24 and #36. Not as written: Alloy's OTLP listener (4317/4318,
      `O11Y_OTLP_BIND`) was later bound to the VM interface on purpose for gateway traces,
      firewall-scoped (site-config#37), so "Alloy on loopback" no longer holds; the
      requirement needs restating as superseded (review of #375).
- [ ] 1.3 Caddy route `o11y.uhstray.io` to the Grafana port in site-config
      `caddy_managed_sites`, `forward_auth` to Authentik per the existing route shape,
      with two paths exempted from `forward_auth`: `/api/health` (unauthenticated liveness,
      no data) and `/api/datasources/uid/*/health` (Grafana validates the watcher's
      service-account bearer token itself; Authentik's embedded outpost knows only
      forward-auth providers, so the token would be rejected before Grafana saw it);
      `manage-caddy-sites.yml` through Semaphore
      2026-10-02 rescope: PARTIAL — the route is declared (site-config#25) and applied by task
      1372; the public hostname serves Grafana. The route is a plain proxy because Grafana
      uses native Authentik OIDC (PR #275), so the `forward_auth` exemptions are moot.
      Remaining: production sign-in returns to the login page (O11Y-GRAFANA-AUTH-INCIDENT.md);
      close this task when that incident's phase 3 acceptance passes.
- [x] 1.4 `firewall_allow_rules` on the o11y host: Grafana port from the Caddy host; Loki
      push port from the two node addresses; `apply-firewall.yml` through Semaphore
      2026-10-02 rescope: rules declared in site-config#36 and applied by firewall tasks 1661
      and 1853 (and 1373 earlier); task 1639 read back default-deny plus a Caddy-only Grafana
      port.
- [ ] 1.5 Deploy; record resident memory and disk after 24 hours in `design.md`
      2026-10-02 rescope: PARTIAL — deployed (task 1359); budget tasks 1823 and 1831 measured
      7.06%/6.75% root free and ~87.6% memory headroom (o11y README). Remaining: copy those
      into `design.md` with per-container memory. Root is now full (task 2115).
- [ ] 1.6 Validation gate: a second deploy run reports no changes and the three health
      endpoints return 200, proving scenario "Deploy converges and verifies"; `curl` to
      the Grafana port from a LAN host is refused while `https://o11y.uhstray.io` serves
      Grafana, proving scenario "Grafana reachable only through the front door"
      2026-10-02 rescope: PARTIAL — the health endpoints returned 200 on repeated deploys
      (1403, 1822, 1830, 1851) and https://o11y.uhstray.io serves Grafana. No receipt shows a
      no-change second deploy or a refused LAN curl to the Grafana port. Blocked by the full
      guest root (tasks 2103, 2115).

## 2. Scrape the nodes, receive their logs
- [x] 2.1 `config/prometheus.yml`: `scrape_config_files: [scrape.d/*.yml]`;
      `templates/scrape-dgx-spark.yml.j2` rendered from inventory (`dgx_spark_nodes`,
      exporter ports, head API port) with `labels: {cluster: dgx-spark, env: prod}` and
      per-target `node`; local profile renders no file. Render and YAML tests passed
      on 2026-09-22; production target reachability remains task 2.4.
- [x] 2.2 `deploy-o11y.yml`: Prometheus `--web.enable-lifecycle` and a `POST /-/reload`
      step after the scrape file changes. The flag was already present in compose;
      the conditional reload and Ansible syntax check passed on 2026-09-22.
- [x] 2.3 Coordinate with dgx-spark task 1.3: hand over the o11y host address as
      `telemetry_scrape_src` and the Loki push URL as `telemetry_loki_url`
      2026-10-02 rescope: dgx-spark PR #25 admitted the receiver (probes 1413/1414 returned
      200); dgx-spark task 1.3 is ticked; dgx-spark PRs #28/#29 activated Loki shipping.
- [x] 2.4 Confirm every `dgx-spark` target `up == 1`; confirm
      `{cluster="dgx-spark", service="vllm"}` returns lines after a rank restart on the
      nodes (dgx-spark window)
      2026-10-02 rescope: named-target tasks 1634/1635/1636 and 1687-1689; dgx-spark
      docs/HARDWARE.md (PR #29) records 451 spark-1 and 131 spark-2 `service=vllm`,
      `cluster=dgx-spark` entries after the paired restart. The GPU exporter is deliberately
      not scraped.
- [ ] 2.5 Fault drill, encoded: `o11y-fault-drill.yml -e drill=exporter` stops one node
      exporter, waits twelve minutes (longer than the alert's five-minute pending window
      plus scrape and evaluation delay), asserts the target is `up == 0` and the
      `telemetry-missing` rule is firing in Grafana, then restores the exporter in an
      `always:` block so an interrupted run never leaves it stopped; re-running after a
      restore is a no-op. Run in a dgx-spark window; the overview panel shows a gap, not zero
      2026-10-02 rescope: OPEN — no `o11y-fault-drill.yml`. The generic production drill (task
      1701) and canary (1395) proved failed-scrape → firing → Discord on a synthetic target
      only. Needs a dgx-spark window.
- [ ] 2.6 Validation gate: 2.4 proves scenario "All node targets up" and scenario "Boot
      journal is queryable"; 2.5 proves scenario "Missing scrape is a telemetry failure"
      including the alert firing;
      a push to the Loki port from the controller Mac is refused, proving scenario
      "Unlisted source cannot push"
      2026-10-02 rescope: PARTIAL — "All node targets up" and "Boot journal is queryable" are
      proven by the 2.4 evidence. Still missing: the DGX exporter drill (2.5). The controller Mac's refused push is recorded: dgx-spark `node-telemetry-and-placement-benchmark` task 3.1 notes its push timed out while both Sparks' pushes returned 204.

## 3. Dashboards, alerts, synthetic probe
- [ ] 3.1 Import the metric-name list from dgx-spark `results/vllm-metric-names-*.txt`
      into `config/grafana/dashboards/inference-latency-capacity.json`; build
      `inference-fleet-health.json` and `inference-placement-comparison.json`
      (variable `model_alias`, link field to dgx-spark `results/`)
      2026-10-02 rescope: OPEN — no `inference-*.json` dashboard exists; the source list is
      dgx-spark `results/vllm-metric-names-506e66caa3ef.txt`.
- [ ] 3.2 `config/grafana/provisioning/alerting/inference.yml`: groups
      `inference-failing`, `telemetry-missing`, `memory-thermal`, `benchmark-gate`
      (last one with a single placeholder rule marked disabled until the manifest metric
      exists); contact point Discord webhook from OpenBao `secret/services/o11y:discord_webhook`
      2026-10-02 rescope: PARTIAL — `alerts.yml.j2` has one group, `service-telemetry`
      (service-down `up`, per-target missing-telemetry, receiver disk-low). The Discord
      contact is rendered from OpenBao `secret/services/o11y:alert_discord_webhook_url`;
      delivery proven by task 1395, readback by 1403. Remaining: the inference-failing,
      memory-thermal and benchmark-gate groups; rules at `for: 5m` or longer.
- [ ] 3.3 Synthetic probe: `platform/services/o11y/deployment/probe/inference-probe.sh`
      (curl, one short chat completion, effort `none`, through
      `https://inference.uhstray.io/v1`, key from OpenBao at deploy into the gitignored
      `.env`), systemd timer every 5 min on the o11y host, writes
      `inference_probe_success` and `inference_probe_latency_seconds` to node_exporter's
      textfile directory. node_exporter is provisioned on the o11y VM by the same deploy
      (`platform/playbooks/install-node-exporter.yml`, textfile collector directory
      `/var/lib/node_exporter/textfile`) and added as a Prometheus scrape target, so the
      probe metrics exist for the dashboards and alerts
      2026-10-02 rescope: OPEN — no probe or timer. A receiver-host node-exporter container
      exists (receipts 1802-1804) without a textfile collector; extend it instead of
      `install-node-exporter.yml`.
      2026-10-02 implementation: CODE LANDED, RUNTIME UNPROVEN — `probe/inference-probe.sh`,
      the `inference-probe` service and five-minute timer, and the `probe/compose.textfile.yml`
      overlay that adds the textfile collector to the existing receiver-host exporter
      (scraped by the existing `receiver-host` job; no `install-node-exporter.yml`). Wired
      into `deploy-o11y.yml` behind `o11y_inference_probe_enabled` (default false; off
      renders the pre-probe secrets, env files and exporter command). The key is a
      `_shared_reads` field (default service `agentgateway`) rendered to the gitignored
      0600 `probe/inference-probe.env`, not `.env`, which Grafana loads. Tested by
      `platform/tests/test_inference_probe.py`. Still open: the site-config values, an
      enabled deploy whose readback shows a fresh sample for the configured model, and the
      alert rule (3.2).
      2026-10-02: the code is PR #402 (merged). Runtime pending: inventory values and an enabled Deploy o11y
- [ ] 3.4 `platform/tests/test_service_o11y.bats`: dashboards and alerting files are
      valid JSON/YAML, every `vllm:` name in a dashboard appears in the imported list,
      probe script `shellcheck` clean and contains no literal key
      2026-10-02 rescope: PARTIAL — `test_service_o11y.bats` already checks dashboard JSON
      validity and alert rendering. The vllm-name list check and the probe
      shellcheck/no-literal-key check wait on 3.1 and 3.3.
- [ ] 3.5 Validation gate: wipe and redeploy o11y; the three dashboards render, proving
      scenario "Dashboards render from provisioning alone"; `o11y-fault-drill.yml -e
      drill=probe` points the probe at a model name the server does not serve, starts
      `inference-probe.service` immediately (so the first failed sample does not wait for
      the five-minute timer), holds the invalid target for twelve minutes (the rule's
      `for: 5m` plus evaluation and notification margin), asserts `/health` returned 200
      throughout and the Discord message arrived, then restores the probe target in an
      `always:` block; `vllm-node` on the nodes was not restarted; proving scenario "Alert
      reaches the contact point" and scenario "Health up, inference down"
      2026-10-02 rescope: PARTIAL — "Alert reaches the contact point" is proven for scrape
      failure (tasks 1395, 1701). The wipe/redeploy, the dashboards and the probe drill are
      absent; the production clean deploy refuses a nonbaseline tuple, and the root disk is
      full.
- [ ] 3.6 External liveness watcher on a path the firewall permits: a Semaphore schedule
      runs `check-o11y-liveness.yml` from the Semaphore host every 10 min against the
      Caddy front door, not the VM: Grafana `https://o11y.uhstray.io/api/health` (exempt
      from `forward_auth`, task 1.3) and Prometheus readiness through Grafana's datasource
      health API (`/api/datasources/uid/<prometheus>/health`) with a read-only Grafana
      service-account token from OpenBao `secret/services/o11y:watcher_token`; Discord
      webhook on failure. No direct VM port is opened for the watcher. Drill:
      `o11y-fault-drill.yml -e drill=grafana` stops Grafana for 15 minutes, asserts the
      Discord message arrived from the watcher, and restarts Grafana in an `always:` block
      2026-10-02 rescope: OPEN — no watcher playbook or schedule. Grafana uses native OIDC, so
      `/api/health` through Caddy needs no `forward_auth` exemption.
      2026-10-02: CODE — `check-o11y-liveness.yml` (Dev-bound `Check o11y Liveness (Dev)`,
      `*/10` schedule) and `provision-o11y-watcher-token.yml` mint path landed with fake
      Grafana/Discord tests. Still OPEN: mint run, first scheduled run and the drill.
      2026-10-02: the code is PR #410 (merged). Runtime pending: inventory values, the watcher-token provisioning run and the first scheduled run

## 4. Retention, thresholds, records
- [ ] 4.1 After seven days: read Prometheus TSDB size and Loki ingestion per day; set
      retention against the VM disk with the arithmetic in `env.j2` comments; set alert
      thresholds from the baseline week
      2026-10-02 rescope: OPEN, blocked — retention stays 15d/7d/168h with a 0B cap. Reaching
      the 90d/45d/1080h target needs a seven-day forecast, ≥30% free space and an
      isolated-restore receipt (estate-wide 4.2/4.6e). The root disk is full (task 2115).
- [ ] 4.2 Append dated status lines to `plan/development/05-observability.md` and
      `plan/architecture/06-observability-instrumentation.md`
      2026-10-02 rescope: PARTIAL — 05-observability.md carries the dated production lines
      (2026-09-26 through 2026-09-29). 06-observability-instrumentation.md still states
      (2026-09-22) that no production receiver exists; append a dated correction.
- [ ] 4.3 `plan/architecture/` record: static node scrape jobs and a dedicated o11y VM,
      with the rejected alternatives from the design
      2026-10-02 rescope: OPEN — no `plan/architecture` record of the production telemetry
      decisions exists yet.
- [ ] 4.4 Validation gate: both plan documents carry the dated lines and `git diff` shows
      only appended text, proving scenario "Status is dated and append-only"; on archive,
      retain the outcome (worked / dead end / corrected) into bank `agent-cloud-750a33b9`
      2026-10-02 rescope: OPEN — waits on 4.2, 4.3 and the archive.
