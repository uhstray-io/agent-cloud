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

### Requirement: Read-only journal positions diagnostics use a permanent local serialization inode
The Dev-bound bootstrap SHALL create `/var/lib/agent-cloud/o11y-journal-positions-diagnostic.lock` only when absent, as an empty root-owned single-link regular file with mode `0644`, on a verified supported local filesystem. Before any target creation, it SHALL use a bounded read-only `findmnt --kernel=mountinfo --json --target` query on the existing lock path or, when absent, its existing parent. After creation, it SHALL query the full lock-file path again. Each response SHALL contain exactly one well-formed result with an allowlisted filesystem and `/dev/` source; missing, malformed, nonlocal, or changed pre/post effective mount evidence SHALL fail closed. For an existing lock, final stat SHALL match its original device and inode. It SHALL verify the exact receiver checkout as its owning rootless account before the root-owned lock is created. It SHALL refuse unsafe pre-existing parent or lock objects and SHALL never unlink or replace the lock inode. Check mode SHALL perform no target writes. The explicit positions diagnostic SHALL open the inode read-only, verify the same inode before and after observation, and acquire a bounded exclusive flock while it runs the existing metadata survey. It SHALL not write positions state, run or mount a Podman container, or start a worker. The lock SHALL be documented and reported as diagnostic serialization only; it SHALL NOT authorize journal runtime actions or claim host-wide admission.

#### Scenario: Bootstrap proves the effective local mount and preserves an existing safe inode
- **WHEN** the Dev-bound bootstrap runs on a supported local filesystem
- **THEN** it creates the inode only when absent, leaves a safe existing inode unchanged, and refuses symlinks, non-regular files, wrong ownership/mode, hard links, or missing, malformed, nonlocal, ambiguous, or changed mount evidence
- **AND** an existing lock's final device and inode match the pre-creation stat
- **AND** check mode does not create the directory or lock

#### Scenario: Positions diagnostic serializes a read-only observation
- **WHEN** the explicit diagnostic action runs with a safe, uncontended lock inode
- **THEN** it opens that inode read-only, takes a bounded exclusive flock, observes positions metadata, and proves the inode identity stayed stable
- **AND** contention, a missing inode, or an unsafe inode fails closed without positions-volume writes, a container run, a mount, or a new worker
- **AND** apply, repair, proof, stop, and ordinary survey remain refused pending host-wide admission

### Requirement: Receiver journal positions state is surveyed and repaired safely
The repair clauses below retain the bounded historical repair contract; they SHALL NOT authorize touching the protected legacy volume or repairing/normalizing fresh state in the selected direct Podman workflow. Its preservation and creation-receipt requirements below take precedence.

The receiver-host journal collector SHALL preserve its existing Compose named positions volume untouched during the fresh-volume workflow below; the selected positions volume SHALL be mounted at `/alloy-state`, with Alloy `--storage.path=/alloy-state`. Every helper access probe, exact live-mount check, and rollback volume lookup SHALL require that same destination. A survey SHALL identify only the unique existing local volume with the exact expected name and receiver Compose project. A missing logical-volume label is allowed only for that exact name and project; a conflicting label SHALL be refused. The survey SHALL read bounded root/direct-child metadata in the rootless Podman user namespace without mounting the volume or reading positions content. Initialization flags SHALL come from a fixed-template named-volume read bracketed by JSON identity reads; JSON field presence SHALL remain distinct from effective flag values because false-valued fields may be omitted. Only literal template `false` or `true` values with matching inventory/named identity SHALL be accepted. Repair SHALL refuse shared or in-use volumes, ACLs, mount boundaries, symlinks, non-regular or multiply linked children, child ownership ambiguity, and unsafe modes except for the pending-only write-bit reduction defined below. For an initialized empty volume whose root ownership alone blocks collector access, repair MAY change only root owner/group to `0:0` after fresh checks, preserve mode/children, verify readback, and run the restricted isolated write/rename/delete probe. As a narrow exception, repair MAY do the same root-only change for the exact empty unused pending state `NeedsChown=false` and `NeedsCopyUp=true`; this branch SHALL not mount an access-probe container, and it SHALL verify empty contents and the exact expected root mode. Repair success SHALL NOT authorize first mount or collector apply; those require an explicit later task and full fresh pre-start checks. Pending repair SHALL compare the volume creation timestamp and stable volume/root identity across pre-mutation observations. After any pending-volume mutation, a failure SHALL preserve the volume and return bounded uncertain status; automatic mode or owner restoration is prohibited because a mount followed by removal cannot be ruled out from current state. Check mode SHALL report the positions-helper result as unverified without that helper's Podman or filesystem operations. Operator output SHALL use fixed categories and bounded counts. The survey's `pending_repair_diagnostic` SHALL additionally classify group write, other write, setuid, setgid, and sticky bits independently as `present`, `absent`, or `unverified`, using the same metadata snapshot and preserving the combined unsafe-bit category and exact `failed_checks` gate semantics. These five fields SHALL remain `unverified` when mode metadata is unavailable, including check mode and survey-exception receipts; they SHALL NOT alter repair acceptance.

