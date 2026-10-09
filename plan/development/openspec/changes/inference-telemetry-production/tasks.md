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
      2026-10-04: `Audit o11y Containers (Dev)` task 2743 at `21891372` reached 15 hosts; only the o11y
      receiver host runs `o11y-*` containers (its own seven, expected). `nocodb`, `nemoclaw` and
      `openhands` were unreachable over SSH ("Host is unreachable"), so the task ended in error.
      Not ticked: the operator decides whether to close as superseded or investigate those three.
- [ ] 0.3 Allocate the o11y VM in site-config `proxmox/vm-specs.yml` (Auxiliary tier,
      Podman) and provision it through the onboarding checklist phases 1 to 2
      2026-10-02 rescope: PARTIAL — the VM is reserved and provisioned under
      `observability-estate` (DHCP refusal task 1342, reservation 1347, provisioned by 1353,
      05-observability.md "Production receiver receipt, 2026-09-26"; declared in
      site-config#24). Remaining: onboarding phases 1 to 2 also back up and harden the
      per-service SSH key and add a service `CLAUDE.md`; site-config has no `secrets/ssh/o11y`
      entry and the deployment directory has no such document (review of #375).
      2026-10-04: the service `CLAUDE.md` now exists
      (`platform/services/o11y/deployment/CLAUDE.md`, with the relative `AGENTS.md` symlink, listed
      in the root `AGENTS.md`). Still open: the per-service SSH key backup to site-config and
      hardening.
      2026-10-04 (task ids reported by the coordinator; only 2743 output was read here): per-service
      SSH key generated (`Generate Service SSH Key (Dev)` task 2738, `secret/services/ssh/o11y`),
      backed up to a site-config branch (`Back Up Service SSH Key (Dev)` task 2740; the site-config PR
      is pending), distributed with key auth confirmed (`Distribute SSH Keys (Dev)` task 2742).
      Still open: SSH hardening, waiting on the harden-ssh pre-proof change, and the site-config merge.
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
      2026-10-05: the catalog now declares main-bound `Deploy o11y` and `Clean Deploy o11y`, each
      with `dev_variant: true`, following the 2026-10-04 operator decision to keep the main/dev
      twins. The directly Dev-bound `Deploy o11y (Dev)` declaration is replaced by the base's
      generated twin of the same name; publication finds a live template by name, so it updates the
      existing one (template 224 as recorded above) rather than adding one. `Clean Deploy o11y`
      offers no `expected_repository_sha`: the imported deploy checks it only after
      the destroy. Not published and not dry run; site-config's `templates-prod` entry is still
      open. Left unticked.
      2026-10-08: the `templates-prod` clause is stale: no other file or key of that name exists in
      this repository at `origin/dev`, and none in the local site-config checkout (searched
      2026-10-08; the checkout may differ from site-config's `origin`). `Deploy o11y` and
      `Clean Deploy o11y` are main-bound with `dev_variant`, as recorded above. Publication of
      the main-bound pair waits for promotion of `dev` to `main`. Left unticked.
- [x] 1.2 `templates/env.j2`: `O11Y_PROM_RETENTION` default `15d`, `O11Y_LOKI_RETENTION`
      default `7d`, binds loopback for Prometheus and Alloy, Loki and Grafana bound to the
      VM address; compose reads the retention vars
      2026-10-02 rescope: PARTIAL — the retention defaults are 15d/7d from 884e565b (PR #198),
      read by compose and the Loki config, and the Grafana and Loki VM binds are declared in
      site-config#24 and #36. Not as written: Alloy's OTLP listener (4317/4318,
      `O11Y_OTLP_BIND`) was later bound to the VM interface on purpose for gateway traces,
      firewall-scoped (site-config#37), so "Alloy on loopback" no longer holds; the
      requirement needs restating as superseded (review of #375).
      2026-10-04 restatement: the "Alloy on loopback" clause is SUPERSEDED, by design. The OTLP
      listener binds `${O11Y_OTLP_BIND:-127.0.0.1}` on 4317/4318 (`compose.yml:102,105`,
      `templates/env.j2:24`); production sets `o11y_otlp_bind` to the VM address for gateway
      traces, and `deploy-o11y.yml:257-268` refuses a loopback or non-IPv4 bind, or one that
      differs from the gateway's `agw_otlp_host`, when gateway tracing is enabled. Prometheus stays
      loopback by default (`compose.yml:52`, `env.j2:20`). The remaining text is met; this box
      stays unticked because its text as written is not, and closes as superseded at archive.
      2026-10-08: ticked, with the "Alloy on loopback" clause closed as SUPERSEDED by design
      (the 2026-10-04 restatement above) rather than left to archive. Re-read: the retention
      defaults are `O11Y_PROM_RETENTION` 15d (`templates/env.j2:26`) and `O11Y_LOKI_RETENTION`
      7d (`templates/env.j2:29`), and compose reads both (`compose.yml:43`, `compose.yml:67`).
      The Loki and Grafana VM-address binds rest on site-config #24/#36, not re-read here.
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
      2026-10-04: `Diagnose Grafana OAuth Failure (Dev)` task 2746 at `21891372` ran end to end (2103 did
      not) and reported `category=no_oauth_failure_in_window`; the playbook then stops by design,
      because it requires a failure category (`diagnose-o11y-grafana-auth.yml:85-97`). Next: the
      operator attempts a Grafana sign-in and, if it fails, reruns the diagnostic inside the window.
      Not ticked.
- [x] 1.4 `firewall_allow_rules` on the o11y host: Grafana port from the Caddy host; Loki
      push port from the two node addresses; `apply-firewall.yml` through Semaphore
      2026-10-02 rescope: rules declared in site-config#36 and applied by firewall tasks 1661
      and 1853 (and 1373 earlier); task 1639 read back default-deny plus a Caddy-only Grafana
      port.
- [x] 1.5 Deploy; record resident memory and disk after 24 hours in `design.md`
      2026-10-02 rescope: PARTIAL — deployed (task 1359); budget tasks 1823 and 1831 measured
      7.06%/6.75% root free and ~87.6% memory headroom (o11y README). Remaining: copy those
      into `design.md` with per-container memory. Root is now full (task 2115).
      2026-10-04: the task 1823 and 1831 readings are copied into `design.md` Context. Still
      open: per-container resident memory and a 24-hour read; neither receipt has them.
      2026-10-06 (task ids and readings reported by the coordinator; the task output was not
      read here): the 24-hour read exists. Verify o11y Production Budgets (Dev) task 3141, more
      than 24 hours after Deploy o11y (Dev) 2967, reported `guest_memory_headroom_percent`
      86.46, root filesystem 84.41% free (87.5 GB of 103.7 GB), and store sizes of about 607 MB
      and 36 MB. Still open: per-container resident memory (the receipt does not carry it; PR
      #460 pending) and copying the 3141 readings into `design.md`. Not ticked.
      2026-10-07: DONE (task ids and readings reported by the coordinator; the task output was
      not read here). Baseline corrected by the coordinator from the Semaphore API: the last
      Deploy o11y (Dev) is task 2991 (newest in its template's task list; Clean Deploy o11y was
      never run), ended 2026-10-05T18:34:53Z. Task 3141 ran 2026-10-06T18:05:13Z, 23 h 30 min
      after 2991, so it is NOT a 24-hour read (the 2026-10-06 note above measured from 2967 and
      is superseded on this point). Verify o11y Production Budgets (Dev) task 3314 (after #460
      merged) ran 2026-10-07T18:40:41Z, 48 h 06 min after 2991, and is the 24-hour-or-more
      read for all three figures: guest memory headroom 86.24%, root filesystem 84.23% free, and
      resident memory for each of the seven o11y containers (50.2, 70.1, 81.5, 8.1, 99.8, 35.6
      and 43.6 MiB; 388.9 MiB in total). The tick rests on 3314 alone; 3141 is recorded in
      `design.md`, "Measured 2026-10-07 (task 1.5)", as an earlier point read. o11y Fault Drill
      (Dev) probe-mode task 3295 ran between the two. In `o11y-fault-drill.yml` the probe path
      rewrites the probe environment file and starts `inference-probe.service`
      (`:341-373`) and does not stop or restart a container (the container stop belongs to the
      `grafana` mode, `:24`); that is the playbook source, not an observation of task 3295.
- [x] 1.6 Validation gate: a second deploy run reports no changes and the three health
      endpoints return 200, proving scenario "Deploy converges and verifies"; `curl` to
      the Grafana port from a LAN host is refused while `https://o11y.uhstray.io` serves
      Grafana, proving scenario "Grafana reachable only through the front door"
      2026-10-02 rescope: PARTIAL — the health endpoints returned 200 on repeated deploys
      (1403, 1822, 1830, 1851) and https://o11y.uhstray.io serves Grafana. No receipt shows a
      no-change second deploy or a refused LAN curl to the Grafana port. Blocked by the full
      guest root (tasks 2103, 2115).
      2026-10-05: convergence half PROVEN — Deploy o11y (Dev) task 2839 at `bdc13789` (first
      change-aware deploy, `DEPLOY_CHANGED=true`), then task 2841 reported `changed=0`,
      `DEPLOY_CHANGED=false`. Still open: no receipt of a refused LAN `curl` to the Grafana port,
      so the front-door scenario is unproven and the box stays open.
      2026-10-06: DONE (task ids and output lines reported by the coordinator; the task output
      was not read here). Front-door half PROVEN: Probe Reachability (Dev) task 2984, a TCP
      connect from the agentgateway host (a LAN host that is not the Caddy host) to the o11y
      host, reported `3002 closed (expected closed); 4317 open (expected open)`. 3002 is the
      Grafana port: `o11y_grafana_port` defaults to `3002`
      (`platform/services/o11y/deployment/templates/env.j2:19`) and the private inventory
      overrides only the bind, to the VM address, so the port is published on the LAN and the
      refusal is the firewall's (task 1.4 allows it from the Caddy host only), not a loopback
      bind. `probe-reachability.yml` counts `closed` only for a refusal or a
      timeout on every address, never for a resolution or network error. The open OTLP port in
      the same run shows the probing host does reach the o11y host. The public hostname serving
      Grafana is recorded under 1.3 (task 1372). Convergence stays proven by 2839 then 2841;
      later deploys that changed things (2967, `changed=11`, enabling the probe; 2991,
      `changed=3`, enabling gateway span logs from site-config #62) each applied a new
      declaration, so they are first runs of a new configuration, not failed second runs.
      Ticked: both scenarios' conditions are met.

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
      2026-10-08: the playbook now exists: `platform/playbooks/o11y-fault-drill.yml` with
      `drill=exporter` (PR #433, merged into `dev`; the header documents the mode and its
      `drill_window_confirmed=true` refusal). Remaining: site-config must declare the
      `o11y_fault_drill_exporters` map (node name to inventory host and node-exporter systemd
      unit; the drill refuses an undeclared node). That map was not found in the local
      site-config checkout, and the unit names are unverified. Then one run in a dgx-spark
      window. Left unticked.
- [ ] 2.6 Validation gate: 2.4 proves scenario "All node targets up" and scenario "Boot
      journal is queryable"; 2.5 proves scenario "Missing scrape is a telemetry failure"
      including the alert firing;
      a push to the Loki port from the controller Mac is refused, proving scenario
      "Unlisted source cannot push"
      2026-10-02 rescope: PARTIAL — "All node targets up" and "Boot journal is queryable" are
      proven by the 2.4 evidence. Still missing: the DGX exporter drill (2.5). The controller Mac's refused push is recorded: dgx-spark `node-telemetry-and-placement-benchmark` task 3.1 notes its push timed out while both Sparks' pushes returned 204.

## 3. Dashboards, alerts, synthetic probe
- [x] 3.1 Import the metric-name list from dgx-spark `results/vllm-metric-names-*.txt`
      into `config/grafana/dashboards/inference-latency-capacity.json`; build
      `inference-fleet-health.json` and `inference-placement-comparison.json`
      (variable `model_alias`, link field to dgx-spark `results/`)
      2026-10-02 rescope: OPEN — no `inference-*.json` dashboard exists; the source list is
      dgx-spark `results/vllm-metric-names-506e66caa3ef.txt`.
      2026-10-03: CODE LANDED (PR #380) — the three `inference-*.json` dashboards are under
      `platform/services/o11y/deployment/config/grafana/dashboards/`, with the metric-name list
      vendored as a test fixture. Rendering waits on an enabled Deploy o11y and the 3.5 gate.
      2026-10-04: NOT proven at runtime. Deploy o11y (Dev) task 2671 at `bd76f29c` succeeded and that
      commit carries the three `inference-*.json` files, but its readback checks only the
      self-monitoring and two agentgateway dashboards; nothing reads back an `inference-*`
      dashboard. Needs a readback or the 3.5 gate.
      2026-10-05: PROVEN at runtime. Deploy o11y (Dev) task 2839 at `bdc13789` reads back every
      provisioned dashboard live (#434), the three `inference-*` dashboards included.
- [x] 3.2 `config/grafana/provisioning/alerting/observability.yml` (rendered from
      `templates/alerts.yml.j2`): groups
      `inference-failing`, `telemetry-missing`, `memory-thermal`, `benchmark-gate`
      (last one with a single placeholder rule marked disabled until the manifest metric
      exists); contact point Discord webhook (`contact.yml`, from `templates/alert-contact.yml.j2`)
      from OpenBao `secret/services/o11y:alert_discord_webhook_url`
      (2026-10-05: requirement text corrected to the implemented file and field; originally named inference.yml and discord_webhook)
      2026-10-02 rescope: PARTIAL — `alerts.yml.j2` has one group, `service-telemetry`
      (service-down `up`, per-target missing-telemetry, receiver disk-low). The Discord
      contact is rendered from OpenBao `secret/services/o11y:alert_discord_webhook_url`;
      delivery proven by task 1395, readback by 1403. Remaining: the inference-failing,
      memory-thermal and benchmark-gate groups; rules at `for: 5m` or longer.
      2026-10-03: CODE LANDED, RUNTIME UNPROVEN — `alerts.yml.j2` now renders the
      inference-failing (vLLM queue stall; with `o11y_inference_probe_enabled`, the probe's
      success==0 and staleness rules), telemetry-missing (`up` on any DGX Spark target,
      `absent()` on the vLLM request family), memory-thermal (thermal deferred until the GPU
      exporter is scraped) and benchmark-gate (paused placeholder) groups, every rule
      `for: 5m`, routed to `agent-cloud-ops`. Each flag that is off lists its rules under
      `deleteRules`. Pending: an enabled Deploy o11y and the task 3.5 drill.
      2026-10-04: NOT proven at runtime. Task 2671 rendered the DGX scrape file (so
      `dgx_spark_scrape_enabled` was true and `alerts.yml.j2:351-433` emits all four groups), but
      "Require expected o11y rules and active routed rules" filters uids on `^o11y_`
      (`deploy-o11y.yml:1083-1103`), so no `inference_` rule was read back live. The probe was
      disabled in that run (probe tasks skipped), so the two probe rules were not rendered.
      2026-10-05: PROVEN at runtime. Deploy o11y (Dev) task 2839 at `bdc13789` asserts the live
      rule set equals the provisioned one (#434), so the `inference_` rules are read back, not
      only `o11y_`. The probe was disabled in 2839 (its unit, timer and enable tasks all
      skipped, per the coordinator's read of the output), so its two rules were not rendered or
      read back. This task names only the four groups, which 2839 covers; the probe rules
      belong to task 3.3, which needs the probe enabled in site-config.
- [x] 3.3 Synthetic probe: `platform/services/o11y/deployment/probe/inference-probe.sh`
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
      2026-10-03: the code is PR #402 (merged). Runtime pending: inventory values and an enabled Deploy o11y
      2026-10-05: gateway-identity path code on branch `feat/o11y-probe-gateway-mtls`, not
      merged, runtime unproven. Until the route switch (gateway task 4.3) a gateway key through
      the public hostname reaches vLLM, so the probe can instead present its own internal-CA
      client leaf (`o11y_inference_probe_client_leaf`) straight to the gateway's mutual-TLS
      listener. `deploy-o11y.yml` refuses the declaration unless the gateway admits the leaf and
      the daily renewal can prove it from the o11y host, places the CA bundle beside the leaf, and
      maps the gateway's name through the shared resolution step. The script presents the leaf
      only with all three TLS inputs. Tested in `platform/tests/test_inference_probe.py`. Still
      open: the site-config declarations (leaf, allowlist, `agw_clients` entry, the gateway's
      firewall rule for the o11y host), the leaf's issuance, and an enabled deploy.
      2026-10-05: DONE, PROVEN at runtime through the gateway client-identity path (code merged
      in #449, `b0f599ad`). The probe goes straight to the gateway's mutual-TLS listener, not
      through the public hostname this task first named: until the route switch (gateway task
      4.3) a gateway key on the public path reaches vLLM. Evidence, in the order run:
      site-config #61 merged (`848d9b33`) and the Semaphore inventory synced; Deploy step-ca
      (Dev) 2954/2961 (name policy 7 names); Deploy agentgateway (Dev) 2959 (recreated,
      `client_o11y-probe` minted, keyed verify completion OK); Apply Firewall (Dev) on the
      gateway 2963; Issue Internal Leaf (Dev) `o11y-probe` 2964 dry run, 2965 real; Deploy o11y
      (Dev) 2966 dry run, 2967 real, whose forced sample read back
      `inference_probe_success{model_name="qwen3.8-flash-next"} 1`, latency 0.159245 s; Renew
      Internal Certs (Dev) dry run 2969 classified `o11y-probe` as a probe-client leaf with
      29 of 30 days left. Not yet shown: a renewal run that re-issues and proves the
      `o11y-probe` leaf from the o11y host (2969 was a dry run, outside the renewal window);
      the probe's two alert rules firing (task 3.5 drill); moving the probe to the public
      hostname after task 4.3, if wanted.
      2026-10-06 (task ids and output reported by the coordinator; the task output was not read
      here): o11y Fault Drill (Dev) `drill=probe` task 2998 observed a firing alert for the
      drill model, delivered to Discord, with `/health` 200 throughout, then restored and
      verified. Alert identity is NOT proven: the drill accepts any firing alert whose
      `model_name` is the drill model (`o11y-fault-drill.yml:363-375`, no `alertname` filter),
      and its final message prints the configured text "inference_probe_failing fired"
      (`:140`) whatever matched. Follow-up: the drill must match `alertname`. The first
      attempt, 2981, failed on the controller's resolution of the `/health` target, fixed in
      #455. Still not shown: `inference_probe_failing` specifically firing, the renewal
      re-issue of the `o11y-probe` leaf, and the staleness rule firing.
      2026-10-07 (task id and output as quoted by the coordinator; the task output was not read
      here): the alert-identity follow-up above is RESOLVED live. o11y Fault Drill (Dev)
      `drill=probe` task 3295 at `ffe4bb33` (after #466) reported that `inference_probe_failing`
      (title "Synthetic inference probe failing") fired for model
      `o11y-drill-absent-model-e247d7b43ffe` and reached Discord naming both, with `/health` 200
      throughout, then restored and verified. Still not shown: the renewal re-issue of the
      `o11y-probe` leaf and the staleness rule firing. The box was already ticked.
- [x] 3.4 `platform/tests/test_service_o11y.bats`: dashboards and alerting files are
      valid JSON/YAML, every `vllm:` name in a dashboard appears in the imported list,
      probe script `shellcheck` clean and contains no literal key
      2026-10-02 rescope: PARTIAL — `test_service_o11y.bats` already checks dashboard JSON
      validity and alert rendering. The vllm-name list check and the probe
      shellcheck/no-literal-key check wait on 3.1 and 3.3.
      2026-10-03: DONE in code — the dashboard name check against the vendored
      `platform/tests/fixtures/vllm-metric-names-506e66caa3ef.txt` landed with 3.1; alert
      rules are checked against the same list; the probe's shellcheck and no-literal-key
      check is `test_inference_probe.py`, which the o11y BATS suite pins rather than
      duplicates.
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
      2026-10-06 (task ids and output reported by the coordinator; the task output was not read
      here): probe drill run, alert identity unproven. `o11y-fault-drill.yml` now exists, and
      o11y Fault Drill (Dev) `drill=probe` task 2998 observed a firing alert for the drill
      model, delivered to Discord, with `/health` 200 throughout, then restored and verified
      (the probe mode rewrites only the o11y host's probe environment, per the playbook header,
      so nothing on the nodes is restarted). That a firing alert reached the contact point and
      `/health` stayed up is shown; that the alert was `inference_probe_failing` is NOT: the
      drill accepts any firing alert whose `model_name` is the drill model
      (`o11y-fault-drill.yml:363-375`, no `alertname` filter), and its final message prints the
      configured text "inference_probe_failing fired" (`:140`) whatever matched. Follow-up: the
      drill must match `alertname`, then rerun. The first attempt, 2981, failed on the controller's resolution of the
      `/health` target, fixed in #455. Still open: the wipe and redeploy with the three
      dashboards rendering ("Dashboards render from provisioning alone"), and the inference
      dashboard showing the model as not serving during the fault (the rest of "Health up,
      inference down"). The root disk is no longer full (task 1.5: 84.41% free in task 3141).
      Not ticked.
      2026-10-07: the alert-identity follow-up above (and under 3.3) is enforced in code on
      branch `fix/drill-alert-identity` (PR #466), not yet proven live: the firing proofs
      require the `alertname` read back from the provisioned rule's title, the Discord
      receipt must hold that name and this run's model within one message, and the final
      message names what matched from data. A rerun of o11y Fault Drill (Dev) `drill=probe`
      after merge is still needed to show `inference_probe_failing` specifically firing.
      Open gap, left as is: the post-restore resolved check has no `alertname` filter; it
      fails closed, and a restore-only run reads no rule, so it has no title to match.
      2026-10-07 (task ids and output as quoted by the coordinator; the task output was not read
      here): two more pieces PROVEN live, the box still open. (1) Alert identity: o11y Fault
      Drill (Dev) `drill=probe` task 3295 at `ffe4bb33` (after #466) reported
      `inference_probe_failing` firing for the drill model and reaching Discord naming both,
      with `/health` 200 throughout, then restored and verified; this resolves the follow-up in
      the 2026-10-06 and 2026-10-07 notes above and shows scenario "Alert reaches the contact
      point" for the probe. (2) Dashboards render: Verify o11y Dashboard Data (Dev) tasks 3303,
      3304 and 3305, lookback 1h, rendered `inference-fleet-health` with 10 Prometheus panels,
      `inference-latency-capacity` with 12 and `inference-placement-comparison` with 8 (the
      Loki panels were skipped). Still open: the wipe and redeploy half (destructive,
      operator-scheduled, not run), so "Dashboards render from provisioning alone" is not
      proven; and the inference dashboard showing the model as not serving during the fault,
      the rest of "Health up, inference down". Not ticked.
- [x] 3.6 External liveness watcher on a path the firewall permits: a Semaphore schedule
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
      2026-10-03: `o11y-fault-drill.yml`, which 2.5, 3.5 and this task name as their drill,
      does not exist in `platform/playbooks/`. The drills that exist are
      `drill-o11y-active-alert-delivery.yml`, `drill-o11y-alert-canary.yml` and
      `drill-o11y-unreachable.yml`; none implements the `drill=exporter|probe|grafana` modes.
      2026-10-03: the code is PR #410 (merged). Runtime pending: inventory values, the watcher-token provisioning run and the first scheduled run
      2026-10-08: ticked on live runs (Semaphore task ids as reported by the operator session;
      not re-read from Semaphore here). `Provision o11y Watcher Token (Dev)` published as 3512;
      dry run 3513 reported "absent; would mint", real run 3514 minted the token, re-dry run
      3515 reported "valid; reusing it". `Check o11y Liveness (Dev)` and its `*/10` schedule were
      published (3517, schedule 3520); manual run 3522 reported "o11y liveness OK"; scheduled
      run 3526 (20:30Z) ended in error because the watcher caught Grafana down during the
      drill; scheduled run 3532 (20:40Z) succeeded. Drill `o11y Fault Drill (Dev)` with
      `drill=grafana`: dry run 3523, real run 3524 reported "fault induced, the liveness
      watcher's Grafana-health failure reached Discord; restored and verified."

## 4. Retention, thresholds, records
- [ ] 4.1 After seven days: read Prometheus TSDB size and Loki ingestion per day; set
      retention against the VM disk with the arithmetic in `env.j2` comments; set alert
      thresholds from the baseline week
      2026-10-02 rescope: OPEN, blocked — retention stays 15d/7d/168h with a 0B cap. Reaching
      the 90d/45d/1080h target needs a seven-day forecast, ≥30% free space and an
      isolated-restore receipt (estate-wide 4.2/4.6e). The root disk is full (task 2115).
      2026-10-08: baseline partly read; NOT ticked. `Verify o11y Production Budgets (Dev)`
      task 3531 at a2224685 (as reported by the operator session; not re-read from Semaphore
      here): 12998 active series against the 2000 `sample_limit` match, memory headroom 86.58%,
      root filesystem about 84% free, retention Loki 7d and Prometheus 15d, container memory
      in MiB: alloy 49.5, grafana 69.2, loki 99.9, node-exporter 7.7, prometheus 99.9.
      Remaining: per-day ingestion and the alert thresholds from the baseline week; the
      retention increase stays blocked on the estate-wide 4.6e isolated-restore receipt.
- [x] 4.2 Append dated status lines to `plan/development/05-observability.md` and
      `plan/architecture/06-observability-instrumentation.md`
      2026-10-02 rescope: PARTIAL — 05-observability.md carries the dated production lines
      (2026-09-26 through 2026-09-29). 06-observability-instrumentation.md still states
      (2026-09-22) that no production receiver exists; append a dated correction.
      2026-10-04: DONE — `06-observability-instrumentation.md` gains the dated "Production receiver
      correction, 2026-10-04" callout (appended; no existing line changed).
- [x] 4.3 `plan/architecture/` record: static node scrape jobs and a dedicated o11y VM,
      with the rejected alternatives from the design
      2026-10-02 rescope: OPEN — no `plan/architecture` record of the production telemetry
      decisions exists yet.
      2026-10-04: DONE as **Proposed** — section "Production telemetry receiver: dedicated o11y VM
      and static node scrape" in `plan/architecture/06-observability-instrumentation.md` (the
      repo's convention: a section in the numbered doc), with the three rejected alternatives
      from design decisions 1 and 2. Accepted when Joe confirms the text.
- [ ] 4.4 Validation gate: both plan documents carry the dated lines and `git diff` shows
      only appended text, proving scenario "Status is dated and append-only"; on archive,
      retain the outcome (worked / dead end / corrected) into bank `agent-cloud-750a33b9`
      2026-10-02 rescope: OPEN — waits on 4.2, 4.3 and the archive.
