# Inference benchmarking for internal teams: gateway overhead, capacity ceiling, self-serve runs

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Scoped 2026-09-27 from the operator's
decisions of the same day, against agentgateway's published benchmarking method
(<https://agentgateway.dev/docs/standalone/latest/documentation/llm/inference/benchmarking/>,
fetched 2026-09-27).

Boundary (unchanged from the companion changes): the two DGX Spark machines and the vLLM
API are dgx-spark's; everything outside them is agent-cloud's. The benchmark runner is
outside, so it is agent-cloud's. Companion changes: agent-cloud
`inference-gateway-agentgateway` (the gateway whose overhead is measured and whose limits
this change derives), agent-cloud `inference-telemetry-production` (the stack the results
land in) and dgx-spark `node-telemetry-and-placement-benchmark` (the node-side placement
benchmark and its immutable result manifest, which this change keeps continuity with).

## Why

The inference endpoint has a capacity figure, but no repeatable way to produce one:

- **The recorded ceiling was found by hand.** dgx-spark's
  `docs/LOAD-TESTING-2026-09-14.md` (lines 19 and 60) records about six concurrent
  requests as the practical ceiling and throughput flat at roughly 0.22 to 0.32 requests
  per second past eight in flight. The 2026-09-15 closed-loop run recorded 44.3, 70.5 and
  100.6 aggregate output tokens per second at one, two and four concurrent streams
  (dgx-spark `results/vllm-bench-qwen3.8-flash-next-nvfp4.txt`,
  `results/MERGED-DEPLOYMENT-2026-09-15.md:78`), and single-stream decode is about 60
  tokens per second (dgx-spark `docs/TEAM-ENDPOINT.md:105`). Those runs came from
  different scripts, vantage points and prompt sets; none is an open-loop arrival test.
- **The gateway's limits are placeholders.** The gateway template ships one global request
  bucket and a per-key hourly token budget with default figures
  (`platform/services/agentgateway/deployment/templates/config.yaml.j2:153,169`), and the
  gateway change's task 4.2 is still "derive both figures from the measured ceiling". Its
  conformance scenario asks for first-token latency "within an agreed margin of the direct
  path" (`inference-gateway-agentgateway/specs/platform/inference-gateway/spec.md`) with
  no method for agreeing one.
- **Teams cannot answer their own capacity questions.** Anyone planning an agent fan-out
  or a batch job today has to ask the operator or saturate the shared endpoint. The
  endpoint is shared host-wide, so an ad-hoc test degrades everyone.
- **The gateway's own latency metrics cannot be the measurement.** At v1.5.0 the
  first-token histogram's highest finite bucket is 10 seconds and the generative request
  duration's is 81.92 seconds (`crates/agentgateway/src/telemetry/metrics.rs`,
  `FIRST_TOKEN_BUCKET` and `REQUEST_DURATION_BUCKET`, read at tag v1.5.0 on 2026-09-27),
  while a reasoning request on this endpoint runs for minutes. Server-side histograms
  corroborate; a client-side tool has to be the source of truth.

agentgateway's method gives the team a shape to copy: a Poisson arrival ladder, warm-up
stages excluded, a fixed shared-prefix workload, and a baseline-versus-gateway comparison
against one identical backend, reported as first-token latency p50 and p90, inter-token
latency p50, peak output tokens per second and achieved requests per second. Its ladder
(3 to 60 requests per second on sixteen H100 GPUs) is two orders of magnitude above this
endpoint and has to be scaled, not copied.

## What Changes

- **A dedicated benchmark VM.** Allocated in site-config `proxmox/vm-specs.yml`, reserved
  in NetBox, provisioned and hardened through the existing onboarding templates. Its
  firewall admits it to the vLLM API and to the gateway listener; its egress is limited
  to those, the o11y host, the public inference hostname and the image registries. It
  runs no long-lived service: each tool runs in a container pinned by digest for the
  duration of one job.
