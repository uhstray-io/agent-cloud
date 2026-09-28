## 1. Establish the accepted receiver baseline

- [ ] 1.1 Complete the existing o11y upgrade, self-monitoring and agentgateway dashboards, trace-to-log/metrics checks, and production receipt gates in their owning OpenSpec changes; record reviewed `dev` revision and successful Semaphore task IDs.
- [ ] 1.2 Capture a representative seven-day production baseline for backend ingest, active series, span rate and size, retention/disk growth, receiver drops, query load, p95 CPU and memory, and current VM/guest filesystem capacity without publishing private topology.
- [ ] 1.3 Validation gate — show the existing signal and active-alert baseline remains healthy before any new sender is enabled; record a successful exact-target receipt for **Scenario: Service passes three-signal verification** on the already instrumented gateway.

## 2. Declare and reconcile estate coverage

- [ ] 2.1 Implement the coverage declaration schema and read-only report using existing service/agent definitions plus private inventory; include lifecycle, owner, runtime, signal applicability, method, identity, budget, and receipt fields.
- [ ] 2.2 Reconcile the 23 platform service directories, four agent directories, and declared Proxmox/guest/runner/network/DGX targets against Semaphore templates and live readback; record deployed, planned, retired, and excluded states without guessing from a directory.
- [ ] 2.3 Extend onboarding and the shared o11y verifier so a stale or missing exact-target signal fails with its target name and a receipt; keep private endpoint values in site-config.
- [ ] 2.4 Validation gate — exercise a missing deployed signal and a scaffolded directory, proving **Scenario: Deployed target lacks a signal** and **Scenario: Scaffold is not counted as deployed**.

## 3. Bound collection and pilot the reusable methods

- [ ] 3.1 Add declarative per-host log and metric collection through the existing Semaphore/OpenBao boundaries; pilot one infrastructure VM and one application VM with least-privilege access and private ingress.
- [ ] 3.2 Pilot one request-serving runtime's supported zero-code OTel agent; verify version compatibility, startup/rollback, context propagation, redaction, sampling, and stable `service.name`. Record unsupported runtimes honestly and add a manual span only for a demonstrated critical gap.
- [ ] 3.3 Add receiver memory/backpressure, per-target cardinality and volume limits, drop/refusal monitoring, and provisioned missing-telemetry alerts; prove no secret, prompt, or request body enters the result.
- [ ] 3.4 Validation gate — use an unapproved sender, over-budget pilot, and unsupported runtime to prove **Scenario: Unapproved sender attempts export**, **Scenario: Ingestion budget exceeded**, and **Scenario: Instrumentation is unsupported**.

## 4. Gate each wave on receiver and VM capacity

- [ ] 4.1 Implement a read-only forecast report from measured peak ingest/compression, declared retention, pilot load, and VM resource headroom; make it refuse a wave on insufficient headroom, drops, or unhealthy queries.
- [ ] 4.2 For a refused wave, review the private VM spec and inventory change; use the existing Dev-bound Semaphore `resize-vm.yml` workflow only if growth is required. Confirm backup and guest filesystem growth path, keep reboot opt-in, and preserve all telemetry volumes.
- [ ] 4.3 Read back Proxmox and guest CPU/memory/disk, Grafana/backend health, data access, and alert baseline; rerun the forecast before enabling senders. If no resize is needed, record the passing no-change forecast.
- [ ] 4.4 Validation gate — deliberately fail a forecast to prove **Scenario: Wave exceeds capacity**; if a resize occurred, prove **Scenario: VM capacity increased** with live readback.

## 5. Roll out infrastructure and application cohorts

- [ ] 5.1 Onboard the deployed control-plane and infrastructure targets: o11y, Caddy, DNS, CA, OpenBao, OPA, Authentik, Semaphore, NetBox, Proxmox, guest/container hosts, runners, and pfSense or other declared managed devices. Use native/exporter metrics and logs; trace only compatible request-serving components.
- [ ] 5.2 Onboard deployed stateful and user services: n8n, Postiz, tududi, honcho, UhhCraft, ERPNext, OpenHands, and any confirmed Nextcloud/NocoDB/WikiJS targets. Include their declared DB/queue components, workers, dashboards, and meaningful alerts.
- [ ] 5.3 For each batch, merge reviewed public/private declarations, deploy from `dev` through Semaphore, run the capacity gate, collect fresh exact-target receipts, and restore the prior declaration if verification fails.
- [ ] 5.4 Validation gate — verify one controlled request and the required fresh signals for every deployed target in these cohorts, proving **Scenario: Service passes three-signal verification** and **Scenario: New instrumentation fails validation** in a rollback drill.

## 6. Roll out agent and inference cohorts and close coverage

- [ ] 6.1 Preserve the proven agentgateway/DGX signal path while onboarding deployed NemoClaw, NetClaw, WisBot, WebSmith, ComfyUI, Hunyuan3D, and other declared inference/agent workers; classify each runtime and apply a supported agent or documented alternative.
- [ ] 6.2 Add provisioned per-cohort dashboards and alerts only for verified data, including receiver loss, service health, worker failures, inference traffic/latency, and VM disk/capacity trends.
- [ ] 6.3 Reconcile all deployed inventory entries, recent receipts, budget forecasts, and exclusions; update the architecture's as-built notes and recovery guidance with the actual mechanisms and measured capacity decision.
- [ ] 6.4 Validation gate — produce a complete deployed-target coverage report and a reviewed failure/recovery receipt, proving **Scenario: Deployed target lacks a signal** and **Scenario: New instrumentation fails validation**.
