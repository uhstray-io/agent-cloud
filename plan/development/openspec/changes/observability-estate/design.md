## Context

See `proposal.md`. At the branch base, the local o11y compose has Grafana, Prometheus, Loki, and Alloy. Prometheus scrapes itself; Alloy discovers container logs over the local Podman socket. `inference-telemetry-production` has uncommitted changes to the o11y deploy, scrape config, compose, and tests in a different worktree. `plan/architecture/06-observability-instrumentation.md` describes the desired contract but contains as-built claims that require verification.

## Goals / Non-Goals

**Goals:** implement the contract in `specs/platform/observability-estate/spec.md` through composable config and Semaphore; prove each signal on one local pilot before broad adoption.

**Non-Goals:** modify DGX Spark nodes, allocate the production o11y VM, replace the production inference change, or claim production health from static files.

## Decisions

1. **Ship in dependency order.** First inventory and baseline evidence, then local metrics discovery and correlation, then provisioned alerts with a delivery drill, then traces. This follows `PRINCIPLES.md` §7 and prevents trace storage from preceding basic failure detection. Alternative rejected: deploying Tempo with the first metrics change.
2. **Use declaration-based collection within a host, generated targets across hosts.** The local o11y collector can inspect its own Podman socket. A target on another VM is outside that socket; its endpoint and permitted route come from inventory and a generated scrape fragment. Neither path asks an operator to hand-edit `prometheus.yml` per service. The pilot must join a network the collector reaches; the presence of labels alone is not proof. Alternative rejected: assuming local socket discovery covers remote VMs or isolated Compose networks.
3. **Normalize identity at collection.** Container name is the stable `service` label, matching `PRINCIPLES.md` §7. Keep `container` as a compatibility label for existing Loki dashboards; add `service` without replacing it until dashboards migrate. High-churn containers are dropped by a relabel rule. Alternative rejected: rename the existing Loki label in place.
4. **Keep provisioning declarative.** Grafana reads dashboards and alert rules from committed files. Ansible retrieves notification material from OpenBao and renders only gitignored, owner-readable runtime files. The alert delivery drill is an explicit Semaphore workflow. Alternative rejected: UI-created contact points or alert rules.
5. **Set budgets before expansion.** Inventory controls retention and a target series ceiling. The pilot records series count and storage growth before enabling remote targets or Tempo. The eventual Tempo receiver has real downstream consumers and a bounded block retention; a listener with no consumer is excluded.
6. **Protect parallel work.** This branch first edits its own change artifacts, onboarding contract, and generic dashboard. It avoids the o11y files currently dirty in `inference-telemetry-production`. Rebase and reconcile those files after that work lands; tests must pass against the combined tree before local deployment.

## Risks / Trade-offs

- [A mounted engine socket grants powerful API access even with a read-only bind] → constrain it to the o11y collector, review engine permissions, and never expose it over TCP.
- [Labels exist but the collector cannot route to a service] → require a live target query in the onboarding gate and name unreachable endpoints.
- [An alert only proves Grafana evaluated a rule] → run a delivery drill and confirm receipt at the configured destination.
- [Concurrent telemetry work changes shared o11y files] → keep ownership disjoint until rebase and review the combined diff before implementation of shared scrape files.
- [Historical plans describe future components in the present tense] → add dated, evidence-backed as-built notes without deleting the original design rationale.

## Migration Plan

1. Record current local and production evidence, and mark unverified claims explicitly.
2. Add the generic dashboard and onboarding verification contract on this branch; validate static artifacts.
3. Rebase after the production inference telemetry change, then implement the shared collection and alerting mechanisms, pilot locally through Semaphore, and record live results.
4. Enable tracing only after metrics and alert gates pass. Roll back by reverting provisioned config and redeploying through Semaphore; preserve volumes and retained data.
