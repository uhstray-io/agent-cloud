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

Alloy's Loki external label and Prometheus's external label use the same
`o11y_cluster` inventory value. Local development defaults to
`agent-cloud-local`; production deploys require the label explicitly. This
implements the cluster-label portion of `estate-wide-observability-instrumentation`
task 3.3 (2026-09-28); the remaining instrumentation work stays with that change.
The source configs are `templates/config.alloy.j2` and
`templates/prometheus.yml.j2`; `Deploy o11y` renders their runtime files under
`config/` before starting the containers.

The agentgateway operations dashboard currently displays raw 4xx access records
from Loki. Its `http.status` filter does not parse or group a rejection reason.
Gateway tasks 4.2/4.3 must capture a real OTLP record and verify the exported
line shape and reason field before a reason-grouped panel can be implemented or
claimed complete.

Gateway spans always go to Tempo. Setting `o11y_gateway_span_logs_enabled: true`
in private inventory (default off) also writes one structured Loki line per span
under `{service="agentgateway", signal="span"}`: span name, status, duration and
trace id only, with no span attributes copied. It reuses the gateway OTLP/gRPC
receiver, so it needs no new port and the same receiver-inbound rule from the
gateway host to `o11y_otlp_bind:4317`. The client-view dashboard plots HTTP and
model (`agentgateway_gen_ai_server_request_duration`) p95 side by side, and the
inference latency dashboard links to it.

The production Semaphore inventory is a static copy of private site-config. After
a private inventory change is reviewed and merged, run the code-managed
operator-side `platform/semaphore/sync-inventory.yml` check and apply from the
reviewed agent-cloud worktree against Semaphore. Set `inventory_source` to the
production inventory file from the reviewed site-config revision, then verify
Semaphore's readback matches that source before a Dev-bound deploy relies on the
new values. Pin that deploy to the exact reviewed and pushed `dev` SHA. A
site-config merge alone does not update Semaphore's inventory copy.

Deployment does not establish target reachability. Confirm each target reports
`up == 1` after the observability host and DGX firewall source rules are set.
Run `Verify o11y Metrics Target (Dev)` through Semaphore for each enabled
exporter, supplying the exact reviewed `dev` SHA, its `service` label, the
inventory-declared `host:port`, and one metric name from that exporter. The
read-only receipt requires that exact instance to be healthy and its named
series to exist in Prometheus. A different failed instance does not mask this
target's result; a service-wide check without an instance still requires every
scrape to be healthy. Keep the separate Loki log receipt for log shippers.

## Inference dashboards and alerts

Three provisioned dashboards cover the DGX Spark pair: `inference-latency-capacity`
(first-token, end-to-end, inter-token, queue, prefill and decode latency; running and
waiting requests; KV-cache use; preemptions; token throughput), `inference-fleet-health`
(scrape health, memory headroom against the memory guard's thresholds, CPU, load, root
filesystem and the vLLM journal from Loki), and `inference-placement-comparison`
(the same signals against an earlier window, with a link to the dgx-spark `results/`
run manifests). Each has a `model_alias` variable. vLLM labels its series with
`model_name`, which carries the served model name, so the variable filters on that
label; Loki streams carry the same value as `model_alias`.

Every `vllm:` name the dashboards and alert rules use must appear in
`platform/tests/fixtures/vllm-metric-names-506e66caa3ef.txt`, a vendored copy of the
list dgx-spark recorded from the running image. Every `node_` name must appear in
`platform/tests/fixtures/node-exporter-metric-names-v1.12.1.txt`. The o11y BATS suite
enforces both. When dgx-spark re-pins the vLLM image and records a new list, replace the
fixture and fix any panel that names a metric the new list no longer has.

The alert template adds three groups only when `dgx_spark_scrape_enabled` is true, so
local renders never carry rules without a producer. When it is false the template lists
every inference rule under `deleteRules` instead: Grafana keeps a provisioned rule whose
group leaves the file, so turning the scrape off would otherwise leave the rules live and
routed.

- `inference-failing`: requests are waiting while vLLM has generated no tokens for five
  minutes. Health can still answer while this fires.
- `memory-thermal`: a node's `MemAvailable` stays under the memory guard's stall line
  (512 MiB) or its `MemFree` stays under the guard's floor (1 GiB) for five minutes. The
  guard kills the serving container well inside that time, so the alert means the guard
  did not recover the node. Override the lines with `o11y_dgx_spark_memavailable_stall_bytes`
  and `o11y_dgx_spark_memfree_floor_bytes` if dgx-spark changes them. Thermal rules wait for
  the GPU exporter, which is not scraped yet.
- `benchmark-gate`: one placeholder rule that evaluates a constant and stays paused until
  the dgx-spark benchmark manifest writer publishes a metric.

