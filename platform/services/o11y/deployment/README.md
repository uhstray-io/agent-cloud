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
