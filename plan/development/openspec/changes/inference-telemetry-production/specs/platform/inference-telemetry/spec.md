# platform/inference-telemetry

Production metrics, logs, dashboards and alerts for the inference estate, with the two
DGX Spark nodes as the first external targets.

## ADDED Requirements

### Requirement: Observability stack runs in production through the platform
The o11y stack (Grafana, Prometheus, Loki, Alloy) SHALL run on a production host
declared in the inventory, deployed and verified through Semaphore, with Grafana
published through Caddy at `o11y.uhstray.io`, and MUST NOT share a host with Caddy,
Semaphore or any inference or load-generation workload.

#### Scenario: Deploy converges and verifies
- WHEN the o11y deploy template runs against the production inventory
- THEN Grafana `/api/health`, Prometheus `/-/ready` and Loki `/ready` return 200 and a
  second run reports no changes

#### Scenario: Grafana reachable only through the front door
- WHEN a client requests `https://o11y.uhstray.io`
- THEN Caddy serves Grafana; and WHEN a LAN host connects to the Grafana port directly
- THEN the estate firewall refuses it unless the source is the Caddy host

### Requirement: DGX Spark targets are scraped
Prometheus SHALL scrape the host exporter and GPU exporter on both nodes and vLLM
`/metrics` on the head at a 15-second interval, from the single address the nodes
admit, and MUST attach the labels `cluster=dgx-spark`, `env=prod` and `node` to every
series.

#### Scenario: All node targets up
- WHEN both nodes' exporters are running
- THEN every `dgx-spark` job target reports `up == 1` and the vLLM scheduler, cache,
  preemption and latency families are queryable

#### Scenario: Missing scrape is a telemetry failure
- WHEN a node exporter stops answering for five minutes
- THEN the "telemetry missing" alert fires for that target, and no dashboard panel
  renders the gap as zero load

### Requirement: Node logs are received
Loki SHALL accept pushes from the two node addresses only, and every received stream
MUST carry `service`, `component`, `cluster`, `env`, `node` and `model_alias`.

#### Scenario: Boot journal is queryable
- WHEN both ranks restart on the nodes
- THEN `{cluster="dgx-spark", service="vllm"}` returns both nodes' boot lines within one
  minute of emission

#### Scenario: Unlisted source cannot push
- WHEN any host other than the two nodes posts to the Loki push port
- THEN the estate firewall refuses the connection

### Requirement: Three dashboards and four alert groups as code
Grafana SHALL provision, from committed files, a fleet and resource dashboard, an
inference latency and capacity dashboard, and a model and placement comparison
dashboard, and alert groups for sustained inference failure, missing telemetry,
memory or thermal risk and failed benchmark gate; every panel and rule MUST use metric
names recorded from the pinned vLLM image, and no alert MAY trigger an automated restart
or model change.

#### Scenario: Dashboards render from provisioning alone
- WHEN the o11y stack is wiped and redeployed
- THEN the three dashboards exist with every panel returning data or an explicit
  "no data" for a documented reason, with no manual step

#### Scenario: Alert reaches the contact point
- WHEN the synthetic inference probe fails for five consecutive minutes
- THEN the sustained-failure alert is delivered to the configured Discord contact point
  and nothing on the nodes is restarted

### Requirement: Synthetic inference distinguishes health states
A probe on the o11y host SHALL send one short chat completion through the public
hostname every five minutes and export its status and latency as metrics, so that
reachable host, ready model and successful inference are three distinct signals on one
dashboard.

#### Scenario: Health up, inference down
- WHEN `/health` returns 200 but the chat completion fails or exceeds its latency budget
- THEN the probe metric records the failure and the inference dashboard shows the model
  as not serving while the host shows as reachable

### Requirement: Plan documents state the verified status
`plan/development/05-observability.md` and
`plan/architecture/06-observability-instrumentation.md` SHALL carry dated status lines
recording that the stack was not in the production inventory on 2026-09-14 and the date
it entered production, appended without editing prior text.

#### Scenario: Status is dated and append-only
- WHEN a reader opens either document
- THEN a dated status line names this change and the production date, and the
  document's prior sections are unchanged