Every rule holds for at least five minutes and routes to the existing `agent-cloud-ops`
Discord contact point. Their uids start with `inference_`, so the deploy readback and the
drills, which check only the `o11y_` rules, do not cover them yet.

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
prints those budget values, series count, current guest root filesystem capacity,
and current memory headroom. Record the numeric ID
from its successful Semaphore task record as both receipts after
reviewing the result. The check changes no configuration.
Supply the reviewed Dev controller SHA and current deployed receiver SHA as
separate survey values. This lets the evidence workflow verify the existing
receiver before a gated redeploy, with both revisions recorded in Semaphore.

The budget receipt reports current guest root filesystem size/free bytes,
memory headroom, and read-only mount/storage observations for each existing
Prometheus, Loki, Tempo, Grafana, and Pyroscope named volume. Volume names are
derived from destination-specific mounts on the running containers, then
cross-checked against `podman volume inspect`; unresolved or ambiguous mounts
refuse a receipt. Allocated bytes are measured through `podman unshare du` so
rootless volume ownership is read inside Podman's user namespace. The receipt
reports those bytes and backing filesystem total/free bytes without printing
mount paths. A production retention expansion refuses to change config or pull
images unless each resolved volume filesystem has at least 30% free space.
Ordinary deploy and recovery remain available without this expansion gate.
The read-only volume receipt is intentionally limited to rootless Podman until
the Docker listing and mount formats have equivalent tested support.
Task 1789 (2026-09-28) passed the
retention/sample/cardinality checks but observed only 805,421,056 bytes free on
the 10,464,022,528-byte guest root; that guest observation alone does not prove
where the named volumes live. Record exact volume-backed measurements before
any retention or growth decision. During a retention expansion, a clean first
deploy is allowed only when all five backend containers and corresponding
named volumes are absent, and Podman's `store.volumePath` from
`podman info --format json` is an absolute existing directory whose backing
filesystem meets the same 30% threshold. This measures where Podman will create
the named volumes instead of assuming its storage shares guest `/`. The
read-only budget verifier remains fail-closed on missing, partial, or ambiguous
volume state. This playbook does not resize storage. Full-disk recovery requires
a separately reviewed backup-and-growth workflow. The destructive clean-deploy
playbook rejects any nonbaseline production retention tuple before removing
containers or volumes.

The production target is Prometheus 90d, Loki 45d, and Tempo 1080h
(45d). Keep the current 15d / 7d / 168h tuple until a measured capacity receipt
is available. A normal deploy refuses any production tuple change unless
`o11y_prom_retention_size` is nonzero and private inventory contains a numeric
`o11y_capacity_receipt_id` referencing a separately reviewed successful receipt. Do not create that receipt
until receiver host metrics have at least seven days of CPU/memory and
per-backend stored-byte growth, the forecast meets >=30% free disk, >=25%
memory headroom, and CPU p95 <70%, and any guest filesystem growth is repeatable
with backup before resize. The 30% current backing-filesystem preflight is
limited to production retention expansion; it does not block ordinary deploy
or recovery. Code now includes a private-network receiver-host
node_exporter with no published port, read-only root mount, and bounded CPU,
memory, root-filesystem, and load collectors. Its exact container isolation,
metrics availability, and host-versus-guest root filesystem parity require a
passing Semaphore runtime readback. No seven-day history or backend growth
receipt exists, so the target remains undeployable. The numeric ID is only a
reference; it is not itself a capacity forecast or proof.

Read-only Semaphore receipts on 2026-09-28: task 1793 resolved all five
observability volumes to the guest root filesystem and measured 777 MB free
(7.43%); task 1794 passed deploy check mode; task 1796 started the stack but
failed the node-exporter host-PID assertion; task 1797 confirmed the volumes
remained present and retention stayed at 15d / 7d / 168h, with about 736 MB
free. These receipts do not establish why Podman reported the PID mode or
whether guest storage can be expanded safely. The Dev-bound `Diagnose o11y Host
Storage` task is read-only and requires exact controller/receiver revisions; it
reports sanitized exporter PID/mount/network/port state, guest-root device
ancestry, and Podman's volume/graph filesystem capacity. It uses kernel device
numbers to resolve `/dev/mapper` aliases against `lsblk`; ambiguous mappings
fail closed. At that point, no runtime diagnostic receipt had been collected.

