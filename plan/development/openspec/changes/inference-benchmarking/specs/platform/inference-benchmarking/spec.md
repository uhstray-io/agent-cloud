# platform/inference-benchmarking

Repeatable, attributable, bounded benchmark runs against the inference estate, from a
dedicated runner, with immutable results.

## ADDED Requirements

### Requirement: Benchmarks run only from a dedicated runner host
Every benchmark SHALL run from one dedicated benchmark VM, declared in site-config and
provisioned and hardened through the platform's Semaphore templates, whose firewall
admits it to the vLLM API and the gateway listener and denies egress other than to those,
the o11y host, the public inference hostname and the pinned image registries. Every
benchmark tool MUST run in a container pulled by a digest recorded in inventory, and no
credential MUST appear in a container's argument vector.

#### Scenario: Runner reaches exactly its targets
- WHEN the firewall declaration is applied to the benchmark VM
- THEN a request from it to the vLLM API, the gateway listener and the public hostname
  succeeds, and a connection to any other LAN host is refused

#### Scenario: Tool image is pinned
- WHEN a run starts with an image reference in inventory that is not a sha256 digest
- THEN the playbook refuses before sending any request

#### Scenario: Direct path survives the vLLM firewall narrowing
- WHEN dgx-spark narrows the vLLM API's allowed sources to the gateway
- THEN a direct request from the benchmark VM still succeeds, and a direct request from
  any other LAN host is refused

### Requirement: Runs follow one method and one metric set
A measured run SHALL use Poisson arrivals in stages whose rates are fractions of a
saturation rate measured by a calibration sweep for the same workload shape, with a
short warm-up stage of its own at a repeated rate that MUST be excluded from results. A
stored saturation rate SHALL be reused while the workload file, the served model and
profile and the calibration tool's image digest are unchanged, and the sweep MUST run
again when any of them differs. Each measured stage SHALL last long enough to send the
declared sample target at its rate. Each measured stage
MUST report TTFT p50 and p90, ITL p50, TPOT p50, output tokens per second, achieved
requests per second, and completed, failed and in-flight counts, taken from the client
tool; gateway and vLLM histograms SHALL be recorded as corroboration and never as the
reported figure. First-token latency MUST count the first streamed delta carrying either
reasoning or content.

#### Scenario: Warm-up is excluded
- WHEN a ladder runs with its warm-up stage at a rate repeated in the measured stages
- THEN the summary lists only the measured stages and the manifest records the warm-up
  as excluded

#### Scenario: Ladder is scaled, not copied
- WHEN a capacity campaign starts for a workload shape
- THEN a calibration sweep records a saturation rate first, and the measured ladder's
  rates are the declared fractions of it, none above the absolute cap

#### Scenario: Calibration is reused while its inputs hold
- WHEN a capacity campaign starts for a shape whose stored saturation rate was measured
  with the same workload file digest, served model and profile, and calibration image
  digest
- THEN no calibration sweep runs and the manifest names the calibration run it reused

#### Scenario: Stage length follows the sample target
- WHEN a ladder is planned
- THEN each measured stage's duration is the sample target divided by the stage's rate,
  and the warm-up stage has its own fixed duration

#### Scenario: Low-sample stage is flagged
- WHEN a measured stage completes fewer than 50 requests
- THEN its percentiles are reported and marked low-sample

### Requirement: Every run leaves an immutable, secret-free result bundle
Each run SHALL write a new directory named by its run id that is never overwritten,
holding a manifest, each tool's native output, the rendered configuration with credential
fields redacted to their names, and a summary in one schema across tools. The manifest
MUST record the requester, template, target, workload shape and file digest, ladder and
stage durations, tool names and versions, image digests, the dgx-spark harness commit when
used, this repository's commit, the served model and profile, the gateway version when the
gateway is the target, the window, the outcome and per-stage sample counts. So that two
runs' inputs can be compared, the manifest MUST also record every seed each tool uses
for prompt generation and for its arrival process, the sha256 of every dataset or prompt
file a tool reads, and the sha256 of each rendered tool configuration after redaction.
The playbook MUST set every such seed explicitly in the rendered configuration and MUST
NOT leave one to a tool default, because a default can be the current time. The manifest
and summary SHALL be copied on a new branch per run to the private results location,
reporting names only; the other bundle files SHALL stay on the runner, named in the
manifest with their digests.

#### Scenario: Bundle contains no credential
- WHEN a run through the gateway completes
- THEN no file in its bundle and no line of its task output contains the `bench` key or
  the vLLM key

#### Scenario: A run id is never reused
- WHEN a run is started with a run id whose directory already exists
- THEN the playbook refuses and the existing bundle is unchanged

#### Scenario: Every seed is explicit and recorded
- WHEN a run renders its tool configurations
- THEN every seed field each tool reads is set in the rendered configuration, and the
  manifest records each seed with the configuration digests

### Requirement: Gateway overhead is measured as an A/B against one backend
The gateway-overhead benchmark SHALL run the same workload shape, ladder, tool and image
digests against direct vLLM and through the gateway within one window and one serving
profile, in alternating order, and MUST report the per-stage difference in each metric.
The resulting first-token and inter-token differences SHALL be recorded as the gateway
change's conformance margin.

