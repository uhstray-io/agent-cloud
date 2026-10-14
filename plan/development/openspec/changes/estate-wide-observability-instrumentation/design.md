## Context

### Cross-signal identity and profile adoption baseline

The initial receiver contract uses the bounded `service` identity for metrics
and Loki, `service.name` for OTLP resources, and `service_name` for profiles.
`cluster` and `environment` come from controlled inventory values. Alert rules
add bounded `owner` and `severity` values. Request/user identifiers, trace IDs,
timestamps, raw paths, addresses, prompts, and secrets are not indexed metric
or Loki stream labels. Container and instance details remain for drill-down.

Service Overview lists metric-enabled services from Prometheus and queries Loki
by `service`; log-only coverage remains visible in the estate inventory and
Logs Drilldown. Missing telemetry remains a gap rather than a healthy default.
Profiling begins with one opt-in, measured Alloy self-profile target after its
resource and privacy gates pass. Add no other producer until measured ingestion,
retention, host headroom, and a successful profile receipt support the next wave.

See [proposal.md](proposal.md) for motivation and [the capability spec](specs/platform/estate-instrumentation/spec.md) for the behavior contract. The production receiver uses Prometheus for metrics, Loki for logs, and a private Alloy OTLP/gRPC path into single-node Tempo with local persistent storage (`platform/services/o11y/deployment/config/`, `compose.yml`). Grafana self-monitoring and agentgateway dashboards are provisioned. On 2026-09-28, a Dev-bound production deploy of reviewed commit `970dfa3ab545fa64f3c3829f99cf8ab29b7d32f2` passed live checks for all five stack components, the data sources, both dashboards, provisioned cross-signal link settings, alert rules, and the Discord contact point. Separate successful Semaphore receipts proved a named metrics target, a recent trace, alert delivery, retention, and cardinality; their IDs remain in private inventory. A user-visible trace-pivot click-through and some owning OpenSpec task checkboxes remain open. Production Alloy has no engine-socket mount, so its local container log/metrics discovery is not a production coverage receipt. These facts establish a receiver baseline, not estate-wide coverage; the broader rollout remains tracked by `observability-estate` and `inference-telemetry-production`. The architecture's old socket-scraped Prometheus and future Mimir sketches are not current collection mechanisms (`plan/architecture/06-observability-instrumentation.md`, as-built amendments).

The candidate set is generated from the current `platform/services/` and `agents/` trees at census time, including empty scaffolds. A directory is not proof of deployment: the first implementation phase must reconcile every candidate with private inventory, Semaphore templates, and live read-only discovery. The inventory also includes Proxmox, guest OS and container hosts, pfSense/network, DGX Spark, and external endpoints that Agent Cloud actually manages; scope is based on a declared managed target, not an indiscriminate network scan.

## Goals / Non-Goals

**Goals:** Reuse the receiver and onboarding verifier; give each deployed target an explicit signal contract; favor automatic runtime instrumentation; make cost, capacity, privacy, and recovery visible before increasing telemetry.

**Non-Goals:** Require traces from non-request infrastructure, claim traces from unsupported runtimes, switch to Mimir/object storage or high availability in this change, expose OTLP publicly, or clean-redeploy o11y and delete its current data. Future Kubernetes injection is a separate migration concern; the present estate uses Compose/Podman and VMs.

## Architecture

```mermaid
flowchart LR
  app[Applications and agents] -->|stdout, native metrics, supported OTel agent| edge[Per-host collector or declared scrape]
  infra[VMs, hypervisor, network, control plane] -->|journald/exporter/API metric| edge
  edge -->|private native metrics scrape| prom[Prometheus]
  edge -->|private sampled OTLP logs and traces| alloy[Receiver Alloy]
  alloy -->|internal logs| loki[Loki]
  alloy --> tempo[Tempo]
  loki --> grafana[Grafana dashboards and alerts]
  prom --> grafana
  tempo --> grafana
  budget[Capacity and coverage gate] -->|permits each wave| edge
  budget -->|reviewed site-config spec if needed| resize[Semaphore VM resize]
```

## Decisions

### 1. Inventory the actual estate before choosing exporters

Create one coverage declaration keyed by stable `service.name`/target identity, with lifecycle (`deployed`, `planned`, `retired`, `excluded`), owner, runtime/language, environment, host, signal applicability, method, expected endpoint, budget, and verification receipt. Generate an expected-target report from public service/agent definitions and private inventory; compare it with live read-only discovery, then resolve each discrepancy by a reviewed declaration. The public repo holds the schema and generic methods; `site-config` owns real hosts and network details. Extend the existing `Verify o11y Service` exact-target pattern instead of adding a separate verifier per service.

The audit enumerates every current service and agent directory at run time, including scaffolds, then adds managed infrastructure represented by deployment/inventory (Proxmox, guest OS/container runtimes, pfSense, DGX, and any supported edge dependencies). Every candidate gets an explicit lifecycle classification. This is a **candidate audit**, not a claim that all entries are running.

Alternative rejected: mark every repository directory covered. Several are scaffolds or retired; that would create false-green coverage.

### 2. Use the simplest supported signal source per target

