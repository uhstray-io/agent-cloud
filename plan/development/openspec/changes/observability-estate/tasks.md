## 1. Service identity and local collection

- [x] 1.1 Record dated, source-backed as-built corrections in `05-observability.md` and `06-observability-instrumentation.md`; distinguish local code, live local proof, and unverified production state.
- [x] 1.2 Provision a generic Service Overview dashboard with a service selector and panels for matching logs and metrics; keep existing dashboard IDs stable.
- [x] 1.3 Reconcile the merged production DGX scrape files; add local engine discovery, opt-in filtering, service relabeling, high-churn exclusion, and a collector-reachable network path.
- [x] 1.4 Add one pilot service's metrics declaration and a Semaphore-run verification query that names an unreachable target; update the onboarding checklist to require that evidence.
- [x] 1.5 Validation gate: local Semaphore deploy and target/log queries prove spec scenarios "Opted-in service is collected", "Unreachable endpoint fails visibly", "Operator pivots between signals", and "New service has live evidence".

## 2. Provisioned alerts and budgets

- [x] 2.1 Add inventory-controlled metric retention and per-scrape sample limits, with a local pilot measurement recorded before wider rollout; production size selection remains gated on its receiver audit.
- [x] 2.2 Provision service-down and missing-telemetry rules, a generic dashboard link, and a contact point whose credential is rendered from OpenBao by Ansible.
- [x] 2.3 Encode a Dev-bound, local-only pilot fault drill that temporarily activates OpenBao-backed provisioning, verifies both rule firing and Discord receipt, and always restores paused rules and removes the contact point before success. Keep persistent enablement separate.
- [ ] 2.4 Validation gate: a wipe/redeploy plus the drill prove spec scenarios "Rebuild restores views and rules" and "Failure reaches an operator".

## 3. Trace ingestion after alert proof

- [ ] 3.1 Record the passed metrics, alert, retention, and cardinality gates; make trace enablement refuse an incomplete gate.
- [x] 3.2 Add a real Alloy OTLP-to-Tempo path with a pinned image, inventory-controlled retention, self-hosted storage, and no receiver without a consumer. The production receiver health and trace receipts passed.
- [ ] 3.3 Instrument one service with the stable service identity and verify trace-to-log and trace-to-metric pivots through the provisioned datasource.
- [ ] 3.4 Validation gate: a refused premature enablement and a successful pilot trace query prove spec scenario "Trace rollout gate" and re-prove "Operator pivots between signals".

## 4. Local review and production receipt

