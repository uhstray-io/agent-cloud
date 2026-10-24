# Local Alloy metric label authority

## Goal

Make validated o11y inventory values authoritative for `cluster` and
`environment` on every opt-in local container metric sample before remote write.
Conflicting labels emitted by an application must not override the inventory
identity used to query the estate.

## Scope

- Add two unconditional `replace` rules to the existing
  `prometheus.relabel.bounded_labels` component, after the forbidden-label drop
  and before forwarding to Prometheus.
- Use `o11y_cluster` for `cluster` and the existing local-mode default expression
  for `o11y_environment` (`local` when unset).
- Extend `test_o11y_scrape_budgets.py` to render default and explicit inventory
  values and prove the label rules preserve service identity, target identity,
  bounded labels, filters, and scrape limits while replacing conflicting app
  labels.
- Keep the production template free of the local discovery/scrape/relabel path.

## Constraints

Do not change discovery eligibility, high-churn exclusion, forbidden-label
filtering, scrape intervals, sample or label budgets, production jobs, Loki or
trace writers, or cardinality/retention gates. Do not add a sender, workload,
or dependency. Historical Prometheus data is outside this change; a template
change does not prove live-series migration.

## Verification

Run the focused Python test module. If an existing Podman runtime and the pinned
Alloy receiver image are available, render the template and run Alloy validation
in a short-lived container with read-only config mounts and no secrets or
network access. Report unavailable runtime validation as blocked. Do not start
or stop the observability stack or other services.