Container stdout/stderr and journald/file logs go through a least-privilege, independently deployable host collector where central socket discovery cannot reach the host; the production receiver is the first such pilot because its engine socket is not mounted. Host collectors send OTLP logs and traces to receiver Alloy, which forwards them internally to Loki and Tempo. Native application/exporter `/metrics` is preferred over a second instrumentation library; two existing required opt-in labels (`scrape`, `port`) and optional `path`/`high_churn` labels serve co-located local containers, while remote targets use private inventory-rendered Prometheus scrape declarations. Do not expose Loki push or Prometheus remote-write as new external ingress. A process that serves HTTP/RPC requests gets a compatibility pilot for the current [OpenTelemetry zero-code mechanism](https://opentelemetry.io/docs/concepts/instrumentation/zero-code/) of its runtime, using deployment parameters rather than a source fork. Zero-code covers supported libraries, not the application's own domain operations; add a small explicit span only for a critical unobserved boundary. Default to no body/prompt/token capture, stable resource attributes, context propagation, bounded attribute values, and sampled traces. Do not inject unreviewed agents into privileged control-plane processes or require eBPF privileges across the fleet.

**Receiver capability survey prerequisite (source only, 2026-10-11):** The generic shared checkout guard compares the pinned commit tree, index entries/flags, and raw tracked worktree bytes/types/modes under bounded non-writing Git reads (10 seconds per Git command, 30 seconds total, 1 MiB metadata, 8,192 entries, 4 MiB per blob, 64 MiB aggregate); split-index and unsupported index formats refuse without repair. It does not impose a remote-ref policy on its five callers. The receiver survey retains its local `origin/dev`-to-reviewed-SHA gate and receives only helper bytes captured from the checked commit; check mode skips receiver probes. This is conditional checkout consistency, assuming a trusted launcher and verifier, not authenticated pre-execution launch or proof that the loaded Ansible config/plugins and playbook were approved. The survey reports bounded Linux, systemd, cgroup, and declared receiver-user categories; it does not inspect the Podman store or service configuration, and its checkout/category report proves neither journal delivery nor restart, delegation, cancellation, or containment. Keep the separate 1.2a admission/lifecycle work incomplete and task 1.2 open until their existing telemetry and baseline acceptance gates pass.

**Coverage receipt checkout prerequisite (source only, 2026-10-11):** Reuse `require-reviewed-checkout.yml` before Census can read OpenBao credentials or execute its Semaphore receipt helper. The strict exact-target verifier imports the same guard only in strict mode, before inventory-digest calculation and receiver helper execution; legacy verification keeps its prior path. The guard establishes conditional consistency of the pinned commit, index, and tracked checkout bytes, types, and modes under the existing trusted-launcher boundary. This does not authenticate the Semaphore launch or close tasks 2.2/2.3: private inventory reconciliation and installed acceptance remain required.

**Receiver-host journal pilot decision (2026-10-09):** Dev-bound Semaphore survey 3662 at reviewed `dev` revision `7c863948` observed rootless Podman's `journald` default, seven running o11y containers using that driver, and 216 exact-name entries in a bounded user-journal metadata read. Receiver Alloy has no read-only host journal mount. This proves a journal source exists for the receiver host; it does not prove container-side file access or log delivery. Pilot an independent Alloy v1.9.2 collector with only declared read-only journal directories, persistent collector state, no engine socket, no host network, and no published listener. Use [Alloy's journal source](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.journal/) and [Loki-to-OTLP bridge](https://grafana.com/docs/alloy/latest/reference/components/otelcol/otelcol.receiver.loki/) to send bounded, allowlisted container records to the existing private receiver OTLP endpoint. Keep `service`, `signal`, `cluster`, and `environment` bounded; exclude raw journal fields, request identifiers, and content from labels. The read-only survey uses only a cached receiver Alloy v1.5.1 image, with `--pull=never`, as a rootless UID `0:0` file-read probe; it does not validate collector configuration. If that cached probe image is absent, report the fixed `probe_unavailable` result and do not pull. Apply separately acquires the exact Alloy v1.9.2 collector image if absent, then runs the official [Alloy `validate` command](https://grafana.com/docs/alloy/latest/reference/cli/validate/) before any collector recreation. The command performs supported static configuration validation of syntax, components, and properties, with runtime stability flags matched; v1.9.2's [official changelog](https://github.com/grafana/alloy/blob/main/CHANGELOG.md) includes a validator fix for `livedebugging` blocks. First prove the exact host journal directory exists and is readable by the rootless collector, then require a receiver-side Loki query for the declared pilot container. Refuse ambiguous directories, permissions, an empty source, or a missing receiver receipt. Explicit stop and failed-delivery rollback stop and remove only the pilot container without `podman rm -v`, verify that it is absent from Podman's reboot restart set, and verify the named positions volume remains. This matches [Podman's restart behavior](https://docs.podman.io/en/latest/markdown/podman-create.1.html) and [container removal semantics](https://docs.podman.io/en/latest/markdown/podman-rm.1.html). Deploy and rollback through separate Dev-bound Semaphore workflows without changing or deleting the receiver's volumes. The current local socket pipeline remains local-only; a later cohort may reuse this collector mechanism after its privacy and volume gates pass.

**Positions-volume survey and repair gate (2026-10-09):** The explicit `survey` action resolves the one existing local `journal-collector-state` volume by Compose project and logical-volume labels, including when the journal collector is absent. It reads bounded root and direct-child metadata in the rootless Podman user namespace and never mounts the volume or reads positions content. A fixed `podman volume inspect --format` template reads `NeedsChown` and `NeedsCopyUp` as literal `false`/`true`; JSON field presence remains a separate diagnostic because Podman v4.9.3 may omit false-valued fields. Inventory, named JSON reads before/after the template, and template identity are cross-checked; invalid output or identity races refuse every gate. The separate `repair-positions` action retains its initialized-volume path: after fresh evidence proves an unused initialized volume's root ownership alone blocks `0:0`, it changes only root owner/group, verifies readback, and runs the restricted Alloy create/write/rename/delete probe. **Narrow pending-volume exception:** for exactly one empty unused volume with `NeedsChown=false`, `NeedsCopyUp=true`, blocked non-`0:0` root ownership, and fresh identity/metadata checks, repair may change only the root owner/group to `0:0` when mode is already safe. The source-level pending mode-reduction implementation additionally permits clearing only group/other write bits before that owner change; reviewed live Semaphore repair and collector/Loki acceptance remain pending. It performs no access-probe mount, preserves contents, and verifies identity, owner, the exact expected mode, and emptiness; repair success does not authorize first mount or collector apply; after any pending-volume mutation, failure preserves the volume and returns bounded uncertain status; no automatic owner restoration is attempted because a mount followed by removal cannot be ruled out from current state. No owner rollback or state deletion is permitted after Compose's first mount. Free-byte and free-inode thresholds are checked independently from stable metadata: above-threshold counter changes do not invalidate root/child/ACL/mount evidence, while low or unavailable counters refuse repair. Pending repair checks both GraphRoot and volume-filesystem capacity; initialized repair checks the volume filesystem. Both paths refuse ACLs, mount boundaries, symlinks, non-regular or multiply linked children, and child ownership mismatch. Unsafe modes refuse except for the implemented pending-only group/other write reduction; initialized-volume repair retains mode unchanged. Check mode reports `check_mode_unverified` without Podman or filesystem calls.

**Pending-volume repair diagnostics (2026-10-09):** Survey and the initial `pending_volume_unsupported` refusal share one fixed checklist derived from the strict pending owner-only gate. The implemented mode-reduction exception evaluates the same checks while permitting only the combined write/special-bit check to fail from group/other write bits alone; a present write-bit diagnostic remains unsafe until actual reduction and fresh readback prove otherwise. Expose only owner-RWX and unsafe-mode-bit enums, separate volume/GraphRoot byte and inode categories (`sufficient`, `low`, `unverified`), and the bounded `failed_checks` enum list. The diagnostic uses the existing discovery snapshot; it adds no Podman command, probe mount, content read, or ownership change. Repair refusal remains `refused`; the receipt explains only the initial observation and does not replace fresh identity rechecks or authorize mutation. Check mode and exception fallback preserve the schema with all evidence unverified.

**Pending-volume mode-bit follow-up (2026-10-09):** The Dev-bound read-only survey 3917 at exact merged Dev SHA `63946505875639b8b0080a045fa1e0ae08ab4a9b` confirmed the existing combined group/other-write-or-special-bit check failed while the pending volume remained otherwise available for diagnosis. Add five separate fixed categories (`group_write`, `other_write`, `setuid`, `setgid`, and `sticky`) from the same metadata snapshot. Preserve the combined category and `failed_checks` predicate; these extra fields explain the combined result but do not alter any acceptance or repair condition. Missing mode, check mode, and survey exception output use `unverified`. No raw mode, ownership, path, or capacity values are exposed, and the change adds no runtime probe or mutation.

**Pending mode reduction implementation (2026-10-09):** User-supplied sanitized evidence records successful read-only Dev-bound Semaphore task 3925 at exact merged Dev SHA `8050f2768903e09f68285f90c1598cb120006219`: the pending positions volume was unused, empty and unmounted, owner mismatched, owner RWX complete, ACL absent, collector absent, and volume/GraphRoot byte and inode capacity sufficient. Only `group_other_write_or_special_absent` failed; `group_write` was present while `other_write`, `setuid`, `setgid`, and `sticky` were absent. Earlier repair task 3899 refused before mutation. These receipts were supplied by the user, not independently reread here; they bound the implemented exception but do not authorize mutation, establish repair readback, collector readiness, or Loki delivery.

Retain the safe-mode pending owner-only path and the initialized-volume path. Add only an explicit pending repair exception when fresh evidence proves exactly one local expected volume, `NeedsChown=false`, `NeedsCopyUp=true`, no collector in all containers (including stopped), zero consumers/mounts, exactly empty root, owner mismatch with collector access blocked, complete owner RWX, ACL absent, and all identity, metadata and capacity guards passing. The only permitted initial failed check is `group_other_write_or_special_absent`, and its cause must be proven group/other write alone: special bits absent and mode an exact integer in `0..0o7777`. Unknown or malformed observations refuse. Derive `target_mode = original_mode & ~0o022`; never substitute a fixed `0700`, add owner permissions, strip group/other read/search, clear special bits to make an unsafe case eligible, or apply this exception to initialized/nonempty state. Keep the combined diagnostic and failed-check list truthful for the original snapshot; separate mode-bit diagnostics do not authorize repair.

Pin the volume creation timestamp, configuration, expected name/project and mountpoint, root device/inode, original owner/group and mode. The mode and owner changes use separate rootless helper processes, so one open descriptor cannot span the required full Podman discovery between mutations. Before each mutation, open the exact directory without following symlinks and compare descriptor metadata with the pinned root device/inode and the exact expected owner/mode. The first helper reduces permissions to `original_mode & ~0o022` and verifies exact target mode plus unchanged identity/owner/emptiness. Then repeat full discovery and emptiness/ACL/use/mount/capacity checks with the reduced mode as the sole expected metadata change. Only after that readback passes may the second helper change root owner/group to `0:0`; it independently pins and rechecks the same root device/inode before changing ownership. Verify fresh discovered identity, exact target mode, new owner, empty contents, initialization flags, collector absence, zero consumers/mounts, ACL absence and capacity. No recursive operation, content read/write, probe mount, volume creation/deletion, or Compose start belongs to pending repair. A rerun of an already converged safe volume must not mutate it again or start a collector.

Any failure before mutation refuses with no change. Once permission reduction or ownership change may have occurred, every command failure, exception, identity/metadata drift or failed readback returns bounded `uncertain`, preserves the volume, and never automatically restores mode or owner. Descriptor pinning protects the selected inode but does not serialize Podman, other same-user processes, child creation, ACL changes, or mount history; repeated snapshots cannot rule out mount-then-remove races. Require an operator-controlled quiescent repair window and fresh checks; refuse observed races, retain uncertainty after mutation, and require separately reviewed recovery if certainty cannot be re-established. No claim of atomicity or historical first-mount proof is made.

Alternatives rejected: fixed `0700` removes unrequested read/search permissions; owner-first leaves write exposure during the owner transition; automatic restoration can reintroduce write permissions or alter state after an unobserved mount; broad mode normalization admits unsafe special-bit or owner-access cases. The permission reduction is deliberately limited to the empty pending root.

Implementation: the pending branch retains the original failed-check list and permits only exact write-bit reduction for an empty, unused pending volume when it is the sole failed guard. The mode helper pins and verifies the root descriptor; complete fresh discovery/readback then gates the existing owner helper, which independently pins the same device/inode. Post-mutation failures return bounded `uncertain` with no restore. Focused coverage includes write-bit combinations with read/search preservation, safe-mode owner-only behavior, initialized-path behavior, converged rerun, malformed/special modes, missing owner RWX, unrelated failed guards, pre/post-mutation races, readback, capacity and sanitized failure results. Check mode remains unverified and runtime-free. Source-level validation does not accept the live repair: after independent exact-head review and green CI, run the Dev-bound check-mode workflow, a fresh read-only survey at exact merged SHA, a separately selected `repair-positions` task, and a separate read-only post-repair survey. Record sanitized task IDs/SHA and safe-mode `0:0` readback; this implementation session runs none of those live tasks. Only a separately authorized later collector-apply task, with its full pre-apply and immediate pre-start gates, may perform Compose's first mount. Require live mount/cursor/access/health, unchanged receiver set and fresh exact-target Loki delivery before closing task 1.2.

**First-start bootstrap and bounded survey fallback (2026-10-09):** The pre-apply and immediately-before-start read-only gates may return `bootstrap_allowed` for a missing expected volume only after complete bounded inventory, no expected-name collision, and an all-container inventory (including stopped containers) proves the journal collector absent. An existing pending volume is eligible only with one exact local identity, zero users and mounts, effective `NeedsChown=false` and `NeedsCopyUp=true`, and an empty, unambiguous root already owned by `0:0` with owner RWX and no group/other write, special bits, ACL, nested mount, or metadata ambiguity. The narrowly scoped root-only repair and implemented mode-reduction exception above may establish that same safe ownership and mode before first mount only after comparing volume `CreatedAt`, volume configuration, and root device/inode across observations; Compose `up` remains the sole creator/initializer. The first-mount headroom gate requires at least 16 MiB available and 128 available inodes on both GraphRoot and the pending volume filesystem; this does not replace or weaken the independent 30% retention headroom gate. The immediate pre-start check runs outside rollback handling. After Compose's first mount, a persistent `NeedsCopyUp=true` flag is not success by itself: require the exact live RW mount, namespace create/write/rename/delete, bounded allowlisted Alloy layout, the parsed `positions.yml` map entry for `cursor-loki.source.journal.o11y_alloy` with an empty label set and a valid journal cursor, collector health, unchanged seven receiver containers, and a fresh exact-target Loki receipt. Empty image destination can leave copy-up pending; partial copy or unexpected files refuse and preserve the volume. Rollback removes only the pilot container, without `-v`; it never restores ownership or deletes copied state. The strict cursor gate can expose first-start failure or replay risk when initialization is partial. Unexpected survey exceptions remain bounded as `survey_failed`; the positions helper remains `check_mode_unverified` in check mode without its own Podman or filesystem operations. These are source-level facts, not live receiver evidence; task 1.2 remains open.

The helper's `verify` action is the pre-start gate; `verify-live` is the post-start gate. Pre-start may return `ready` without a cursor only for a complete observed volume with both initialization flags false, no collector in the all-container inventory, zero consumers and mounts, an exact safe identity, root `0:0`, access and capacity evidence on GraphRoot and the volume filesystem, no ACL or nested mount, an allowlisted empty/seed/component layout, and explicitly observed absence of `positions.yml`. This permits one bounded Compose start attempt and is not delivery proof. A present invalid or unreadable file, unknown cursor presence, or incomplete/unsafe component layout refuses. Here, partial copy means a present invalid/unreadable cursor file, a pending temporary positions file, or any layout outside the allowlist; proven cursor absence before start is a separate retry case. Post-start always requires the valid parsed cursor plus the live mount and access evidence; it never returns `bootstrap_allowed`. No phase deletes or replaces positions state or automatically restores ownership after pending mutation. Empty or allowlisted state cannot establish historical copy-up or cursor-loss history, so replay/loss safety remains unproven until live evidence is recorded.

**Collector state-path permission correction (2026-10-09):** The pinned [Alloy v1.9.2 Dockerfile](https://github.com/grafana/alloy/blob/v1.9.2/Dockerfile) creates `/var/lib/alloy` for UID/GID 473 with mode `770`. The journal pilot intentionally runs as `0:0` with all capabilities dropped, so its named state volume must not sit below that image-owned parent. Keep the existing named volume and mount it at `/alloy-state`, set `--storage.path=/alloy-state`, and require the helper's exact destination check plus the playbook's live write probe and rollback volume lookup to use that same path. This avoids relying on copy-up through an inaccessible image ancestor and preserves the UID/capability boundary. Dev task 3973 failed health and rolled back after 80 permission-denied log lines, with the fixed positions target and fixed TCP readiness probe both failing; read-only task 3977 found the collector absent and the existing named volume empty, unused, unmounted, owned by UID 0 with RWX mode and no ACL. These receipts do not establish successful collector health or Loki delivery; task 1.2 remains open.

**Positions initialization-schema diagnostic follow-up (2026-10-09):** Dev survey 3856 succeeded with `volume_initialization_unverified`, zero consumers and entries, root owner mismatch, blocked access, ACL absent, clear mount state, and safe direct children. The journal source was available while the target positions path was denied. Keep initialization flags fail-closed for every gate. The survey alone adds a second exact-name volume inspection and Podman client/server version query after unique volume identity is established; it reports only field presence/type, actual boolean values, named-query outcome, identity comparison, and validated bounded version strings. Unrecognized values, command failures, and malformed or duplicate named results remain categorical/unavailable. Check mode performs no Podman calls and returns the same diagnostic shape as unverified. The extra named-volume identity diagnostic and version query run only during survey and never change gate decisions; the identity-bracketed initialization-flag read is required by every gate and does control decisions.

**Extra positions-directory diagnostic (2026-10-10):** Our read-only Dev-bound Semaphore API survey task 4009 succeeded at merged Dev SHA `9bc543d7d0f80be77ddab3983f9db6a983257f06`. The receipt reported a pending initialization volume with three top-level entries: the seed file, the existing journal component directory, and one unidentified other directory. It also reported no collector or volume users, matching root owner, RWX, no ACL, clear mount, sufficient free space, ambiguous children, unavailable cursor, and live mount/namespace fields unverified or not run. The third directory's identity and purpose remain unknown; do not infer a persistent queue or any other state from its existence. A diagnostic-only extension compares only the exact public `otelcol.receiver.loki.journal` and `otelcol.exporter.otlp.journal` component names. It emits bounded fixed-role counts and, for a matching directory, owner/access, ACL, mount, and direct-child categories. Reads are descriptor-relative with no-follow opens and identity rechecks; unsafe links, metadata errors, and races return `unverified`. Unknown names remain aggregated. Recognized and unknown extra directories both keep `component_layout=false`; no pre-start or live acceptance predicate changes. The extension adds no survey write, probe, or mount. Task 1.2 remains open.

**Unmatched-directory metadata and Dev receipt (2026-10-10):** Our read-only Dev-bound Semaphore API survey task 4016 succeeded at exact Dev merge `736af5b16917f28a243b3715d56c76c00bfe7a8a`. The sanitized receipt reported `volume_initialization_pending`, no collector, no valid cursor, three direct entries, `seed_file=1`, `journal_component=1`, `other_directories=1`, and zero recognized receiver/exporter directories. Volume identity, capacity, root owner, and root RWX were good; `NeedsCopyUp=true` and the `NeedsChown` field was missing. `component_layout` failed. The pending-repair `failed_checks` included `children_empty`, `child_count_zero`, `owner_mismatch`, and `collector_access_blocked`. These are our API-observed sanitized results, not user-provided evidence. The helper now reports aggregate categorical owner/access/ACL/mount/direct-child-empty status over anonymous unmatched direct child directories, bounded by the existing 32-entry cap. Descriptor-pinned no-follow access and post-observation identity/listing checks make races, symlink substitutions, ACL or mount ambiguity, malformed data, and incomplete observations `unverified`. This is diagnostic only: unmatched directories remain outside the accepted layout and cannot pass repair, pre-start, or live gates. Stop on remaining or unverified entries; do not infer queue state or mutate/apply the volume. Obtain a fresh reviewed Dev survey after merge. Task 1.2 remains unchecked.

Signal selection per cohort:

| Cohort | Default collection | Trace decision |
|---|---|---|
| o11y, Caddy, DNS, step-ca, OpenBao, OPA, Authentik, Semaphore, NetBox | host/container logs; native or vetted exporter metrics; readiness | request-serving components only after runtime compatibility/security pilot |
| App and data services: n8n, Postiz, tududi, honcho, UhhCraft, ERPNext, OpenHands, Nextcloud/NocoDB/WikiJS if deployed | stdout plus DB/queue/app metrics; request/worker identity | runtime agent where supported, manual span for an important workflow gap |
| Agent and inference workloads: agentgateway, DGX, NemoClaw, NetClaw, WisBot, WebSmith, ComfyUI, Hunyuan3D | retain proven gateway/DGX paths; add worker/job and GPU/model metrics where supported | keep existing gateway trace path; pilot agent/runtime tracing with prompt redaction |
| Proxmox, guest OS, container engine, pfSense and other managed devices | node/hypervisor/device exporter or read-only API metrics, system logs, health | not applicable unless a request-serving software component has safe support |
| GitHub runner hosts | host health and bounded metrics first; job logs only after an entitlement and redaction decision | job tracing is not assumed |

Alternative rejected: mandate the same three signals for every process. A router or database can be fully diagnosable with logs and metrics while a fabricated trace requirement obscures missing real coverage.

### 3. Preserve one private receiver and bound every signal

Collectors use site-config source allowlists enforced by the reviewed host firewall; this is source-IP admission, not cryptographic host identity. OpenBao supplies any credentials through Ansible memory. Prometheus scrapes only declared private targets; OTLP ingress stays on a private interface behind source-scoped rules. Task 3.4 tests a rejected connection from a named, unapproved vantage host. Receiver Alloy gets explicit memory/backpressure handling and exporter retry/queue limits before more senders are added, following [Alloy's access and permission guidance](https://grafana.com/docs/alloy/latest/access_permissions/). Per-target scrape sample limits, allowed labels, log redaction/volume controls, trace head sampling, backend ingestion limits, and retention are declared and verified. Set a non-zero Prometheus size retention and receiver disk-free alert before the first expansion wave. Normalize the environment/cluster label from inventory in both Prometheus and Alloy before cross-environment joins; migrate existing dashboards and alerts with the label change. Alert on receiver refusal, collector drops, unhealthy scrape, missing logs, and absent expected traces. Correlate by normalized `service`/`service.name`, environment, and bounded instance identity; never put request IDs, user IDs, prompts, secrets, or raw URLs in metric labels.

Alternative rejected: allow every discovered host to export all telemetry. It expands network and cardinality risk before a service has a budget or owner.

### 4. Resize from evidence, not a guessed VM size

First deploy and verify a receiver-host node metric source through Semaphore; the current five-component scrapes do not provide VM CPU, memory, or filesystem capacity. This bounded source is the only sender allowed before the baseline. Extend the existing production budget verifier and comparator for a read-only forecast rather than creating a parallel receipt path. The forecast has separate **live readback** and **candidate proposal** inputs: live retention and size cap are read from the current receiver, normalized, and compared with their separate current private declarations; proposed retention and the proposed Prometheus size cap are separate private candidate values and are never written by this operation. The documented 90d Prometheus / 45d Loki / 45d Tempo target and a nonzero Prometheus cap are candidates only. A reviewed live-setting change is evaluated against its updated declaration and its own gates. A forecast pass grants no permission to change retention, resize a VM, or enable a sender.

The initial baseline starts at the first successful exact-target receiver-host receipt. Later wave forecasts use the most recent independently observed complete seven-consecutive-day window after that receipt. Windows and bins are UTC timestamps on a 24-hour grid anchored to the first receipt; all seven bins must be complete and contiguous. A window ends no more than 24 hours before evaluation; live capacity and health readback is no older than 15 minutes. Require at least 95% of cadence-expected samples and no observed gap over one hour. Missing, stale, incomplete, nonfinite, negative, or invalidly timestamped evidence makes the forecast `unavailable`; never turn missing data into zero. A measured zero is valid only when an actual sample records zero. The declared pilot sources and schedules must cover known peak periods, with owner confirmation that no outage, maintenance, or abnormal operation invalidated the window. Otherwise extend and recapture; do not extrapolate from a shorter or unrepresentative sample.

Keep timestamp provenance explicit: raw scrape timestamps identify source observations, while Prometheus `query_range` timestamps identify query evaluation times and do not prove distinct scrapes. Preserve source timestamps and deduplicate reused observations; do not relabel evaluation timestamps as scrape receipts. Prometheus sample counters count samples, not bytes. Loki and Tempo received-byte counters describe received input and are not proof of end-to-end persisted bytes.

The current shared running backends cannot prove same-batch persisted-byte conversion or transient compaction allowance through the existing read-only interface. Background ingestion, older-block compaction, retention deletion, and temporary allocations are not attributable to one pilot batch through those readbacks. Therefore the forecast remains `unavailable` until an independently observed seven-day baseline and either a repeatable isolated calibration or a separately proven conservative storage upper bound establish all required inputs. The planned calibration path follows a verified backup and isolated restore: run the pinned runtime and configuration on a restored or disposable isolated target, submit an attributable batch, observe accepted input and persisted bytes after normal compaction, and measure peak transient storage through that cycle. Keep unrelated writers and retention deletion out of the calibration interval. Record private evidence and expose only sanitized projections. A conservative-bound route is a later reviewed alternative, not the default path; no assumed multiplier or ratio substitutes for evidence.

For each backend, derive peak incoming bytes per day as the maximum of seven timestamped complete 24-hour totals in the accepted window. Measure the conversion ratio only from the isolated same-batch calibration above. Require the exact-target task receipt to be no older than 15 minutes and carry a positive numeric Semaphore task ID. Measure pilot-to-wave ingest, series/cardinality, CPU p95, and peak-memory increments against a comparable baseline, bound to the deployed receiver runtime/configuration and accepted window. Missing or incomparable pilot evidence makes the forecast `unavailable`.

Normalize live retention and size-cap readback to days and bytes and compare with separate current private declarations to verify the current configuration; use separate candidate 90d Prometheus / 45d Loki / 45d Tempo retention and candidate Prometheus cap for projection and the candidate gate. A later reviewed live change passes only when fresh readback matches its changed declaration. Keep candidate retention fixed at 90d Prometheus / 45d Loki / 45d Tempo with a nonzero Prometheus cap. Calculate full projected retained bytes as `(baseline_peak_ingest_bytes_per_day + scaled_wave_ingest_bytes_per_day) * candidate_retention_days * measured_storage_conversion_ratio + transient_allowance_bytes`. Compare full projected Prometheus retained bytes with the candidate cap. Charge each backend's full projected retained bytes, including the full candidate-retention baseline, scaled wave, and transient allowance, against current filesystem free bytes; grant no resident-byte credit unless a separately specified and independently verified resident-byte measurement proves it. This intentionally double-counts bytes already resident in the backend because current free space already reflects them. Obtain filesystem groups only from the current complete five-volume named-volume receipt and join backend volumes by their known volume names; never accept a group from backend forecast input. Group all backend volumes by their shared backing filesystem, sum each backend's full projected retained bytes exactly once, and subtract that sum once per filesystem. Hypothetical deletion, cleanup, retention expiry, or moving data receives no capacity credit. Require `(current_free_bytes - summed_full_projected_retained_bytes) / filesystem_total_bytes >= 0.30` for every affected filesystem, projected peak memory headroom of at least 25% of declared VM memory, and projected CPU p95 below 70% of declared VM CPU capacity. A measured drop/refusal or unhealthy representative backend query refuses the wave regardless of numeric margins. Missing or stale query-health evidence is `unavailable`; an observed unhealthy result is `refused`.

Keep the controller and deployed-receiver Git SHAs separate from the 64-character effective-inventory digest; they identify different evidence and must not be copied into one another's fields. Require a successful exact-target receipt with a positive numeric Semaphore task ID and bind it through an independent trusted verifier. A standalone evaluator or source fixture can establish arithmetic and validation behavior only, never production measurements or live capacity. Emit a versioned sanitized `pass`, `refused`, or `unavailable` receipt with fixed categories, bounded windows, assumptions, projections, and margins; omit private topology, raw backend responses, identifiers, credentials, and exception text. Live acceptance requires a fresh read-only Dev-bound Semaphore run tied to reviewed source and effective-inventory evidence. Forecast evaluation never changes retention, resizes a VM, or enables a sender. A separate live resize still requires the existing VM/guest readback, isolated restore, and preserved-telemetry gates.

If the capacity gate fails, change `site-config` VM resource declarations and inventory together, review them, then run existing `platform/playbooks/resize-vm.yml` through its Dev-bound Semaphore template. That playbook grows only the Proxmox disk; before any disk resize, implement and check an idempotent Semaphore-managed guest partition/LV/filesystem growth path for the receiver's actual layout. Refuse unsupported layouts and require a backup/restore receipt. The receipt must identify an immutable backup artifact and exact source disk/layout, prove restoration to an isolated target, validate the restored guest filesystem and preserved telemetry-volume access, and record cleanup of the isolated target. Snapshot creation and snapshot-list presence are insufficient: existing `snapshot-vm.yml` does not restore data. No such restore-test workflow currently exists, so guest growth remains prohibited until it is designed, reviewed, and successfully exercised. Its reboot remains opt-in; first read the current-vs-desired diff and confirm Proxmox storage, schedule any required reboot, then read back VM capacity, guest filesystem capacity, stack readiness, and preserved telemetry. A size increase is a measured gate, not a preselected number. Tempo's own [sizing guidance](https://grafana.com/docs/tempo/latest/set-up-for-tracing/setup-tempo/plan/size/) depends on span rate, span size, query rate, and retention; the same variables are recorded here.

2026-09-28: read-only receiver task 1801 confirmed the exporter runtime boundary (private PID, read-only host-root mount, one private network, no published ports). It found guest root is ext4 on an LVM-backed virtual disk and Podman's volume/graph paths share that filesystem; only 6.88% free remains, below the retention-expansion threshold. Exact device identifiers/capacities are private. No restore-test receipt exists; the current snapshot workflow is not a substitute.

Alternative rejected: immediately allocate a larger o11y VM. It could waste resources or still under-size the wrong bottleneck; retention and label cardinality must be measured first.

### 5. Deliver coverage in controlled cohorts

Complete the remaining o11y/agentgateway user-visible and owning-task receipts first. Build the coverage census and budget gate next. Pilot receiver-host metrics/log collection, then one other infrastructure VM and one application VM before expanding to control-plane/network, stateful applications, and agent/inference cohorts. For each remote target, probe from the receiver, apply the reviewed source-scoped firewall rule, prove HTTP 200 or OTLP admission, and only then merge its scrape/export declaration; an early `up=0` fires the existing service-down alert. Runner hosts stay a separate decision gate: their private-repository job output cannot enter shared Loki without an entitlement/redaction review, and their egress and OpenBao restrictions require a reviewed collector design. Each wave is a reviewed feature-to-`dev` change with one completed CodeRabbit or Claude review and green final-head CI, followed by Dev-bound Semaphore dry-run/check mode where meaningful, apply, exact-target proof, dashboards/alert drill, and an operator-driven reviewed revert/redeploy drill. Keep tightly related public changes in few PRs; private site-config declarations get their own reviewed PR because that repository owns production facts. Reconcile the coverage report after each wave; only a complete deployed-target inventory with fresh receipts closes this change.

## Risks / Trade-offs

- **Host collector permissions or remote logs expose sensitive data** → pilot least privilege; filter/redact before export; verify secrets and request bodies do not appear in Loki or task output.
- **Automatic agents alter process behavior or create high-cardinality telemetry** → compatibility matrix and one-service pilot per runtime; explicit version pin, startup/rollback switch, sampling and attribute limits.
- **Single-node local Tempo storage or receiver VM fills** → capacity gate, retention, disk alerts, no clean-volume validation; document backup/restore and separate storage migration if the measured load outgrows this topology.
- **A green `up` masks the wrong target** → exact instance and fresh log/trace checks; failed or stale receipts remain visible.
- **A VM resource change requires downtime** → keep reboot opt-in, verify guest filesystem and health after it, and pause the next wave until the previous baseline is restored.

## Migration Plan

1. Reconcile remaining receiver and agentgateway acceptance tasks with their live receipts; verify a user-visible trace pivot. Deploy a receiver-host node metric source and log collector, obtain an exact-target receipt, then begin the seven-day measurement window.
2. Add census declarations and extend the existing production budget verifier for read-only coverage/capacity reports; resolve all unclassified deployed targets. No other sender is enabled in this phase.
3. Pilot reusable host collection and runtime auto-instrumentation, then gate and release one cohort at a time. Add code-provisioned dashboards and alerts only for metrics and traces actually present.
4. When a wave needs capacity, merge the private resource declaration, converge it via Semaphore, run the reviewed guest-growth playbook if disk grew, verify preserved state, and rerun the budget forecast before enabling the wave.
5. On failure, an operator reverts the reviewed declaration through the branch workflow and redeploys from `dev`, verifies the old signal/alert baseline, and retains data. Disk growth is not rolled back by shrinking; unused capacity is left in place.

## Open Questions

- Which source-only service directories correspond to current production workloads? Resolve from private inventory and Semaphore/live readback in the census, then record lifecycle explicitly.
- What are the actual per-backend daily ingestion and compression factors? Measure after current acceptance; they determine the VM number but do not change this design.

## 2026-09-28 production receipts and bounded follow-up

Non-destructive production o11y deploy task 1822 succeeded on reviewed `dev`
SHA `c0f65d9b4dca84a3c5b2e33f7e876e72ccac8b25` and passed the receiver-host
versus guest-root metric check. Read-only budget task 1823 resolved all five
named volumes to the shared guest-root backing filesystem and measured 7.06%
free. The deploy preserved existing volumes and they remained mounted and
observed; this does not establish historical data continuity inside every
volume. Retention remains Prometheus 15d, Loki 7d, Tempo 168h, and the
Prometheus size cap remains 0B. Task 1823 observed 13,088 active head series,
87.55% guest memory headroom, and sample limit 2,000. These are point-in-time
readings, not a seven-day capacity forecast or Loki/Tempo growth trend. Log
delivery and the seven-day baseline remain open.

The provisioned root-filesystem warning is scoped to the verified
`receiver-host` job, `o11y/receiver-host` service, and `/` mountpoint. It uses a
less-than-10% threshold for 15 minutes, warning severity, and the existing ops
contact; missing filesystem samples become alerting. Source and render tests
are not live firing evidence; see the 2026-09-29 production receipt below.

The Dev-bound backup readiness survey uses OpenBao-sourced Proxmox credentials
and read-only API requests against the single declared `o11y_svc` VM. Its
sanitized result contains aggregate counts only: active storage entries that
declare backup content, their PBS versus non-PBS backend classes, and matching
backup-content records for the VM. Missing or malformed backend types refuse
the result; storage identifiers, paths, and free-form values are never shown.
Listed records are candidates only: the survey cannot establish immutable
retention, choose an artifact, identify an isolated restore target, or prove a
restore. The separately reviewed restore workflow must validate the restored
guest filesystem and telemetry-volume access, then record isolated-target
cleanup before guest growth is allowed.

Read-only Semaphore survey task 1835 succeeded against exact reviewed `dev`
SHA `fb7e5315f4f8d85a6b5b26a4f24d93f4692dbc7a`; it reported three active
backup-capable storage entries and zero matching backup artifacts. Later,
read-only Dev-bound survey task 1945 succeeded and reported one candidate
artifact, three non-PBS backup-capable stores, zero PBS stores, and a verified
target VM. Its listing was complete, while artifact immutability, isolated
target verification, and restore verification were all false. It did not
select storage or alter Proxmox state. These facts still do not identify an
immutable source or restore destination, so tasks 4.2 and 4.6 remain gated.

The new Dev-bound `survey-o11y-backup-artifact.yml` is read-only and reports
only allow-listed candidate metadata (backend class, format, byte size,
creation epoch, and protection status) plus source VM disk device/size/backup
inclusion layout, including EFI and TPM state disks. Raw API results, volume
IDs, storage names, and free-form values remain under `no_log`; no hash or
reversible identifier is included in the visible receipt. A future restore
workflow must use a separately declared private artifact selector. The
inspector validates PBS VM records in the official `backup/vm/<vmid>/<UTC
timestamp>` form with `pbs-vm` format, and requires the volume storage prefix to
match the listing source. See the [Proxmox PBS storage implementation](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Storage/PBSPlugin.pm).
An absent protection field remains `unknown`; a reported protection flag is
not immutability evidence. The survey always reports immutability and restore
verification as false. Reviewed Dev task 1953 succeeded with one non-PBS
candidate and complete source disk sizes. At least one backup-inclusion flag was
absent, so backup inclusion remains unverified; parsed layout does not prove
guest state was included. The run did not establish immutability, an isolated
target, or restore success. Read-only Proxmox cluster validation task 1955
reported image-storage capacity facts, but did not inspect physical disks, LVM,
or filesystems. Use these facts to declare any artifact selector and then
design the isolated target and immutable storage mechanism in private
site-config before implementing a restore workflow.

**Private selector proposal compatibility (task 4359, 2026-10-11):** the
proposal refused before recensus and before any private inventory write. After
flow-style sequences were admitted, valid nested flow mappings in firewall
lists and coverage records still failed the global mapping-style guard; after
that guard was scoped, a standard finite YAML float in an unrelated gateway
sampling value failed the tag whitelist. The insertion validator now requires
block style for the root, each mapping along the literal
`agent_cloud.children.o11y_svc.hosts` path, and the target-host mapping. It
recursively validates all nodes, accepts nested flow
maps/lists and standard finite floats elsewhere (including below the target
host), and continues to refuse duplicate keys, aliases, merge keys, custom
tags, nonfinite floats, an existing selector, or semantic changes beyond the
new selector. In-memory private-catalog and inventory compatibility passed
without printing values. This parser compatibility result does not establish
private-layout acceptance, select an artifact, or complete OpenSpec `4.6e`.

The follow-on restore-feasibility receipt is deliberately narrow. It reports
source-layout parse and size completeness separately from whether exactly one
supported artifact candidate is present. It reports the count of distinct
storage IDs that Proxmox reports active, non-shared, and image-capable on at
least one online node other than the source VM's node, with enough
point-in-time `avail`/`total` capacity for the complete source disk layout while
retaining at least 30% of reported total capacity. If any source disk size is
unknown, no storage qualifies. EFI and TPM state disks are included in the
aggregate size. An absent or malformed `shared` field fails closed for an
active image-capable storage; shared storage is not counted. It does not choose
a storage; non-shared storage IDs count once. Malformed or incomplete
node/storage reads fail closed. A returned
`cluster/nextid` value means only that a candidate was available at read time:
the value is suppressed and no ID is reserved. Neither result proves a future
restore can be placed or that an artifact is usable.

Reviewed Dev task 1964 at merged SHA
`2c8382c77b8ccf2542b69faf72044a18d2c7f9fc` completed this feasibility
receipt; publisher tasks 1962 (check mode) and 1963 (apply) passed. It reported
one supported non-PBS candidate, complete source disk sizes, backup inclusion
unverified, three distinct off-source non-shared image-storage IDs meeting the
point-in-time capacity/headroom test, and a current VMID candidate that was not
reserved. Immutability, an isolated target, and restore success remain
unverified. The storage count is based on Proxmox status rows, not a physical
device mapping or a reservation.

The selected backup destination remains local-only PBS on a separately declared
cluster node; the node declaration belongs in private `site-config`. The
current image-capacity and VMID checks do not establish that node's physical
disk safety or PBS datastore readiness. The new Dev-bound physical-storage
survey reads only `/nodes/{node}/disks/list?include-partitions=1`, `/disks/lvm`,
`/disks/lvmthin`, `/disks/directory`, and `/nodes/{node}/storage`, after
validating the private node declaration against the live node list. These
official Proxmox endpoints expose device paths, usage classes, VG/PV and thin
pool sizes, managed mount paths/devices/types/options, and storage status. The
survey maps these to allow-listed aggregate counts and coarse reported-capacity
bands under `no_log`; it emits no names, IDs, paths, serials, mount details, or
exact capacities. See the upstream [disk inventory API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Disks.pm),
[LVM API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Disks/LVM.pm),
[thin-pool API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Disks/LVMThin.pm),
[directory API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Disks/Directory.pm),
and [storage status API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Storage/Status.pm).
The Proxmox API token needs `Sys.Audit` for node/disk inventory and
`Datastore.Audit` (or `Datastore.AllocateSpace`) for each visible storage
status read.
An absent disk `used` field remains unknown. Directory entries can represent
local directories or mounted shares; the survey does not establish locality.
Storage capacity bands are reported as per-storage counts rather than summed
capacity, since separate storage declarations may overlap backing resources.
All reported capacity is descriptive, not PBS suitability or physical
readiness. The survey is GET-only and never selects a device. Separately
reviewed idempotent disk/LVM/filesystem automation must match the private
declared layout before any write; no unused device may be presumed safe or
selected automatically. Live survey execution remains pending.

The physical survey succeeded as Dev-bound Semaphore task 1981 at merged SHA
`ab98eaa3489937a854431ad046ddc5cd19c5c196`; publisher tasks 1978 (check mode)
and 1979 (apply) passed. Its sanitized receipt reported 7 physical devices,
3 partitions, 3 disk-usage values unknown, 4 LVM volume groups containing 4
physical volumes, 1 thin pool, 0 Proxmox-managed directories, 6 visible
storage-status rows (5 active), 3 explicitly non-shared rows, 3 shared rows,
and 5 rows with positive reported capacity while 1 capacity was unreported.
Device selection/safety, filesystem readiness, PBS suitability/readiness, and
write authorization remained false. A first invocation refused because the
private VM-image storage declaration was absent; the existing code-managed
inventory synchronization reconciled that private declaration before task
1981 succeeded. Private identifiers and exact capacities remain outside this
repository.

The local-only PBS placement strategy is a VM with a new virtual data disk on
the separately declared image store; this does not authorize initializing a
physical host disk. The GET-only preflight requires the exact declared active,
non-shared `lvmthin` `images` row, reads its storage configuration to resolve
`vgname`/`thinpool`, and requires one matching thin-pool record. The node
storage-status `total`/`used`/`avail` must agree with the linked thin-pool
`lv_size`/`used`; any disagreement refuses the receipt. The content request
has no `content=images` filter, so it can include every visible image and
rootdir row. Each volume ID must have the exact declared storage prefix, a
supported `vm-` or `base-` owner/name form, a unique ID, an allowed content
type, a raw format, and a positive size. IDs and capacities are never printed.
However, this is only a visible-volume inventory: Proxmox's LVM-thin
`list_images` excludes `snap_*` logical volumes, while its snapshot operations
create those volumes. The preliminary calculation also requires the storage
configuration to declare exactly both `images` and `rootdir`: an images-only
configuration could omit preexisting rootdir volumes from the content API
listing. No snapshot-complete read is established in this survey.
Therefore `snapshot_inventory_complete_verified` is always false and the
256/512/1024-GiB results are named
`snapshot_unverified_visible_volume_preflight_passes_*`; they are preliminary
facts only, not safe-allocation guidance. VM/disk provisioning requires a
separate reviewed snapshot-complete audit gate before any allocation.

The preliminary visible-volume calculation passes only if the listed virtual
sizes plus the proposed disk leave at least 30% of pool size, a hypothetical
full write of the proposal leaves at least 30% of pool size in reported
physical availability, and current thin-pool metadata free space is at least
30% of metadata size. The metadata condition is a current threshold, not a
forecast of metadata use caused by a new unwritten disk. These are
point-in-time, non-reserving facts and never authorize allocation or establish
PBS, guest filesystem, physical safety, or restore readiness. The Proxmox
LVM-thin plugin reports volume `size` from `lv_size`, while its status reports
pool size, used, and available; the thin-pool API exposes metadata size and
used bytes. The storage-content API can silently filter rows using per-VM
`VM.Config.Disk` permissions, so HTTP 200 alone does not prove the visible
listing is permission-complete. Before interpreting it, the survey reads the
caller's effective permissions for the exact declared storage and requires
both `Datastore.Allocate` (which upstream `check_volume_access` uses to bypass
per-VM filtering) and `Datastore.Audit` (required by the content endpoint).
If either privilege is absent from a valid permissions response, the fixed
`visible_volume_permissions_verified` fact and all preliminary size booleans
remain false. If the private declaration is missing or malformed, or a
required Proxmox API request fails (including permission-denied content
listing), the task can stop before producing a sanitized receipt; raw request
details remain hidden by `no_log`. No API-token ACL is changed here. Exact
pool/status `used` equality is required across separate GET requests; concurrent
writes can cause a fail-closed refusal, in which case rerun the survey after
activity settles. No tolerance is applied. See the [Proxmox storage
guide](https://pve.proxmox.com/pve-docs/pvesm.1.html) and [LVM-thin status
implementation](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Storage/LvmThinPlugin.pm), [storage-content API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Storage/Content.pm), [thin-pool API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Disks/LVMThin.pm), and [effective-permissions API](https://github.com/proxmox/pve-access-control/blob/master/src/PVE/API2/AccessControl.pm).
Live execution of the expanded virtual-disk preflight remains pending.

## 2026-09-29 thick-LVM visible-store preflight

Task 1998's sanitized result showed one directory, two LVM, one LVM-thin, and
two other visible storage backends. The follow-on uses the existing Dev-bound
physical-storage survey with one added protected `GET /storage`. It joins
visible storage config/status rows to `/nodes/{node}/disks/lvm` by validated
storage ID and `vgname`. Only active `lvm` rows with explicit `shared=0` and
`images` content are candidates; each must map to exactly one unique volume
group. Status total/used/available must exactly match that VG's size/free
extents. Malformed or duplicate IDs/VG mappings and inconsistent values fail
closed. Because configuration fields such as `content` can be optional, rows
are validated for storage ID and type globally; `content` is required and
validated only when a matching active, non-shared LVM status row advertises
image content. A config row with `disable=1` is excluded even if node status
reports it active. Any other visible `lvm` or `lvmthin` config row naming the same
VG excludes that candidate, including shared, inactive, non-image, or
foreign-node rows; incomplete or node-unmapped local VG mappings
suppress only thick-LVM candidate and headroom counts, while aggregate
diagnostic counts remain available.
Either a local-applicable LVM/LVM-thin config row without same-type target-node
status (including disabled configs), or a target-node status row without a
matching same-type local-applicable config row, makes the mapping incomplete
and suppresses only thick-LVM candidate and headroom counts. A matching
foreign-scoped config/status pair marked `enabled=0` is excluded from local
completeness; local-applicable configs remain checked even when disabled. An
invalid or duplicate storage ID, or a missing/non-string
type, in the new visible cluster-config response can refuse the entire sanitized
survey; types may repeat. Only the candidate's linked capacity tuple is
required to match exactly. Existing unrelated node-status and VG validation
remains governed by the original survey.

Task 2011 completed the GET-only survey at reviewed SHA
`4c1806652ed15cab8d1a4b50fd241bab52724646`; the receipt showed incomplete
thick-LVM mappings and zero candidate/headroom counts but did not identify the
cause. Do not infer that cause. Proxmox's `/storage` config API emits optional
`nodes` as an encoded comma-separated string; omission means no node restriction,
while an explicit list restricts applicability. The inspector validates this
scope only for LVM/LVM-thin configs and builds local VG joins and candidate
selection from configs applicable to the declared online node. Alias exclusion
checks each local candidate against every visible LVM/LVM-thin config, including
foreign-node rows, because backing identity is not established by node scope. It
reports only aggregate counts for foreign LVM config rows, incomplete local VG
joins, unmatched local status/config rows, and visible alias-suppressed
candidates. Malformed LVM node scopes fail closed; unrelated storage types do
not need the field. These are
permission-filtered visible facts; hidden config/status rows cannot be ruled out.
Source behavior: [Config.pm](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Storage/Config.pm), [Plugin.pm](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Storage/Plugin.pm), [Storage.pm](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Storage.pm), [Status.pm](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Storage/Status.pm).

The existing private declared-store detail/config/permissions/content reads now
support either the existing LVM-thin path or a separately declared thick-LVM
candidate. The thin path and its snapshot-unverified booleans are unchanged. For
thick LVM, the inspector requires an exact candidate plus equality between the
detail config and visible config/status facts, then emits only an exact-match
boolean and fixed 256/512/1024-GiB reported-VG-headroom booleans. A mismatch or
ineligible row leaves them false. These facts neither change the private store
declaration nor establish backing locality, complete visibility, PBS readiness,
reservation, allocation authority, or write authority; all authorization and
physical-backing flags remain false. No API request is added.

The sanitized output contains aggregate visible-candidate counts and fixed
256/512/1024-GiB headroom counts. A count includes only a candidate whose
reported free extents after the hypothetical size retain at least 30% of
reported VG size. It does not identify/select a store, reserve capacity, or
authorize allocation. Proxmox's current [storage guide](https://pve.proxmox.com/pve-docs/pvesm.1.html)
states regular LVM allocates blocks when a volume is created; the [LVM plugin](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Storage/LVMPlugin.pm)
reports status from VG size/free and checks free extents before allocation.
The [storage config API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Storage/Config.pm)
and [node status API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Storage/Status.pm)
silently filter rows without per-store `Datastore.Audit` or
`Datastore.AllocateSpace`, so counts are visible-store facts and may be
incomplete. Explicit `shared=0` does not establish direct local physical media
or rule out thin allocation behind a SAN/LUN.

The existing private `proxmox_pbs_vm_storage_id` declaration still names the
thin store until a separate site-config review. Thick-LVM candidate counts do
not use or modify that declaration; if it still selects the thin store, the
separate snapshot-complete allocation audit remains required before
provisioning. Physical backing, device safety, filesystem readiness, PBS
readiness, and write authorization remain false; raw API data is protected by
`no_log`.

Dev task 2024 succeeded at reviewed SHA
`fc967ca1faf17e4fe31ce51dcce2e9227b625357`. Its sanitized receipt reported one
visible thick-LVM candidate with reported headroom and complete config/VG
mappings; physical backing remained unverified. This completes the visible-store
thick-LVM survey step only.

Task 2070 ran at reviewed SHA
`fc967ca1faf17e4fe31ce51dcce2e9227b625357`. Subsequent sanitized artifact/job
survey receipts report two non-PBS artifacts and one backup-job membership;
those inspect runs did not select an exact job ID. Multiple artifacts leave
artifact selection unresolved. No immutability or isolated restore was
verified; membership is not restore evidence.

## Candidate PV path lineage follow-on

The next code-only extension reuses the disk and LVM API responses already held
by the survey. For eligible visible thick-LVM candidates, compare each PV path
with the reported disk paths and, for a reported partition, its parent path.
Report only aggregate candidate-PV totals and counts of exact direct path
matches, exact partition-parent matches, exact path joins with absent `used`,
missing paths, unverifiable relations, and candidate VGs whose PV child list is
absent or empty. Malformed or duplicate disk/PV inventory refuses the sanitized
receipt; ambiguity is not reported as a count because duplicate paths are
malformed input. Candidate PV names that are not exact disk paths are
unverifiable; unrelated VG PV names retain the broader LVM inventory
validation. When `used` is present, only exact `LVM` is accepted; absent `used`
stays unknown and is reported separately from path lineage. Exact path joins
describe only the two API inventories. They cannot prove physical locality,
underlying media identity, safe device use, or filesystem readiness. No new API
request or write is added, and physical-backing, allocation, and write
authorization remain false. The implementation is present on
`dev` after PR #361. Dev-bound task 2085 ran at merged, reviewed SHA
`700de080ffd19d3eea1a8859bd1e8a73dbda2384` and a clean controller checkout.
It reported one candidate PV, one exact direct disk-path join with `LVM` use,
no other join or incomplete-inventory count, and one visible thick-LVM store
meeting each fixed reported-headroom check. Backing, allocation, PBS readiness,
and write authorization remained false. The path fields follow Proxmox's
[disk-list API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Disks.pm),
where `parent` is supplied for partitions, and its
[LVM inventory API](https://github.com/proxmox/pve-storage/blob/master/src/PVE/API2/Disks/LVM.pm),
whose child rows represent underlying PVs. The
[disk usage classifier](https://github.com/proxmox/pve-storage/blob/master/src/PVE/Diskmanage.pm)
reports exact `LVM` usage for paths recognized as PVs. Its disk scan admits
several block-device classes while excluding iSCSI, so this API path join
does not prove directly attached physical media. Separate physical-origin
evidence and a reviewed private store declaration remain required before
PBS VM disk allocation.


The 90d/45d retention target remains blocked: keep the effective 15d
Prometheus / 7d Loki / 168h Tempo settings and 0B Prometheus size cap until the
representative seven-day forecast, nonzero cap, at least 30% backing-filesystem
headroom, and immutable isolated-restore receipt pass review.

The backup-job reconciliation is a separate Dev-bound Semaphore workflow.
It reads the Proxmox job list before mutation, reports candidate job IDs only
in the private task receipt, and takes the selected `o11y_backup_job_id` from
private inventory. The selected job must be enabled and use an explicit VMID
list. A `PUT /cluster/backup/{id}` with only the unioned `vmid` membership is
safe for that mode; Proxmox clears `all`, `exclude`, and `pool` on a `vmid`
update, so those modes are refused. The playbook changes no schedule, storage,
retention, or other job option, reads the job back, and converges to a no-op
on a second run. Check mode issues no write. Backup artifact and isolated
restore validation remain independent follow-on gates.

### Read-only Proxmox scheduler snapshot

The inspect-only backup-job workflow also reads the declared node's
`pvescheduler` service state after verifying node-scoped `Sys.Audit`, using the
existing OpenBao-sourced Proxmox token. This is one read-only snapshot, not
evidence that a scheduled run occurred or produced an artifact. Validate the
exact service identity and field types, then emit only `running`,
`not_running`, or `unknown` with a fixed reason; permission gaps, request
errors, malformed data, and unrecognized state remain unknown. Raw responses
stay under `no_log`. Add no survey variables or credentials. This diagnostic
does not close the backup-job, artifact, immutability, isolated-restore, or
retention gates.

### Archived task history recency summary

The inspect-only archived `vzdump` summary adds exactly one sanitized field,
`latest_success_within_26h`. It reuses the latest task's validated `starttime`
and the existing archive query's `until` cutoff; it adds no API request and
does not expose either timestamp or a computed age. The cutoff must be a
positive integer (with booleans, strings, and other coercible values rejected).
The fixed window is 93,600 seconds, inclusive at both ends: the field is
`true` only for a classified success whose age is from zero through 93,600
seconds, and `false` for a known failure or warning, an older success, or a
verified empty archive. It is `null` whenever the existing outcome is unknown,
including permission, request, pagination, identity, schema, cutoff, or
conflicting-latest-status ambiguity. Existing outcome, reason, task count,
`job_attribution: unavailable`, the 30-day query, and inspect-only guards stay
unchanged. Task rows remain VMID-filtered and cannot establish which scheduled
job produced an archive; this field is not job attribution, artifact presence,
immutability, or restore readiness. An empty result or stale latest task means
only that no recent VMID-identified task was returned by this query.

### Bounded node-wide archived vzdump history diagnostic

Inspect mode MAY issue an additional read-only `GET /nodes/{node}/tasks` using
the existing OpenBao Proxmox token only after the existing exact-node
`/access/permissions?path=/nodes/{node}` check reports integer `Sys.Audit: 1`.
Zero, booleans, strings, missing rights, a different path, or a failed
permission response return `permission_unverified` without issuing the task
GET. The query
fixes `source=archive`, `typefilter=vzdump`, a 72-hour `since`/`until` window,
and omits `vmid`. Reuse the existing 50-row page size and 250-row total cap;
failed, malformed, truncated, or over-cap responses return a fixed `unknown`
reason with all counts and recency unknown. Validate every row's `vzdump`
type, unique task identifier, start time within the fixed window, task-ID
shape, and nonempty status before summarizing. Keep raw API data, task
identifiers, node, VMID, status text, and credentials under `no_log`.

The sanitized receipt may report only total recent node-level `vzdump` tasks,
the count whose task ID equals the declared VMID, and
`potential_multi_vm_or_unattributed_task_count`, which counts rows with a
missing or empty task ID. A missing task ID is not proof of a multi-VM run.
Every receipt fixes `job_attribution` to `unavailable`; it cannot prove the
selected job ran or included o11y. If the latest task outcome is unknown, return
the fixed `task_status_unavailable` unknown receipt with counts and recency
unknown. Task success is not backup success without a matching fresh artifact.
This is a source-only diagnostic, not live proof, and does not close parent
4.6e, immutable-artifact, isolated-restore, capacity, or retention gates.

## 2026-09-29 production disk-alert receipt

Non-destructive production deploy task 1830 and read-only budget task 1831
passed with retention unchanged at Prometheus 15d / 0B size cap, Loki 7d, and
Tempo 168h. At approximately 04:56 UTC, Grafana showed
`o11y_receiver_root_disk_low` firing with healthy evaluation while the
service-down rule remained normal. Grafana showed one notification routed to
the existing ops contact, and matching Discord delivery was independently
verified. This verifies the low-space warning and delivery path for this event.
It does not establish a seven-day capacity forecast, backup/restore readiness,
or delivery of other alert classes.

## 2026-10-10 exact artifact selection preflight

The read-only artifact survey accepts an optional private inventory selector with
exactly `volid`, `backend`, `format`, `creation_time_epoch`, and `size_bytes`.
It validates strict field types and schema, then requires those values to match
exactly one artifact in the complete VM/store enumeration. An absent selector
preserves the existing census; the survey never chooses the newest candidate.
Missing, duplicate, stale, or mutated selections fail with fixed sanitized
refusals. The protected inspector receipt adds only the selected candidate's
zero-based census index and already-sanitized metadata; it never returns its
identifier. The feasibility inspector receives that index and metadata and
revalidates them against the census while retaining the total candidate count.
Selecting an artifact does not establish immutability or restore success, and
the source VM's current `backup` flags cannot prove the selected archive's disk
contents, so `backup_inclusion_verified` remains false for a selected artifact.
Raw API rows, private selector values, and helper stderr remain protected from
visible task output. This work adds no Proxmox write, secret change, restore,
provisioning, or site-config declaration.

The root agent's sanitized read-only receipts 4293, 4295, and 4296 ran at exact
Dev SHA `7362aaa7080d87b91606c4d6a65f884dbf67b0b0`; the reported census had six
non-PBS `vma.zst` candidates. This count does not select an artifact or prove
archive disk inclusion, immutability, an isolated restore, or restore cleanup.

## 2026-10-10 private artifact capture and selector proposal slice

The public automation adds distinct Dev-bound capture and proposal playbooks.
Both use the existing artifact inspector and the same GET-only VM, node-storage,
backup-content, and VM-config census inputs. An exact existing proposal is
verified before any Proxmox request; new work validates the complete census and
stores a versioned catalog at the fixed private path
`catalogs/o11y/backup-artifacts.json`. The private catalog holds the exact
volid-to-row mapping, target identity, storage scope, runner-generated UTC epoch,
and reviewed public/private base SHAs. Its visible report contains only ordered
row numbers and sanitized metadata.

Proposal requires the full capture commit, explicit one-based row, reviewed
public revision, and reviewed private integration branch/base. The capture
commit must have one parent at that private base and change only the fixed
catalog. A new proposal repeats the GET-only census, requires age 0–1,800 seconds,
and compares complete normalized storage and artifact sets independent of API
ordering. It writes only `inventory/production.yml`, adding the existing
five-field selector to the literal sole-host location proposed for
`agent_cloud.children.o11y_svc.hosts`. The root, each mapping along that path,
and the target-host mapping must use block style; unrelated or descendant flow
maps and sequences and standard finite YAML floats are accepted. Recursive
validation refuses duplicate YAML keys, aliases, anchors, merge keys, custom
tags, nonfinite floats, alternate layouts, identity mismatch, an existing
selector, and any semantic diff beyond that selector. Task 4359 refused before
recensus and before a private write: after flow sequences were admitted, valid
nested flow maps in firewall lists and coverage records failed the global
mapping-style guard; after that guard was scoped, an unrelated gateway sampling
float failed the tag whitelist. In-memory private compatibility passed without
printing values; actual private-layout acceptance remains unverified.

The deterministic proposal ref is
`proposal/o11y-artifact-<capture-commit>-row-<row>`. A matching existing branch
is accepted only after its base, one-parent provenance, exact one-file diff,
commit metadata, remote tip, and reconstructed inventory bytes agree. It returns
the existing branch and commit without a new commit or freshness claim. The
shared Git helpers add optional reviewed-base and deterministic-ref handling;
legacy fresh-branch callers keep their existing mode. Base-tip checks before
and after push detect observed drift but are not atomic with the separate
proposal-ref update. A post-push drift refuses and preserves the unmerged branch.

Private site-config compatibility is not established by public source or
synthetic tests. Runtime must refuse any layout that differs from the proposed
literal schema. Catalog selection does not prove immutability, archive contents,
isolated restore, cleanup, or restore success; OpenSpec 4.6e remains unchecked.
## 2026-10-13 observed-window scrape-budget candidate preview

The Dev-bound baseline survey has an optional, source-only candidate preview.
It validates the existing bounded public receipt and the reviewed finite
`o11y_scrape_limits` inventory object, then compares all five observed maxima
for every fixed public job category. `receiver-host` and `host-node` use the
smaller of the global sample limit and the existing
`o11y_scrape_sample_limit` candidate (the committed source default is 2,000).
Strict headroom is required: an observed maximum equal to a candidate is
incompatible. Missing or unreviewed candidates produce a fixed unavailable
category without changing or rejecting the existing baseline receipt. The
preview returns only fixed status/reason values and fixed public job names; it
does not return private inventory values or measured maxima.

This preview is not live enforcement or acceptance of Prometheus's effective
loaded configuration. Reading a mounted config file would establish only its
on-disk contents, not that the running process loaded those bytes. The next
source slice establishes a bounded read-only status-config receipt for inline
jobs only; scrape-file jobs remain unverified. It does not upgrade Prometheus,
read mounted config as runtime proof, or claim runtime rejection.

The operational context recorded for this slice is Dev Alloy-health task 5057,
successful deployment check-mode task 5058, real prior deployment tasks 5037
and 5042, and baseline task 5062. Task 5062 reported 11 healthy targets, 24-hour
sample maxima of 3,059 for Loki and 2,285 for Grafana, three DGX-category
targets, and a 24-hour stored-series label maximum of 42. Response-body size is
measured by one point-in-time GET and remains distinct from the 24-hour sample
and series-label observations. These receipts inform candidate review but do
not close runtime acceptance or task 3.3.

## 2026-10-13 loaded inline scrape-limit coverage source slice

Extend the existing exact-SHA-guarded Dev scrape survey with a best-effort
loaded-limit receipt, separate from its required active-load baseline and the
source-only candidate preview. Use the pinned Prometheus v3.1.0 GET APIs
`/api/v1/status/buildinfo`, `/api/v1/status/runtimeinfo`, and
`/api/v1/status/config`. The pinned source exposes build `version`, runtime
`reloadConfigSuccess`/`lastConfigTime`, and config as a YAML string produced
from the in-memory config object. Bound every response with the survey's
existing API byte ceiling, timeout, curl transport, and protected helper task.
Read config twice between runtime snapshots; require version `3.1.0`, successful
reload state, identical process/config timestamps and config bytes. Any HTTP,
shape, size, parse, reload, or stability
failure returns only a fixed `unavailable` reason and cannot suppress the
existing baseline or candidate preview. The multi-request readback is not an
atomic Prometheus snapshot and cannot detect an unobserved change-and-revert
between matching observations; a matched result describes only the checked
readback point.

Parse the loaded YAML with duplicate-key rejection. Compare the five reviewed
inventory limits (`body_size_limit`, `sample_limit`, `label_limit`,
`label_name_length_limit`, and `label_value_length_limit`) against every
exposed inline scrape job after Prometheus's serialized effective defaults.
Use the active target's exact `scrapePool` association, not a guessed match
from labels; reject missing or ambiguous associations. Apply the existing
stricter `o11y_scrape_sample_limit` to the `receiver-host` pool. Missing,
invalid, or mismatched loaded values never pass. Emit only fixed overall and
per-public-category statuses/reasons; never include config, paths, target
labels, inventory values, raw responses, or exception details.

Prometheus 3.1.0's status-config handler serializes the main `Config` object.
Its `scrape_config_files` patterns do not expand into the serialized
`scrape_configs` list, so any pools supplied by scrape files (including
conditional DGX and host-node jobs) are explicitly `unverified`; do not read
mounted files or claim coverage for those jobs. This receipt demonstrates
only that exposed inline loaded values match reviewed candidates at the
readback point. It does not prove over-budget runtime rejection or close task
3.3. Preserve the 2026-10-13 observed-window baseline evidence in its separate
receipt and keep all task 3.3 acceptance gates open.

Verified live evidence supplied for this slice: Dev survey task 5194 at reviewed
SHA `ed547385` succeeded with 11 targets across nine public categories and a
compatible candidate preview for all nine. The task predates these source edits
and did not read loaded limits or prove runtime rejection; it validates only the
separate baseline and candidate-preview inputs. No live task or deployment is
part of this source change.

## 2026-10-13 alert-input availability source slice

Extend the same read-only baseline survey with an independent best-effort
availability receipt for the six fixed raw counter families referenced by the
provisioned Alloy receiver/exporter, Loki discard, and Tempo discard alerts:
`otelcol_receiver_refused_log_records_total`,
`otelcol_receiver_refused_spans_total`,
`otelcol_exporter_enqueue_failed_spans_total`,
`otelcol_exporter_send_failed_spans_total`, `loki_discarded_samples_total`,
and `tempo_discarded_spans_total`. Query each raw family without the alert
expressions' `or vector(0)` so absence remains visible.

Set `lookback_delta=5m` explicitly on every raw selector query and do not add
`or vector(0)`. An empty vector is reported as `missing_or_stale`; do not compare
the response `value[0]` (the query evaluation timestamp) with a local clock or
claim it is the original sample timestamp. Validate the vector response shape,
then join each returned `job`/`instance` privately against the exact active-target
set. Accept multiple distinct reason series; require well-formed unique label
sets and finite nonnegative counter values. Report one fixed public family/status/reason row per
family: `observed_zero`, `observed_nonzero`, `missing_or_stale`, or `unavailable`.
No labels, sample values/timestamps, selectors, URLs, response bodies, or
exceptions enter the receipt. A per-family query or validation failure does not
fail or replace the required active-load baseline, source-only candidate
preview, or loaded-limit receipt. Check mode reports the new receipt as
`check_mode_unverified`.

This is source-level availability evidence only. It does not prove pinned-image
metric exposition, controlled refusals/discards, alert firing or delivery, or
close task 3.3. Agentgateway and DGX split-file jobs remain `unverified`; do not
promote them into loaded-limit coverage.

Operator-supplied verified sanitized evidence for Dev survey task 5208 reports
11 healthy targets in nine public categories; seven inline categories matched
reviewed loaded limits, while agentgateway and dgx-spark remained unverified
split-file jobs. The independent source-only candidate preview was compatible
for all nine categories. This receipt predates the alert-input source edits and
does not validate their runtime behavior. No live survey or deployment is part
of this source change.

## 2026-10-13 split scrape-file source-drift receipt

Extend the same exact-SHA-guarded receiver survey with a separate read-only
source receipt for the exact generated `agentgateway.yml` and `dgx-spark.yml`
files in the receiver's declared deployment checkout. Open each file without
following symlinks; require a regular single-link file, bounded size, valid
UTF-8/YAML, no aliases or duplicate keys, and only the expected fixed job names.
Read only the five scrape-limit fields. Compare their finite positive values
with the reviewed inventory limits using the existing controller filter.

Keep raw bytes, paths, job names beyond the fixed public categories, and limit
values inside the protected survey task. Its visible result contains only
`agentgateway` and `dgx-spark` categories with fixed matched/mismatch/unavailable
statuses and reason codes. An unexpected path or filesystem object, missing
file, malformed YAML, duplicate or unexpected job, invalid limit, or inventory
mismatch does not pass. Check mode reports `check_mode_unverified`.

This receipt establishes only that the exact on-disk source files read during
the survey match the reviewed five-field limits. It does not show that
Prometheus mounted or loaded those bytes, that its runtime enforces the limits,
or that an over-budget scrape is rejected. Keep agentgateway and dgx-spark
`unverified` in the separate loaded-limit receipt, preserve all other survey
receipts, and leave task 3.3 unchecked. This is a source-only change; no live
survey or deployment is claimed.

## 2026-10-13 expected metrics target contract source slice

Keep the production rule path disabled until private inventory review sets
`o11y_expected_metrics_targets_reviewed: true`; the variable defaults false.
When production alerts are enabled and the flag is true, the declaration must
contain one to 100 active targets, or a non-empty explicit retirement set for a
retirement-only rollout. Each active row contains exactly `uid`, `job`,
`instance`, `service`, `cluster`, and `environment`. Use a stable UID in the
owned `o11y_missing_<suffix>` namespace. Identity values are bounded printable
ASCII restricted to letters, digits, `_`, `.`, `:`, `/`, `@`, and `-`; template
values are JSON-serialized into YAML and PromQL. Duplicate identities/UIDs,
invalid values, and more than 100 active or retired entries refuse. An
in-memory parse of the provisioned alert document then checks that rule UIDs
remain unique and retired UIDs do not collide with any active provisioned rule
before repository placement or file writes.

When the reviewed flag is false, the production template does not render
per-target rules even if unreviewed target values are present. Local mode keeps
its current Caddy pilot fallback and paused/routed behavior. The existing rule
uses all five exact matchers in `absent_over_time(up{...}[5m])`, holds for two
minutes, and uses `agent-cloud-ops` routing. `up=0` remains the endpoint-down
case; absence from the active target set is the disappeared-target case.
Explicit retired UIDs use the existing `deleteRules` list and are refused if
they match another rendered rule; there is no group-wide deletion.

Keep coverage as a separate read-only action, `Survey o11y Expected Metrics
Targets (Dev)`. It privately reads `/api/v1/targets?state=active`, validates
each target's five identity labels, includes unhealthy targets, and compares
unique exact tuples with the reviewed declaration. The receipt exposes only
status, a fixed reason, and expected/active/matched/missing/unexpected/unhealthy
counts. Invalid or ambiguous API identities return bounded unavailable output;
missing/unreviewed declarations and check mode are unverified. Private targets,
selectors, API payloads, URLs, and exception text stay under `no_log`. This
action does not alter the scrape baseline, loaded-limit, candidate-preview, or
alert-input receipts.

Sanitized Dev-bound read-only survey task 5221 observed 11 healthy active
targets across nine public categories. Four loss counters had current zero
samples; two optional error-only counters had no sample in a five-minute
lookback. The latter absence is not telemetry-loss evidence. This receipt
predates the expected-target survey and does not validate this source slice or
production rules. Production currently has alerts enabled without an expected
target declaration, so the reviewed flag remains false until the private
declaration rollout is separately reviewed.

Live acceptance requires a reviewed private inventory change, a reviewed
Dev-bound non-destructive Deploy o11y run, exact rule UID/readback, and a fresh
expected-target survey with all expected identities matched, no missing,
unexpected, or ambiguous identities, and unhealthy targets resolved. The config
digest change may recreate the receiver Compose project; do not clean-deploy,
delete volumes, or claim data continuity from a container restart. Task 3.3
remains unchecked; the task 5221 receipt is context, not live acceptance for
this implementation.

### Expected-target alert disable and restore transition

Grafana retains omitted provisioned rules, so turning off production alerts or
the reviewed-target gate SHALL keep each previously owned expected-target UID
in the generated file as a paused, unrouted `vector(0)` rule with generic
metadata. The prior file is the ownership record and is read before placement;
ownership requires the expected-target rule structure, exact identity labels,
and the `agent-cloud` / `service-telemetry` folder-group contract. A UID prefix
alone grants no authority. Before placement, live rules must have the exact
owned UID set and match the prior query, labels, pause state, notification
route, rule group, and the UID of the uniquely resolved `agent-cloud` folder.
Missing live rules, unowned live target UIDs, folder drift, or rule drift refuse.
Retired rules remain generic paused `vector(0)` tombstones, making ownership
durable across gate-off, canary restoration, and interrupted retries without
deleting live rules. A retired UID cannot be reactivated; a new identity needs a
new stable UID. Canary restoration and interrupted retries use the same
preparation and validation path. Render/readback tasks carrying private identities suppress
Ansible output and diffs; only fixed summaries and counts are visible. Check
mode performs source validation only, skips rendering, and does not claim a live
transition. Live acceptance remains separate; task 3.3 and the private rollout
gate remain open/off.

Alert probes and active-drill recovery also protect raw Prometheus/Grafana
responses and every identity-bearing assertion. Their visible success receipts
use fixed wording and contain no target labels or endpoints.

## 2026-10-14 bounded Proxmox backup-task failure diagnostic

Extend inspect-only backup reconciliation only after its exact-node
`Sys.Audit=1` permission check and complete bounded 72-hour node-task listing
pass. A unique latest failed `vzdump` task within 26 hours permits a read-only
task-log GET: read one line to obtain `total`, then fetch at most the last 100
lines. Proxmox `start` is a zero-based offset while returned `n` values are
one-based (`n = start + 1` for the first returned row); require contiguous
one-based numbering on metadata and final pages. Reject malformed UPIDs,
unknown statuses, ties, synthetic `no content` or `unable to open file` rows,
invalid numbering or schema, oversized output, and changed totals. Match the
upstream UPID shape exactly: validated node, eight-hex PID, eight- or nine-hex
process start, eight-hex start time, `vzdump`, an empty or decimal VMID, a
supported `user@realm` or `user@realm!tokenid`, and a final colon. Classify
status using Proxmox's normalization: `OK` is success, `WARNINGS: N` is
incomplete, exact `unexpected status` is unknown, and other nonempty status
text is failure ([`PVE::UPID`](https://raw.githubusercontent.com/proxmox/pve-common/master/src/PVE/UPID.pm#L15-L97)).
Keep task identity, status, raw log, URL, URI result, parser input/output, and errors
under scoped `no_log`; keep only sanitized history and diagnostic `set_fact`
results visible. Publish only fixed status/reason and `no_space`,
`storage_unavailable`, `permission_denied`, `guest_lock`, `transport`, or
`unknown` hints. Without an eligible unique recent failure, do not request the
task log.

At exact Dev merge `5b628700fccb2abdae1961428783a9e1c9f9a39e`, inspect-only task
5571 succeeded: scheduler running; one enabled explicit-VMID job included
o11y; VMID-filtered history count was one with no success in 26 hours; and the
72-hour node archive contained three tasks, zero task IDs matching the declared
VMID, three missing/empty IDs, and a latest failure within 26 hours. Job
attribution remained unavailable. Read-only artifact-details task 5572
succeeded with six dated candidates, the newest about 225.3 hours old, and all
protected flags unknown. No evidence ties that failure to the selected job or
o11y VM. Backup readiness, immutability, isolated restore, capacity, and
retention gates remain open.