The read-only survey MAY compare extra top-level directories only against the two exact public journal receiver and OTLP exporter component names. It SHALL emit fixed role counts bounded to 0 through 32 and, for each exact directory match, only categorical owner/access, ACL, mount, and direct-child state (`empty`, `nonempty`, or `unverified`). Classification SHALL use descriptor-relative no-follow opens and identity rechecks; unsafe links, races, and metadata errors SHALL report unverified or refuse. Unknown directory names SHALL remain aggregated and unidentifiable. Every extra directory, recognized or unknown, SHALL keep `component_layout=false`; these diagnostics SHALL NOT change pre-start or verify-live acceptance, inspect nested contents, infer queue state, or add a survey write, probe, or mount. The read-only Dev survey task 4009 at merged SHA `9bc543d7d0f80be77ddab3983f9db6a983257f06` observed one other directory in a three-entry pending volume but did not identify its purpose; live mount and namespace fields remained unverified/not run.

#### Scenario: Positions survey runs without collector initialization
- **WHEN** the explicit positions survey runs and the journal collector container is absent
- **THEN** it resolves one existing locally scoped named volume from receiver Compose labels, reports bounded ownership, mode-access, ACL, mount, child-type, and use-count categories, and does not create, mount, initialize, or read positions content
- **AND** when mode metadata is observed, `pending_repair_diagnostic` separately classifies group write, other write, setuid, setgid, and sticky bits as `present` or `absent`; missing mode metadata reports `unverified`, while the existing combined unsafe-bit field and exact `failed_checks` remain unchanged

#### Scenario: Extra positions directory is classified without changing acceptance
- **WHEN** the bounded read-only survey finds one of the exact public receiver or exporter component directories at the top level
- **THEN** it reports a fixed role count and only bounded owner/access, ACL, mount, and direct-child categories, using no-follow descriptor-relative observation and identity rechecks
- **AND** an unknown directory remains aggregated, and every extra directory keeps `component_layout=false`; the diagnostic does not infer queue state or authorize a start

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

