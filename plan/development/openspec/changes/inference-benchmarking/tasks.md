# Tasks: inference benchmarking for internal teams

Every task detects the state it would produce and converges if it already exists, so it
is safe to re-run after an interruption (store rule). Requests to the shared GPUs happen
only inside the windows named below.

## 0. Branch and decisions
- [ ] 0.1 Feature branch from `dev`: `feat/inference-benchmarking`. Pull requests only
      when Joe asks for them (repo rule)
- [ ] 0.2 Joe answers design open questions 1 (results home), 2 (windows) and 4 (team
      caps), or accepts the defaults; record the answers in `design.md` with the date
- [ ] 0.3 Agree with dgx-spark how the vLLM API rule admits the benchmark VM after the
      narrowing (open question 3); record the dgx-spark change name here
- [ ] 0.4 Validation gate: `openspec validate inference-benchmarking` passes and the three
      answers are recorded, which is the precondition for scenario "Direct path survives
      the vLLM firewall narrowing"

## 1. Runner host and identity
- [ ] 1.1 Declare the benchmark VM in site-config `proxmox/vm-specs.yml` (Infrastructure
      tier, no GPU, sized for client load generation) and its inventory group; reserve
      explicit addresses with `netbox-allocate-ip.yml -e reserve=true`
- [ ] 1.2 Provision and harden through the existing templates: Provision VM, Generate
      Service SSH Key (backed up to site-config), Distribute SSH Keys, Verify Host Access,
      Harden SSH
- [ ] 1.3 Firewall declaration per design decision 5 (`firewall_allow_rules`, SSH inbound
      only; `firewall_deny_egress` except the vLLM API port, the gateway listener, the o11y
      Loki and Prometheus ports, the public hostname and the pinned registries); apply with
      Apply Firewall
- [ ] 1.4 Add `bench` to `agw_clients` and an `agw_client_policies.bench` entry
      (`tokens_per_hour` sized per design decision 8, `allowed_models` the served names);
      redeploy the gateway; hand the key to the runner through the gateway's OpenBao path
      only. Record whether the budget charges cached prefix tokens at the full rate
- [ ] 1.5 dgx-spark side (tracked, not done here): the vLLM API allow rule includes the
      benchmark VM; confirm with one direct `/v1/models` request from the VM
- [ ] 1.6 Validation gate: the firewall probes prove scenario "Runner reaches exactly its
      targets"; a gateway request from the VM appears in the access log as `bench`,
      proving scenario "Benchmark requests are attributed"

## 2. Toolchain and run contract
- [ ] 2.1 Pin all four tools by digest in inventory (inference-perf `v0.7.0`, guidellm
      `v0.7.4`, a vLLM image for `vllm bench serve`, and a Python base image for
      dgx-spark's `vllm/bench_c1c6.py` at a pinned dgx-spark commit). For each tool,
      record in `design.md`: its config schema at that version, how it takes the API key
      (never argv), which streamed delta it counts as first token, and, for guidellm, how
      `sweep` chooses rates. Confirm `vllm bench serve` runs on the GPU-less VM, or record
      the image that does. Confirm against the pinned images the seed behaviour design.md
      records from source (Context, "Seeds and replay"): two inference-perf runs with the
      same `load.base_seed` and `data.shared_prefix.seed` send the same prompts, and two
      `vllm bench serve` runs with the same `--seed` send the same prompts at the same
      intervals. Record whether `vllm bench serve`'s saved result carries an in-flight
      count; if it does not, remove `vllm-bench` from the team survey (design decision 11)
- [ ] 2.2 Workload files for `agw-reference` and `agw-reference-scaled` (design decision
      4) as committed inference-perf configs; equivalent parameters for the other tools
      rendered from the same source values, so one shape has one definition
- [ ] 2.3 Committed caps file (team and operator caps, public rate cap, in-flight cap 24,
      output cap 4,096), read by the playbook with a file lookup so variable precedence
      cannot raise it; lock on the runner; run-id directory creation that refuses an
      existing id
- [ ] 2.4 Run playbook: preflight (caps, window, lock, budget and global-bucket plan
      checks, image digests), render configs with owner-only permissions and every seed
      set explicitly (generated once per campaign, or read from the manifest on a re-run),
      run tool containers, write the manifest (seeds, dataset and prompt file sha256,
      post-redaction config sha256) and `summary.json`, redact configs, release the lock
      in `always:`. A re-run compares each rendered config and file digest with the stored
      manifest and refuses on any difference, naming the file. Team mode refuses
      `guidellm` and `dgx-harness`. Credential-handling tasks alone carry `no_log`
- [ ] 2.5 Abort watcher (design decision 9) polling Prometheus; thresholds as inventory
      values with conservative defaults until task 3.1 sets them
- [ ] 2.6 Tests: pytest for the manifest writer, the summary schema across the four
      tools' native outputs (fixtures), redaction and the budget-plan arithmetic; BATS
      for the playbook's refusals (unpinned image, cap above file, run outside window,
      existing run id, team mode with a direct target, team mode with `guidellm` or
      `dgx-harness`, re-run with a drifted config or file digest); pytest that no rendered
      config leaves a seed field unset
- [ ] 2.7 Prove the whole contract against a local-dev upstream (the fake inference
      upstream or LM Studio) through the local Semaphore
