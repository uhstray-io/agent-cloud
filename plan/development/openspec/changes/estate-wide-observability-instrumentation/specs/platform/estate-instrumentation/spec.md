## Purpose

Provide complete, evidence-backed logs, metrics, and tracing coverage for Agent Cloud's deployed services and infrastructure while keeping collection private, bounded, and repeatable.

## ADDED Requirements

### Requirement: Coverage inventory reflects deployed estate
The platform SHALL maintain a reviewable coverage declaration for every deployed Agent Cloud service, agent, VM, and managed infrastructure target. Each entry SHALL identify its owner, lifecycle state, signal applicability, collection method, expected identity, and last successful verification receipt. Planned, retired, unsupported, and intentionally excluded targets SHALL be distinguished from missing telemetry.

#### Scenario: Deployed target lacks a signal
- **WHEN** a deployed target has no verified required signal or its receipt is stale
- **THEN** the coverage report identifies that target and signal as incomplete without counting a healthy sibling as proof

#### Scenario: Scaffold is not counted as deployed
- **WHEN** a source directory exists without a deployed target declaration
- **THEN** the coverage report lists it as planned or unclassified and does not assert live telemetry

### Requirement: Required telemetry is collected and correlated
Every deployed target SHALL declare applicable logs, metrics, health, and traces with a verified source or a reviewed exclusion reason. Supported host and application logs SHALL be queryable; metrics-capable targets SHALL expose verified metrics; request-serving targets SHALL provide traces when their runtimes support safe instrumentation. The same declared service identity and inventory-derived environment SHALL join applicable logs, metrics, and traces, and verification SHALL use a fresh signal from the exact target.

#### Scenario: Service passes three-signal verification
- **WHEN** a service is declared to require logs, metrics, and traces and emits a controlled request
- **THEN** verification finds a fresh matching log, a healthy named metric target, and a retrievable trace with the declared service identity

#### Scenario: Instrumentation is unsupported
- **WHEN** automatic tracing is incompatible with a target runtime or would violate its security boundary
- **THEN** the declaration records the reason and an approved alternative signal or manual instrumentation plan, without claiming trace coverage

### Requirement: Expected metrics targets are reviewed and compared exactly
Production per-target missing-telemetry rules SHALL be active only when
`o11y_expected_metrics_targets_reviewed` is true and production alerts are
enabled. The flag SHALL default false. Previously owned target UIDs SHALL remain
represented as paused, unrouted inert rules when either gate is off. An enabled declaration SHALL be bounded
and contain active identities or explicit retirements; each active identity
SHALL contain one stable rule UID and exact `job`, `instance`, `service`,
`cluster`, and `environment` values. Invalid or duplicate UIDs, duplicate
identities, unsafe matcher/YAML values, and collisions with other provisioned
rule UIDs SHALL fail before placement. Local behavior SHALL remain compatible.

Each missing-target rule SHALL match all five declared identity labels exactly,
use `absent_over_time(up{...}[5m])`, hold for two minutes, and use the existing
ops route when active. A target that is still active with `up=0` SHALL remain a
service-down condition; an absent active target SHALL be a disappeared-target
condition. Retirement SHALL retain the exact owned UID as a generic paused
`vector(0)` tombstone with no target labels or notification route; it SHALL NOT
delete a live rule by omission.

A separate read-only survey SHALL compare validated expected identities with
all active Prometheus target identities, including unhealthy ones. Its public
receipt SHALL contain only fixed status/reason values and bounded counts. Raw
labels, selectors, URLs, API responses, private inventory values, and
exceptions SHALL remain protected. Missing/unreviewed declarations and check
mode SHALL be unverified. This receipt SHALL remain independent of baseline,
loaded-limit, candidate, and alert-input receipts.

#### Scenario: Production target rules remain gated
- **WHEN** production alerts are enabled and the reviewed flag is false
- **THEN** no per-target missing-telemetry rules are rendered
- **AND** when the flag is true, an empty active-and-retired declaration or an invalid declaration refuses before placement

#### Scenario: Expected target is down or disappears
- **WHEN** the exact target remains active with `up=0`
- **THEN** coverage counts it as matched and unhealthy while the service-down rule remains responsible for endpoint failure
- **WHEN** the target is absent from active Prometheus targets
- **THEN** coverage counts it as missing and the five-minute absent rule evaluates independently

#### Scenario: Active target identity is unexpected or ambiguous
- **WHEN** an active target identity differs from the reviewed declaration
- **THEN** the survey reports bounded matched, missing, unexpected, and unhealthy counts without target labels
- **AND** duplicate active identities return a fixed ambiguous/unavailable result

#### Scenario: Retire one expected target
- **WHEN** a reviewed declaration removes one active target and names only its owned stable UID for retirement
- **THEN** the generated rule retains that UID as a generic paused `vector(0)` tombstone without identity labels or a notification route
- **AND** repeated deploy, gate-off, canary restoration, and recovery preserve that tombstone
- **AND** no live UID is deleted by omission

#### Scenario: Disable and re-enable expected-target alerts
- **WHEN** either the production alert switch or reviewed-target gate turns off
- **THEN** every safely owned expected-target UID remains provisioned as a paused `vector(0)` rule with generic labels and no notification route
- **AND** re-enabling restores the current reviewed rule for each previously owned UID, or requires explicit retirement before omitting it
- **AND** an explicitly retired UID remains inert across later gate changes and cannot silently become active again under the same UID
- **AND** malformed prior ownership, an unowned live target UID, or live drift refuses before placement or restart
- **AND** live folder UID, query, labels, pause state, and owned UID set match the validated prior rule contract
- **AND** unrelated rules remain unchanged

#### Scenario: Restore or retry a paused expected-target baseline
- **WHEN** canary restoration or an interrupted retry runs
- **THEN** it uses the same validated previous ownership and paused-rule representation
- **AND** protected rendering/readback emits no private identity in verbose or diff output
- **AND** check mode does not render or claim live transition verification

#### Scenario: Alert probe and recovery output protect private target identities
- **WHEN** the unreachable-endpoint probe or active-alert recovery reads Prometheus targets or Grafana rules
- **THEN** raw registered responses and identity-bearing consumers remain protected from normal and verbose Ansible output
- **AND** visible success output contains only a fixed receipt without target labels, endpoints, or rule payloads

#### Scenario: Expected-target survey is unverified
- **WHEN** the declaration is missing, unreviewed, empty, or check mode skips Prometheus access
- **THEN** the survey reports an unverified status rather than coverage success

## MODIFIED Requirements

### Requirement: Receiver journal positions state is surveyed and repaired safely
The receiver-host journal collector SHALL use a fresh, versioned Compose named positions volume `journal-collector-state-v3`, mounted at `/alloy-state` with long-syntax `volume.nocopy: true`, and Alloy SHALL use `--storage.path=/alloy-state`. Every helper access probe, exact live-mount check, and rollback volume lookup SHALL require that same destination and resolve only the v3 logical volume. Both legacy `journal-collector-state` (v1) and `journal-collector-state-v2` (v2) volumes SHALL remain untouched for later independent forensic/disposition review; no collector helper or Compose service SHALL inspect, admit, repair, delete, or mount either preserved volume. A fresh first-start gate SHALL permit `bootstrap_allowed` only when complete bounded inventory proves the v3 expected name absent and an all-container query proves the collector absent, including stopped containers. Any preexisting v3 volume without a valid parsed journal cursor SHALL refuse first-start bootstrap; a pending or cursorless volume SHALL NOT use repair or retry as an automatic recovery path. Existing v3 state may start again only after strict exact identity, access, layout, and cursor checks. Initialization flags SHALL come from a fixed-template named-volume read bracketed by JSON identity reads; JSON field presence SHALL remain distinct from effective values, and only literal template `false` or `true` values with matching inventory/named identity SHALL be accepted. Check mode SHALL report the positions-helper result as unverified without Podman or filesystem operations. Survey SHALL read bounded root/direct-child metadata in the rootless Podman user namespace without mounting v3 or reading positions content. Operator output SHALL use fixed categories and bounded counts. A fresh start can replay only Alloy's supported `max_age` window; it does not recover older journal entries. Source tests and parsed Compose output SHALL NOT establish Podman provider runtime behavior; separate reviewed runtime and exact-target Loki acceptance remains required.