### Requirement: First collector start is gated by safe positions-volume bootstrap
The collector SHALL have ONE lifecycle owner: idempotent Ansible direct rootless Podman tasks in the existing Dev-bound Semaphore journal playbook. The seven receiver services SHALL remain Compose-owned. The collector SHALL have no executable Compose start/mount fallback; ordinary receiver reconciliation/orphan cleanup SHALL NOT select it. First initialization SHALL require an independently verified matching selected direct Podman proof, explicit volume creation receipt and fresh preflight as required below. Earlier Compose bootstrap/repair assumptions are historical and SHALL NOT authorize fresh apply or old-volume mutation. Before start, the positions gate MAY return `bootstrap_allowed` for a missing expected volume only when bounded inventory is complete, no expected-name collision exists, and an all-container query proves the collector absent, including stopped containers. An existing fresh pending volume SHALL prove its owned creation receipt, one exact local identity, zero consumers and mounts, effective template flags matching the selected direct no-copy proof, and an observed empty `0:0` root with owner RWX, no group/other write or special bits, ACL, nested mount, or metadata ambiguity. The gate SHALL require at least 16 MiB available and 128 available inodes both on the Podman graph root and on the pending volume filesystem for this small first mount; this threshold is separate from the 30% retention headroom gate. Legacy survey label compatibility SHALL NOT authorize fresh-volume adoption; fresh project/logical labels and creation receipt SHALL match exactly. `bootstrap_allowed` for missing state SHALL authorize only guarded explicit creation, never container mount/start; receipted state SHALL pass repeated pre-mount checks outside rollback handling. After the first mount, `NeedsCopyUp=true` alone SHALL NOT prove success: post-start verification requires an exact live RW mount, namespace write/rename/delete, bounded allowlisted Alloy layout, the expected `loki.source.journal.o11y_alloy` cursor parsed from `positions.yml`, health, unchanged seven receiver containers, and a fresh exact-target Loki receipt. A still-pending flag may be accepted only when all these checks pass and the isolated selected direct Podman/image proof establishes the effective flag behavior; the flag alone does not establish image contents or provenance. Partial copy or unexpected layout SHALL refuse and preserve the volume; rollback removes only the pilot container without `-v`, never restores ownership or deletes copied state. Unexpected survey exceptions return bounded `survey_failed`; check mode remains unverified and nonmutating.

The helper SHALL expose separate pre-start `verify` and post-start `verify-live` actions. Pre-start MAY return `ready` without a cursor only when a complete observation proves initialization flags matching the selected direct proof, owned creation receipt, collector absent from all containers, zero consumers and mounts, exact safe volume identity, root `0:0`, access, GraphRoot and volume-filesystem capacity, no ACL or nested mount, a safe allowlisted empty/seed/component layout, and `positions.yml` explicitly absent. This result authorizes a bounded start attempt only. Present invalid or unreadable cursor files, unknown cursor presence, and incomplete or unsafe positions artifacts SHALL refuse. For this requirement, partial copy means a present invalid/unreadable cursor, pending temporary positions file, or layout outside the allowlist; proven pre-start absence is a separate retry case. `verify-live` SHALL require a present collector, exactly one consumer and mount, the exact expected RW volume mount, and a valid parsed cursor; it SHALL never return `bootstrap_allowed`. No gate may delete or replace positions state or automatically restore mode or ownership after pending mutation. An empty or allowlisted layout does not establish historical copy-up completion or cursor-loss history; replay/loss safety remains unverified.

#### Scenario: Missing expected volume is safe to initialize
- **WHEN** bounded volume and all-container inventories complete, no expected-name collision exists, and the named collector is absent
- **THEN** the pre-start gate may return `bootstrap_allowed` only to the guarded Ansible explicit-volume-creation step after selected direct proof readback; verified creation receipt and repeated pre-mount gates are required before direct Podman mounts it

#### Scenario: Existing empty volume is awaiting initialization
- **WHEN** one exact receipt-owned fresh local project volume has zero consumers and mounts, effective flags matching the selected direct no-copy proof, sufficient first-mount free bytes/inodes, and an empty unambiguous root with `0:0` ownership and required access
- **THEN** the pre-start gate returns `bootstrap_allowed`; any unknown identity, unsafe metadata, or conflicting label refuses

#### Scenario: Safe observed volume has no positions cursor before start
- **WHEN** flags match the selected direct proof, the owned creation receipt matches, the collector is absent from all containers, consumers and mounts are zero, capacity/access/identity/layout checks pass, and metadata explicitly proves `positions.yml` absent
- **THEN** pre-start `verify` returns `ready` for a start attempt, while `verify-live` refuses until a valid parsed cursor exists