- [ ] 2.8 Validation gate: the BATS refusals prove scenarios "Tool image is pinned",
      "Caps cannot be raised from the survey", "Second concurrent run is refused" and "A
      run id is never reused", "Re-run refuses a drifted input" and "Team template refuses
      a non-ladder tool"; a scan of the local run's bundle and task output proves scenario
      "Bundle contains no credential"

## 3. Direct baseline and calibration (window named by Joe, before the narrowing)
- [ ] 3.1 guidellm sweep against direct vLLM for each shape; record `R_sat` per shape, the
      prefix-cache hit ratio during the first shared-prefix stage, and the abort
      thresholds set from the idle and saturated readings
- [ ] 3.2 inference-perf measured ladder against direct vLLM for the scaled shape, then
      the reference shape
- [ ] 3.3 `vllm bench serve` cross-check at the scaled shape's ladder; dgx-spark harness
      C1 to C8; compare C1, C2 and C4 with 44.3, 70.5 and 100.6
- [ ] 3.4 Repeat 3.2 in two more windows; record run-to-run and tool-to-tool tolerance per
      shape in `design.md` decision 4, replacing the provisional 10 percent
- [ ] 3.5 Validation gate: the summaries prove scenarios "Warm-up is excluded", "Ladder is
      scaled, not copied", "Low-sample stage is flagged" (or record that no stage was
      low-sample) and "Continuity row is present"

## 4. Gateway A/B and limit figures
- [ ] 4.1 A/B template run: direct and gateway alternating, same shape, ladder, tool and
      digests, one window
- [ ] 4.2 Record the first-token and inter-token differences as the conformance margin in
      the gateway change (`inference-gateway-agentgateway` tasks 2.2 and 2.4), citing the
      run ids
- [ ] 4.3 Capacity run: select the highest stage within the declared objectives (TTFT p90
      and error rate, figures agreed with Joe before the run); derive
      `agw_rate_requests_per_minute_total` and the default and per-identity
      `tokens_per_hour` from it, with the arithmetic as comments in site-config inventory;
      hand the figures to the gateway change's task 4.2
- [ ] 4.4 Re-run of one stored manifest with its recorded seeds: every rendered config
      and dataset or prompt file digest matches the stored manifest, and the new manifest
      differs only in the fields the spec scenario allows
- [ ] 4.5 Validation gate: the A/B report proves scenario "Overhead report pairs the two
      targets"; the inventory comments prove scenario "Limits cite their run"; 4.4 proves
      scenario "Result is reproducible from its manifest"; a budget-exceeding plan refused
      in preflight proves scenario "Budget-exceeding plan is refused"

## 5. Public path
- [ ] 5.1 Confirm whether the site's egress address is shared with LAN clients of the
      public hostname; set the public rate cap accordingly (0.3 requests per second
      unless the answer allows more) and record the answer in `design.md` decision 10
- [ ] 5.2 Public-path run at the scaled shape plus dgx-spark `public_probe.py` `long`
- [ ] 5.3 Validation gate: an over-cap request refused and an in-cap run with edge 429s
      in their own field prove scenario "Public run stays under the edge limit"; the long
      stream proves scenario "Long stream passes the edge"

## 6. Results in the observability stack
- [ ] 6.1 Loki push of one line per measured stage (pattern of the conformance
      collector), bounded labels only
- [ ] 6.2 Benchmark dashboard as a provisioned file beside the existing dashboards: run
      list, per-stage figures, run-window annotations, gateway and vLLM series overlay
- [ ] 6.3 Durable copy of each bundle to the results home from task 0.2 on a new branch
      per run, size cap enforced, names only in output
- [ ] 6.4 Validation gate: a completed run on the dashboard proves scenario "Dashboard
      shows a finished run"

## 7. Self-serve template and guard drill
- [ ] 7.1 Templates in `platform/semaphore/templates.yml`: "Run Team Inference
      Benchmark" (survey per design decision 11, no secret fields), "Run Inference
      Benchmark A/B" and "Run Inference Capacity Benchmark"; each with a Dev variant.
      Settle which Semaphore task field supplies the requester
- [ ] 7.2 Team guide in the service README: what each shape means, how to read the
      dashboard, the caps and why they exist
- [ ] 7.3 Abort drill in a window: raise load until the team-latency guard trips (or
      lower its multiple for the drill) and confirm the stop and the bundle outcome
- [ ] 7.4 Validation gate: one team member's run proves scenario "Team run completes and
      is visible"; the refusal proves scenario "Team template cannot target vLLM
      directly"; 7.3 proves scenario "Team latency guard stops the run"

## 8. Records and archive
- [ ] 8.1 Service `context/architecture.md` for the benchmark service; root `AGENTS.md`
      workflow table rows for the three templates; `docs/MISTAKES.md` entries for
      anything this change got wrong along the way
- [ ] 8.2 dgx-spark `docs/TEAM-ENDPOINT.md` capacity section points at the dashboard
      (dgx-spark change, coordinated)
- [ ] 8.3 Validation gate: 1.5 re-checked after the dgx-spark narrowing lands proves
      scenario "Direct path survives the vLLM firewall narrowing"; on archive, retain the
      outcome (worked / dead end / corrected) into bank `agent-cloud-750a33b9`