The read-only survey MAY compare extra top-level directories against the exact `remotecfg` service name under the guarded exception below and the three exact configured component names: the public journal receiver, the `otelcol.processor.attributes.journal_labels` attributes processor, and the public OTLP exporter. It SHALL emit fixed role counts bounded to 0 through 32 and, for each exact directory match, only categorical owner/access, ACL, mount, and direct-child state (`empty`, `nonempty`, or `unverified`). Classification SHALL use descriptor-relative no-follow opens and identity rechecks; unsafe links, races, and metadata errors SHALL report unverified or refuse. Unknown directory names SHALL remain aggregated and unidentifiable. Except for the narrowly admitted empty `remotecfg` service directory below, every extra directory, recognized or unknown, SHALL keep `component_layout=false`; these diagnostics SHALL NOT change pre-start or verify-live acceptance, inspect nested contents, infer queue state, or add a survey write, probe, or mount. The read-only Dev survey task 4009 at merged SHA `9bc543d7d0f80be77ddab3983f9db6a983257f06` observed one other directory in a three-entry pending volume but did not identify its purpose; live mount and namespace fields remained unverified/not run.

#### Scenario: Positions survey runs without collector initialization
- **WHEN** the explicit positions survey runs and the journal collector container is absent
- **THEN** it resolves only the v3 locally scoped named volume from receiver Compose labels and never inspects or mounts either preserved v1/v2 volume, reports bounded ownership, mode-access, ACL, mount, child-type, and use-count categories, and does not create, mount, initialize, or read positions content
- **AND** when mode metadata is observed, `pending_repair_diagnostic` separately classifies group write, other write, setuid, setgid, and sticky bits as `present` or `absent`; missing mode metadata reports `unverified`, while the existing combined unsafe-bit field and exact `failed_checks` remain unchanged

#### Scenario: Stage, survey, and apply require the exact installed checkout before provider configuration
- **WHEN** the operator selects stage, read-only survey, or apply
- **THEN** stage uses the shared monorepo placement task pinned to `expected_repository_sha`, reads back the exact target `HEAD`, and refuses Ansible check mode
- **AND** stage does not start or recreate a container or inspect, mount, or change any positions volume; survey remains read-only and does not call placement
- **AND** each action requires the installed target `HEAD` to equal `expected_repository_sha` before running the installed Compose provider config parser; stale SHA refuses before provider configuration is read
- **AND** in Ansible check mode, provider runtime reads are skipped and the gate reports `check_mode_unverified`, never `verified`
- **AND** the parser runs with its full output protected and requires exactly one `/alloy-state` mount from `journal-collector-state-v3` with `volume.nocopy: true`
- **AND** the resolved configuration check does not claim that Podman applied the runtime mount behavior; exact v3 volume identity and runtime acceptance remain separate gates

#### Scenario: Extra positions directory is classified without changing acceptance
- **WHEN** the bounded read-only survey finds one of the exact public receiver, configured attributes processor, or exporter component directories at the top level
- **THEN** it reports a fixed role count and only bounded owner/access, ACL, mount, and direct-child categories, using no-follow descriptor-relative observation and identity rechecks
- **AND** an unknown directory remains aggregated, and each of these diagnostic-only component directories keeps `component_layout=false`; the diagnostic does not infer queue state or authorize a start

#### Scenario: Positions repair is limited to a proven root ownership mismatch
- **WHEN** the operator selects `repair-positions` and fresh evidence proves the unused volume root owner alone blocks the configured collector identity while all supported safety checks pass
- **THEN** the workflow changes only that root's owner and group, verifies the preserved mode and child metadata, and requires the restricted isolated access test before reporting success for an initialized volume
- **AND** the exact empty pending state `NeedsChown=false` and `NeedsCopyUp=true` may use a root-only pre-mount repair without an access-probe container
- **AND** any changed evidence, shared use, ACL, mount, symlink, child ambiguity, failed readback, or failed access test refuses success without recursive ownership/permission changes or volume deletion
- **AND** a failed pending-volume mutation preserves the volume and returns bounded uncertain status without automatic owner restoration
- **AND** free-byte and free-inode thresholds are checked independently from stable metadata equality: above-threshold counter changes do not invalidate root, child, ACL, or mount evidence, and below-threshold or unavailable counters refuse repair
- **AND** pending repair checks both GraphRoot and volume-filesystem capacity, while initialized-volume repair checks volume-filesystem capacity

#### Scenario: Empty pending root permits only write-bit reduction
- **WHEN** explicit pending repair freshly proves the exact empty unused unmounted `NeedsChown=false` / `NeedsCopyUp=true` state, absent collector, owner mismatch and blocked access, complete owner RWX, absent ACL and special bits, exact integer mode in `0..0o7777`, sufficient volume/GraphRoot bytes and inodes, and only `group_other_write_or_special_absent` fails because group/other write is present
- **THEN** repair SHALL reduce only the pinned root mode to `original_mode & ~0o022` before changing root owner/group to `0:0`, preserving all group/other read/search bits and empty contents
- **AND** each mutation SHALL use a no-symlink directory descriptor that matches the verified root device/inode; a full fresh identity, metadata, initialization, emptiness, ACL, use/mount and capacity discovery plus exact mode readback SHALL pass between the mode and owner changes
- **AND** repair SHALL perform no mount, content operation, collector start or recursive change; a converged rerun SHALL perform no further mutation

#### Scenario: Pending mode exception refuses an unrelated unsafe condition
- **WHEN** special bits are present, owner RWX is incomplete, mode is unknown/malformed, any unrelated guard fails, or state is initialized, nonempty, shared, mounted or in use
- **THEN** the pending mode exception SHALL refuse before mutation; existing initialized-volume behavior SHALL retain its mode-preservation boundary
- **AND** the combined diagnostic and original failed-check list SHALL remain truthful rather than reporting the planned reduced mode as an observed safe state

#### Scenario: Pending mutation cannot prove safe completion
- **WHEN** a check fails before mutation, or an operation/check fails after permission reduction or owner change may have occurred
- **THEN** pre-mutation refusal SHALL make no change; any postmutation failure SHALL preserve the volume and return bounded uncertain status without automatic mode/owner restoration or state deletion
- **AND** descriptor identity and fresh snapshots SHALL NOT be presented as proof that no intervening mount occurred; observed races refuse success and collector apply waits for separate authorization and fresh pre-start gates

#### Scenario: Positions check mode remains unverified
- **WHEN** survey, repair, or apply runs in Ansible check mode
- **THEN** the positions-specific result is `check_mode_unverified`, and the positions helper performs no Podman or filesystem operation; unrelated playbook checks are outside this guarantee

