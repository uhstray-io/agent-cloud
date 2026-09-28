# Design: inference benchmarking for internal teams

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

## Context

Verified 2026-09-27 unless marked otherwise.

**agentgateway's method** (the benchmarking page linked in the proposal, and the
benchmark suite README it cites, `agentgateway/benchmarks` at commit `522fc04`,
`inference/README.md`):

- Harness: "Inference-perf is the only implemented harness"; GuideLLM is named as not yet
  supported by that suite.
- Workload: Qwen3-32B on vLLM, 8 decode replicas at tensor parallel 2 (16 H100 GPUs),
  150 shared-prefix groups of 5 prompts, 6,000-token shared system prompt, 1,200-token
  question, 1,000-token requested output, Poisson arrivals, 300-second request timeout.
- Ladder: 3, 10, 15, 20, 22, 25, 30, 35, 40, 43, 46, 49, 52, 55, 57, 60 requests per
  second. "Warm-up stages whose requested rate repeats in the measured ladder are
  excluded." The report generator refuses to compare treatments with different ladders or
  repetition counts.
- Comparison: a plain Kubernetes Service against agentgateway standalone, one backend.
  Reported: TTFT p50 and p90, ITL p50, peak output tokens per second, achieved requests
  per second.

**The tools** (release listings via `gh release list`, 2026-09-27):

- `inference-perf`: latest `v0.7.0` (2026-09-15); `pip install inference-perf`; image
  `quay.io/inference-perf/inference-perf`; `inference-perf --config_file <file>`. Its
  config schema (`docs/config.md` on main) has `load.type: constant|poisson|concurrent|…`
  with `load.stages[].rate/duration` and a `sweep` block; `api.type: completion|chat`,
  `api.streaming`; `server.type: vllm`, `model_name`, `base_url`, `ignore_eos`, `api_key`;
  `data.type: shared_prefix` with `num_groups`, `num_prompts_per_group`,
  `system_prompt_len`, `question_len`, `output_len`; `metrics.type: prometheus` with a
  Prometheus URL; `report.request_lifecycle.summary/per_stage/per_request`;
  `storage.local_storage.path`. Whether the v0.7.0 release matches main's schema field for
  field is unverified (task 2.1 renders the files against the pinned image).
- `vllm bench serve` (docs.vllm.ai, latest): `--request-rate` (default `inf`),
  `--burstiness` (default 1.0), `--max-concurrency`, `--percentile-metrics` (allowed
  `ttft`, `tpot`, `itl`, `e2el`, …), `--metric-percentiles` (default `99`),
  `--save-result`, `--result-dir`, `--result-filename`, `--dataset-name` (`random`,
  `sharegpt`, `prefix_repetition`, …), `--backend` (`openai-chat` among others),
  `--base-url`, `--endpoint`, `--ignore-eos`, `--num-warmups`, `--goodput`. vLLM latest
  release `v0.30.0` (2026-09-22). At `v0.30.0`, `vllm/benchmarks/serve.py` documents
  burstiness 1 as a Poisson process (lines 419 to 423) and draws each interval from a gamma
  distribution with shape equal to the burstiness, which is exponential at 1 (lines 471 to
  475); one invocation takes one rate, so a ladder is one invocation per stage, and its
  saved result carries `completed` and `failed` counts (lines 1288 to 1289). Unverified:
  whether the saved result carries an in-flight count, how the tool takes an API key, and
  whether `vllm bench serve` runs on a GPU-less host from the published `vllm/vllm-openai`
  image (task 2.1).
