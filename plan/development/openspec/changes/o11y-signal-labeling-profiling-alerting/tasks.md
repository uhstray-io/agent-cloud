## 1. Signal contract and service view

- [x] 1.1 Document bounded labels, forbidden dimensions, per-signal mappings, and ownership in the o11y and estate-wide onboarding plans.
- [x] 1.2 Update the service overview and self-monitoring dashboards for service identity, remote target absence, cardinality, and ingestion context.
- [x] 1.3 Add focused config/dashboard checks and validate the spec scenarios “A new service is instrumented,” “A high-cardinality attribute is received,” and “A declared remote target stops reporting.”

## 2. Profile backend and pilot

- [x] 2.1 Provision private persistent Pyroscope, its Grafana datasource, and non-destructive deploy/read-back verification.
- [x] 2.2 Add inventory-gated Alloy self-profile scrape and write pipeline with bounded labels and documented resource/privacy budget.
- [ ] 2.3 Validate config and runtime sampling, storage persistence, and the spec scenarios “Profile pilot is disabled” and “Profile pilot is enabled.”

## 3. Actionable alerts

- [x] 3.1 Add bounded ownership/context labels, dashboard/runbook annotations, and rule-level grouping/timing to existing Grafana rules without provisioning the policy tree.
- [ ] 3.2 Extend deploy read-back and focused tests for rule settings and run a bounded Discord delivery drill.
- [ ] 3.3 Validate the spec scenarios “Several target failures occur in one service” and “A notification change is deployed.”

## 4. Promotion and evidence

- [ ] 4.1 Run focused tests, lint, and strict OpenSpec validation; review the diff for secrets, volume preservation, and architecture compliance.
- [ ] 4.2 Push one PR against `dev`, obtain one CodeRabbit or Claude review, resolve feedback, and merge only with green CI.
- [ ] 4.3 Deploy non-destructively through Semaphore from `dev`, record runtime and resource evidence, and validate the spec scenario “Configuration is promoted.”

## 5. Production retention capacity gate

- [x] 5.1 Record targets of Prometheus 90d, Loki 45d, and Tempo 1080h; keep the effective production tuple unchanged and refuse a tuple change without a nonzero Prometheus size cap and numeric ID referencing a separately reviewed successful capacity receipt.
- [x] 5.2 Add read-only current guest root filesystem and memory observations plus exact named-volume/backing-filesystem and stored-byte observations to the budget receipt; label these as observations, not a volume-growth forecast. For a clean first deployment during retention expansion, validate `podman info` `store.volumePath` and gate on that path's backing filesystem instead of assuming guest `/` holds named volumes. Record production budget verifier task 1789 as a passing settings/cardinality read with insufficient guest-root free space and no named-volume resolution.
- [ ] 5.3 Add a private same-host receiver metrics source with no published port and a bounded read-only host mount; prove exact host metrics and host-versus-guest filesystem parity through Semaphore (local mode skips host-versus-guest parity because Podman runs in its VM). Keep source-scoped firewall proof for REMOTE collectors. Collect at least seven days of CPU, memory, filesystem, and per-backend stored-byte growth evidence. The code now provides the collector and runtime checks, but no live receipt or seven-day history exists yet.
- [ ] 5.4 Derive and review a 90/45-day storage forecast meeting >=30% free disk, >=25% memory headroom, and CPU p95 <70%; implement idempotent guest partition/filesystem growth with backup before resize if required. Semaphore task 2115 on reviewed `dev` SHA `2d567d86eef1f2cd9f7cda63ba0ac03f6663d642` reported zero bytes available on the 10 GiB root filesystem, a roughly 10 GiB root LVM block, an 18.2 GiB partition, and a 100 GiB virtual disk; all rootless Podman volumes share the root filesystem. The unprivileged root-LV join was unresolved. A separate privileged read-only LVM survey is implemented in the [Grafana incident plan](../../../O11Y-GRAFANA-AUTH-INCIDENT.md), but its live readback is pending. This evidence does not authorize growth; backup immutability, isolated restore, and the required capacity history remain gates.
- [ ] 5.5 Update private inventory and size the o11y VM only after the receipt passes; deploy the target retentions through Semaphore and verify all existing volumes remain intact.
- [x] 5.6 Report sample-limit match, head-series result count, and positive-series status separately in the read-only budget verifier; emit only normalized values and fixed invalid markers so a failed aggregate receipt is diagnosable without exposing labels or URLs. Production deploy task 1783 and alert drill 1786 succeeded at merged revision `1226d8b`; budget verifier tasks 1784 and 1785 failed the aggregate check after retention comparison, prompting this diagnostic. Their runtime cause remains unverified until rerun.