#### Scenario: Initialization schema diagnostics remain survey-only
- **WHEN** a unique exact positions volume identity has been established during the explicit survey
- **THEN** the survey may inspect that exact named volume a second time and query Podman client/server versions
- **AND** it emits only fixed presence/type categories, genuine boolean flag values, bounded named-query and identity categories, and validated bounded version strings
- **AND** malformed values, failed queries, malformed/duplicate named results, or invalid versions are represented only by fixed categories or `unavailable`; raw values, names, paths, labels, stderr, and exceptions are never emitted
- **AND** the additional second named-volume identity diagnostic and version query do not run for verify, repair, or apply; the required identity-bracketed initialization-flag read remains part of every gate and controls its decision
- **AND** check mode performs no Podman calls and reports the diagnostic fields as unverified/unavailable

#### Scenario: Pending-volume repair diagnostics preserve the gate
- **WHEN** the positions survey reports a pending initialization volume, or pending repair refuses with `pending_volume_unsupported`
- **THEN** both receipts expose the same fixed failed-check categories derived from the pending gate, including owner RWX, group/other write or special bits, and separate volume/GraphRoot byte and inode checks
- **AND** capacity values are only `sufficient`, `low`, or `unverified`; owner RWX and unsafe mode bits use fixed enums, and failed checks come from a fixed bounded allowlist
- **AND** the diagnostic adds no Podman call, probe mount, content read, or ownership change, and the refusal remains `status=refused`
- **AND** missing observed metadata, an unavailable root mode, or missing/invalid volume or GraphRoot capacity is reported as unverified or failed and cannot pass the initial checklist; a passing checklist does not replace later identity rechecks or authorize repair
- **AND** check mode and exception fallback retain the schema with unverified categories, without filesystem or Podman operations

### Requirement: Journal component diagnostics remain metadata-only and cannot admit state
The existing descriptor-safe read-only positions survey SHALL expose fixed bounded categories for the exact journal component's presence/type, owner match, owner RWX, forbidden write/special bits, link-count predicate, ACL, mount safety and direct-child layout. It SHALL separately classify the exact positions-file role's presence/type, owner match, required owner RW and forbidden mode bits, single-link predicate, ACL and mount safety, with bounded temporary-file counts and aggregated unknown direct-child kinds. Counts SHALL be integers in 0 through 32 or `unverified`; missing or unstable evidence SHALL remain unverified, never a passing observation. Descriptor-relative no-follow observation and parent/child identity rechecks SHALL reject symlinks, races, ACL/mount ambiguity and overflow. It SHALL NOT read file contents, enumerate deeper descendants, emit raw names/paths/modes/IDs/hashes/sizes/cursors/secrets/stderr/exceptions, create a probe, mount, mutate state or add a survey action. Public names used in this specification identify fixed roles, not permission to emit observed names.

The helper and playbook SHALL validate and expose only the bounded diagnostic schema through the protected parsing and summary boundary, retaining unverified check-mode and error fallbacks. The diagnostic SHALL NOT change any acceptance predicate, repair eligibility, bootstrap refusal, parsed-cursor requirement or live gate. An absent or unparsed survey cursor SHALL NOT be described as proven corruption. Failed startup, positions or delivery verification SHALL retain collector-only rollback and preserve all volumes. The rescue wording SHALL cover all three stages without claiming that a Loki query ran after an earlier positions failure. No automatic retry, repair, deletion or v4 bootstrap is permitted by this diagnostic follow-up.

#### Scenario: Journal metadata explains categories without reading positions content
- **WHEN** the explicit read-only survey observes the exact journal component in an unused v3 volume
- **THEN** it reports only bounded component/file metadata and structural categories, distinguishes absent from unverified, and aggregates unknown children without names or content reads
- **AND** existing cursorless-v3 pre-start and verify-live refusals remain unchanged even when every diagnostic metadata category passes

#### Scenario: Journal diagnostic ambiguity remains unverified
- **WHEN** metadata is missing, malformed, inaccessible, linked, ACL/mount-ambiguous, racing or over the observation bound, or execution is in check mode or exception fallback
- **THEN** the diagnostic retains its fixed schema with affected fields unverified or refusal, with no probe, mount, content read or mutation
- **AND** malformed helper output cannot leak through the visible playbook summary or become a passing gate

#### Scenario: Positions failure precedes delivery verification
- **WHEN** a healthy collector first fails the live positions readiness assertion before the Loki query
- **THEN** rescue describes a startup, positions, or exact-target delivery verification failure, removes only the pilot and preserves its volume
- **AND** neither the message nor the receipt claims a failed Loki query, successful delivery, root cause or permission for automatic retry

### Requirement: Seed and remaining layout diagnostics cannot authorize apply
The existing descriptor-safe read-only survey SHALL expose only categorical metadata for the exact seed-file role: presence/type, owner match, single-link predicate, owner-execute/group-other-permission/special-bit presence, ACL and mount safety. The group/other-permission category is observational and may include safe read-only bits. The diagnostic introduced in task 1.2c did not add an owner-RW requirement or change acceptance. It SHALL reuse existing root, top-level-role, journal/positions and temporary/unknown-child observations to account for remaining `component_layout` conditions, adding only missing fixed categories for seed refusal, nested child-count/role/file-metadata failure or incomplete/unstable observation. It SHALL NOT introduce another acceptance evaluator or turn independently passing metadata into root-cause proof; unexplained disagreement SHALL remain unverified.

Observation SHALL use the existing bounded descriptor-relative no-follow scan and stable parent/child identity, mode/link, ACL and mount rechecks. Counts SHALL be integers in 0..32 or `unverified`; enumeration SHALL stop on the 33rd entry and overflow SHALL remain unverified/refused. The diagnostic SHALL NOT traverse deeper, read file contents, emit observed names/paths/raw modes/IDs/link counts/sizes/hashes/cursors/secrets/stderr/exceptions, add a Podman call/probe/mount/action or mutate state. Exact schema keys, mapping shapes, enums and count types/bounds SHALL be validated inside the protected helper/Ansible parsing and summary boundary; boolean counts, missing/extra keys and injected text SHALL be rejected before visible output. Only validated safe categories SHALL reach the visible summary. Check-mode/error fallbacks SHALL retain the complete schema with affected evidence unverified and no check-mode runtime probes.

Task 1.2c SHALL remain behind an explicit production no-apply gate: source implementation and a reviewed, exact-revision read-only stage/survey may proceed, while source tests and diagnostics SHALL NOT authorize collector apply/retry, repair, delete/recreate, mount, content read, permission normalization, cursor synthesis, v4 bootstrap or fallback to v1/v2. All source acceptance, repair, parsed-cursor, bootstrap, verify-live and collector-only rollback predicates SHALL remain unchanged; v1/v2/v3 SHALL be preserved. This session launched and read back Dev-bound stage 6099 and survey 6100 through the Semaphore API; those receipts SHALL NOT establish root cause, cursor validity or delivery. Parent task 1.2 SHALL remain incomplete.

#### Scenario: Seed metadata distinguishes a layout condition without proving cause
- **WHEN** the survey observes the seed role in the existing unused initialization-pending v3 volume
- **THEN** it reports only fixed seed metadata categories against the unchanged predicates, distinguishing absent, unsafe and unverified observations without a content read
- **AND** a reported seed failure describes an observed condition, not its historical cause; passing seed categories do not establish cursor validity or readiness

#### Scenario: Remaining layout conditions stay bounded and unverified on disagreement
- **WHEN** `component_layout` fails despite passing journal/positions categories, or metadata races, errors, links, mount/ACL ambiguity or overflow prevent a complete stable explanation
- **THEN** the diagnostic reuses bounded existing observations and exposes only any missing fixed condition categories, marking unexplained disagreement or unstable evidence unverified
- **AND** it neither emits raw identifiers nor changes the failed-check list, acceptance or rollback decisions

