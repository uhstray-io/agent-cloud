# Design: inference telemetry in production

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

## Context

Verified 2026-09-14:

- `platform/services/o11y/deployment/compose.yml`: Prometheus `v3.1.0` on 9090, Loki
  `3.3.2` on 3100, Alloy `v1.5.1` (logs only, `--server.http.listen-addr=0.0.0.0:12345`),
  Grafana `11.4.0` on 3002, all binds default to `127.0.0.1`.
  `config/prometheus.yml`: 15 s interval, self-scrape only, Phase 2 targets in comments.
  `config/config.alloy`: `loki.write` to Loki; the OTLP receiver is a comment.
- `platform/inventory/production.yml`: service groups `openbao_svc`, `nocodb_svc`,
  `n8n_svc`, `semaphore_svc`, `netbox_svc`, `nemoclaw_svc`, `caddy_svc`, `postiz_svc`,
  `uhhcraft_svc`, `inference_comfyui_svc`, `inference_hunyuan3d_svc`. No `o11y_svc`.
- `platform/infra/cloudflare/dns.tf`: `o11y` is in the managed subdomain list, proxied
  to the Caddy origin.
- `plan/development/05-observability.md`: PROPOSED; Phase 3 (prod) lists Mimir, Tempo,
  Alertmanager, retention metrics 1 yr / traces 3 mo / logs 1 yr as the prod extension.
  `plan/architecture/06-observability-instrumentation.md`: PROPOSED; states the stack
  "is deployed" and logs are zero-touch; wants labels `service`, `component`,
  `cluster`, `env`; metrics via socket discovery with two compose labels.
- `platform/playbooks/apply-firewall.yml`: `firewall_allow_rules` entries of `port`,
  `proto`, `from`, `comment`; `firewall_deny_egress` for semi-trusted hosts.
- dgx-spark companion design: exporters as systemd units on the nodes; pull for
  metrics, push for logs; exporter ports admitted from one source address
  (`telemetry_scrape_src`); vLLM `/metrics` on the API port 8000, LAN-readable; label
  set `service`, `component`, `cluster=dgx-spark`, `env=prod`, `node`, `model_alias`;
  metric names to be read from the pinned nightly, with the documented families
  `vllm:num_requests_running`, `vllm:num_requests_waiting`, `vllm:kv_cache_usage_perc`,
  `vllm:num_preemptions`, `vllm:time_to_first_token_seconds`,
  `vllm:request_time_per_output_token_seconds`, `vllm:e2e_request_latency_seconds`.
- Ecosystem document: one observation server acceptable if its failure is explicit;
  load generation and telemetry ingestion separated so a stress test cannot erase its
  own evidence; health states distinguish reachable host, ready model, successful
  synthetic inference, acceptable interactive service, sufficient capacity; missing
  scrape is a telemetry failure, not zero load.

Not verified: which estate host has capacity for the stack; whether any o11y containers
run anywhere in production today outside the inventory (task 0.2 checks).

2026-09-22 inventory attempt: the authenticated Semaphore front door returned a
Cloudflare 502 host error at `/project/1/templates`. No production host or container
state was observed; task 0.2 remains open.

## Goals / Non-Goals

Goals: one production Grafana with the two nodes and vLLM on graphs within a week of
the companion exporters landing; alerts that page on telemetry going missing before
they page on load; scrape and push paths that the estate firewall and the node firewall
both name explicitly; plan documents that say what is true.

Non-Goals: traces (no producer until the gateway change); Mimir and long retention
(size from measurement first); the Grafana MCP wiring; per-service auto-discovery for
the nodes (they are not containers on the podman socket; static jobs are the honest
shape); benchmark coordination (its own change once the gateway exists).

## Decisions

1. **Static scrape jobs for the nodes, not socket discovery.** The instrumentation
   contract's zero-touch path discovers containers over the local podman socket. The
   nodes are remote hosts running systemd units, so they are static targets by nature.
   One `dgx-spark` job file included from `prometheus.yml`, rendered from inventory
   variables so the local profile carries no node addresses. Alternative rejected:
   running Alloy on the nodes as a remote-write agent so Prometheus sees one target,
   because the companion design chose pull for the lowest node overhead and the
   ecosystem document allows either.

2. **Host choice: a dedicated small VM, not co-location with Caddy or Semaphore.** The
   ecosystem document requires the telemetry store to survive a GPU-host failure and to
   be separated from load generation. Caddy is the front door and Semaphore is the
   control plane; a stress test or a disk-filling Loki on either is a platform outage.
   Sizing per plan 05's local caps scaled up (Prometheus 2 GiB, Loki 2 GiB, Grafana
   1 GiB, Alloy 512 MiB) as starting points, measured in task 1.5. Alternative
   rejected: co-locate with the future gateway host, because the gateway is in the
   request path and the store must not be. Open question below names the VM id.