#### Scenario: Overhead report pairs the two targets
- WHEN the A/B template completes
- THEN the report shows, per stage, both targets' figures and their difference, and it
  refuses to pair runs whose ladder, shape, tool digest or served profile differ

### Requirement: The gateway's limits derive from a measured ceiling
The capacity benchmark SHALL identify the highest measured stage at which TTFT p90 and
the error rate stay within declared service objectives, and the gateway's global request
bucket and per-key token budgets MUST be set from that figure with the derivation written
beside the values in inventory, citing the run id.

#### Scenario: Limits cite their run
- WHEN the gateway's limit figures are changed in inventory
- THEN each figure's comment names the capacity run id and the arithmetic from its
  stage results

### Requirement: The recorded baselines stay comparable
Each capacity campaign SHALL also run dgx-spark's closed-loop harness at a pinned
dgx-spark commit with its prompt set digest recorded, and MUST report its C1, C2 and C4
aggregate output tokens per second against the recorded 2026-09-15 figures of 44.3, 70.5
and 100.6.

#### Scenario: Continuity row is present
- WHEN a capacity campaign's summary is produced
- THEN it contains the closed-loop harness row with the prompt set digest and the
  difference from the 2026-09-15 baseline

### Requirement: Benchmark traffic is attributable and bounded
Benchmark requests through the gateway SHALL authenticate as the dedicated `bench`
identity with its own token-budget override, and the playbook MUST refuse a run whose
planned token volume per UTC hour exceeds that budget or whose top stage exceeds half the
gateway's global request bucket. Rates, in-flight requests, output and input lengths and
duration MUST be checked against caps read from a committed file that no survey value or
extra variable can raise; runs above the team caps MUST fall inside a declared window;
and only one run MUST execute at a time.

#### Scenario: Caps cannot be raised from the survey
- WHEN a team run is launched with a rate above the team cap
- THEN the playbook refuses before any request is sent

#### Scenario: Budget-exceeding plan is refused
- WHEN a gateway-target run's planned tokens per hour exceed the `bench` budget
- THEN the playbook refuses and reports the planned and allowed figures

#### Scenario: Second concurrent run is refused
- WHEN a run starts while another holds the runner's lock
- THEN it exits without sending requests and names the running run id

#### Scenario: Benchmark requests are attributed
- WHEN a run through the gateway completes
- THEN every gateway metric series and access-log line for its requests carries the
  `bench` identity

### Requirement: A run aborts before it harms the shared service
During a run a watcher SHALL poll Prometheus and MUST stop the run, and record the
tripped condition, when for two consecutive polls the non-429 error rate exceeds its
threshold, vLLM waiting requests exceed their threshold, vLLM preemptions increase, a
node memory alert fires, or the first-token p90 seen by identities other than `bench`
exceeds its multiple of the pre-run value.

#### Scenario: Team latency guard stops the run
- WHEN other identities' first-token p90 exceeds the declared multiple of its pre-run
  value for two polls
- THEN the tools are stopped, the bundle is written with outcome `aborted` and the
  condition, and no further stage starts

### Requirement: The public path is probed within the edge's limits
Runs against the public inference hostname SHALL use streaming only, a rate cap below
the edge's per-source limit, and the scaled workload shape, MUST report 429 responses
carrying Cloudflare's mitigation separately from the error rate, and SHALL include a
stream longer than the edge's 125-second read timeout.

#### Scenario: Public run stays under the edge limit
- WHEN a public-path run is requested above the public rate cap
- THEN the playbook refuses; at or below it, the run completes with any edge 429s
  counted in their own field

#### Scenario: Long stream passes the edge
- WHEN the public-path run's long stream exceeds 125 seconds
- THEN it completes without a 524

### Requirement: Teams can run a bounded benchmark themselves
A Semaphore template SHALL let any team member run a benchmark against the gateway as
`bench` within the team caps, with the primary ladder tool only, choosing from a survey
with no secret fields: workload shape, rates, output and input tokens, served model and a
label. The calibration sweep tool, the closed-loop continuity harness and the direct-target
cross-check tool MUST NOT be offered, and the template MUST NOT offer the direct or public
targets.

#### Scenario: Team template refuses any tool but the ladder tool
- WHEN the team template is launched with any tool other than the primary ladder tool
- THEN the playbook refuses before any request is sent and names the tool it accepts

#### Scenario: Team run completes and is visible
- WHEN a team member launches the template with in-cap values
- THEN the run's bundle is stored, its summary appears on the benchmark dashboard under
  their label, and the requester is recorded in the manifest

#### Scenario: Team template cannot target vLLM directly
- WHEN the team template is launched with a direct or public target
- THEN the playbook refuses

### Requirement: Results are visible in the platform's observability stack
Each measured stage SHALL be pushed to Loki as one structured line with bounded labels
(service, environment, target, tool, shape) and the run id in the line, and a Grafana
dashboard provisioned as code MUST show per-run stage results beside the gateway and vLLM
server-side series for the same window.

#### Scenario: Dashboard shows a finished run
- WHEN a run completes
- THEN the benchmark dashboard lists it by run id with its stage figures and overlays the
  gateway and vLLM series for its window