#### Scenario: Present or unreadable positions cursor cannot use the retry path
- **WHEN** `positions.yml` is present but invalid or unreadable, or its presence cannot be determined
- **THEN** both pre-start and post-start verification refuse; only a proven absent cursor may use the bounded pre-start retry

#### Scenario: Survey raises an unexpected exception
- **WHEN** an unexpected exception interrupts the explicit positions survey
- **THEN** output contains only the bounded unavailable summary and fixed reason `survey_failed`, with no raw exception details

#### Scenario: Bootstrap gate passes but post-start proof fails
- **WHEN** the collector starts but the live RW mount, namespace write/rename/delete, bounded Alloy layout, valid expected journal cursor, health, unchanged receiver set, or fresh exact-target Loki receipt fails
- **THEN** the workflow refuses success and removes only the pilot container without `-v`, preserving the positions volume and any copied state

#### Scenario: Copy-up remains pending after first mount
- **WHEN** Podman still reports `NeedsCopyUp=true` after direct Podman starts the collector
- **THEN** the gate requires the complete allowlisted layout and parsed expected `positions.yml` cursor; selected direct Podman/image proof must explain the observed flag behavior, while partial copy or unexpected layout refuses without owner restoration or state deletion

#### Scenario: Loki contains only a stale matching stream
- **WHEN** the exact `o11y/alloy` container stream's newest receipt predates the current apply start time
- **THEN** the delivery gate refuses success and performs only pilot-container rollback while preserving the positions volume

### Requirement: Journal operations share host-wide admission and attributable durable intent
Journal survey, proof, apply, repair, stop and rollback SHALL share admission for the verified receiver host with every competing receiver lifecycle path that can affect these assets, across tasks and runtime user/storage contexts. Survey SHALL remain read-only against target volumes, collector and receivers and obtain a consistent admitted observation. Mutating operations SHALL retain exclusive admission through cleanup and acceptance readback. Unsupported participation, uncertain ownership or an unverifiable quiescent window SHALL block execution; repeated snapshots or a journal-only runner lock SHALL NOT satisfy this gate or imply atomicity against arbitrary operations outside controlled admission.

Before every mutation, including proof preparation and cleanup, the workflow SHALL persist and read back protected durable operation intent binding authenticated owner/task, exact reviewed checkout, independently derived consumed inventory/policy context, action, observed asset identities and mutation boundary. It SHALL reconcile intent with actual state after mutation, retain unresolved intent and uncertain assets on interruption, and refuse further mutation when persistence, readback or reconciliation is unavailable. It SHALL NOT authorize stale takeover from elapsed time, lease expiry, absent task-ID value or runner restart alone. Recovery SHALL require authenticated prior-task status, proof that prior host execution cannot still mutate, and intent/asset reconciliation under shared admission; terminal task status alone SHALL NOT establish quiescence.

Before helper or runtime operation execution, actual controller and target code SHALL match the exact independently reviewed, CI-green merged Dev checkout. This workflow SHALL remove moving-branch placement in the coupled implementation and refuse branch-name-only or post-execution verification. Proof, intent and creation receipts SHALL bind the independently observed consumed inventory revision/content and effective policy/config to authenticated run context; an echoed expected revision or caller digest SHALL NOT establish that binding. Attribution and protected durable receipt readback SHALL be independently authenticated; a Semaphore task-ID environment value, extra var, stdout claim or label alone SHALL NOT authenticate the task. Unverified mechanisms SHALL keep runtime proof blocked and apply/repair refused until implemented, independently reviewed and accepted in the same coupled PR.

#### Scenario: A competing receiver operation lacks shared admission
- **WHEN** survey or journal lifecycle is requested while a competing receiver deploy, reconcile, stop, clean/reset, orphan cleanup or network/volume path cannot participate in the same host-wide admission
- **THEN** execution refuses before runtime proof or lifecycle mutation; repeated receiver snapshots do not substitute for admission
- **AND** survey introduces no target volume, collector or receiver write

