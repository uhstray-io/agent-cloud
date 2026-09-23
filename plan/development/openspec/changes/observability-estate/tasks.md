## 1. Service identity and local collection

- [x] 1.1 Record dated, source-backed as-built corrections in `05-observability.md` and `06-observability-instrumentation.md`; distinguish local code, live local proof, and unverified production state.
- [x] 1.2 Provision a generic Service Overview dashboard with a service selector and panels for matching logs and metrics; keep existing dashboard IDs stable.
- [ ] 1.3 After `inference-telemetry-production` lands, rebase and reconcile its o11y scrape files; add local engine discovery, opt-in filtering, service relabeling, high-churn exclusion, and a collector-reachable network path.
- [ ] 1.4 Add one pilot service's metrics declaration and a Semaphore-run verification query that names an unreachable target; update the onboarding checklist to require that evidence.
- [ ] 1.5 Validation gate: local Semaphore deploy and target/log queries prove spec scenarios "Opted-in service is collected", "Unreachable endpoint fails visibly", "Operator pivots between signals", and "New service has live evidence".

## 2. Provisioned alerts and budgets

- [ ] 2.1 Add inventory-controlled metric retention and series limits, with a local pilot measurement recorded before wider rollout.
- [ ] 2.2 Provision service-down and missing-telemetry rules, a generic dashboard link, and a contact point whose credential is rendered from OpenBao by Ansible.
- [ ] 2.3 Encode a reversible pilot fault drill through Semaphore and verify both rule firing and notification receipt without manual Grafana changes.
- [ ] 2.4 Validation gate: a wipe/redeploy plus the drill prove spec scenarios "Rebuild restores views and rules" and "Failure reaches an operator".

## 3. Trace ingestion after alert proof

- [ ] 3.1 Record the passed metrics, alert, retention, and cardinality gates; make trace enablement refuse an incomplete gate.
- [ ] 3.2 Add a real Alloy OTLP-to-Tempo path with a pinned image, inventory-controlled retention, self-hosted storage, and no receiver without a consumer.
- [ ] 3.3 Instrument one service with the stable service identity and verify trace-to-log and trace-to-metric pivots through the provisioned datasource.
- [ ] 3.4 Validation gate: a refused premature enablement and a successful pilot trace query prove spec scenario "Trace rollout gate" and re-prove "Operator pivots between signals".