#### Scenario: Seed diagnostic schema fails closed without apply
- **WHEN** helper fields are malformed, missing, extra or injected, counts are booleans/out of bounds, or execution uses check mode or error fallback
- **THEN** protected validation prevents raw output and preserves the fixed unverified/refusal schema before any safe visible summary
- **AND** no runtime check-mode probe, apply/retry, repair, mount, content read or state mutation occurs; existing cursorless-v3 refusal and v1/v2/v3 preservation remain unchanged

### Requirement: Alloy seed acceptance matches the pinned upstream read-only mode
The collector layout predicate SHALL accept only the exact owner-matching, single-link regular seed with absent ACL and mount boundary, absent owner execute and special bits, and absent group/other write or execute. Group/other read bits MAY be present. This changes no cursor, pre-start, repair, rollback or volume-preservation rule.

#### Scenario: Alloy's read-only seed mode is accepted without mutation
- **WHEN** the exact `alloy_seed.json` is an owner-matching, single-link regular file with absent ACL and mount boundary and mode `0644` or stricter read-only group/other bits
- **THEN** the helper SHALL accept its mode for `component_layout` while retaining the existing owner-execute, group/other write-or-execute and special-bit refusal
- **AND** the bounded diagnostic SHALL continue to report group/other permission presence without treating read-only bits alone as unsafe
- **AND** no seed chmod, content read, volume recreation, collector start or cursorless-v3 exception SHALL follow from this source correction

### Requirement: Cursor validity can be proved by an explicit read-only survey
The Dev-bound journal workflow SHALL expose a distinct `survey-cursor` action in its declared Semaphore survey, published through the scoped Dev-bound template workflow. It SHALL use the same exact reviewed revision, provider configuration, volume identity, protected output validation and fixed summary schema as ordinary `survey`. Only `survey-cursor` MAY invoke the helper's existing bounded positions-content parser; ordinary `survey` SHALL remain metadata-only. The explicit action SHALL report only fixed cursor validity/presence categories and no raw content, cursor value, error text or private path. It SHALL not mutate, mount, repair, start the collector or change the pending-state/pre-start/rollback gates. Check mode SHALL report unverified without a runtime read.

#### Scenario: Explicit cursor survey proves only a valid parsed cursor
- **WHEN** `survey-cursor` finds the exact safe positions file with stable descriptor identity and a valid expected journal cursor within the existing 64 KiB limit
- **THEN** it reports the fixed valid category and no cursor value or file contents
- **AND** the existing pending v3 volume remains blocked from collector apply until a separately reviewed safe-resume gate passes

#### Scenario: Unsafe or unreadable cursor remains unverified or refused
- **WHEN** the positions file is absent, oversized, malformed, unreadable, linked, wrongly owned, mode-unsafe, ACL/mount-ambiguous or changes during the bounded read
- **THEN** the action emits only fixed unavailable/refusal categories and preserves the volume and collector state
- **AND** ordinary `survey` and check mode never read positions contents

### Requirement: Only a safe empty remotecfg service directory may extend the positions layout
The layout observer SHALL conditionally accept at most one optional exact direct child `remotecfg` only when descriptor-relative no-follow observation and parent/child identity rechecks prove a real empty directory, namespace owner/group `0:0`, owner RWX, no group/other write or special bits, no access/default ACL, and no mount boundary or exact bind mount. Emptiness SHALL include hidden entries and SHALL be rechecked against the same identity. Unknown, malformed, inaccessible or racing evidence SHALL refuse. Absence SHALL remain allowed. Every other extra directory SHALL remain refused, including the diagnostic-only receiver, exporter and attributes processor. Fixed bounded role/count/state fields SHALL distinguish safe, unsafe and unverified exact `remotecfg` from aggregated unknown names in survey, check-mode and failure output without printing raw names or private paths. Survey SHALL remain read-only and SHALL NOT read cursor content, probe-mount, repair, create or delete state. Layout acceptance SHALL NOT relax cursor, identity, capacity, access, bootstrap or live gates; existing cursorless v3 SHALL refuse start, and v1/v2 SHALL remain preserved and excluded.

#### Scenario: Exact safe empty remotecfg is accepted only as layout
- **WHEN** `remotecfg` is absent or its exact optional directory passes every emptiness, ownership, mode, ACL, mount and stable-identity check, and the remaining layout passes
- **THEN** layout acceptance is permitted and bounded diagnostics distinguish it from unknown extras
- **AND** an existing cursorless volume still refuses pre-start; live success requires the parsed expected journal cursor and all other live gates

#### Scenario: Unsafe remotecfg or any other extra directory refuses
- **WHEN** the exact directory is nonempty (including hidden entries), linked, wrongly owned, unsafe in mode, ACL-bearing, mounted, inaccessible, malformed, unstable, or unverified, or any other extra directory exists
- **THEN** layout refuses without changing contents, permissions or ownership, with bounded diagnostics only

### Requirement: First collector start is gated by safe positions-volume bootstrap
The collector SHALL allow first initialization only through its declared Compose `up`, with explicit long-syntax `volume.nocopy: true` on the new v3 volume. Before start, the positions gate MAY return `bootstrap_allowed` only when complete bounded inventory proves the exact v3 expected name absent and an all-container query proves the collector absent, including stopped containers. Any existing v3 volume without a valid parsed journal cursor SHALL refuse bootstrap; neither initialization-pending state nor proven cursor absence authorizes a retry. A separate explicit repair action may operate only on the exact v3 identity and SHALL never resolve either preserved v1/v2 volume. For a missing v3 volume, the pre-start gate SHALL require at least 16 MiB available and 128 available inodes on GraphRoot before Compose creates it. It cannot check the new volume filesystem before that volume exists. Existing-volume checks use that volume filesystem's counters; pending-volume repair additionally requires the same thresholds on GraphRoot. This threshold is separate from the 30% retention headroom gate. The first mount remains outside rollback handling. After startup, post-start verification requires an exact live RW mount, namespace write/rename/delete, bounded allowlisted Alloy layout, the expected `loki.source.journal.o11y_alloy` cursor parsed from `positions.yml`, health, unchanged seven receiver containers, and a fresh exact-target Loki receipt. Any unexpected layout SHALL refuse and preserve the new volume; rollback removes only the pilot container without `-v`. Unexpected survey exceptions return bounded `survey_failed`; check mode remains unverified and nonmutating. Source validation SHALL NOT be represented as proof of Podman honoring `nocopy` or exact runtime volume identity.

The helper SHALL expose separate pre-start `verify` and post-start `verify-live` actions. Pre-start MAY return `bootstrap_allowed` only when complete bounded inventory proves the v3 volume absent and the collector absent from all containers, including stopped containers; this path checks GraphRoot capacity before Compose creates the volume. Any preexisting v3 volume without a valid parsed cursor SHALL refuse first start, including an empty volume, initialization-pending volume, or a volume whose cursor is explicitly absent, invalid, unreadable, or unknown. No cursorless pre-start retry or bootstrap of existing v3 state is permitted. Existing v3 state may start only when strict identity, access, layout, capacity, and cursor checks pass. `verify-live` SHALL require a present collector, exactly one consumer and mount, the exact expected RW volume mount, and a valid parsed cursor; it SHALL never return `bootstrap_allowed`. No gate may delete or replace positions state or automatically restore mode or ownership after pending mutation. An empty or allowlisted layout does not establish historical copy-up completion or cursor-loss history; replay/loss safety remains unverified.

#### Scenario: Missing expected volume is safe to initialize
- **WHEN** bounded volume and all-container inventories complete, no expected-name collision exists, and the named collector is absent
- **THEN** the pre-start gate returns `bootstrap_allowed` and Compose `up` is the only operation that creates or initializes the volume