#### Scenario: Interrupted intent cannot be resolved by expiry alone
- **WHEN** an operation is interrupted or its admission expires with unresolved durable intent
- **THEN** another task refuses takeover until authenticated prior-task status, host execution quiescence and current owned-asset reconciliation are established under shared admission
- **AND** uncertain assets and intent are retained; terminal status or elapsed time alone cannot approve recovery

#### Scenario: Mutation intent cannot be persisted or read back
- **WHEN** durable intent persistence, protected readback or authenticated owner binding is missing or uncertain
- **THEN** no proof preparation, asset creation, lifecycle mutation or cleanup proceeds; unresolved intent and existing uncertain assets remain for reviewed recovery

#### Scenario: Checkout or consumed context is not independently established
- **WHEN** actual code differs from the exact reviewed merged Dev commit, only a moving branch is known, verification occurs after execution, or inventory/policy binding only echoes expected caller inputs
- **THEN** helper and runtime execution refuse without treating the expected SHA, revision or digest as observed evidence

#### Scenario: Task-ID claim cannot authenticate a receipt
- **WHEN** attribution relies only on a Semaphore task-ID environment value, operator input, stdout or labels, or independent authenticated receipt readback is missing, stale, partial or context-mismatched
- **THEN** proof/creation/intent attribution remains unverified and cannot authorize apply or repair

### Requirement: Fresh journal positions switch requires isolated direct Podman proof
The platform SHALL implement selected direct Podman lifecycle/proof, removal of the competing collector Compose path, proof/creation receipt authentication and the separately named positions switch in ONE coupled reviewed future public PR through the existing Dev-bound Semaphore workflow. Independent exact-head review and green final-head CI SHALL precede any runtime proof or apply. The fresh logical key SHALL be `journal-collector-state-v2` under the verified receiver Compose project, with the same `/alloy-state` storage path, consistent helper/apply/stop/rollback lookups and no arbitrary volume/project/network selector, external alias or Compose `name:` override. Exact physical volume names SHALL derive from the verified receiver project/rendered identity model and runtime labels; the collector SHALL attach only to the exact existing receiver `o11y` network name/ID with matching project labels and verified receiver membership. Missing/foreign/ambiguous network or project identity SHALL refuse without creating a substitute. Volume/container project labels `com.docker.compose.project` and `io.podman.compose.project`, the fresh `com.docker.compose.volume` logical label and reviewed collector config digest SHALL be consistent across creation, verification and cleanup; labels SHALL NOT confer lifecycle ownership or authorize broad cleanup. The legacy volume SHALL remain untouched: no mount, ownership/mode change, content/cursor operation, migration, deletion or automatic fallback. Read-only task 4038 at merged `a052555060b4c9ca5fb59fb44fa64906fc38bd69` is previously recorded independently read survey evidence only (not rerun for this reconciliation): initialization pending, collector absent, use count 0, three entries (`seed_file=1`, `journal_component=1`, `other_directories=1`), unknown count 1 fully verified collector-owned/RWX/ACL-absent/same-mount/empty, no cursor, failed component layout and live mount/UID proof not run. These facts SHALL NOT prove provenance, authorize ownership repair of the nonempty matching-root volume, or substitute for selected direct Podman proof.

