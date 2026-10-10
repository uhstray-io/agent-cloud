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

## Recreate only on change

`deploy.sh` starts the stack through `compose_up_if_changed` (`platform/lib/common.sh`). It
hashes every input that must restart a container — the compose files in effect (including
`compose.prod.yml` and the probe overlay when set), `.env`, `env/*.env` and everything under
`config/` — and stores the digest as a label on each container. A deploy whose digest and
image IDs match the running containers leaves them alone; a changed input, a re-pulled tag,
a stopped or missing container, or a leftover container whose service is no longer
declared recreates the whole project (`--remove-orphans` removes the leftover). The service set
comes from the effective config (`compose config --services` over the same files). The script prints
`DEPLOY_CHANGED=true` or `DEPLOY_CHANGED=false`, and `deploy-o11y.yml` reports the task
changed only on the first. Operator decision 2026-10-04: a second deploy with identical
inputs is a true no-op.

**Force a recreate.** Launch `Deploy o11y (Dev)` with the survey input
`deploy_force_recreate=true` (default `false`; only the literal `true`, any case, forces — `yes` or `1` do not); the playbook passes `FORCE_RECREATE=true` to
`deploy.sh` and every container is recreated even though its digest matches. Use it when a
running container holds a stale bind mount: a mounted directory (`config/scrape.d`,
`config/grafana/provisioning`, `config/grafana/dashboards`) deleted and recreated under it
keeps pointing at the deleted inode while every input hashes the same. Deploys converge those
directories in place; `platform/tests/test_no_bind_mount_dir_delete.py` fails if a deploy
task deletes one.

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

The alert template adds the DGX rules only when `dgx_spark_scrape_enabled` is true, and
the synthetic probe rules only when `o11y_inference_probe_enabled` is true, so local
renders never carry rules without a producer. For each flag that is false the template
lists its rules under `deleteRules` instead: Grafana keeps a provisioned rule whose group
leaves the file, so turning a flag off would otherwise leave the rules live and routed.

- `inference-failing`: requests are waiting while vLLM has generated no tokens for five
  minutes (`inference_queue_stalled`, DGX scrape). Health can still answer while this
  fires. With the probe on, two more rules read the probe's samples from the
  `receiver-host` job, per `model_name`: `inference_probe_failing` when
  `inference_probe_success` stays 0 for five minutes, and `inference_probe_stale` when the
  newest `inference_probe_last_run_timestamp_seconds` is more than 15 minutes old, or no
  sample exists at all. The second rule matters because a stopped timer leaves the last
  success value in place, and the first rule alone would read that as healthy.
- `telemetry-missing`: any DGX Spark scrape target (`up{cluster="dgx-spark"}`) is down,
  or `vllm:num_requests_running` has been absent from the vLLM job, for five minutes
  (`inference_target_down`, `inference_vllm_metrics_absent`). Without these, a quiet
  inference board can mean no data rather than no problem.
- `memory-thermal`: a node's `MemAvailable` stays under the memory guard's stall line
  (512 MiB) or its `MemFree` stays under the guard's floor (1 GiB) for five minutes. The
  guard kills the serving container well inside that time, so the alert means the guard
  did not recover the node. Override the lines with `o11y_dgx_spark_memavailable_stall_bytes`
  and `o11y_dgx_spark_memfree_floor_bytes` if dgx-spark changes them. Thermal rules wait for
  the GPU exporter, which is not scraped yet.
- `benchmark-gate`: one placeholder rule that evaluates a constant and stays paused until
  the dgx-spark benchmark manifest writer publishes a metric.

