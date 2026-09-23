## Context

See `proposal.md`. Local Grafana, Prometheus, Loki, and Alloy were healthy at baseline. Local Semaphore candidate task 1065 checked out commit `653758c6a89cec76b0b8e703b3ca7f3b68e759a6` and deployed o11y; its Grafana, Prometheus, and Loki checks passed. Grafana completed an Authentik login through the chain-verified TLS front door, and the signed-in dashboard loaded again after that deploy. Prometheus still scrapes only itself; Alloy ships local container logs with a `service` label. The clean production inference telemetry branch is merged here and provides inventory-rendered DGX scrape jobs, but no production receiver is verified. `plan/architecture/06-observability-instrumentation.md` describes the desired contract but contains historical as-built claims.

On 2026-09-23, the local Prometheus v3.1.0 TSDB reported 616 active series, all under `job=prometheus`; this is a self-scrape baseline, not the service pilot measurement required by task 2.1. A read-only inventory listing confirmed that the declared `grafanapodman` host sits under `virtualmachines`, outside `agent_cloud`. The audit playbook now targets both, and `ansible-playbook --list-hosts` includes `grafanapodman`; no production container state has been observed yet.

The branch-bound local Semaphore verifier checked out `be5bbe8b3a0eafa3829a74baaccb72c0a8ae97e2`. Task 1079 found an `o11y-grafana` Loki log from the preceding 15 minutes in log-only mode. Task 1080 used the same revision with metrics required and failed at the named service with `targets found=0`, matching Prometheus's observed self-only target list. This proves the read-only receipt gate and leaves the opt-in metrics pilot unfulfilled. Task 1078 exposed the local Semaphore runner's separate loopback namespace; the verifier now queries Loki and Prometheus across the o11y container network.

## Goals / Non-Goals

**Goals:** implement the contract in `specs/platform/observability-estate/spec.md` through composable config and Semaphore; prove local SSO and each signal on one pilot; review via PR to `dev`; deploy and verify a production receiver for agentgateway and DGX Spark telemetry.

**Non-Goals:** modify DGX Spark nodes outside their owning task, enable an unproven GPU exporter, or claim production health from static files.

## Decisions

1. **Ship in dependency order.** First inventory and baseline evidence, then local metrics discovery and correlation, then provisioned alerts with a delivery drill, then traces. This follows `PRINCIPLES.md` §7 and prevents trace storage from preceding basic failure detection. Alternative rejected: deploying Tempo with the first metrics change.
2. **Use declaration-based collection within a host, generated targets across hosts.** The local o11y collector can inspect its own Podman socket. A target on another VM is outside that socket; its endpoint and permitted route come from inventory and a generated scrape fragment. Neither path asks an operator to hand-edit `prometheus.yml` per service. The pilot must join a network the collector reaches; the presence of labels alone is not proof. Alternative rejected: assuming local socket discovery covers remote VMs or isolated Compose networks.
3. **Normalize identity at collection.** Container name is the stable `service` label, matching `PRINCIPLES.md` §7. Keep `container` as a compatibility label for existing Loki dashboards; add `service` without replacing it until dashboards migrate. High-churn containers are dropped by a relabel rule. Alternative rejected: rename the existing Loki label in place.
4. **Keep provisioning declarative.** Grafana reads dashboards and alert rules from committed files. Ansible retrieves notification material from OpenBao and renders only gitignored, owner-readable runtime files. The alert delivery drill is an explicit Semaphore workflow. Alternative rejected: UI-created contact points or alert rules.
5. **Set budgets before expansion.** Inventory controls retention and a target series ceiling. The pilot records series count and storage growth before enabling remote targets or Tempo. The eventual Tempo receiver has real downstream consumers and a bounded block retention; a listener with no consumer is excluded.
6. **Integrate committed work without changing its checkout.** The clean production inference telemetry branch is merged into this isolated branch. Its DGX jobs are inventory-generated and optional; local discovery is additive. Review the combined diff and test it before local deployment.
7. **Validate the branch that will be reviewed.** The existing local Semaphore repository record is mounted from the main checkout, so its successful run cannot validate this feature branch. Use a declared, separate repository/template binding or equivalent isolated local Semaphore mechanism; never repoint the shared record. Verify the exact revision used by the task.
8. **Treat production receipt as the finish line.** Private inventory names a `grafanapodman` host outside the current `agent_cloud` service group, while the prior audit did not include it. Audit that host and its ownership before allocating another VM. Then provision or adopt the receiver through the service VM and Semaphore workflows, verify its network and SSO boundary, and query named DGX and agentgateway series/log labels. Keep GPU collection disabled until the DGX owner proves the exporter and approved access path.

## Risks / Trade-offs

- [A mounted engine socket grants powerful API access even with a read-only bind] → constrain it to the o11y collector, review engine permissions, and never expose it over TCP.
- [Labels exist but the collector cannot route to a service] → require a live target query in the onboarding gate and name unreachable endpoints.
- [An alert only proves Grafana evaluated a rule] → run a delivery drill and confirm receipt at the configured destination.
- [The local Semaphore template executes the main checkout] → create an isolated branch binding and record the task revision before calling a local run valid.
- [A remote scrape job exists but its endpoint is unreachable] → gate production on inventory, firewall, target-UP, and Loki receipt checks; name the failing source.
- [An unexamined legacy Grafana host is mistaken for a new or empty VM] → audit `grafanapodman` through approved read-only automation before host selection.
- [Historical plans describe future components in the present tense] → add dated, evidence-backed as-built notes without deleting the original design rationale.

## Migration Plan

1. Record current local and production evidence, and mark unverified claims explicitly.
2. Add the generic dashboard and onboarding verification contract on this branch; validate static artifacts.
3. Implement shared collection and alerting mechanisms, pilot the isolated branch through Semaphore, and record live results including SSO.
4. Open a PR to `dev` after the local gate. Reconcile any intervening changes and rerun focused checks before production.
5. Provision and deploy the production receiver through Semaphore, then prove DGX and agentgateway telemetry receipt. Enable tracing only after metrics and alert gates pass. Roll back by reverting provisioned config and redeploying through Semaphore; preserve volumes and retained data.