The separately selected Dev-bound proof SHALL use the actual rootless Podman executable identity and client/server versions, resolved pinned Alloy v1.9.2 image digest/ID, UID/storage context, selected direct lifecycle/security/mount arguments, config digest and receiver-derived project/network context. The existing Compose provider copy-behavior subproof SHALL remain diagnostic only; neither its success nor its failure SHALL approve the selected production mount or select an alternative owner. Installed Compose behavior and selected direct-mount behavior remain unverified until independently observed; the direct production mount SHALL itself pass the full proof before apply. It SHALL compare ordinary copy and explicit no-copy on separate unused task-owned disposable volumes, observe the image destination independently, prove the selected direct RW/no-copy mount behavior with explicit guarded creation receipts and safe ownership/layout/RW access, and demonstrate a real expected journal cursor persisting and advancing after restart against a bounded synthetic journal fixture with a local sink. It SHALL run synthetic proof containers with networking disabled and an in-process local sink, observing receiver network identity read-only without attaching to it. It SHALL NOT mount old or fresh operational volumes, bind live journal data, export to receivers, change receiver services, publish a listener or use host networking/engine socket. Pinned-image config/sink/fixture support SHALL be validated; unavailable or inconclusive behavior SHALL refuse, including an empty image destination that cannot distinguish copy behavior unless a bounded independently observed nonempty benign destination from the SAME pinned image supplies a positive copy/no-copy control on separate disposable volumes, followed by actual `/alloy-state` first-mount verification. The image SHALL NOT be modified or derived for this control. The chosen fresh policy SHALL be explicit verified no-copy with no silent fallback. A new disposable-volume repetition SHALL pass. Cleanup SHALL target only independently reverified owned container IDs, volume identities/labels and scratch paths, with no foreign consumers or mounts; uncertain assets SHALL be retained. Before and after each operation, exact old-volume identity, effective initialization flags and stable root/bounded direct-child metadata (ownership, modes, ACL/mount boundaries and child identities) SHALL be verified unchanged without mounting or reading old contents/cursor. All seven receiver IDs/states/health, backend health and existing signal/active-alert baseline SHALL be verified unchanged. Incomplete, raced, unavailable or changed preservation evidence SHALL refuse acceptance; a count or running state alone SHALL NOT pass.

Apply SHALL independently read back a successful matching Dev-bound Semaphore proof task, binding task identity, reviewed merged SHA, inventory revision, direct Podman executable/version, image bytes, UID/storage context, receiver project/network identity and relevant config/security/lifecycle/mount policy; caller booleans, stale proofs, check mode and surveys SHALL NOT pass. Apply SHALL enforce the host-wide shared admission, durable pre-mutation intent, exact pre-execution checkout, derived consumed inventory/policy binding and authenticated task/receipt readback requirement above; proof, survey, repair, stop/rollback and competing receiver lifecycle SHALL participate without claiming atomicity against unrelated operations. Fresh preflight SHALL require complete bounded inventories, exact distinct old/fresh resolved names, project/logical labels, local driver/options, no collision, collector absence including stopped containers for first start/stopped-state resume, exclusive use/mount checks and sufficient GraphRoot/backing-filesystem bytes/inodes including proof/image peak use (at least 16 MiB/128 inodes reserved for first mount). An existing fresh volume SHALL match an owned creation receipt and pinned volume/root identity with safe ACL/mode/link/layout/cursor metadata; name/emptiness alone SHALL NOT authorize adoption. Pending state SHALL be exactly empty and match the proved ownership/flag policy, superseding any assumed copy-enabled `NeedsChown=false`/`NeedsCopyUp=true` pair for this fresh no-copy workflow. An already accepted live collector SHALL use no-op verification with exactly one verified consumer/mount and no foreign users rather than the absent-collector gate. Missing fresh state SHALL be explicitly created only by the guarded Ansible direct Podman workflow after proof and absent-name/collision preflight. Before container creation/mount, it SHALL persist and independently verify an owned creation receipt binding task/SHA/inventory, exact name, project/logical labels, driver/options, creation timestamp, mountpoint and root device/inode, then repeat complete metadata/use/mount/capacity checks. An interrupted creation, missing/unpersisted receipt, identity drift or implicit replacement SHALL preserve uncertain assets and refuse rather than adopt, recreate, delete or normalize them. A created container SHALL have its exact volume/network/security/mount policy verified before start; no implicit unreceipted creation SHALL pass. Preflight SHALL repeat immediately before mount outside rollback handling; proof or preflight failure SHALL leave collector/receivers unchanged.