#### Scenario: Existing empty v3 volume refuses first start without a valid cursor
- **WHEN** one exact local project volume has zero consumers and mounts, including an initialization-pending volume with an empty unambiguous root and no valid parsed cursor
- **THEN** the pre-start gate refuses; only a proven-missing v3 volume can return `bootstrap_allowed`

#### Scenario: Existing v3 volume with an absent positions cursor refuses before start
- **WHEN** initialization flags are false, the collector is absent from all containers, consumers and mounts are zero, capacity/access/identity/layout checks pass, and metadata explicitly proves `positions.yml` absent
- **THEN** pre-start `verify` refuses, and `verify-live` also refuses until a valid parsed cursor exists

#### Scenario: Populated pending v3 resumes only under reviewed Podman semantics
- **WHEN** the exact v3 volume remains initialization-pending with literal `NeedsChown=false` and `NeedsCopyUp=true`, has a nonempty allowlisted layout and a stable valid parsed cursor, and the collector is absent with zero users and mounts
- **AND** bounded identity, ownership, access, ACL, mount, volume and GraphRoot capacity checks pass under the reviewed local Podman 4.9.3 client
- **THEN** pre-start `verify` MAY return `ready` with a fixed pending-resume reason, without changing the volume or its flags; the same conditions SHALL be rechecked immediately before Compose mount
- **AND** any changed, unknown, cursorless, unsafe, differently versioned or incomplete observation SHALL refuse before start; post-start exact live mount, namespace access, valid cursor, health, unchanged receiver set and fresh exact-target Loki delivery remain mandatory

#### Scenario: Invalid, unreadable, or unknown positions cursor refuses without retry
- **WHEN** `positions.yml` is present but invalid or unreadable, or its presence cannot be determined
- **THEN** pre-start and post-start verification refuse; no existing-volume state may use a cursorless retry

#### Scenario: Survey raises an unexpected exception
- **WHEN** an unexpected exception interrupts the explicit positions survey
- **THEN** output contains only the bounded unavailable summary and fixed reason `survey_failed`, with no raw exception details

#### Scenario: Bootstrap gate passes but post-start proof fails
- **WHEN** the collector starts but the live RW mount, namespace write/rename/delete, bounded Alloy layout, valid expected journal cursor, health, unchanged receiver set, or fresh exact-target Loki receipt fails
- **THEN** the workflow refuses success and removes only the pilot container without `-v`, preserving the positions volume and any copied state

#### Scenario: Copy-up remains pending after first mount
- **WHEN** Podman still reports `NeedsCopyUp=true` after Compose starts the collector
- **THEN** the gate requires the complete allowlisted layout and parsed expected `positions.yml` cursor; an empty image destination may retain the flag, but partial copy or unexpected layout refuses without owner restoration or state deletion

#### Scenario: Loki contains only a stale matching stream
- **WHEN** the exact `o11y/alloy` container stream's newest receipt predates the current apply start time
- **THEN** the delivery gate refuses success and performs only pilot-container rollback while preserving the positions volume

### Requirement: Telemetry is private and bounded
Collection SHALL use declared private network paths and source-scoped firewall rules, protect credentials and sensitive content, and enforce per-signal retention and ingestion/cardinality budgets before broadening the rollout. Remote logs and traces SHALL enter through receiver Alloy; remote metrics SHALL use declared private scrapes unless another reviewed path is established. The receiver SHALL surface dropped or refused telemetry and low disk headroom as observable failures.

#### Scenario: Ingestion budget exceeded
- **WHEN** a target exceeds a declared scrape, log, or trace budget
- **THEN** the collection path limits or rejects the excess, records the affected target, and alerts without exposing a secret or request body

#### Scenario: Unapproved sender attempts export
- **WHEN** a sender outside the declared private allowlist attempts to reach an ingestion endpoint from a known vantage host
- **THEN** the source-scoped firewall denies it and the source is not enrolled by discovery alone

### Requirement: Capacity gates each rollout wave
The platform SHALL compare observed and forecast ingestion, storage, CPU, and memory demand with the declared o11y VM capacity before enabling each wave. A read-only forecast SHALL keep live retention readback distinct from private candidate retention and candidate Prometheus size-cap values; evaluating a candidate SHALL NOT change live retention, resize a VM, or enable a sender. The candidate 90d Prometheus / 45d Loki / 45d Tempo retention and nonzero Prometheus size cap SHALL be evaluated independently from the current live configuration. A passing forecast SHALL NOT authorize a retention change, resize, or wave. Current live retention and destructive/restore gates remain in force until their own reviewed changes and acceptance receipts pass.

The initial baseline SHALL start at the first successful exact-target receiver-host receipt and SHALL use an independently observed complete seven-consecutive-day window. The seven complete 24-hour UTC bins SHALL be contiguous and aligned to the first receipt. A window end SHALL be no older than 24 hours and live capacity/health readback SHALL be no older than 15 minutes. Require at least 95% of cadence-expected observations and no observed gap greater than one hour. Raw scrape timestamps SHALL remain distinct from Prometheus `query_range` evaluation timestamps; query evaluation times SHALL NOT establish distinct scrape observations. Prometheus sample counters SHALL NOT be treated as bytes. Loki and Tempo received-byte counters SHALL NOT establish end-to-end persisted bytes. A measured zero SHALL require an actual observation of zero; missing data SHALL produce `unavailable`, never zero. Missing, stale, incomplete, nonfinite, negative, or invalidly timestamped evidence SHALL produce `unavailable`. A representativeness assessment SHALL establish source/schedule coverage, known peak periods, and no invalidating outage, maintenance, or abnormal operation.

For each backend, forecast inputs SHALL include seven timestamped complete UTC 24-hour ingest totals and an isolated same-batch calibration of accepted input bytes, persisted bytes after normal compaction, and observed peak transient storage. The current shared running backends' read-only interface SHALL NOT be treated as proof of same-batch persisted-byte conversion or transient allowance. The planned calibration SHALL occur only after a verified backup and isolated restore, on a restored or disposable target with pinned runtime/configuration and no unrelated writes or retention deletion during measurement. Until an independently observed seven-day baseline and either repeatable isolated calibration or a separately proven conservative storage upper bound establish the required inputs, the forecast SHALL be `unavailable`. The conservative-bound route is a later reviewed alternative; an assumed multiplier or ratio SHALL NOT satisfy this requirement. The exact-target task receipt SHALL include a positive numeric task ID and be no older than 15 minutes. Source fixtures and standalone evaluator consistency checks SHALL establish source behavior only, not live measurements or capacity. Candidate retention SHALL remain fixed at 90d/45d/45d with a nonzero Prometheus cap. Keep normalized live retention and size-cap readback separately compared with declared live settings. Full projected retained bytes SHALL equal `(baseline peak ingest bytes/day + scaled wave ingest bytes/day) * candidate retention days * measured storage-conversion ratio + transient allowance bytes`; the candidate Prometheus cap SHALL be checked against this same full projection. Filesystem capacity SHALL charge each backend's full projected retained bytes against current free bytes, with no resident-byte credit unless a separately specified and independently verified resident-byte measurement proves it. This intentionally double-counts bytes already resident because current free space already includes them. Derive backend-volume filesystem groups only from the current complete five-volume receipt, sum each backend's projected bytes exactly once, and subtract the sum once per shared filesystem. Assume no deletion, cleanup, retention-expiry, or relocation credit. Require projected free disk >=30% on every affected filesystem, projected peak memory headroom >=25%, CPU p95 <70%, and no observed drops/refusals or unhealthy representative queries. An observed threshold breach SHALL be `refused`; missing required evidence SHALL be `unavailable`.