- `guidellm`: latest `v0.7.4` (2026-09-16); image `ghcr.io/vllm-project/guidellm`
  (multi-arch). At v0.7.4 the README's command is `guidellm run --backend
  kind=openai_http,target=<url> --profile kind=<sweep|poisson|constant|synchronous|
  throughput|concurrent>`, with `rate=` for constant and Poisson and `warmup=`/`cooldown=`
  inside the profile. The operator's shorthand `--profile sweep/poisson` maps to that
  form; older releases used a different command, so the invocation is pinned with the
  image. How `sweep` chooses its intermediate rates is read from source below ("Seeds
  and replay"); task 2.1 confirms it against the pinned image.
- dgx-spark's harness (dgx-spark repository, read 2026-09-27): `vllm/bench_c1c6.py` is a
  stdlib-only closed-loop streamed benchmark (imports at lines 4 to 15) with prompt set
  `code-reasoning-v1` hashed into the result (`PROMPT_SET`, `prompt_sha256`), arguments
  `--url --rounds --max-tokens --levels --model --thinking --seed --timeout
  --total-timeout` (lines 386 to 397), key read from `OPENAI_API_KEY`. `benchmark.yml`
  runs it on the head node behind an explicit `benchmark_enabled` gate, levels 1 to 8,
  and `vllm/bench_manifest.py` writes `results/<run-id>/manifest.json` and
  `raw-results.json`, refusing an unpinned image digest. `vllm/public_probe.py` has modes
  `long`, `queue`, `efforts` against the public URL.

**Seeds and replay at the pinned versions** (source read with
`gh api .../contents/<path>?ref=<tag>`, 2026-09-27):

- inference-perf `v0.7.0`: `load.base_seed` defaults to the current time in milliseconds
  (`inference_perf/config/loadgen/config.py:225-228`); each worker reseeds with
  `base_seed + worker id` (`inference_perf/loadgen/load_generator.py:491-496`), so the
  worker count is part of the input; the `shared_prefix` data type reads its own
  `data.shared_prefix.seed`, default unset (`inference_perf/config/datagen/config.py:74`,
  `inference_perf/datagen/synthetic/shared_prefix_datagen.py:90`). The Poisson arrival
  timer draws from an unseeded generator (`inference_perf/loadgen/load_timer.py:68-76`,
  `np.random.default_rng()` with no argument), so prompts replay from recorded seeds but
  the realised arrival times do not. The project's own comparison table says the same of
  the prompt seeds (`docs/comparability.md:121`).
- `vllm bench serve` `v0.30.0`: `--seed`, default `0`
  (`vllm/benchmarks/datasets/datasets.py:1608`), seeds both Python's and NumPy's global
  generators before the run (`vllm/benchmarks/serve.py:2025-2026`), and the arrival
  intervals come from NumPy's global generator (`serve.py:475`), so one seed fixes both
  the prompts and the arrival intervals.
- guidellm `v0.7.4`: the Poisson strategy's `random_seed` defaults to `42` and seeds its
  own generator (`src/guidellm/scheduler/strategies.py:582-585`, `662-663`); the run
  arguments carry a `seed` of kind `static`, example value `42`
  (`src/guidellm/benchmark/schemas/entrypoints.py:212-217`). A `sweep` runs synchronous
  and throughput strategies first and spaces its later rates evenly between the two
  measured rates (`src/guidellm/benchmark/profiles/sweep.py:55`, `121`, `128`), so a
  calibration sweep does not replay its rates exactly even with its seeds fixed.
- dgx-spark's harness takes `--seed` (listed above) and hashes its prompt set into the
  result.

**The endpoint** (dgx-spark `profiles/qwen3.8-flash-next-nvfp4/model.yaml`): tensor
parallel 2 across both nodes, `max-model-len` 262144, `max-num-seqs` 8,
`max-num-batched-tokens` 8192, chunked prefill on, a 16 GiB KV cache budget, thinking on
by default through `default-chat-template-kwargs`, `sse-keep-alive-interval` 15. Whether
prefix caching is on is not stated in the profile (unverified). The pinned vLLM exports
`vllm:prefix_cache_queries_total` and `vllm:prefix_cache_hits_total`, with
`vllm:num_requests_waiting`, `vllm:num_requests_running` and `vllm:num_preemptions_total`
(dgx-spark `results/vllm-metric-names-506e66caa3ef.txt`); task 3.1 reads the hit ratio
during the first shared-prefix stage to settle whether the workload measures cache reuse.

**The gateway** (this repository): stats on container port 19002, readiness on 19001
(`platform/services/agentgateway/deployment/templates/config.yaml.j2:34-35`); every
metric carries `identity`; per-identity token budget with override
`agw_client_policies.<name>.tokens_per_hour`, default 2,000,000 (line 153); one global
request bucket `agw_rate_requests_per_minute_total`, default 120 (line 169). Metric names
at v1.5.0 (`schema/metrics.md` at the tag) include
`agentgateway_gen_ai_server_time_to_first_token`,
`agentgateway_gen_ai_server_time_per_output_token`,
`agentgateway_gen_ai_server_request_duration`, `agentgateway_request_duration_seconds`,
`agentgateway_request_processing_seconds` (request received to upstream call sent) and
`agentgateway_response_processing_seconds`. Bucket bounds at the tag
(`crates/agentgateway/src/telemetry/metrics.rs`): first token 1 ms to 10 s, per output
token 1 ms to 2.5 s, generative request duration 10 ms to 81.92 s, HTTP request duration
1 ms to 80 s, processing 50 µs to 250 ms.

**The edge** (root `AGENTS.md`, "Inference edge"): a per-source ceiling of 10 requests per
10 seconds on `/v1/*`, counted per Cloudflare data centre, blocking for 10 seconds; the
125-second Cloudflare read timeout is an accepted decision; non-streamed requests past it
return 524 (dgx-spark `docs/LOAD-TESTING-2026-09-14.md`).

**The platform** (this repository): Prometheus runs with the remote-write receiver
enabled (`platform/services/o11y/deployment/compose.yml:25`); the conformance collector
pushes structured lines to Loki (`platform/playbooks/collect-service-conformance.yml:274`);
Semaphore templates declare `survey_vars` and must never carry secrets
(`platform/semaphore/templates.yml:10-11`); `apply-firewall.yml` takes
`firewall_allow_rules` and `firewall_deny_egress`; `tasks/site-config-clone.yml` and
`tasks/site-config-push.yml` implement a branch-per-run push to the private repository
that reports names only.

## Goals / Non-Goals

Goals: (1) a gateway-overhead A/B that gives the gateway change its "agreed margin";
(2) a measured capacity ceiling from which the gateway's global request bucket and
per-key budgets are derived; (3) a self-serve benchmark any team member can launch within
fixed caps; every result reproducible from its manifest and comparable with the recorded
dgx-spark baselines.

Non-Goals: placement or replica comparisons (dgx-spark's placement benchmark); model
quality evaluation; benchmarking other model servers in the estate; scheduled continuous
benchmarking; tuning vLLM itself (results inform dgx-spark, which owns the profile).

## Decisions

1. **inference-perf is the primary tool; the other three each have one job.** The
   operator chose all four. inference-perf runs every measured ladder, because it is the
   harness agentgateway's suite uses and its `shared_prefix` data type and Poisson stages
   express agentgateway's workload directly, so our results are comparable in method.
   `guidellm` runs the calibration sweep that finds the saturation rate the ladder is
   scaled to (decision 3). `vllm bench serve` repeats one ladder per campaign as a
   cross-check, with `--percentile-metrics ttft,tpot,itl --metric-percentiles 50,90
   --save-result`, run once per rate in a loop because it takes a single rate per
   invocation; a disagreement beyond the tolerance in decision 4 between it and
   inference-perf voids the campaign until explained. dgx-spark's `bench_c1c6.py` runs
   its closed-loop C1 to C8 waves at a pinned dgx-spark commit, because it is the only
   tool whose earlier results exist; each campaign's C1, C2 and C4 aggregate tokens per
   second are compared with 44.3, 70.5 and 100.6 from 2026-09-15.
   Alternative rejected: one tool only. Tools disagree on how they count first token and
   inter-token latency, and a single tool cannot show when that is the cause of a change.
   Alternative rejected: drop the dgx-spark harness, because that throws away the only
   recorded baseline for the serving profile.

2. **Metrics and their definitions are fixed per run.** Every stage reports TTFT p50 and
   p90, ITL p50, TPOT p50, output tokens per second (per stage, and the peak across
   stages) and achieved requests per second, plus completed, failed and in-flight counts.
   The client tool is the source of truth; gateway and vLLM histograms are recorded
   beside it as corroboration only, because their bucket bounds (Context) saturate on
   this endpoint's multi-minute requests. First token means the first streamed delta
   carrying either reasoning or content: thinking is on by default, so a tool that waits
   for content would report reasoning time as first-token latency. Each tool's definition
   is recorded in the manifest (task 2.1 verifies which delta each tool counts).
   Synthetic workloads send `ignore_eos` with a fixed output length so output tokens are
   deterministic whatever the model reasons.

3. **The ladder is scaled to a measured saturation rate.** agentgateway's ladder runs
   from about half of its baseline's achieved rate (6.70 requests per second) to about
   nine times it, on hardware two orders of magnitude larger. Copying the absolute rates
   would put every stage far past this endpoint's knee, which is about 0.22 to 0.32
   requests per second for the code-reasoning prompts (dgx-spark load test). So the
   campaign first runs a guidellm sweep for the workload shape to measure a saturation
   rate `R_sat`, bounded above by 1 request per second. The measured ladder is then
   `0.25` (warm-up, repeated and excluded), `0.25, 0.5, 0.7, 0.85, 1.0, 1.15, 1.3` times
   `R_sat`, finer near the knee where the gateway limits are set, and stopping at 1.3
   because past saturation an open-loop stage only grows a queue (the load test found
   depth raises latency, not errors). For the code-reasoning shape and an `R_sat` of 0.3
   this is 0.075 to 0.39 requests per second, sub-1 throughout. Stages last 600 seconds
   by default; a stage with fewer than 50 completed requests reports its percentiles
   flagged as low-sample rather than hiding them.
   Alternative rejected: agentgateway's absolute ladder, for the reason above.
   Alternative rejected: a fixed sub-1 ladder with no calibration, because `R_sat` moves
   with the workload shape and the serving profile, and a stale ladder silently measures
   only overload or only idle.

4. **Two workload shapes, both committed files.** `agw-reference` is agentgateway's
   shape exactly (150 groups of 5, 6,000 system, 1,200 question, 1,000 output), kept for
   method comparability and run at its own calibrated ladder. `agw-reference-scaled`
   keeps the group structure and divides every length by four (1,500 system, 300
   question, 250 output): the prefix-to-question-to-output ratio is preserved and a
   stage collects about four times the samples in the same time, which matters when
   `R_sat` for the full shape is a small fraction of a request per second (unverified
   estimate: a 1,000-token output at about 60 tokens per second single-stream is over 16
   seconds per request before queueing). The scaled shape is the default for the A/B and
   the capacity run. Tolerance between tools and between repeated runs is recorded per
   shape after the first three campaigns (task 3.4); until then a difference under 10
   percent in a p50 is not reported as a change.

5. **A dedicated VM that stays admitted at the vLLM API.** The runner is an
   Infrastructure-tier VM in site-config `proxmox/vm-specs.yml`, sized for client load
   generation, with no GPU. It is the only host that runs benchmarks, so its address is
   the only benchmark source in every log and allow rule. Firewall: inbound SSH only;
   egress allowed to the vLLM API port, the gateway listener, the o11y host's Loki and
   Prometheus ports, the public inference hostname and the pinned registries, and denied
   otherwise (`firewall_deny_egress`). The A/B needs the direct path permanently, because
   the gateway's overhead must be re-measured on every gateway upgrade and every serving
   profile change, not once. So when dgx-spark narrows `vllm_api_allowed_cidr` to the
   gateway, the benchmark VM stays admitted; the allow rule today takes one source
   (dgx-spark `vllm.yml:262`), so that narrowing needs a list, which is a dgx-spark change.
   Ordering, whichever way dgx-spark implements it: the first direct baseline runs before
   the narrowing lands, and the narrowing's verification includes a direct request from
   the benchmark VM.
   Alternative rejected: run the tools from the gateway VM. Load generation on the
   gateway host distorts the gateway measurement it is supposed to take.
   Alternative rejected: run once before narrowing and drop the direct path, because a
   one-shot baseline goes stale on the first upgrade.

6. **Tools run in containers pinned by digest, invoked by one playbook.** No tool is
   installed on the VM. The playbook renders the workload and tool configuration from
   committed templates, pulls each image by the digest recorded in inventory, runs it
   with the results directory mounted, and writes the manifest. Credentials reach a tool
   through an environment file or a rendered config file with owner-only permissions,
   never an argument vector, and the manifest records the config with the key field
   replaced by its name. dgx-spark's harness runs from a copy fetched at a pinned commit
   SHA with its script digest recorded. Alternative rejected: pip installs on the host,
   because the tool version would then drift with the host rather than the manifest.

7. **Results: an immutable bundle, a durable private copy, a summary in Loki.** Each run
   writes `<run-id>/` on the VM (owner-only, never overwritten): `manifest.json`, each
   tool's native output, the rendered (redacted) configs, and `summary.json` in one
   schema across tools. The manifest carries: run id, requester, template, target,
   workload shape and its file digest, ladder and stage durations, tool names and
   versions, image digests, the dgx-spark harness commit, this repository's commit, the
   served model and profile name as reported by `/v1/models` and the gateway version when
   the target is the gateway, the window it ran in, abort outcome, and per-stage sample
   counts. For replay it also carries every seed the tools use (for inference-perf
   `load.base_seed` and `data.shared_prefix.seed`; for `vllm bench serve` `--seed`; for
   guidellm its `seed` and the Poisson `random_seed`; for dgx-spark's harness `--seed`),
   the sha256 of every dataset or prompt file, and the sha256 of each rendered
   configuration after redaction. The playbook generates each seed once per campaign and
   writes it into the configuration, because inference-perf's default seed is the clock
   and its shared-prefix seed is unset (Context, "Seeds and replay"). A re-run from a
   manifest renders from the recorded seeds and refuses when any configuration or file
   digest differs from the stored one. What a re-run may still change: run id, time,
   window, results, and inference-perf's realised arrival times, because its Poisson timer
   is unseeded at `v0.7.0`; the stage rates and durations that shape those arrivals are in
   the configuration digest. Alternative rejected: storing and replaying the full
   generated request list and arrival schedule, because the seeds reproduce the prompts
   for every pinned tool and the arrival schedule for all but one, at a fraction of the
   bundle size; if tolerance work (task 3.4) shows inference-perf's arrival variance
   matters, its `trace_replay` load type, which replays a request timing trace
   (`inference_perf/config/loadgen/config.py:28`, `179-180`), is the follow-up. The bundle
   is pushed on a new branch per run to the private site-config
   repository under `benchmarks/<run-id>/`, through the existing clone and push tasks,
   with a size cap; per-request raw data beyond the cap stays on the VM for a declared
   retention and the manifest says so. One summary line per stage goes to Loki with
   bounded labels (`service`, `env`, `target`, `tool`, `shape`) and the run id in the line
   body; the dashboard reads those lines and overlays gateway and vLLM series for the same
   window. Alternative rejected: this public repository, because bundles carry internal
   hostnames and are data, not code. Alternative rejected: dgx-spark's `results/` as the
   default, because it is the node owner's record and would need a second write key in
   OpenBao; dgx-spark's own placement bundles stay there and our manifests reference
   them by run id. Alternative rejected: Prometheus remote write for the summary, because
   a playbook would need a protobuf and snappy client for a dozen points per run, and a
   run id as a label is unbounded cardinality. Alternative rejected for now: an object
   store, because no bucket or credential exists for it and git carries the current size
   (open question 1).

8. **Benchmark traffic through the gateway is the `bench` identity.** Added to
   `agw_clients` in site-config, minted and enrolled by the gateway deploy like any
   other client, with an `agw_client_policies.bench.tokens_per_hour` override. The
   override is sized from the planned ladder: the playbook computes the planned token
   volume per UTC hour (rates times stage durations times input plus output tokens) and
   refuses a gateway-target run whose plan exceeds the `bench` budget, because a run that
   trips the budget measures the budget, not the model. Whether the budget charges input
   tokens at the full rate for cached prefixes is unverified (task 1.4). The global
   request bucket is shared with the team; a ladder whose top stage exceeds half of it is
   refused for the same reason. Direct-target runs use the vLLM key by shared read from
   the gateway's secret path (single custody, never copied into a new path).

   Once the gateway's listeners require client certificates (gateway change task 6.1),
   a gateway-target run from the benchmark VM also presents the `bench` client leaf
   declared in `production-internal-ca` decision 4: its key is generated on the
   benchmark VM, it is issued through the same CSR flow as every other leaf, and it is
   on the gateway's client allowlist (that change's decision 5). The run goes straight
   to the gateway listener, not through Caddy, so the A/B against direct vLLM still
   measures the gateway's own overhead with one hop on each side. The playbook refuses a
   gateway-target run when the leaf file is missing or expires within the run's planned
   duration, and the manifest records the leaf's serial (never its key). inference-perf
   `v0.7.0` takes a client certificate and key in its model-server config (`cert_path`,
   `key_path`, `inference_perf/config/client/modelserver/config.py:39-40`). `vllm bench
   serve` `v0.30.0` does not: its TLS setting is only on or off
   (`vllm/benchmarks/serve.py:2086-2089`, with `--insecure` at lines 1985-1991), so it
   runs against the direct vLLM target only, which is where decision 1 uses it, and it
   leaves the team survey once the gateway requires client certificates (decision 11).
   unverified: how inference-perf `v0.7.0` is told which CA verifies the gateway's
   server certificate; task 2.1 records it.

9. **Guardrails are code and are checked before and during a run.** Before: the rates,
   in-flight cap (24, the load test's practical offered-depth limit), output length
   (at most 4,096), input length and total duration are asserted against caps read with a
   file lookup from a committed caps file, so a survey value or extra variable cannot
   raise them; operator runs above the team caps must fall inside a declared window in
   site-config inventory; a lock on the VM allows one run at a time. During: a watcher
   polls Prometheus every 15 seconds and stops the run when any of these holds for two
   polls: non-429 error rate above 5 percent, `vllm:num_requests_waiting` above 16, any
   increase in `vllm:num_preemptions_total`, a node memory-pressure alert firing, or the gateway's first-token p90 for
   identities other than `bench` more than double its value in the 15 minutes before the
   run (a relative guard, because the absolute value depends on what the team is
   doing). An abort writes the bundle with `outcome: aborted` and the tripped condition.
   Exact thresholds are inventory values, set from the first calibration run (task 3.1).
   Alternative rejected: rely on the gateway's global request bucket as the guard,
   because the direct target bypasses it and it cannot see latency.

10. **The public path is a probe, not a ladder.** Every request from the benchmark VM to
    the public hostname leaves through the site's shared egress address, which is also
    the source address of any team member on the LAN using the public hostname, and the
    edge limit is per source. So the public-path profile caps at 0.3 requests per second
    (below the 10-per-10-seconds ceiling with Poisson bursts), uses the scaled shape and
    streaming only, excludes 429 responses that carry Cloudflare's mitigation from the
    error rate and reports them separately, and runs dgx-spark's `public_probe.py`
    `long` mode for the 125-second read timeout. Its purpose is the edge's added latency
    at low load, compared with the gateway target at the same rate. Unverified: whether
    the site's egress address is in fact shared with LAN clients of the public hostname
    (task 5.1 checks before the first run).

11. **Self-serve is one Semaphore template with a narrow survey.** "Run Team Inference
    Benchmark" targets the gateway only, as `bench`, within the team caps, inside a
    standing low-rate window declared in inventory. Survey (no secret fields): shape
    (preset list), tool (`inference-perf` default, `vllm-bench`), rates (comma list or the
    preset `calibrated`), stage seconds, output tokens, input tokens (shared-prefix
    lengths), served model name, and a free-text label. The tool list holds only tools
    that run the measured ladder the spec defines: inference-perf does (decision 1), and
    `vllm bench serve` draws Poisson arrivals at burstiness 1 and yields one result per
    stage invocation (Context), so it stays on the list only if task 2.1 confirms its
    saved result supplies every per-stage metric, including the in-flight count, and
    only until the gateway requires client certificates, because it cannot present one
    (decision 8); otherwise the list is `inference-perf` alone. guidellm and dgx-spark's
    harness stay operator-only: guidellm is the calibration sweep and dgx-harness runs
    closed-loop waves for baseline continuity (decision 1), and neither run is the staged
    measured ladder, so offering them would produce team results the summary schema cannot
    report as one. The playbook refuses either tool in team mode, whatever the survey
    sends.
    Alternative rejected: separate team run types and result schemas for calibration and
    continuity runs, because teams need latency and throughput figures for their own
    shape, which the measured ladder already gives. The requester is taken from
    Semaphore's task record (unverified: which field the playbook can read at run time;
    task 7.1 settles it). Operator templates,
    "Run Inference Benchmark A/B" and "Run Inference Capacity Benchmark", add the direct
    and public targets and the higher caps, and require the window. All three call one
    playbook; the team template's wrapper refuses a `bench_mode` other than team.

## Risks / Trade-offs

- [A benchmark degrades the team's service] → windows, caps, the relative latency guard
  in decision 9, and one run at a time; the self-serve caps are low enough that the
  worst case is a run's own queueing.
- [Tools disagree] → the cross-check in decision 1 is there to catch it; the definitions
  in the manifest explain it; a campaign with an unexplained disagreement is not used for
  limit figures.
- [Thinking inflates first-token latency] → first token counts reasoning deltas
  (decision 2); workloads fix output length with `ignore_eos`.
- [The runner becomes a bottleneck] → sub-1 request rates are far below a single client
  process's capacity; the manifest records runner CPU and memory peaks, and a stage
  where the runner saturated is marked invalid.
- [The bench identity becomes a back door] → it has its own budget, is attributed on
  every line, and its key is held only on the VM via the gateway's normal OpenBao path.
- [Results leak internal detail] → the durable copy is private, the Loki line carries
  no addresses, and configs are redacted before they are written.

## Migration Plan

1. Declare and provision the VM; enrol `bench` at the gateway; firewall both ends.
2. Pin the four tools; render the workloads; prove each against a local-dev upstream.
3. First direct-vLLM calibration and ladder in a window the operator names, before the
   dgx-spark narrowing lands; compare the dgx-spark harness with the 2026-09-15 figures.
4. Gateway A/B at the same ladder; record the margin in the gateway change; derive the
   limit figures for its task 4.2.
5. Public-path probe.
6. Summary to Loki and the dashboard; then the self-serve template.
7. Archive; retain the outcome into bank `agent-cloud-750a33b9`.

## Open Questions

1. Is site-config the right durable home for bundles, or should they go to dgx-spark's
   `results/` beside the placement bundles, or an object store? Default if unanswered:
   site-config `benchmarks/`, with a per-bundle size cap.
2. The benchmark windows: which hours are standing low-rate (self-serve) and how an
   operator declares a capacity window. Default: self-serve any time within team caps;
   capacity and A/B runs only in a window Joe names, recorded in inventory with its date.
3. Does dgx-spark accept a list of API sources (gateway plus benchmark VM), or a second
   rule for the benchmark VM? Either satisfies decision 5; the choice is dgx-spark's.
4. Team caps: proposed 0.2 requests per second, 30 minutes, output 1,024 tokens, input
   8,192 tokens, in-flight 8. To be confirmed after the first capacity run.
