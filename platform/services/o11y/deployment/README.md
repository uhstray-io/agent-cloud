# o11y deployment

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

`platform/playbooks/deploy-o11y.yml` manages the stack through Semaphore. Its
private inventory may define `dgx_spark_nodes` (name and address per node),
`dgx_spark_head_address`, `dgx_spark_head_name`,
`dgx_spark_node_exporter_port`, and `dgx_spark_api_port`. Set
`dgx_spark_gpu_exporter_enabled` and `dgx_spark_gpu_exporter_port` only after the
GPU exporter is validated on the nodes. `dgx_spark_scrape_enabled` defaults to
false: declaring nodes alone does not start scraping or page the ops channel.
The deployment removes the DGX scrape file while scraping is disabled.

Before enabling scraping, publish `Probe o11y DGX Exporter (Dev)` through the
scoped Semaphore template workflow. With the exact reviewed Dev SHA, select one
inventory-declared `probe_node_name` per run. The play reports the receiver's
route/source and makes one direct five-second `/metrics` request. Leave
`probe_expect_reachable=false` while the DGX owner observes the blocked packet
and its on-wire source. After the reviewed source-scoped firewall rule is
applied, repeat with `probe_expect_reachable=true` and require HTTP 200 on both
nodes. `Probe o11y Metrics Endpoint (Dev)` then checks the head's vLLM
`/metrics` with `probe_target=dgx-vllm`; its head address/name and API port
must match the private inventory. Only after all three metrics endpoints answer
from the receiver, enable `dgx_spark_scrape_enabled` in a separate private
inventory PR and verify the named Prometheus series. Keep GPU scraping and Loki
shipping separately gated.

For agentgateway, first bind its stats listener to its static LAN address and
apply a firewall rule limited to the o11y receiver. Run the same endpoint probe
with `probe_target=agentgateway`. Declare `agentgateway_metrics_address` and
`agentgateway_metrics_port` in a later private inventory PR only after that
probe gets HTTP 200. The receiver deploy refuses a target that differs from
the gateway's declared stats bind and port. An early scrape declaration creates
`up=0` and can fire the production service-down alert on the next o11y deploy.

The playbook renders `config/scrape.d/dgx-spark.yml` from the private inventory
and reloads Prometheus only if that file changes. The file is gitignored; keep
real node addresses in `site-config`. Metrics use the `service`, `component`,
`cluster`, `env`, and `node` labels. Retention defaults to 15 days for Prometheus
and 7 days for Loki until measured ingestion justifies a change.

Deployment does not establish target reachability. Confirm each target reports
`up == 1` after the observability host and DGX firewall source rules are set.
Run `Verify o11y Metrics Target (Dev)` through Semaphore for each enabled
exporter, supplying the exact reviewed `dev` SHA, its `service` label, the
inventory-declared `host:port`, and one metric name from that exporter. The
read-only receipt requires that exact instance to be healthy and its named
series to exist in Prometheus. A different failed instance does not mask this
target's result; a service-wide check without an instance still requires every
scrape to be healthy. Keep the separate Loki log receipt for log shippers.

## Local alert-delivery canary

After `Seed o11y Alert Webhook (Dev)` stores the approved webhook in OpenBao,
launch `Drill o11y Alert Canary (Dev)` through Semaphore with the exact pushed
`dev` SHA. It requires the local paused baseline, renders an active rule only
for its unique disposable probe, proves rule firing and a newer Discord message
from that webhook, and restores the paused rules and removes the contact point
in an Ansible `always` path. Discord history access and credentials are
checked before activation. A failed receipt is not a passed drill.

If the controller is interrupted before cleanup, run `Restore o11y Alert
Baseline (Dev)` with the reviewed `dev` SHA. It renders paused rules and
contact-point removal from code, removes the webhook line from the existing
`.env`, and verifies the live paused state. It can restore alerts while
OpenBao is unavailable. Persistent alert enablement is a separate reviewed
inventory rollout after a successful canary receipt.

## Production alert-delivery drill

When production alerts are already enabled, use `Drill o11y Active Alert
Delivery (Dev)` with the reviewed Dev controller SHA and the exact current
deployed receiver SHA. It checks that the
service-down rule and all o11y rules are active, the Discord contact exists
once, and the bot can read channel history before it adds a uniquely labeled
failed scrape. The shared probe waits for the named `up=0`, Grafana firing, and
a newer matching Discord message. Its `always` path removes only the generated
scrape file, reloads Prometheus, and verifies the active rules and contact are
still present.

If the controller stops before that verification, normal receiver deploys
refuse the `.o11y-active-alert-drill` marker. Run
`Recover o11y Active Alert Drill (Dev)` with the same two reviewed SHAs. Recovery is
safe to repeat and clears the marker only after Prometheus no longer has the
drill job and the active alert/contact state is read back. It does not remove
stored metrics, logs, or traces. This recovery is separate from the paused
canary's `Restore o11y Alert Baseline (Dev)` workflow.

## Trace rollout receipts

Both receiver and gateway deploys use the shared trace gate when tracing is
requested. Private receiver inventory must set
`o11y_trace_rollout_enabled: true`, retain `o11y_alerts_enabled: true`, declare
`agentgateway_metrics_address`, and store the four Semaphore task IDs in
`o11y_metrics_receipt_id`, `o11y_alert_delivery_receipt_id`,
`o11y_retention_receipt_id`, and `o11y_cardinality_receipt_id`. It also needs
explicit `o11y_prom_retention`, `o11y_prom_retention_size`,
`o11y_loki_retention`, `o11y_tempo_retention`, and
`o11y_scrape_sample_limit` values. Keep all receipt IDs and live inventory
values in private `site-config`.

Run `Verify o11y Production Budgets (Dev)` against the current receiver. It reads the live
Prometheus, Loki, Tempo, and Alloy settings plus active Prometheus series, then
prints only those budget values and the series count. Record the numeric ID
from its successful Semaphore task record as both receipts after
reviewing the result. The check changes no configuration.
Supply the reviewed Dev controller SHA and current deployed receiver SHA as
separate survey values. This lets the evidence workflow verify the existing
receiver before a gated redeploy, with both revisions recorded in Semaphore.

The o11y self-monitoring dashboard requires five healthy component scrapes and
Tempo span/byte rates. Receiver deployment reads back the provisioned dashboard
and Tempo datasource correlation mappings; a stale four-component dashboard
or missing trace-to-metric/log mapping fails verification. A production UI
click-through confirmed one agentgateway trace opened its same-span Loki log;
trace-to-metrics still needs a functional operator receipt.