3. **Retention from a measured day.** Deploy with Prometheus `15d` and Loki `7d`; after
   seven days read ingestion volume and set retention against the disk allocated,
   recording the arithmetic in `env.j2` comments. Alternative rejected: plan 05's
   1 year figures now, because they assume Mimir and object storage this change does not
   add.

4. **Alerting rules in Grafana provisioning, no Alertmanager yet.** Grafana 11's unified
   alerting evaluates Prometheus and Loki queries and routes to a contact point; the
   platform's existing notification surface is Discord (plan 06's synthetic probe
   pattern). Alertmanager is Phase 3 of plan 05 and is not needed for four rule groups.
   Every rule is `for: 5m` or longer and the runbook link points at dgx-spark's
   `docs/VLLM-BRINGUP.md` or the relevant profile README. Rule groups: inference failing
   (`/health` synthetic probe non-200 or `vllm:num_requests_waiting` high with zero
   generation tokens), telemetry missing (`up == 0` for any node job, `absent()` on the
   vLLM families), memory and thermal (node `MemAvailable` under the memory guard's
   threshold, thermal counters when the GPU check provides them), benchmark gate (a
   pushed metric from the companion's manifest writer; deferred until that exists).

5. **A synthetic inference probe, not just `/health`.** The ecosystem document
   separates reachable host, ready model and successful inference. A blackbox-style
   probe that sends one short chat completion every five minutes through the public
   hostname (so it also exercises Cloudflare and Caddy) and records latency as a metric
   is the cheapest way to have all three states on one graph. Implemented as a small
   script on the o11y host under a systemd timer, exporting through node_exporter's
   textfile collector on that host; the API key comes from OpenBao at deploy time.
   Alternative rejected: Prometheus blackbox_exporter, because a POST with a JSON body
   and a Bearer header is outside its HTTP prober's comfortable shape and adds a
   container for one probe.

6. **Dashboards import the live metric list.** The three dashboards are provisioned JSON
   built from the metric names the companion change records from the pinned nightly,
   not from documentation, so a renamed metric shows up as a failing panel review in the
   PR rather than an empty graph in production.

7. **Firewall both ends, named in both repos.** The estate host's `firewall_allow_rules`
   admit the Loki push port from the two node addresses and Grafana's port from the
   Caddy host only; Prometheus and Alloy ports stay loopback. The node side is the
   companion's `telemetry_scrape_src`, which is this host's address. Both are inventory
   values in site-config.

8. **Plan documents corrected, not rewritten.** Plan 05 gains a dated status line
   ("production deploy via change `inference-telemetry-production`, 2026-09"); the
   instrumentation contract gains a dated correction that the stack was not in the
   production inventory on 2026-09-14 and is as of this change's archive. Records are
   append-only.

## Risks / Trade-offs

- [Loki fills the disk during a node boot storm] → Loki retention plus a per-stream
  rate limit; the nodes' Alloy caps its WAL; disk alert at 80 %.
- [Single observation host fails] → accepted per the ecosystem document, with one
  external watcher: a Semaphore-scheduled liveness check (plan 06's synthetic-probe
  pattern) curls Grafana `/api/health` and Prometheus `/-/ready` from the Semaphore host
  and posts to the Discord contact point on failure, since a dead o11y VM cannot alert
  on itself. Until that scheduled check exists (task 3.6), an o11y VM failure has no
  timely alert; this is stated, not hidden. No redundancy until availability
  requirements justify it.
- [Alert thresholds page on a normal load test] → thresholds are set after one baseline
  week and the load-test window is annotated in Grafana by the benchmark manifest.
- [Synthetic probe consumes a concurrency slot] → one short request per five minutes
  at effort `none`; negligible against the measured six-slot ceiling, and it is the
  same shape as the companion reliability change's verify probe.
- [Metric names change on an image re-pin] → decision 6; the companion change re-records
  the list on every pin and the dashboard PR fails review if a name is gone.

## Migration Plan

1. Inventory: confirm no o11y containers run in production today; pick and provision
   the VM through the onboarding checklist (Auxiliary tier, Podman).
2. Deploy o11y through Semaphore with retention defaults; Caddy route; Grafana
   reachable; datasources provisioned.
3. Add the node scrape jobs and the Loki allow rules once the companion's exporters
   are up; confirm targets `up`; confirm log streams arrive.
4. Provision the three dashboards and the four alert groups (benchmark gate deferred);
   synthetic probe timer; contact point to Discord.
5. After seven days: set retention and thresholds; correct plan 05 and the
   instrumentation contract; archive; retain the outcome into bank
   `agent-cloud-750a33b9`.

## Open Questions

- Which Proxmox VM id and address the o11y host gets (site-config `vm-specs.yml`).
  Default if unanswered: a new small VM per the Auxiliary tier, not co-location.
- Discord contact point: reuse the WisBot webhook path or a dedicated alerts channel.
  Default: dedicated channel, webhook in OpenBao.
- Whether Grafana sits behind Authentik `forward_auth` from day one. Default: yes, the
  Caddy template already renders that route shape; the synthetic probe and MCP paths do
  not go through Grafana's UI.
