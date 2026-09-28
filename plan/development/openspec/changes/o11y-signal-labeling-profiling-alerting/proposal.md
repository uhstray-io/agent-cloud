## Why

The receiver has working metrics, logs, traces, dashboards, and Discord alerts, but its service overview still selects Docker container names, alert labels do not form a reusable ownership contract, and continuous profiles have no backend or verified producer. Wider estate instrumentation needs bounded labels, cross-signal pivots, measured profiling capacity, and notifications that identify an actionable service without multiplying pages.

## What Changes

- Define one service identity and a small, bounded label contract for metrics, Loki streams, traces, profiles, and alerts. Keep request, user, trace, raw path, address, and secret values out of indexed metric/log labels; use log content or structured metadata for investigation detail.
- Add a code-managed cardinality and service-visibility baseline, with a service overview that works for declared remote targets as well as local containers. Show freshness and missing-signal state without fabricating absent data.
- Add Pyroscope as a private, persistent, separately provisioned profile backend. Pilot opt-in low-overhead pprof collection for an explicitly verified in-stack target before onboarding other runtimes; profile data joins on the service identity and has retention, capacity, and privacy gates.
- Enrich Grafana-managed rules with ownership, severity, service, environment, and cluster context. Use bounded rule-level grouping and timing on the existing Discord contact point, with read-back and a delivery drill. Preserve the existing shared notification policy tree.
- Extend the estate-wide instrumentation plan and onboarding guidance so each service declares its signal methods, profile applicability, label budget, alert owner, and proof; roll out by reviewed `dev` changes and non-destructive Semaphore deploys.

## Capabilities

### New Capabilities

- `platform/o11y-signal-governance`: bounded cross-signal labels, service visibility, opt-in continuous profiling, and actionable, low-noise alerting for the self-hosted observability estate.

### Modified Capabilities

None. The estate-wide instrumentation capability is still an open change; this change supplies its signal-governance foundation and records the dependency there.

## Impact

- Public repo: o11y Alloy, Prometheus, Grafana provisioning, dashboards, deploy verification, tests, onboarding and estate-instrumentation documentation.
- Private site-config: only new environment-specific declarations and resource budgets; production topology and secrets stay private.
- Runtime: Grafana, Prometheus, Loki, Alloy, Tempo, and a new private Pyroscope service. A profile producer is enabled only after its target, overhead, and retention budget are verified. No existing telemetry volume is removed.

## Rollback Plan

Revert the reviewed configuration or disable the profile pilot through inventory, merge through `dev`, and run the normal Semaphore deploy. Verify the previous five-component health, data-source, dashboard, and alert baseline. Keep all existing telemetry volumes; leave the new Pyroscope volume intact for a separately reviewed retention or retirement decision. Restore prior rule-level notification settings if a delivery drill shows unexpected grouping.