- [x] 4.1 Add an isolated, declarative local Semaphore binding for this branch; prove the executed revision and complete Grafana Authentik login through the TLS front door.
- [x] 4.2 Merge the local metrics, logs, and SSO baseline PR to `dev` (PR #206, `3de6fe71`). Alerts remain disabled until their destination and drill pass. For each remaining PR, complete one review round (Claude is acceptable when CodeRabbit is rate limited), resolve actionable findings, and confirm all required checks are green on the final head; then merge under the user's standing authorization. Run subsequent local and production automation only from the exact reviewed `dev` revision.
- [ ] 4.3 Record the unreachable legacy `grafanapodman` host without modifying its data (2026-09-26: the operator confirmed it retired; it is removed from the private inventory and from the container audit); reconcile the private pfSense API key into the discovery-owned OpenBao path through a Dev-bound Semaphore task, then prove the live DHCP boundary before reserving an address. Provision a separate receiver and private inventory through NetBox/Proxmox automation. Declare the Grafana Authentik app, Caddy route, and DNS through code, then deploy through Semaphore and verify TLS/SSO, health, retention, and access boundaries.
- [x] 4.4 Coordinate DGX Spark and agentgateway exporters with their owning tasks; verify named DGX node/vLLM series and Loki log receipt, and agentgateway telemetry, on the production receiver. Keep optional GPU scraping disabled until proven. Named receiver checks passed for both nodes, vLLM, and gateway; the DGX team's merged acceptance record covers both Loki streams.

## 5. Agentgateway and original o11y design integration

- [x] 5.1 Inventory every dashboard in the original `uhstray-io/o11y` repository, map each query to currently deployed exporters/backends, and record retained, rewritten, and deferred assets in `05-observability.md`.
- [ ] 5.2 Provision an agentgateway Grafana dashboard using the upstream standalone PromQL contract and stable Prometheus UID. Verify request, error, token, and latency series against the production receiver; make absent metrics visible rather than claiming traffic coverage.
- [ ] 5.3 Implement a pinned, persistent Tempo backend and an Alloy OTLP traces pipeline with bounded retention, private ingress, and a fail-closed trace gate. Provision and health-check Tempo as a Grafana data source. The backend, pipeline, data source, and production trace receipt passed; the fail-closed gate remains open under 3.1 and 3.4.
- [ ] 5.4 Configure agentgateway's sampled trace export with stable `service.name` and no prompt/completion capture; deploy gateway and receiver from reviewed `dev` through Semaphore and prove a real trace in Tempo plus metrics/log correlation.
- [ ] 5.5 Modernize and provision the original host/container dashboard only after its node-exporter and cAdvisor inputs have named live receipts. Keep the seven Mimir dashboards deferred until Mimir and their queries are migrated to the current Grafana schema.
- [x] 5.6 Replace agentgateway's query-bearing `http.path` in stdout and OTLP access records with CEL `request.path`; retain identity and the other default correlation fields.
- [ ] 5.7 After a reviewed Dev-bound Semaphore deploy, verify with a synthetic callback query marker that neither stdout nor OTLP records contain the query while status, identity, model, usage, and trace correlation remain. Assess previously emitted records against the declared retention and handling path; source changes do not sanitize stored history.

## 6. Bounded service graph and correlation coverage

- [ ] 6.1 Add the Tempo 2.10.8 service-graphs and span-metrics processors with bounded dimensions, persistent generator WAL, Prometheus remote-write, and a 2,000 active-series ceiling. Keep processors disabled by default; production enablement requires numeric capacity, backup/isolated-restore, metrics, alert-delivery, retention, and cardinality Semaphore receipts. Provision Grafana serviceMap, span-call query, existing trace-to-log, and Pyroscope trace-to-profile settings; read back the effective Tempo and datasource configuration.
- [ ] 6.2 Extend the existing 5.1/5.2 cohorts with a per-service log, metric, trace, and profile applicability matrix and exact-target receipts for each applicable signal. Require verified spans to underpin graph edges; a node alone, including an inferred peer, does not prove service trace coverage. Mark unsupported or not-applicable signals without inventing coverage.
- [ ] 6.3 Evaluate Grafana 12 trace-correlations UI separately after documented backup/restore and receiver-capacity gates. Do not upgrade the pinned Grafana 11.4 image as part of this baseline.

## 7. Provisioned dashboard query verification

- [x] 7.1 Extend the read-only receiver-side dashboard verifier to evaluate explicitly named Loki stream and metric LogQL targets from the exact provisioned dashboard JSON, with bounded requests and metadata-only reports; whole-dashboard runs skip Loki panels.
- [x] 7.2 Run the Dev-bound dashboard verifier against `service-overview`, selecting `Service log lines per second` and `Recent service logs` with an explicit `service` variable; confirm both Loki targets have data at the returned receiver revision and review the output for counts/status only. Sanitized read-only Dev-bound Semaphore receipts 3350 and 3352 verified both Loki panels for `agentgateway` over 6h. Receipt 3351 checked `Services tracked` and `Step status by service` at 24h but did not prove Grafana table rendering; the conformance table can combine statuses from overlapping collector runs, so that rendering/aggregation behavior remains open.

The dashboard and sampled gateway path are deployed. Requests, request latency,
and token usage have named production metric receipts; the time-to-first-token
histogram has no series yet, and the status-labeled error series has not been
queried separately. A real Tempo trace and same-service Loki/Prometheus signals
passed, but Grafana's trace pivots still need an operator click-through receipt.
The trace receiver is active before the required fail-closed enablement gate
and production alert-delivery proof; both remain open acceptance work. The four
DGX follow-up checks and the data-preserving local rebuild deferral remain open.