The direct collector SHALL retain `0:0`, read-only root, all capabilities dropped, no-new-privileges, read-only declared journal/config binds, loopback readiness, bounded health schedule and restart policy. Recursive ownership/relabel shortcuts SHALL NOT be used. Every mutation SHALL have an exact identity/state read-guard and truthful change reporting; unexplained drift SHALL refuse adoption or force replacement. Post-start SHALL prove the exact fresh RW mount, effective `0:0` identity/access, strict production layout, parsed `loki.source.journal.o11y_alloy` cursor, automatic health/readiness, fresh exact-target Loki delivery after apply start bound to the observed `o11y-alloy` receiver ID and declared cluster/environment, and cursor persistence/continued delivery after collector-only restart. Synthetic or stale receipts SHALL NOT count. Private-ingress acceptance SHALL prove private receiver bind, source-scoped admission and unapproved-vantage denial with no published collector port, host network or engine socket; switch SHALL NOT mutate firewall. All seven receiver IDs/states, backend health and signal/alert baseline SHALL remain unchanged. A local-sink proof SHALL NOT pre-approve extra production component directories.

#### Scenario: Preservation or receiver baseline evidence is unavailable or changes
- **WHEN** old effective flags/root/direct-child metadata or any of the seven receiver IDs/states/health, backend health or existing signal/active-alert baseline cannot be verified or drifts
- **THEN** proof/apply cannot claim acceptance; missing baseline evidence blocks start, and post-start failure permits only admission/intent/identity-guarded collector cleanup preserving both volumes
- **AND** no metadata repair, receiver reconciliation or baseline weakening makes the proof pass

#### Scenario: Selected direct proof is missing or cannot distinguish behavior
- **WHEN** selected direct Podman/image/config capability is missing, no-copy is ineffective, copy behavior is inconclusive, or proof task/context/image identity does not match apply
- **THEN** apply refuses before collector or receiver changes; survey success and `NeedsCopyUp=true` do not imply provenance or test success

#### Scenario: Compose diagnostic cannot authorize direct lifecycle
- **WHEN** the Compose provider subproof succeeds, fails or remains unverified without a matching full selected direct Podman proof
- **THEN** apply stays blocked; the result is diagnostic only and cannot approve the direct mount, select another provider or relax the no-copy/layout/security gates

#### Scenario: Explicit creation is interrupted or competing ownership appears
- **WHEN** the fresh volume lacks a verified creation receipt, identity drifts before mount, or a Compose path/receiver cleanup can manage the collector
- **THEN** apply refuses, preserves any uncertain created asset and leaves collector/receivers unchanged; no name-only adoption, implicit replacement, broad project cleanup or old-volume operation is permitted

#### Scenario: Disposable proof completes and cleans only owned assets
- **WHEN** isolated first-mount cases, repeatability, expected journal cursor resume/advancement and identity-safe cleanup pass
- **THEN** a bounded independently attributable proof receipt is recorded, with the old volume untouched and all seven receiver IDs/states and baseline unchanged
- **AND** any cleanup identity race or foreign use preserves uncertain test assets and refuses success

#### Scenario: Fresh name collides or preflight becomes unsafe
- **WHEN** fresh identity is foreign/ambiguous, volume users/mounts exist, capacity or metadata is unsafe/unverified, or immediate pre-mount evidence drifts
- **THEN** apply refuses without adopting, repairing, deleting or selecting an alternate volume

#### Scenario: Fresh apply fails after first mount
- **WHEN** live mount/UID/layout/cursor/health/private-ingress/Loki/restart or receiver-baseline proof fails
- **THEN** rollback removes only the identity-verified collector without volume removal, verifies absence from restart sets, preserves BOTH volumes and all seven receiver IDs/states, and reports bounded uncertainty
- **AND** it never remounts the unsupported old volume or restores ownership/deletes fresh state to make a retry pass

#### Scenario: Fresh workflow is rerun
- **WHEN** the exact collector/config/image/fresh-volume is already accepted and healthy
- **THEN** apply verifies it and returns no-op without recreation; after stop, resume requires the same owned volume, safe exclusive metadata and valid cursor
- **AND** a proven absent cursor permits only one bounded retry under proved first-mount policy and accepted layout, never after a recorded valid cursor is lost; invalid/unreadable/unknown cursor or unexplained state requires reviewed recovery

