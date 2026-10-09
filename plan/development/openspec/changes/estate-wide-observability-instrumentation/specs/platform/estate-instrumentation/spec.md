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

### Requirement: Receiver journal positions state is surveyed and repaired safely
The receiver-host journal collector SHALL retain its existing Compose named positions volume. A survey SHALL identify only the unique existing local volume with the exact expected volume name and a Compose project label matching the receiver. Its Compose volume label SHALL be `journal-collector-state` when present; a missing volume label is allowed only for that exact name and matching project, and a conflicting label SHALL be refused. The survey SHALL inspect bounded root and direct-child metadata in the rootless Podman user namespace without mounting the volume or reading file contents. A separate repair action MAY change only the volume-root owner and group to the collector's configured `0:0` identity after fresh evidence proves a root ownership mismatch is the supported access failure. Repair SHALL refuse shared or in-use volumes, initialization-required volumes, ACLs, mount boundaries, symlinks, non-regular or multiply linked children, child ownership ambiguity, and modes that cannot provide collector access after the root-only change. It SHALL recheck the volume identity and metadata immediately before mutation, preserve mode and child metadata, verify ownership by readback, and run an isolated create/write/rename/delete probe under the collector's rootless UID and filesystem restrictions. Check mode SHALL perform no mutation and report the result as unverified. Raw Podman, path, ACL, and filesystem diagnostics SHALL remain hidden; operator output SHALL use fixed categories and bounded counts.

#### Scenario: Positions survey runs without collector initialization
- **WHEN** the explicit positions survey runs and the journal collector container is absent
- **THEN** it resolves one existing locally scoped named volume from receiver Compose labels, reports bounded ownership, mode-access, ACL, mount, child-type, and use-count categories, and does not create, mount, initialize, or read positions content

#### Scenario: Positions repair is limited to a proven root ownership mismatch
- **WHEN** the operator selects `repair-positions` and fresh evidence proves the unused volume root owner alone blocks the configured collector identity while all supported safety checks pass
- **THEN** the workflow changes only that root's owner and group, verifies the preserved mode and child metadata, and requires the restricted isolated access test before reporting success
- **AND** any changed evidence, shared use, ACL, mount, symlink, child ambiguity, failed readback, or failed access test refuses success without recursive ownership/permission changes or volume deletion

#### Scenario: Positions check mode remains unverified
- **WHEN** survey, repair, or apply runs in Ansible check mode
- **THEN** the positions-specific result is `check_mode_unverified`, and the positions helper performs no Podman or filesystem operation

### Requirement: First collector start is gated by safe positions-volume bootstrap
The collector SHALL allow first initialization only through its declared Compose `up`. Before start, the positions gate MAY return `bootstrap_allowed` for a missing expected volume only when bounded volume inventory is complete, no expected-name collision exists, and a bounded all-container query proves the journal collector absent, including stopped containers. For an existing volume awaiting initialization, the gate SHALL prove one exact local project volume, zero consumers and mounts, exact boolean initialization flags, and an observed empty root owned by `0:0` with owner read/write/execute access, no group/other write or special bits, no ACL or nested mount, and no metadata ambiguity. A legacy volume-key label may be absent only when the exact expected name and project identity match; a conflicting label SHALL refuse. `bootstrap_allowed` SHALL be accepted only by the pre-apply and immediately-before-start gates; the latter SHALL run outside rollback handling. Post-start verification SHALL require `ready` and the existing live mount/access, health, receiver-preservation, and exact-target Loki checks. Unexpected survey exceptions SHALL return a bounded unavailable summary with fixed reason `survey_failed`; this receipt SHALL NOT be treated as a successful survey.

#### Scenario: Missing expected volume is safe to initialize
- **WHEN** bounded volume and all-container inventories complete, no expected-name collision exists, and the named collector is absent
- **THEN** the pre-start gate returns `bootstrap_allowed` and Compose `up` is the only operation that creates or initializes the volume

#### Scenario: Existing empty volume is awaiting initialization
- **WHEN** one exact local project volume has zero consumers and mounts, boolean initialization flags, and an empty unambiguous root with the required ownership and access
- **THEN** the pre-start gate returns `bootstrap_allowed`; any unknown identity, unsafe metadata, or conflicting label refuses

#### Scenario: Survey raises an unexpected exception
- **WHEN** an unexpected exception interrupts the explicit positions survey
- **THEN** output contains only the bounded unavailable summary and fixed reason `survey_failed`, with no raw exception details

#### Scenario: Bootstrap gate passes but post-start proof fails
- **WHEN** the collector starts but the live mount/access, health, receiver-preservation, or Loki check fails
- **THEN** the workflow refuses success and performs only collector-specific rollback while preserving the positions volume

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
