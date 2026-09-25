# o11y deployment

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

`platform/playbooks/deploy-o11y.yml` manages the stack through Semaphore. Its
private inventory may define `dgx_spark_nodes` (name and address per node),
`dgx_spark_head_address`, `dgx_spark_head_name`,
`dgx_spark_node_exporter_port`, and `dgx_spark_api_port`. Set
`dgx_spark_gpu_exporter_enabled` and `dgx_spark_gpu_exporter_port` only after the
GPU exporter is validated on the nodes. With no nodes declared, the deployment
removes the DGX scrape file and the local profile keeps only self-scrape.

The playbook renders `config/scrape.d/dgx-spark.yml` from the private inventory
and reloads Prometheus only if that file changes. The file is gitignored; keep
real node addresses in `site-config`. Metrics use the `service`, `component`,
`cluster`, `env`, and `node` labels. Retention defaults to 15 days for Prometheus
and 7 days for Loki until measured ingestion justifies a change.

Deployment does not establish target reachability. Confirm each target reports
`up == 1` after the observability host and DGX firewall source rules are set.

## Local alert-delivery canary

After `Seed o11y Alert Webhook (Dev)` stores the approved webhook in OpenBao,
launch `Drill o11y Alert Canary (Dev)` through Semaphore with the exact pushed
`dev` SHA. It requires the local paused baseline, renders an active rule only
for its unique disposable probe, proves rule firing and a newer Discord message
from that webhook, and restores the paused rules and removes the contact point
in an Ansible `always` path. A failed receipt is not a passed drill.

If the controller is interrupted before cleanup, run `Restore o11y Alert
Baseline (Dev)` with the reviewed `dev` SHA. It uses the same OpenBao-backed
rendering and verifies that all o11y rules are paused and the contact point is
absent. Persistent alert enablement is a separate reviewed inventory rollout
after a successful canary receipt.