- **Four tools, one run contract.** `inference-perf` (kubernetes-sigs; the harness
  agentgateway's benchmark suite uses, run standalone against an HTTP endpoint) is the
  primary open-loop tool and reuses agentgateway's workload file shape. `vllm bench
  serve` is the cross-check. `guidellm` runs the calibration sweep. dgx-spark's
  closed-loop harness (`vllm/bench_c1c6.py`, prompt set `code-reasoning-v1`) runs
  unchanged at a pinned dgx-spark commit, so every new result can be compared with the
  recorded baselines. All four write into the same immutable run bundle.
- **Three targets.** Direct vLLM over the LAN, the gateway over the LAN, and the public
  path through `inference.uhstray.io`. The public path runs only a low-rate probe profile,
  because Cloudflare's per-source limit (10 requests per 10 seconds on `/v1/*`) and its
  125-second read timeout bound what it can carry (root `AGENTS.md`, "Inference edge").
- **A ladder scaled to this endpoint.** Stages are fractions of a saturation rate
  measured by calibration, from well below the knee to just past it, instead of
  agentgateway's absolute 3 to 60 requests per second (design decision 3).
- **Three uses, three templates.** An operator gateway-overhead A/B, an operator capacity
  run whose output is the figure set for the gateway's global request bucket and per-key
  budgets (the gateway change's task 4.2), and a self-serve team benchmark launched from
  a Semaphore survey with fixed caps.
- **A `bench` gateway identity.** Benchmark traffic through the gateway authenticates as
  its own enrolled identity with its own token-budget override, so a run is attributable
  in every metric and log line and cannot spend a team member's budget.
- **Results are immutable and visible.** Each run writes a manifest (tool versions, image
  digests, model, served profile, workload, ladder, target, git revisions) and the raw
  per-stage output, never a credential. The bundle is pushed on a new branch per run to a
  private repository (design decision 7), and one summary line per stage goes to Loki,
  where a Grafana dashboard reads it alongside the server-side series.
- **Guardrails.** Rate, concurrency and duration caps; declared benchmark windows for
  anything above the self-serve caps, because the GPUs serve the team at the same time;
  one run at a time; abort thresholds on errors, queue depth and the latency other
  identities see.

No **BREAKING** changes. The one cross-repository dependency is ordering: the direct-vLLM
baseline needs the benchmark VM admitted at the vLLM API, which dgx-spark's
`vllm_api_allowed_cidr` (`group_vars/sparks.yml:38`, a single CIDR today) currently does by
being LAN-wide. Narrowing it to the gateway must keep the benchmark VM admitted, or the
direct baseline can never be re-run (design decision 5).

## Capabilities

### New Capabilities
- `platform/inference-benchmarking`: repeatable, attributable, bounded benchmark runs
  against the inference estate, from a dedicated runner, with immutable results.

### Modified Capabilities
- none. The gateway's limit figures and conformance margin are inputs this change
  produces; the requirements that consume them stay in `inference-gateway-agentgateway`.

## Impact

- Files (all new unless noted): a benchmark service directory under
  `platform/services/` holding the workload files, tool invocation templates, the
  manifest writer and its tests; one run playbook with a team wrapper; Semaphore entries
  in `platform/semaphore/templates.yml`; a Grafana dashboard next to the existing ones in
  `platform/services/o11y/deployment/config/grafana/dashboards/`; BATS and pytest
  suites; the gateway's inventory example gains the `bench` identity.
- site-config: `proxmox/vm-specs.yml` entry and inventory group for the benchmark VM,
  its firewall declaration, the benchmark windows, the `bench` entry in the gateway's
  client list and policy overrides, and the results directory if decision 7 holds.
- OpenBao: the gateway mints the `bench` client key like any other identity; the direct
  target shared-reads the vLLM key the gateway already holds. No new secret path.
- dgx-spark: the API allow rule keeps the benchmark VM admitted after the narrowing
  (a dgx-spark change that accepts more than one source); no change to serving.
- Live: one new VM; GPU time in declared windows; one gateway redeploy to enrol `bench`.
- Out of scope, recorded: benchmarking skynet, ComfyUI or Hunyuan3D; multi-replica or
  placement cells (dgx-spark's companion change owns those); quality or accuracy
  evaluation; continuous scheduled benchmarks (a later change once windows are stable).

## Rollback Plan

- Stop: disable the three Semaphore templates by removing them from `templates.yml` and
  re-running `setup-templates.yml`; nothing in the request path changes, because the
  runner is not in it.
- Remove the identity: delete `bench` from the gateway's client list, redeploy the
  gateway, then run `manage-agentgateway-client-key.yml` with `action=revoke`.
- Remove the host: `destroy-vm.yml` for the benchmark VM; its firewall entries at the
  vLLM API revert in dgx-spark's inventory.
- Results already written are append-only history and are kept; removing the dashboard
  is a revert of its provisioned file on the next o11y deploy.
