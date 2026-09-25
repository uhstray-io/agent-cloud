## 1. Service identity and local collection

- [x] 1.1 Record dated, source-backed as-built corrections in `05-observability.md` and `06-observability-instrumentation.md`; distinguish local code, live local proof, and unverified production state.
- [x] 1.2 Provision a generic Service Overview dashboard with a service selector and panels for matching logs and metrics; keep existing dashboard IDs stable.
- [x] 1.3 Reconcile the merged production DGX scrape files; add local engine discovery, opt-in filtering, service relabeling, high-churn exclusion, and a collector-reachable network path.
- [x] 1.4 Add one pilot service's metrics declaration and a Semaphore-run verification query that names an unreachable target; update the onboarding checklist to require that evidence.
- [x] 1.5 Validation gate: local Semaphore deploy and target/log queries prove spec scenarios "Opted-in service is collected", "Unreachable endpoint fails visibly", "Operator pivots between signals", and "New service has live evidence".

## 2. Provisioned alerts and budgets

- [x] 2.1 Add inventory-controlled metric retention and per-scrape sample limits, with a local pilot measurement recorded before wider rollout; production size selection remains gated on its receiver audit.
- [ ] 2.2 Provision service-down and missing-telemetry rules, a generic dashboard link, and a contact point whose credential is rendered from OpenBao by Ansible.
- [ ] 2.3 Encode a reversible pilot fault drill through Semaphore and verify both rule firing and notification receipt without manual Grafana changes.
- [ ] 2.4 Validation gate: a wipe/redeploy plus the drill prove spec scenarios "Rebuild restores views and rules" and "Failure reaches an operator".

## 3. Trace ingestion after alert proof

- [ ] 3.1 Record the passed metrics, alert, retention, and cardinality gates; make trace enablement refuse an incomplete gate.
- [ ] 3.2 Add a real Alloy OTLP-to-Tempo path with a pinned image, inventory-controlled retention, self-hosted storage, and no receiver without a consumer.
- [ ] 3.3 Instrument one service with the stable service identity and verify trace-to-log and trace-to-metric pivots through the provisioned datasource.
- [ ] 3.4 Validation gate: a refused premature enablement and a successful pilot trace query prove spec scenario "Trace rollout gate" and re-prove "Operator pivots between signals".

## 4. Local review and production receipt

- [x] 4.1 Add an isolated, declarative local Semaphore binding for this branch; prove the executed revision and complete Grafana Authentik login through the TLS front door.
- [x] 4.2 Merge the local metrics, logs, and SSO baseline PR to `dev` (PR #206, `3de6fe71`). Alerts remain disabled until their destination and drill pass. For each remaining PR, complete one review round (Claude is acceptable when CodeRabbit is rate limited), resolve actionable findings, and confirm all required checks are green on the final head; then merge under the user's standing authorization. Run subsequent local and production automation only from the exact reviewed `dev` revision.
- [ ] 4.3 Record the unreachable legacy `grafanapodman` host without modifying its data; reconcile the private pfSense API key into the discovery-owned OpenBao path through a Dev-bound Semaphore task, then prove the live DHCP boundary before reserving an address. Provision a separate receiver and private inventory through NetBox/Proxmox automation. Declare the Grafana Authentik app, Caddy route, and DNS through code, then deploy through Semaphore and verify TLS/SSO, health, retention, and access boundaries.
- [ ] 4.4 Coordinate DGX Spark and agentgateway exporters with their owning tasks; verify named DGX node/vLLM series and Loki log receipt, and agentgateway telemetry, on the production receiver. Keep optional GPU scraping disabled until proven.