The receipt SHALL distinguish controller/deployed-receiver Git SHAs from the 64-character effective-inventory digest; these values SHALL NOT be substituted for one another. Only a trusted verifier SHALL bind task attribution, target, observations, and revisions to authoritative controller, deployment, inventory, and Semaphore state. Sanitized receipts SHALL omit private topology, raw backend responses, task/target identifiers, credentials, and exception text. Source fixtures/tests SHALL establish evaluator behavior only and SHALL NOT close baseline, calibration, or live acceptance gates. Live acceptance SHALL require a fresh read-only Dev-bound Semaphore receipt tied to reviewed source and effective-inventory evidence. Forecast evaluation SHALL NOT mutate retention, VM capacity, or senders. Any separately authorized resize SHALL still require VM/guest readback, verified backup and isolated restore, and telemetry-preservation gates.

When the forecast lacks headroom, the wave SHALL wait for a reviewed, non-destructive capacity change and post-change readback.

#### Scenario: Wave exceeds capacity
- **WHEN** the forecasted wave exceeds a declared capacity or retention budget
- **THEN** rollout stops before enabling new collection and reports the limiting resource and proposed VM-spec change

#### Scenario: Candidate retention is forecast without changing live retention
- **WHEN** an operator evaluates candidate retention and a nonzero Prometheus size cap
- **THEN** the verifier reports live readback separately from candidate values and performs no retention, VM, or sender mutation
- **AND** a candidate forecast pass alone does not authorize those changes

#### Scenario: Capacity evidence is incomplete or invalid
- **WHEN** any required seven-day stream is missing, stale, incomplete, nonrepresentative, nonfinite, negative, or invalidly timestamped
- **THEN** the forecast returns `unavailable` and does not substitute zero or produce a capacity pass

#### Scenario: Shared filesystem projection has no deletion credit
- **WHEN** two or more backend volumes share a backing filesystem
- **THEN** each backend's full projected retained bytes are summed once against that filesystem's current free space, with the shared filesystem charged once and no credit for resident bytes, hypothetical deletion, retention expiry, cleanup, or relocation

#### Scenario: Underfilled retention forecast is a synthetic evaluator fixture
- **WHEN** a synthetic evaluator fixture supplies seven complete, valid daily baseline totals peaking at 1 GiB/day, a synthetic calibrated storage-conversion ratio of 1, seven resident days, a 15-day live retention setting, a 90-day candidate retention, a 120-GiB filesystem with 113 GiB free, and no wave or transient allowance
- **THEN** the forecast SHALL charge the full 90-GiB candidate baseline against free space, leaving 23 GiB (19.17%) projected free, and refuse because this is below the 30% threshold
- **AND** the `refused` result is source-only and does not satisfy live baseline or calibration gates

#### Scenario: Capacity forecast source proof is not live acceptance
- **WHEN** a synthetic source fixture proves a refusal or unavailable result
- **THEN** the result establishes source behavior only; live acceptance remains pending until a fresh read-only Dev-bound Semaphore receipt is tied to reviewed revisions and valid private measurements

#### Scenario: VM capacity increased
- **WHEN** a reviewed VM resource declaration is converged through Semaphore
- **THEN** the live CPU, memory, hypervisor disk, and guest filesystem configuration is read back; any disk growth used an idempotent guest-growth workflow, and the existing telemetry and alert baseline remains accessible before rollout resumes

#### Scenario: Existing backup job includes the receiver
- **WHEN** an operator declares an existing Proxmox backup job for the o11y VM in private inventory and runs the reviewed Dev-bound reconciliation
- **THEN** the workflow verifies the exact job and VM, adds the VM only to an enabled explicit-VMID selection, preserves other job settings and members, verifies the readback, and reports a no-op on repetition; jobs selected by all, pool, or exclusion fail closed
- **AND** backup artifact presence and an isolated restore remain required before guest disk growth or longer retention

#### Scenario: Node-wide archived vzdump history remains bounded and unattributed
- **WHEN** inspect mode reads archived `vzdump` tasks for the declared Proxmox node using the existing OpenBao token after the exact-node permission response reports integer `Sys.Audit: 1`
- **THEN** the GET fixes `source=archive`, `typefilter=vzdump`, and a 72-hour `since`/`until` window, omits `vmid`, paginates in 50-row pages, and refuses totals above 250
- **AND** zero, boolean, string, missing, wrong-path, or failed permission responses report `permission_unverified` and prevent the task GET
- **AND** HTTP failures, malformed or duplicate task rows, out-of-window timestamps, invalid IDs/statuses, incomplete pages, and over-cap results report `status=unknown`, a fixed reason, and no counts or recency
- **AND** a complete response reports only the total node-level task count, count of task IDs equal to the declared VMID, `potential_multi_vm_or_unattributed_task_count` (the count of missing/empty task IDs), latest task outcome plus coarse recency, and `job_attribution: unavailable`
- **AND** an unknown latest task outcome reports the fixed `task_status_unavailable` unknown receipt with counts and recency unknown
- **AND** task status follows Proxmox's normalized status: `OK` is success, `WARNINGS: N` is incomplete, exact `unexpected status` is unknown, and other nonempty status text is failure; status text remains private ([`PVE::UPID::read_status` and `normalize_status_type`](https://raw.githubusercontent.com/proxmox/pve-common/master/src/PVE/UPID.pm#L64-L97))
- **AND** raw API responses, credentials, UPIDs, task IDs, user, node, VMID, and status text remain under `no_log` and do not enter visible or durable output
- **AND** a missing/empty task ID does not prove multi-VM work, and the result does not attribute a task to the selected backup job, prove that job ran or included o11y, or infer backup success without a matching fresh artifact; parent 4.6e, immutable restore, and retention gates remain open

#### Scenario: Recent unique node-task failure gets a bounded sanitized diagnostic
- **WHEN** inspect mode has passed exact-node integer `Sys.Audit: 1` and complete bounded pagination of the existing 72-hour node-wide `vzdump` listing
- **THEN** only one unique latest failed task within 26 hours may trigger a read-only task-log GET; the workflow first reads one metadata line for `total`, then reads at most the final 100 lines; `start` is a zero-based offset and returned row numbers are one-based (`n = start + 1` through `start + limit`)
- **AND** the UPID must match the validated inventory node, exact `vzdump` type, eight-hex PID, eight- or nine-hex process start, eight-hex start time, empty or decimal VMID, supported `user@realm` or `user@realm!tokenid`, and terminal colon; malformed fields, path characters, unsupported identities, missing permission, incomplete or drifting pagination, unknown status, tied latest tasks, zero-based or otherwise invalid log numbering/schema, synthetic `no content` or `unable to open file` rows, oversized responses, or changed totals fail closed
- **AND** exact `unexpected status` and any missing or non-string status fail closed as unknown; other nonempty Proxmox error text may select the bounded metadata/log GET but is never printed; if no unique recent failed task exists, no log GET occurs
- **AND** output contains only fixed status/reason values and a conservative hint from `no_space`, `storage_unavailable`, `permission_denied`, `guest_lock`, `transport`, or `unknown`; sanitized history and diagnostic `set_fact` summaries stay visible while task identity, status text, raw log lines, URL, node, VMID, and exceptions remain protected and are never published
- **AND** the diagnostic does not attribute the failed node task to a scheduled job or the o11y VM and does not satisfy backup readiness, immutability, isolated restore, capacity, or retention gates