Read-only production Semaphore task 1801 reported node-exporter with private
PID isolation, a read-only host-root mount, one private network, and no
published ports. Its diagnostic normalized raw PID readbacks of `None`, empty,
or `private` to `private`. Podman documents a private PID namespace as the
default ([Podman run
reference](https://docs.podman.io/en/latest/markdown/podman-run.1.html)); the
deploy guard accepts only unset, empty, or `private` values and rejects host or
unknown modes. Task 1796 failed at the host-PID assertion while the source
declared host PID; the cause of that declaration/runtime mismatch remains
unresolved. No stale-container or Compose explanation has been established.
Task 1801 also reported
the guest root is ext4 on an LVM-backed
virtual disk and that Podman's volume and graph paths share that filesystem;
free space was 6.88%, below the 30% retention-expansion gate. Exact device
identifiers and capacity values remain in the private Semaphore receipt. No
guest growth occurred. The existing `snapshot-vm.yml` verifies snapshot
creation/presence only; there is no code-managed restore-test workflow or
verified backup/restore receipt, so it does not authorize disk growth.

Use the Dev-bound `Reconcile o11y Backup Job` template to assign the o11y VM to
an existing backup job. Declare its selected ID as `o11y_backup_job_id` in
private inventory after inspection; launch only with the exact reviewed Dev
SHA. Run Semaphore check mode first. The reconciler accepts only an enabled
job with an explicit VMID list, refuses `all`, pool, exclusion, malformed,
missing, duplicate, or concurrently changed job state, and sends only the
updated `vmid` list. It verifies the final membership and preserved job
options. This config change does not run a backup or prove artifact
immutability, retention, or restore success; the separate restore gate still
applies before guest growth.

The Semaphore survey defaults **Inspect backup jobs only** to `true`, so its
first launch is read-only. It verifies the reviewed Dev revision, o11y VM, and
OpenBao access, then lists sanitized candidate IDs, enabled state, selector
type, member count, and declared-VM membership. Inspection ends before job
detail reads or writes. Record the chosen existing job ID on the o11y host in
private inventory, then set inspection to `false` for check mode and the
reviewed apply.

Dev-bound read-only Semaphore task 1802, at controller revision
`5a9d17c7e26778049e1747ee48089985ae16121e`, verified the exact
`o11y/receiver-host` target at `node-exporter:9100` and returned
`node_filesystem_avail_bytes`. Semaphore tasks 1803 and 1804 also returned
`node_cpu_seconds_total` and `node_memory_MemAvailable_bytes` for the same exact
target. These receipts prove presence of the three metric families at that
target, not that their samples describe the host `/` mountpoint or its values.
The host-versus-guest root filesystem comparison remains pending a successful
full deploy. Log delivery, the seven-day capacity forecast, and the
backup/restore prerequisite also remain unproven.

The o11y self-monitoring dashboard requires seven healthy component scrapes and
Tempo span/byte rates. Receiver deployment reads back the provisioned dashboard
and Tempo datasource correlation mappings; a stale four-component dashboard
or missing trace-to-metric/log mapping fails verification. A production UI
click-through confirmed one agentgateway trace opened its same-span Loki log;
trace-to-metrics still needs a functional operator receipt.

## Pyroscope profile pilot

Pyroscope v2.2.0 listens only on the private o11y network and stores profiles
and metastore state in the persistent `pyroscope-data` volume. Its filesystem
retention is seven days with periodic metastore cleanup. The normal deployment
does not remove that volume or any existing observability volume.

Alloy profiling is disabled unless private inventory sets
`o11y_alloy_profile_pilot_enabled: true`. Before enabling it, record successful
Semaphore receipt IDs for the pinned Alloy config check, privacy review, and
measured receiver resource headroom using `o11y_profile_pilot_config_receipt_id`,
`o11y_profile_pilot_privacy_receipt_id`, and
`o11y_profile_pilot_resource_receipt_id`. The deploy refuses an enabled pilot
without all three receipts. The only initial target is Alloy's own
`alloy:12345` pprof endpoint, labeled `service_name=o11y/alloy`, sampled every 60
seconds. Compare CPU, memory, profile ingestion, and retained disk before
adding another producer. Runtime sampling, restart persistence, and measured
resource headroom still require the Dev-bound Semaphore evidence run.

## Receiver disk warning and backup readiness

The provisioned `o11y_receiver_root_disk_low` warning evaluates the exact
`receiver-host` `/` filesystem series and routes through the existing
`agent-cloud-ops` contact. It fires below 10% free for 15 minutes; missing
filesystem samples are treated as alerting. The 2026-09-28 read-only budget
receipt measured 7.06% free, so the threshold is relevant to current conditions.
The rule has a focused render test and deploy readback requires its exact UID to
be active when production alerts are enabled. The 2026-09-29 post-merge drill
verified firing and ops delivery for this low-space event; see the receipt below.

`Survey o11y Backup Readiness (Dev)` is a read-only Proxmox survey. It reads the
single declared production o11y VM from `vm_vmid` and `vm_node` on the sole
`o11y_svc` inventory host; both values must be present in the live inventory.
It reads backup-capable node storage and backup
content listings for that VM using OpenBao-sourced credentials. Its output
contains only status and counts. A listed artifact is a candidate; the survey
does not prove immutability, an isolated restore target, or restore success.
The first production survey attempt, Semaphore task 1828, failed before any
Proxmox request because the Authorization header was templated from play-level
URI defaults before the OpenBao-derived token facts existed. The survey now
attaches that header to each request after credential derivation; this was a
controller-side evaluation-order failure, not evidence of a Proxmox or storage
problem. Credential and request task logging remains suppressed.
The same dynamic play-level header pattern was removed from `snapshot-vm.yml`:
its Proxmox requests now attach Authorization only after the connection facts
are frozen, while retaining the existing certificate setting and `no_log`.

Read-only survey task 1835 succeeded on exact reviewed `dev` SHA
`fb7e5315f4f8d85a6b5b26a4f24d93f4692dbc7a`; the controller SHA and clean
checkout assertions passed. It found three active storage entries that declare
backup content and zero matching backup artifacts for the declared o11y VM.
No storage was selected and no Proxmox state changed. This proves neither
artifact immutability nor isolated restore readiness. The survey source now
adds aggregate PBS-versus-non-PBS storage counts, but task 1835 predates that
output, so its receipt contains no per-backend count.

The 90d Prometheus / 45d Loki / 1080h Tempo target remains blocked. Keep the
current 15d / 7d / 168h retention and 0B Prometheus size cap until the measured
seven-day capacity forecast, nonzero size cap, at least 30% backing-filesystem
headroom, and successful immutable-backup isolated-restore receipt are reviewed.
Follow the official [Proxmox storage reference](https://pve.proxmox.com/pve-docs/pvesm.1.html)
for storage content semantics. Do not grow the guest or change retention until
an immutable backup artifact has been restored and validated on a declared
isolated target, with cleanup recorded.

**Override, 2026-10-02 (Joe).** The guest root is grown ahead of that gate, because
the full 10 GiB root blocked every o11y deploy. `Grow o11y Root (Dev)`
(`grow-o11y-root.yml`) grows the partition, PV and root LV with its ext4 filesystem
online into the 100 GiB disk the VM already has. Retention stays as above. The
backup and isolated-restore gate still governs any retention change.

Production o11y deploy task 1822 succeeded non-destructively on reviewed
`dev` SHA `c0f65d9b4dca84a3c5b2e33f7e876e72ccac8b25` and passed the
host-versus-guest root metric check. Read-only budget task 1823 resolved all
five named volumes to the shared guest-root backing filesystem and measured
7.06% free. The deploy preserved those volumes and they remained mounted and
observed; this is not a historical data-continuity readback for each volume.
Retention remains Prometheus 15d, Loki 7d, and Tempo 168h; the Prometheus size
cap remains 0B. Task 1823 observed 13,088 active Prometheus head series, 87.55%
guest memory headroom, and sample limit 2,000. These are point-in-time readings,
not a seven-day forecast or Loki/Tempo growth trend. Log source, seven-day
baseline, and backup/restore proof remain open.

The corrected non-destructive production deploy, task 1830, succeeded on merged
`dev` SHA `3390557516d05101c60132e0c20e2cc5031a1bcd`. Read-only budget task 1831
observed all five named volumes mounted on guest root with 6.75% free, 13,503
active Prometheus series, and 87.73% guest memory headroom. Retention remains
Prometheus 15d with a 0B size cap, Loki 7d, and Tempo 168h. Mounted-volume
observations do not prove historical data continuity. These are point-in-time
readings, not a seven-day forecast; at the time of task 1831, disk-alert firing
and Discord delivery were still unverified; both were verified by the later
2026-09-29 low-space event described below.

At approximately 2026-09-29 04:56 UTC, the Grafana alert list showed
`o11y_receiver_root_disk_low` firing with a healthy evaluation. Its detail view
confirmed the receiver-root condition while the service-down rule was normal.
Grafana showed one notification routed to the existing ops contact, and
matching Discord delivery was independently verified. This verifies delivery
for the low-space event; it does not prove delivery for other alert classes or
a seven-day capacity forecast.

## Service conformance log receiver

The production service conformance collector sends OTLP/HTTP logs to Alloy on
the declared private `o11y_otlp_bind`, at port `4318` and path `/v1/logs`.
This receiver is separate from the gateway's OTLP/gRPC listener on `4317`.
Site-config allows TCP/4318 only from the controller CIDR. The collector checks
that its URL exactly matches the receiver bind and fails visibly when delivery
fails. Alloy applies `job`, `service`, `step`, and `status` as the conformance-
specific Loki labels. The shared Loki writer also adds `cluster` and
`environment` labels; task identifiers and error details stay in the log body.
Local development keeps the loopback direct-Loki path.