#### Scenario: Fresh cursor changes replay and coverage window
- **WHEN** live apply begins with fresh positions instead of the old cursor
- **THEN** the receipt records switch time, journal retention/max-age and observed replay/gap uncertainty without claiming historical continuity or exactly-once delivery
- **AND** task 1.2 remains open until its metric/log/private-ingress/baseline receipts pass; the representative seven-day baseline starts only after live acceptance and later rollout/retention gates remain independent

### Requirement: Telemetry is private and bounded
Collection SHALL use declared private network paths and source-scoped firewall rules, protect credentials and sensitive content, and enforce per-signal retention and ingestion/cardinality budgets before broadening the rollout. Remote logs and traces SHALL enter through receiver Alloy; remote metrics SHALL use declared private scrapes unless another reviewed path is established. The receiver SHALL surface dropped or refused telemetry and low disk headroom as observable failures.

#### Scenario: Ingestion budget exceeded
- **WHEN** a target exceeds a declared scrape, log, or trace budget
- **THEN** the collection path limits or rejects the excess, records the affected target, and alerts without exposing a secret or request body

#### Scenario: Unapproved sender attempts export
- **WHEN** a sender outside the declared private allowlist attempts to reach an ingestion endpoint from a known vantage host
- **THEN** the source-scoped firewall denies it and the source is not enrolled by discovery alone

### Requirement: Capacity gates each rollout wave
The platform SHALL compare observed and forecast ingestion, storage, CPU, and memory demand with the declared o11y VM capacity before enabling each wave. When the forecast lacks headroom, the wave SHALL wait for a reviewed, non-destructive capacity change and post-change readback.

#### Scenario: Wave exceeds capacity
- **WHEN** the forecasted wave exceeds a declared capacity or retention budget
- **THEN** rollout stops before enabling new collection and reports the limiting resource and proposed VM-spec change

#### Scenario: VM capacity increased
- **WHEN** a reviewed VM resource declaration is converged through Semaphore
- **THEN** the live CPU, memory, hypervisor disk, and guest filesystem configuration is read back; any disk growth used an idempotent guest-growth workflow, and the existing telemetry and alert baseline remains accessible before rollout resumes

#### Scenario: Existing backup job includes the receiver
- **WHEN** an operator declares an existing Proxmox backup job for the o11y VM in private inventory and runs the reviewed Dev-bound reconciliation
- **THEN** the workflow verifies the exact job and VM, adds the VM only to an enabled explicit-VMID selection, preserves other job settings and members, verifies the readback, and reports a no-op on repetition; jobs selected by all, pool, or exclusion fail closed
- **AND** backup artifact presence and an isolated restore remain required before guest disk growth or longer retention

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
- **AND** it reports only `declared_thick_lvm_candidate_exact_match` and fixed `declared_thick_lvm_reported_vg_headroom_passes_{256,512,1024}_gib` booleans for an eligible exact match; absent, non-eligible, or inconsistent declarations leave these facts false
- **AND** fixed headroom booleans describe reported VG free extents after a hypothetical allocation retaining at least 30% of reported VG size; they do not reserve capacity or authorize allocation
- **AND** `storage_allocation_authorized`, `thick_lvm_allocation_authorized`, `thick_lvm_backing_media_verified`, `pbs_readiness_verified`, and `write_authorized` remain false, and no store ID, VG name, path, or exact capacity is reported

### Requirement: Rollout and recovery are reproducible
Instrumentation and receiver changes SHALL be declared as code, applied through reviewed `dev` and Semaphore, and verified per wave. A failed wave SHALL be reversible by an operator-driven reviewed declaration revert and redeploy without deleting existing telemetry volumes.

#### Scenario: New instrumentation fails validation
- **WHEN** a rollout wave fails its exact-target verification or receiver-health gate
- **THEN** rollout stops; an operator reverts the declaration through the branch workflow and redeploys it through Semaphore, preserves stored telemetry, and records the failed target and validation receipt