Every rule holds for at least five minutes and routes to the existing `agent-cloud-ops`
Discord contact point. Their uids start with `inference_`. The deploy readback covers
them: it requires Grafana's live rules in the provisioned folders and groups to equal the
rendered file, whatever the uid ("Require the live rule set to equal the provisioned
rules" in `deploy-o11y.yml`). The drills check only the `o11y_` rules, so they do not
cover these yet.

## Synthetic inference probe

`/health` answering means the host is reachable. It does not mean the model is serving.
The probe sends one short chat completion every five minutes and records the result as
metrics. It sets `reasoning_effort: none` and `max_tokens: 16`. By default it uses the
public inference hostname, so it goes through Cloudflare, Caddy and the gateway, the same
path a client uses. With a gateway client identity (below) it goes straight to the
gateway's mutual-TLS listener instead, which is how production runs it until the public
route points at the gateway. This is task 3.3 of `inference-telemetry-production`.

The probe is off unless private inventory sets `o11y_inference_probe_enabled: true`.
With the flag off, `Deploy o11y` reads the same secrets and renders the same files and
node_exporter command it did before the probe existed. It also needs no sudo for the
probe. `platform/tests/test_inference_probe.py` checks both.

To enable it, set these on the o11y host in site-config:

- `o11y_inference_probe_url`: the public base URL, `https://<host>/v1`. The hostname is
  site-specific, so it is never committed here.
- `o11y_inference_probe_model`: a model name the endpoint serves.
- `o11y_inference_probe_key_field`: the OpenBao field that holds the probe's API key,
  under `secret/services/<o11y_inference_probe_key_service>`. The service defaults to
  `agentgateway`. Give the probe its own gateway identity: add a name to `agw_clients`
  on the gateway host, deploy agentgateway (it mints `client_<name>` once), then set this
  field to `client_<name>`. The probe's traffic can then be identified on the gateway
  dashboards and revoked on its own. The key is read through `_shared_reads`; it is never
  copied into `secret/services/o11y`.
- `o11y_inference_probe_timeout_seconds` (optional): the latency budget, 1 to 120,
  default 30. A request that takes longer than this counts as a failure.

What an enabled deploy installs:

- **Key file.** `probe/inference-probe.env`, mode 0600 and gitignored, holding the URL,
  model, key and budget. It is a separate file from `.env` because Grafana loads `.env` as
  its `env_file`, and the inference key does not belong in Grafana's environment.
- **Units.** `/etc/systemd/system/inference-probe.service` is a oneshot that runs as the
  deploy user. It has `ProtectSystem=strict` and can write only
  `/var/lib/node_exporter/textfile`. `inference-probe.timer` runs it at `OnCalendar=*:0/5`.
  The key reaches curl as a config line on stdin, so it never appears in an argument list
  or the journal.
- **node_exporter overlay.** `probe/compose.textfile.yml` adds `--collector.textfile` to
  the existing receiver-host node_exporter. The host directory is already visible through
  the read-only `/:/host` mount. The existing `receiver-host` scrape job collects the
  metrics, so no new scrape job or published port is needed.
- **Metrics.** Three gauges, each labelled `model_name`:
  - `inference_probe_success`: 1 only for HTTP 200 carrying a completion within the
    budget, otherwise 0.
  - `inference_probe_latency_seconds`: curl's total request time.
  - `inference_probe_last_run_timestamp_seconds`

  The script writes a temporary file in the same directory and renames it into place, so
  node_exporter never reads a partial file. A failed inference is recorded as a sample;
  it is not a failed unit.

The deploy then checks the result. The timer must be active. The deploy records the
host time, takes one sample immediately, and requires all three series in Prometheus
with the configured `model_name` and a last-run timestamp no older than that sample's
start, so a stale series or another model's series does not pass. Under `--check` the
unit files are only simulated, so activation and this check are skipped. The deploy does
not fail when inference itself is failing. That case is alerted by
`inference_probe_failing` (success 0 for five minutes) and `inference_probe_stale` (no
sample for fifteen minutes), both rendered only when `o11y_inference_probe_enabled` is
true (see "Inference dashboards and alerts"). Neither is proven at runtime until the
task 3.5 probe drill.

Setting the flag back to false removes each artefact an earlier enable left, checked one
by one: the timer (stopped and disabled first), the service unit, the last metrics file
and the key file. An interrupted install or removal is therefore still cleaned up.
Privileged steps run only for an artefact that exists, so a host that never ran the
probe needs no sudo.

### Gateway client identity

Until the public inference route points at the gateway (gateway task 4.3), a gateway
key sent through the public hostname reaches vLLM, which does not accept it. The probe
can instead go straight to the gateway's mutual-TLS listener as a gateway client
identity. Set `o11y_inference_probe_client_leaf` on the o11y host to the name of an
internal-CA client leaf declared for it. Unset, the probe stays on the public path and
nothing below runs.

The probe then presents that leaf, verifies the gateway's server leaf against the
internal CA bundle, and names the gateway by its server leaf's SAN. The paths reach curl
as arguments (they are not secrets); the key still travels on stdin only. Before
anything is placed, the deploy refuses the declaration unless all of these hold:

- exactly one `agentgateway_svc` host, with `agw_listener_tls` on and the leaf in its
  `agw_client_cert_allowlist`. The gateway admits a client leaf by its SANs only through
  that list (`agentgateway/deployment/templates/config.yaml.j2`); a leaf outside it gets
  403 even with a valid key.
- one `internal_leaves` entry of that name: profile `client`, host this o11y host, reload
  `none`.
- `o11y_inference_probe_key_field` is `client_<leaf name>`, read from `agentgateway`,
  and the leaf name is in the gateway's `agw_clients`. The key is the leaf's own
  identity. Another client's key would put the probe's traffic on that client's
  budget and dashboards, so it is refused even when that client is enrolled.
- `o11y_inference_probe_url` is exactly `https://<gateway server name>:<agw_port>/v1`, and
  this host declares `agw_verify_base_url` as the same URL without `/v1`. The daily
  `Renew Internal Certs (Dev)` proves a per-call client leaf off the gateway host through
  that variable and refuses the whole run for a leaf it cannot prove, so the probe and the
  renewal proof must use one path.
- one `step_ca_svc` host, and the gateway published on a non-loopback IPv4 address (its
  `agw_bind`, or its `ansible_host` when it binds every interface), with every octet
  0-255. The shared resolution step refuses a malformed name or address too, before it
  writes the hosts line.
- the leaf has been issued: `current/cert.pem` and `current/key.pem` exist, and the key is
  mode 0600 and owned by the deploy user, which the probe unit runs as.

An enabled deploy then places two host inputs that the renewal proof also reads:

- **Trust bundle.** The CA's root and intermediate, written to
  `<leaf dir>/../step-ca-bundle.crt` by `tasks/distribute-ca-root.yml`.
- **Gateway name.** The server leaf's name is mapped to the gateway's address by the
  shared resolution step, `tasks/agw-probe-resolution.yml`. This is the interim marked
  `/etc/hosts` line, or the internal zone once this host declares
  `agw_internal_dns_authoritative`.

The env file gains `INFERENCE_PROBE_CACERT`, `INFERENCE_PROBE_CLIENT_CERT` and
`INFERENCE_PROBE_CLIENT_KEY`. The script requires all three or none, and records a failed
sample naming only the variable for a path that is not an absolute, readable file. When
the probe is disabled, neither host input is removed: the declared leaf must stay
provable until it is removed with `Issue Internal Leaf (Dev)`, action `remove`, and then
its declaration dropped.

Rollout order, as first run in production on 2026-10-05: site-config declarations
(leaf, allowlist, `agw_clients` entry, the gateway's firewall rule for the o11y host,
the o11y probe variables) and an inventory sync; `Deploy step-ca (Dev)` (the CA signs
only declared SANs); `Deploy agentgateway (Dev)` (allowlist rule, `client_<name>`
minted); `Apply Firewall (Dev)` on the gateway; `Issue Internal Leaf (Dev)` with
`target_service=o11y_svc`; `Deploy o11y (Dev)`; then `Renew Internal Certs (Dev)`,
which must classify the leaf as a probe client. Leaf directories under this deployment's `certs/` are
gitignored.

Caveats:

- **Rollback.** On the public path, a `direct`-mode rollback (`rollback-inference-route.yml`)
  points the route at vLLM, which does not accept gateway keys, so the probe records
  failures. To keep it green, point `o11y_inference_probe_key_field` at the published
  `direct_<name>` field until the route is restored. The gateway client-identity path does
  not use the route: that mode moves only Caddy and leaves the gateway running, so the
  probe keeps its `client_<name>` key (and a `direct_` field is refused there).
- **Local canary.** The local-only `Drill o11y Alert Canary` restarts the stack through
  `deploy.sh` without this overlay. On that host the textfile collector stays off until
  the next deploy.
- **Runtime evidence and gaps.** The first enabled production deploy (2026-10-05, Deploy
  o11y (Dev) task 2967) ran the gateway client-identity path: its forced sample read back
  success 1 for the configured model, so the served model accepts `reasoning_effort: none`
  through the gateway. Still unproven: a renewal run that re-issues the probe's leaf and
  proves it from the o11y host (task 2969 was a dry run, with the leaf outside its renewal
  window), and the public path, including the o11y host's egress to the public hostname,
  which the probe does not use until the route points at the gateway (gateway task 4.3).

## Scheduled jobs and internal CA expiry

Two Loki-backed groups render in every environment (change `production-internal-ca`,
task 6.4, design decision 10). Their uids start with `o11y_`, so the deploy readback and
the drills require them active whenever alerts are enabled; like the other `o11y_` rules
they pause while the canary runs.

- `scheduled-jobs`: one rule, "Scheduled job silent", over the declared list
  `o11y_scheduled_jobs` (each entry `job` and `max_silence_hours`). It fires, per job,
  when that job has pushed no successful result line within its silence. The template
  default declares `renew-internal-certs` at 36 hours; an inventory value replaces the
  whole list, so keep that entry when adding a job. An empty list removes the rule
  through `deleteRules`. It is one rule rather than one per job so that removing a job
  rewrites an expression instead of leaving a provisioned rule behind. A job declared
  here fires as soon as alerts are active and the job has not run, so declare it in the
  same rollout that schedules it, or set the list in an environment that does not run it.
- `internal-ca`: a leaf with fewer than seven days left (critical) and the intermediate
  with fewer than ninety (warning).

Every line goes through `platform/playbooks/tasks/push-loki-lines.yml`. The rules select
on these stream labels; anything else belongs in the line body, never in a label:

| Line | Stream labels | Body (JSON) |
|---|---|---|
| Scheduled-job result, one per run, pushed last | `job`=`<declared job>`, `kind`=`run`, `status`=`success` or `failure` | free-form run summary |
| Leaf expiry, one per declared leaf per run | `job`=`renew-internal-certs`, `kind`=`cert`, `role`=`leaf`, `host`=`<consumer inventory host>`, `leaf`=`<declared leaf name>` | `not_after` (epoch seconds), `remaining_seconds` (integer, at push time), optional `serial` |
| Intermediate expiry, one per run | `job`=`renew-internal-certs`, `kind`=`cert`, `role`=`intermediate`, `host`=`<CA inventory host>`, `leaf`=`intermediate` | as for a leaf |

`status` is `success` only when the whole run passed, including every leaf's proof. Only
a `success` line resets the silence: a failing renewal stops publishing expiry lines, and
an expiry series that stops reads as no data, which the expiry rules treat as OK. The
expiry rules read the newest `remaining_seconds` within two days, so the value can be one
run interval stale, and the two-day window outlasts the 36-hour silence, so a stopped run
raises the silent alert before its expiry series disappears.

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

## Fault drills (`o11y Fault Drill (Dev)`)

`platform/playbooks/o11y-fault-drill.yml` induces one bounded fault, proves the alert
path it should trip, restores in an `always:` section and proves the alert cleared. It is
the drill that inference-telemetry-production tasks 2.5, 3.5 and 3.6 name.

| Mode | Fault | Proof | Restore |
|------|-------|-------|---------|
| `exporter` | stop one DGX node exporter's systemd unit | `up{job="dgx-spark-node",node=…} == 0`, then `inference_target_down` (group `telemetry-missing`) firing for that node within 12 min, matched by its `alertname` | unit started; `up == 1`; alert no longer firing |
| `probe` | probe environment points at an unservable model; `inference-probe.service` started at once | `/health` 200 on 24 checks over 12 min (see below for where it is read), `inference_probe_failing` firing for that model, matched by its `alertname`; one contact-point Discord message carrying the `service=vllm` line and, in its Firing section, that `alertname` and model | environment copied back, one good sample taken, alert cleared |
| `grafana` | `o11y-grafana` stopped (≤15 min, `drill_grafana_hold_minutes`) | the liveness watcher's Discord line naming Grafana's own health check (`o11y liveness watcher: grafana /api/health failed`), posted by its scheduled `Check o11y Liveness (Dev)` run | container started; `/api/health` answers |

The probe mode's `/health` is read where it means "the upstream serves while the probe
fails". On the public path it is the probe URL's own `/health`, from the runner. With a
gateway client identity (`o11y_inference_probe_client_leaf`, see "Gateway client
identity") the probe URL names the gateway's mutual-TLS listener, which resolves only on
the o11y host and declares no `/health` route (`config.yaml.j2` routes only the LLM API;
its readiness port binds loopback by default). The drill then reads the gateway upstream's `/health`
(`agw_upstream_base_url` without `/v1`) from the o11y host, and refuses to start without
exactly one gateway declaring that URL.

Every run requires `expected_repository_sha` (clean reviewed controller checkout) and
`confirm_fault_drill` equal to `drill`. `exporter` also refuses unless
`drill_window_confirmed=true` (an agreed dgx-spark window) and `drill_node` is declared in
the private inventory map `o11y_fault_drill_exporters: {<node>: {host: <inventory host>,
unit: <exporter systemd unit>}}`. `exporter` and `probe` refuse a paused rule (alerts must
be enabled). A dry run (`--check`) runs every refusal and read and induces nothing. After an
interrupted run, launch the same mode with `drill_restore_only=true`. A host that becomes unreachable
mid-drill does not skip the restore (the fault section sets `ignore_unreachable`; Ansible
skips `always:` for unreachable hosts); the run then fails naming that recovery. The
probe environment file holds the inference key, so every task touching it sets
`diff: false` and `no_log`. The probe restore requires a success sample for the deployed
model newer than the restore, and the resolved check covers every drill model name.

Alert identity: Grafana sets an alert's `alertname` label to its rule's title, so the drill
reads that title back from the provisioned rule (refusing a rule without one before any
fault) and a firing alert counts only when that name and the fault's own labels (node, or
this run's model) both match; another rule firing for the same node or model proves
nothing. The Discord receipt is matched within one message against the mode's
`_drill_receipt` pattern, and the final report names the rule uid, the title read back and
the node or model from data. The post-restore resolved check does not filter by
`alertname`: it fails closed (any alert still firing for the node or a drill model blocks
it), and a `drill_restore_only=true` run never reads the rule, so it has no title to match.

### Fresh-volume render proof (task 3.5)

"Dashboards render from provisioning alone" is proven on throwaway state, never by
wiping the production receiver (operator decision 2026-10-04):

```bash
make local-o11y-render-proof     # = scripts/o11y-render-proof.sh
```

It renders `templates/alerts.yml.j2` with the DGX and probe groups on (paused), starts a
uniquely named Grafana on an anonymous empty volume with the committed provisioning, reads
back every dashboard uid and alert-rule uid over the API, and removes the container and
volume on exit. It shares no port, network or volume with a running stack.

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
current memory headroom, and the memory of each o11y container. Record the numeric ID
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

The receipt also lists every container of the o11y compose project, stopped
ones included, with its memory in MiB (`o11y_container_memory_mib`). It is one
`podman stats --no-stream` reading of podman's raw cgroup byte count. On cgroup
v2 that count is `memory.current` minus `inactive_file` (the working set) with
podman >= 5.6.0 (containers/common v0.64), and the `anon` line of `memory.stat`
with podman <= 5.5 (common v0.63.x) and every 4.x release; neither is strict
resident memory, so read the receipt against the podman version of the host. Only
container names and MiB values are printed. A stopped container, an unreadable
`podman stats` (for example rootless Podman on cgroup v1), a missing reading or
a reading of zero bytes (what `podman stats` reports for a container that exited
after the listing) refuses the receipt instead of issuing a partial one.
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

## Receiver-host journal collector positions volume

The `Deploy o11y Journal Collector (Dev)` apply action gates the named
`journal-collector-state` volume before it starts the collector. A missing volume
can bootstrap only after bounded volume inventory proves no exact-name collision
and an all-container query, including stopped containers, proves
`o11y-journal-collector` absent. An existing pending volume can bootstrap only
with exact project/name identity, zero consumers and mounts, effective template
flags `NeedsChown=false` and `NeedsCopyUp=true`, and a read-only observation of
an empty root owned by `0:0`, owner RWX, no group/other write or special bits,
no ACL or nested mount, and no metadata ambiguity. Require at least 16 MiB free
bytes and 128 free inodes both on GraphRoot and on the pending volume filesystem
for this small first mount; this gate is separate from the 30% retention headroom gate.

Podman may omit false-valued initialization flags from JSON. The helper therefore
uses one fixed `podman volume inspect --format` template, accepts only literal
`false` or `true`, and brackets it with named JSON identity reads. It keeps raw
JSON field presence separate from effective values and refuses unknown template
output, identity mismatch, or a race. A missing Compose volume-key label is
accepted only for the exact expected name and matching project; a conflicting
label refuses. Pending repair compares `CreatedAt`, volume configuration, and
root device/inode across pre-mutation observations; missing or changed creation
identity refuses.

Repair checks free bytes and inodes independently from stable metadata equality.
Pending repair requires the threshold on both GraphRoot and the volume filesystem;
initialized repair requires it on the volume filesystem. Counter changes above
threshold do not invalidate root, child, ACL, or mount evidence, while a below-
threshold reading refuses the repair.

The separate `repair-positions` action keeps its initialized-volume behavior:
fresh evidence must prove an unused root with only safe regular direct files has
an owner mismatch that alone blocks collector access; repair changes only root
owner/group, reads it back, and runs the restricted write/rename/delete probe in
the cached Alloy image. A narrow pending exception permits the owner/group change
only for the exact empty, unused `NeedsChown=false` / `NeedsCopyUp=true` state. If
group/other write bits are the only failed guard, the helper may first clear only
those bits with `original_mode & ~0o022`; it preserves read/search bits and refuses
special bits, incomplete owner RWX, malformed mode or any unrelated failed check.
The mode reduction and owner change each use a no-follow directory descriptor
pinned to the same verified device/inode, with a full discovery and exact readback
between them. This branch does not mount a probe container or change contents. It
verifies the empty volume before first mount. After either pending-volume mutation
may have occurred, a failure preserves the volume and returns `uncertain`; no
mode/owner restoration or copied-state deletion is automatic because first-mount
history cannot be proved from a later empty state. A converged rerun reports
`already_correct` without another mutation.

Compose `up` is the only volume creation/initialization action. The pre-apply and
immediate pre-start `verify` gates accept `bootstrap_allowed` only for the proven
missing-volume or safe pending-volume cases above. They also permit a bounded
cursorless retry when a complete read-only observation proves an exact volume
with both initialization flags false, the
collector absent from all containers, zero consumers and mounts, safe `0:0` root
and allowlisted layout, `positions.yml` absent, access, ACL/mount checks, and
capacity on GraphRoot and the volume filesystem. This `ready` result authorizes
only a start attempt; it does not prove delivery. A present invalid or unreadable
cursor, unknown cursor presence, or any incomplete evidence refuses. `cursor_checked`
is true only when the safe, bounded file was read completely with stable identity
and its contents were parsed; absent, unsafe, oversized, or unreadable files leave
validity `unverified`. The read-only survey still does not inspect cursor contents.
The apply, immediate pre-mount, and post-start live gates parse positions output in
protected tasks; malformed or non-object JSON becomes fixed `unavailable`, and the
assertions still require normalized `ready`/`bootstrap_allowed` before start and
`ready` after start. The live diagnostic reads only the normalized result. The
immediate pre-start check runs outside rollback handling. After startup, `verify-live` is a
separate strict gate; a lingering
`NeedsCopyUp=true` is not success by itself: require the collector to be present
as the single consumer and mount, with the exact live RW mount,
namespace write/rename/delete, bounded Alloy layout, and a parsed `positions.yml`
entry for `cursor-loki.source.journal.o11y_alloy` with an empty label set and a
valid journal cursor. Also require healthy status, unchanged seven receiver
containers, and a fresh exact-target Loki receipt. An empty image destination
may leave copy-up pending. After start, an absent cursor, a present invalid or
unreadable cursor file, a pending temporary positions file, or any layout outside
the allowlist refuses as incomplete state; the cursorless pre-start retry is not
post-start acceptance. Failed-start rollback removes only the pilot container
without `-v`. The positions helper reports `check_mode_unverified` without its
own Podman or filesystem operations; the surrounding playbook may still run
other checks. Survey exceptions produce a bounded unavailable receipt
with fixed reason `survey_failed`.

The positions survey also emits a bounded `pending_repair_diagnostic` derived
from the same conjuncts as the pending repair gate. It classifies owner RWX,
the combined group/other write or special-bit result, each of group write,
other write, setuid, setgid, and sticky independently, and volume and GraphRoot
bytes/inodes; it lists failed checks from a fixed allowlist. Missing mode or
counters are `unverified`; no
paths, ownership IDs, modes, or counter values are emitted. A pending repair
refusal keeps `status=refused` and `reason=pending_volume_unsupported`; its
receipt carries the diagnostic categories and the refusal message lists the
bounded `failed_checks`. This snapshot
does not cover the repair's later identity rechecks and does not authorize a
repair. Check mode and survey exceptions use the same keys with unverified
categories. No additional Podman call, mount, content read, or ownership change
is part of this diagnostic.

The post-start `verify-live` receipt now adds `live_diagnostic`: fixed helper
status/reason and failed metadata categories, exact collector mount destination,
volume name/source/type/RW and configured-user comparisons, the expected seed
file and journal-component counts, and aggregate other-entry counts by kind.
It emits no entry names, paths, IDs, modes, or contents. Host and collector UID
maps remain under `no_log`; the visible receipt reports only availability and
equality. The existing namespace write/rename/delete probe remains part of the
live gate and emits only a fixed failing stage (`identity`, `create`, `write`,
`rename`, or `cleanup`) or `passed`. None of these diagnostics changes the
acceptance predicates.

The read-only positions survey may report top-level entry roles and aggregate
entry kinds, but it does not inspect cursor contents or run a namespace probe.
It compares extra directories only against the two exact public journal receiver
and OTLP exporter component roles. Matching directories get bounded owner,
access, ACL, mount, and direct-child categories. Unmatched direct child
directories also receive aggregate owner/access/ACL/mount/empty-or-nonempty
counts, without names, paths, hashes, contents, numeric identities, or exception
details. A race, symlink substitution, ACL/mount ambiguity, malformed metadata,
or incomplete observation makes that directory's categories `unverified`.
Both recognized and unknown extra directories remain outside the accepted
component layout. Its live receipt marks host/collector UID maps, equality, and
the namespace probe `not_run`. If the collector is absent, mount comparisons are
`unverified`; survey evidence cannot satisfy the live gate.

**Dev collector gate diagnostic (2026-10-09):** User-provided task 3986 check
mode and task 3987 survey precede task 3988, which started the collector and
passed health/readiness but refused the strict live mount/namespace gate and
rolled back safely. Task 3989 found the collector absent and the named volume
preserved with three entries; entry children and cursor evidence were
ambiguous/unavailable. Those receipts do not identify the failed live
predicate. The next action is a reviewed diagnostic survey at a fresh merged
Dev revision; do not apply again until its evidence is reviewed. Task 1.2 stays
open.

**Dev positions extra-directory diagnostic (2026-10-10):** Our read-only
Dev-bound Semaphore API survey task 4009 succeeded at merged Dev SHA
`9bc543d7d0f80be77ddab3983f9db6a983257f06`. It reported
`volume_initialization_pending`, collector absent, zero volume users, three
entries, matching root owner, RWX access, ACL absent, mount clear, sufficient
free space, ambiguous children, and unavailable journal cursor. Its bounded
top-level categories were one seed file, one existing journal component, one
other directory, and zero other files, symlinks, or kinds. This receipt does
not identify the extra directory: live component mounts and namespace fields
were unverified/not run. The follow-up compares only the exact public receiver
and exporter component roles and reports bounded metadata categories; an
unmatched directory remains anonymous. It does not infer queue state, alter
pre-start or verify-live acceptance, mount or write the volume, or authorize
collector apply. Task 1.2 remains open; do not apply until the fresh diagnostic
receipt and remaining live gates are reviewed.

**Dev unknown-directory metadata receipt (2026-10-10):** Our read-only
Dev-bound Semaphore API survey task 4016 succeeded at exact Dev merge
`736af5b16917f28a243b3715d56c76c00bfe7a8a`. Its sanitized receipt reported a
pending-initialization volume, no collector, no valid cursor, and three direct
entries: one seed file, one journal component, and one unmatched directory; the
recognized receiver and exporter directory counts were zero. Volume identity,
capacity, root ownership, and root RWX checks were good. `NeedsCopyUp` was true
and the `NeedsChown` field was missing. The component-layout check failed; the
pending-repair diagnostic listed `children_empty`, `child_count_zero`,
`owner_mismatch`, and `collector_access_blocked` among its failed checks. This is
our API-observed sanitized evidence, not user-provided evidence. The follow-up
reports only aggregate categorical metadata for unmatched direct child
directories. It does not identify the directory or infer queue state. Stop:
keep repair and apply refused while unmatched entries remain or any category is
unverified; collect and review a fresh survey after this change reaches Dev.
Task 1.2 remains unchecked.

The live Dev survey 3925 at reviewed SHA `8050f2768903e09f68285f90c1598cb120006219`
(sanitized receipt supplied for this implementation; not rerun here) reported
the exact empty, unused pending volume with owner mismatch, owner RWX, no ACL or
mount ambiguity, no collector, and sufficient volume/GraphRoot capacity. Group
write was the only present write/special bit; other write and special bits were
absent. This supports the narrow code path but does not authorize a live mutation,
prove repair readback, or close the delivery gate. The earlier live Dev survey
3865 (user-provided evidence; not rerun by this source-only change) reported
Podman client 4.9.3 with server unavailable, exact all/named volume identity,
omitted `NeedsChown` and `NeedsCopyUp` JSON fields, effective `NeedsChown=false`
and `NeedsCopyUp=true`, an empty unused volume with owner mismatch and blocked
access, and no ACL or mount ambiguity. Earlier Dev positions survey 3856
reported `volume_initialization_unverified`, zero
consumers and entries, root owner mismatch, blocked access, no ACL, a clear
mount, and safe direct children. The journal source was available, but access
to the target positions path was denied. The explicit survey adds sanitized
initialization field presence/type, template values, identity comparisons, and
bounded Podman versions. Raw Podman output is never reported.

OpenSpec task 1.2 remains unchecked until the reviewed Semaphore run records the
production runtime and Loki evidence. Unit tests and metadata-only survey results
do not close that task.