#### Scenario: Failed task identity and selected destination remain sanitized
- **WHEN** inspect mode has a unique recent failed node task and reads its one-line metadata response
- **THEN** the workflow parses only the exact `INFO: starting new backup job: vzdump ` prefix with `shlex.split` as a parser, never executes it, and accepts options only as `--name value` or `--name=value`, consuming a standalone option's following token literally even when it begins with `-`
- **AND** malformed option names, empty or missing option values, short options, and positional VMIDs after options fail closed; `--job-id` and `--storage` are compared only when they are actual option names, never when they occur inside another option's value
- **AND** the declared VMID is compared against unique exact positional VMIDs, with at most one `--job-id` and one `--storage` compared against the private declarations
- **AND** before comparing any identity, every protected backup-list row has one well-formed unique ID and the unique selected row's stable configuration matches the selected detail; malformed or duplicate list identities, missing/duplicate selection, detail drift, malformed/duplicate options, synthetic or oversized lines, API errors, or unavailable private details return unknown/null comparisons
- **AND** inspect mode reads `/nodes/{declared node}/storage` with the existing protected token and reports only the exact selected job destination's state and coarse free band (`zero`, `below_10_percent`, `10_to_30_percent`, `at_least_30_percent`, or `unknown`) after matching exactly one active row declaring `backup` content
- **AND** selected destination capacity is read only after a valid, uniquely listed private job is selected; its detail must match that list row's stable configuration. Missing, ambiguous, malformed, permission-invisible, or incomplete selected destination data returns unknown; no other storage row supplies the selected destination's capacity
- **AND** raw command text, IDs, VMID, storage ID, path, URL, UPID, log line, exact capacities, API response, and errors remain private; the free band is point-in-time evidence and does not prove a full backup fits or that `no_space` came from this destination
- **AND** existing final-100-line failure classification remains unchanged, and backup execution, immutable copy, isolated restore, VM growth, and longer-retention gates remain open

### Requirement: Artifact selection is exact and remains private
The read-only artifact survey MAY accept an optional private selector containing exactly the artifact volid, backend, format, creation epoch, and byte size. It SHALL match every supplied value against exactly one enumerated artifact for the declared VM and its source storage. With no selector it SHALL preserve census behavior and SHALL NOT infer a choice from recency. Malformed, missing, duplicate, stale, or changed selections SHALL fail closed with a fixed nonprivate refusal. Visible output SHALL omit artifact identifiers, selector values, raw API rows, and helper stderr. A selected result MAY expose only its census index and sanitized candidate metadata to the feasibility inspector; the original census count SHALL remain intact. Current VM configuration SHALL NOT be treated as proof of the selected archive's disk contents. Artifact selection SHALL NOT establish immutability, isolated restore, or restore success.

#### Scenario: Operator selects one exact artifact from multiple candidates
- **WHEN** private inventory contains a complete selector that matches one enumerated artifact exactly
- **THEN** the survey reports the same candidate census count and passes only the verified index and sanitized candidate metadata to feasibility
- **AND** stale or ambiguous selection refuses without printing private values, while `backup_inclusion_verified` remains false for the selected artifact

#### Scenario: Capture and propose a private artifact selector
- **WHEN** an operator captures a reviewed GET-only census and later proposes an explicit one-based row from its private capture commit
- **THEN** capture writes only the fixed versioned catalog path on a fresh branch from the reviewed private base and reports sanitized numbered metadata without artifact identifiers
- **AND** proposal verifies capture provenance, requires a 0–1,800-second capture, repeats the census, and compares complete normalized storage and artifact sets independent of API order
- **AND** proposal writes only the production inventory selector at the literal sole-host location; the document root, each mapping along `agent_cloud.children.o11y_svc.hosts`, and the target-host mapping are block-style, while unrelated or descendant flow mappings and sequences and standard finite YAML floats are accepted
- **AND** recursive validation refuses duplicate keys, aliases, anchors, merge keys, custom tags, nonfinite floats, any existing selector, and semantic changes beyond the new selector; original bytes outside the insertion remain unchanged
- **AND** Dev-bound proposal task 4359's parser refusals occurred before recensus or private inventory writes; in-memory private catalog/inventory compatibility passed without printing values, while private-layout acceptance remains unverified
- **AND** an exact deterministic proposal rerun returns its existing branch/commit without a new commit or freshness claim; conflicting content is preserved and refused
- **AND** separate pre/post-push integration-tip checks report and preserve observed cross-reference drift without claiming atomic ref updates
- **AND** private inventory compatibility, immutability, backup contents, isolated restore, cleanup, and restore success remain unverified and do not complete OpenSpec 4.6e

### Requirement: Backup storage preparation is evidence-gated
The platform SHALL use a reviewed, Dev-bound, GET-only survey before considering local backup-storage preparation. The survey SHALL source the node from private inventory, verify that it is uniquely online, and emit only allow-listed aggregate facts and coarse reported-capacity bands. Missing usage data SHALL remain unknown; survey output SHALL NOT identify a device as safe, select a disk, establish physical locality or filesystem readiness, or authorize a write.

#### Scenario: Physical storage survey reports structural facts only
- **WHEN** an operator runs the reviewed physical-storage survey for the privately declared online node
- **THEN** it reports aggregate disk, partition, usage-class, LVM, thin-pool, managed-directory, and storage-status facts without exposing device paths, serials, storage or filesystem names, or exact capacities
- **AND** it performs only GET requests and reports device safety, filesystem readiness, PBS suitability, and write authorization as false

#### Scenario: Declared LVM-thin storage reports snapshot-unverified visible-volume headroom
- **WHEN** the survey reads the private VM-image storage declaration and Proxmox reports its node storage status
- **THEN** it reports only fixed `snapshot_unverified_visible_volume_preflight_passes_*` booleans for 256, 512, and 1024-GiB proposed virtual disks, based on the unfiltered visible `images` and `rootdir` content rows, exact declared-store prefix, unique supported `vm-`/`base-` volume IDs, raw format, and positive sizes; it requires exactly one active, non-shared `lvmthin` row configured for exactly both `images` and `rootdir`, exact storage-config linkage to one thin pool, and matching pool/status total and used bytes
- **AND** each preliminary boolean also requires the visible virtual-size sum plus the proposal to retain at least 30% of logical pool size, a hypothetical full write to retain at least 30% reported pool availability, and current thin-pool metadata free space to retain at least 30% of metadata size
- **AND** it verifies effective `Datastore.Allocate` and `Datastore.Audit` permissions on the exact declared storage before treating the visible-volume listing as permission-complete; absent rights in a valid permissions response leave `visible_volume_permissions_verified` and every preliminary boolean false
- **AND** it always reports `snapshot_inventory_complete_verified=false` and `storage_allocation_authorized=false`, because upstream LVM-thin listing excludes `snap_*` LVs; VM provisioning requires a separate reviewed snapshot-complete audit gate, and these preliminary booleans are not allocation guidance
- **AND** missing/malformed private declarations or failed required API requests may stop the task before a sanitized receipt; duplicate or malformed volume IDs, unsupported content, inactive/shared storage, inconsistent values, or incomplete visible rows fail closed; no storage ID, exact capacity, volume ID, or reservation is reported
- **AND** exact pool/status `used` equality is enforced across separate GETs; if concurrent writes make them disagree, the survey refuses with no tolerance and must be rerun; API request details remain hidden by `no_log`

#### Scenario: Visible thick-LVM image stores report VG headroom facts only
- **WHEN** the reviewed survey reads the private, uniquely online storage node's status, storage configuration, and LVM volume-group inventory
- **THEN** it validates the optional API-encoded `nodes` field only for LVM/LVM-thin configs and treats an omitted restriction as applying across nodes; an explicit comma-separated list applies only to its named nodes
- **AND** it joins only local-applicable visible config rows to the declared node's status and VG rows; foreign-node LVM/LVM-thin configs do not participate in local VG joins or candidate selection
- **AND** visible alias exclusion compares each local candidate against all visible LVM/LVM-thin config rows, including foreign-node rows with the same `vgname`, because backing identity is not established by node restriction
- **AND** it maps only enabled active `lvm` rows with explicit `shared=0`, `images` content, and one unique `vgname` match; a config row with `disable=1` is excluded even if status reports active
- **AND** every visible config row has a unique valid storage ID and a non-empty string type (types may repeat); invalid/duplicate IDs or missing/non-string types fail closed, while incomplete local-applicable visible LVM/LVM-thin VG mappings suppress only thick-LVM candidate and headroom counts and aggregate diagnostic counts remain available
- **AND** any other visible LVM/LVM-thin config row naming the same VG excludes the candidate, even if it is inactive, shared, lacks image content, or is foreign-node scoped
- **AND** `thick_lvm_config_vg_mappings_complete=false` whenever a local-applicable LVM/LVM-thin config row lacks a valid `vgname`, references a VG absent from the node inventory, or lacks same-type target-node status (including when disabled), or a target-node status row lacks a matching same-type local-applicable config row; a matching foreign-scoped config/status pair marked `enabled=0` is excluded from local completeness
- **AND** incomplete local mappings suppress only thick-LVM candidate and headroom counts; aggregate diagnostic counts remain available
- **AND** the receipt reports only aggregate `thick_lvm_foreign_lvm_config_row_count`, `thick_lvm_local_config_vg_join_incomplete_count`, `thick_lvm_unmatched_local_status_row_count`, `thick_lvm_unmatched_local_config_row_count`, and `thick_lvm_visible_alias_suppressed_candidate_count`; it does not disclose node, storage, or VG names or IDs
- **AND** disagreement between a counted candidate's status total/used/available and linked VG size/free extents fails closed
- **AND** optional config `content` is required and parsed only for matching active, explicitly non-shared LVM image candidates; absent content on unrelated rows does not invalidate the survey
- **AND** it reports only aggregate visible-candidate counts and fixed 256/512/1024-GiB headroom counts; a size counts only when reported VG free extents after that hypothetical allocation retain at least 30% of reported VG size
- **AND** `storage_allocation_authorized`, `thick_lvm_allocation_authorized`, `thick_lvm_backing_media_verified`, `device_safety_verified`, `filesystem_readiness_verified`, and `pbs_readiness_verified` remain false; results do not select a storage or reserve capacity
- **AND** `/storage` and node storage status may silently omit rows without `Datastore.Audit` or `Datastore.AllocateSpace` on the store, so counts describe visible stores and may be incomplete; `shared=0` does not prove local physical backing or rule out thin allocation behind a SAN/LUN
- **AND** no storage/VG name or ID, exact capacity, device path, serial, or private topology is reported; raw API responses remain hidden by `no_log`

#### Scenario: Dedicated selector reports one candidate ID for private review
- **WHEN** an operator runs the separate Dev-bound candidate-identification workflow
- **THEN** it imports the existing GET-only physical-storage survey and reuses its protected API responses without issuing duplicate Proxmox reads
- **AND** it reports one candidate storage ID only when visible config/VG mappings are complete, every candidate PV has complete exact disk-path lineage with reported `used=LVM`, and exactly one candidate meets the reported 1024-GiB VG-headroom threshold
- **AND** ambiguous candidates, malformed or incomplete inventory/lineage, or insufficient headroom produce one fixed refusal without private identifiers or topology
- **AND** the regular aggregate survey continues to emit no storage IDs, and the selector emits no node, VG, PV, path, serial, or exact capacity
- **AND** backing media, device safety, filesystem readiness, PBS suitability/readiness, allocation, write authorization, immutable restore, VM growth, and retention remain unverified or false; the selector makes no Proxmox mutation and does not complete tasks 4.6c, 4.6d, or 4.6e

#### Scenario: Candidate thick-LVM PV path lineage reports inventory facts only
- **WHEN** the survey has a non-empty set of eligible visible thick-LVM candidates
- **THEN** it joins candidate VG PV `name` values against the already-fetched disk inventory by exact path and reports aggregate PV count, exact direct disk-path joins, exact partition-to-parent joins, `thick_lvm_pv_path_join_unknown_used_count`, missing paths, unverifiable joins, and candidate VGs with missing or empty PV-child inventories
- **AND** a candidate PV name that is not an exact disk path is unverifiable; unrelated VG PV names retain the broader LVM inventory validation
- **AND** any PV-path join with a present `used` class counts only when that class is exactly the literal `LVM`; a different present value is unverifiable, and absent `used` remains unknown while the exact path join describes inventory lineage only and is counted separately
- **AND** duplicate or malformed disk/PV rows fail closed instead of producing an ambiguous count; a partition whose parent is absent or is not a reported whole-device path is unverifiable
- **AND** these exact path relationships do not establish local physical media, backing-device safety, filesystem readiness, PBS suitability, allocation, or write authorization; all corresponding verification/authorization fields remain false
- **AND** no PV, disk, partition, parent, or storage identifier, path, serial, or exact capacity is exposed, and no Proxmox request is added for this calculation

#### Scenario: Private declared thick-LVM store reports fixed reported-headroom facts
- **WHEN** the private declared PBS VM-image store is an LVM-thin store or an eligible thick-LVM store
- **THEN** the existing LVM-thin snapshot-unverified path retains its current behavior, while the thick-LVM path requires an exact match between the private declared storage ID's detail config, its visible cluster config, its target-node status row, and the linked VG candidate facts
- **AND** it reports `declared_thick_lvm_candidate_in_visible_eligible_set`, `declared_thick_lvm_required_config_fields_match`, `declared_thick_lvm_optional_config_fields_match`, and `declared_thick_lvm_status_content_matches_config` as fixed-schema booleans that decompose the exact-match diagnosis without exposing identifiers
- **AND** required `content` and optional `nodes` lists are validated for non-empty, unique, whitespace-free well-formed members, then compared as sets regardless of order; missing-versus-present `nodes`, malformed lists, and changed sets do not match
- **AND** `type`, `vgname`, and optional `disable` retain literal value and presence comparisons
- **AND** `declared_thick_lvm_candidate_exact_match` remains fail-closed and is true only when the declared candidate is in the visible eligible set and all three config/status comparisons match; absent or non-eligible candidates leave the diagnostic booleans false
- **AND** it reports fixed `declared_thick_lvm_reported_vg_headroom_passes_{256,512,1024}_gib` booleans only for an eligible exact match; mismatches leave these facts false
- **AND** fixed headroom booleans describe reported VG free extents after a hypothetical allocation retaining at least 30% of reported VG size; they do not reserve capacity or authorize allocation
- **AND** `storage_allocation_authorized`, `thick_lvm_allocation_authorized`, `thick_lvm_backing_media_verified`, `pbs_readiness_verified`, and `write_authorized` remain false, and no store ID, VG name, path, or exact capacity is reported

### Requirement: Rollout and recovery are reproducible
Instrumentation and receiver changes SHALL be declared as code, applied through reviewed `dev` and Semaphore, and verified per wave. A failed wave SHALL be reversible by an operator-driven reviewed declaration revert and redeploy without deleting existing telemetry volumes.

#### Scenario: New instrumentation fails validation
- **WHEN** a rollout wave fails its exact-target verification or receiver-health gate
- **THEN** rollout stops; an operator reverts the declaration through the branch workflow and redeploys it through Semaphore, preserves stored telemetry, and records the failed target and validation receipt
