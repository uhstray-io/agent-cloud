## Purpose

Restore confidence in NetBox discovery through evidence-based diagnosis, safe credential recovery, and read-only per-source acceptance and ongoing monitoring.

## ADDED Requirements

### Requirement: Evidence distinguishes history and failure stages

The system SHALL report each enabled source separately with observation time, source identity, deployed revision/configuration identity, tested stage, verdict, and non-secret evidence reference. It MUST distinguish missing secret path or required field, denied path access, vault authentication/transport failure, source authentication/transport failure, failed collection, successful empty collection, failed ingestion/reconciliation, missing expected objects, and insufficient evidence. Historical operator reports MUST remain labeled as history and MUST NOT become measured live status. An untested downstream stage MUST be reported as unknown rather than inferred from an upstream failure.

#### Scenario: Partial vault access does not establish a root cause
- **WHEN** one credential path can be read but a discovery path is denied or cannot be inspected
- **THEN** the report identifies the denied or unknown path and does not claim that its credential is absent merely because another path succeeded

#### Scenario: Distinct failures remain distinguishable
- **WHEN** independent diagnosis cases exercise missing fields, denied reads, rejected source authentication, unreachable endpoints, failed collection, empty collection, and rejected ingestion
- **THEN** each case produces its corresponding stage-specific verdict, preserves unknown downstream stages, and none is reported as recovered

### Requirement: Verification has no hidden mutations

Diagnostic and acceptance runs SHALL be read-only against NetBox, source devices, discovery configuration, credentials, and deployment state. They MUST NOT repair GPS data, create canary objects, mint credentials, trigger scans, deploy containers, or alter source enablement. They SHALL exit unsuccessfully when a required query fails or evidence is missing. Creating bounded non-secret run output and telemetry is permitted; it is not authority to change observed systems.

#### Scenario: Missing GPS remains unchanged
- **WHEN** verification observes a Site without GPS coordinates or encounters a failed NetBox query
- **THEN** the Site remains unchanged, no other repair is attempted, and the failed query yields an unsuccessful result with its diagnostic stage

### Requirement: Source selection is explicit and fail closed

The enabled-source set, allowed targets, schedule, runtime bound, ingestion deadline, credential-consumption deadline, freshness threshold, and expected-object declarations SHALL be validated configuration. Proxmox, pfSense, network, and SNMP SHALL retain their current enabled status unless explicitly changed through reviewed configuration. Missing or invalid configuration, an empty enabled set, and missing credentials MUST NOT produce an overall recovered verdict. An explicitly disabled source SHALL appear as disabled with its recorded reason, never as successfully recovered. Missing credentials MUST NOT silently disable SNMP.

#### Scenario: Enabled SNMP has no usable credential
- **WHEN** SNMP remains enabled and its credential is absent or denied
- **THEN** SNMP fails its prerequisite and overall recovery remains incomplete while independent sources can still be diagnosed

#### Scenario: Approved disablement remains visible
- **WHEN** the operator approves a reviewed configuration that disables SNMP with a reason
- **THEN** the rendered configuration omits that source and its unused credential references, the report displays disabled with the reason, and all other enabled sources retain their full acceptance gates

#### Scenario: Invalid source declarations refuse execution
- **WHEN** enablement is malformed, no sources are enabled, timing bounds are invalid, targets are out of scope, or an enabled source lacks an expected-object declaration
- **THEN** execution refuses with a configuration verdict and does not scan, seed credentials, or claim recovery

### Requirement: Credential recovery validates and preserves state

Recovery SHALL validate required nonempty fields, a matching identity-and-secret pair, the source's read-only authentication, and the agent's required path access before selecting a credential. It MUST NOT substitute a hardcoded identity, invent a credential, replace a differing working credential implicitly, or overwrite unrelated destination fields. Writes SHALL be limited to owned fields and protected against concurrent modification. Repeated identical recovery SHALL be a no-op; failures and interrupted retries MUST preserve the last verified credential and recover without duplicate credential creation. Replacement requires an explicit approved rotation following create, verify, select, retire.

#### Scenario: Invalid candidate leaves working state intact
- **WHEN** a source credential lacks a required field, has a mismatched identity, or fails source authentication
- **THEN** recovery refuses selection and preserves the destination credential and every unrelated field

#### Scenario: Concurrent sibling update survives recovery
- **WHEN** a valid credential is seeded while another writer changes an unrelated destination field
- **THEN** the unrelated change survives, a conflicting owned-field update refuses or retries from fresh state, and an identical subsequent run performs no credential write

#### Scenario: Interrupted rotation can resume safely
- **WHEN** recovery stops after candidate creation or verification and is retried
- **THEN** it reuses and revalidates the recorded candidate or safely reports the unresolved state, does not mint a duplicate, and does not retire the last working credential before successful selection

First recovery after credential selection SHALL independently prove consumption of the selected version by the running agent, followed by successful source authentication and the accepted cycle, within a positive finite configured consumption deadline. Non-secret evidence SHALL record selected KV version, selection time and consumption time; when native consumption evidence is unavailable, an explicitly authorized recorded reload of that version followed by authentication from the agent is required. Missing evidence or a cycle using an older version MUST NOT pass. Retries MUST reuse the original selection/deadline. First-recovery freshness SHALL include this consumption allowance in addition to the cycle budget; steady-state freshness MUST NOT repeatedly extend itself by that allowance.

#### Scenario: Old or unobserved credential consumption cannot pass
- **WHEN** a credential was selected but a cycle uses an older version, consumption cannot be proved, or the consumption deadline expires, including after retry
- **THEN** first recovery remains non-healthy without automatic reload or deadline reset, and acceptance requires timely consumption evidence and successful agent source authentication for the selected version

### Requirement: Secrets stay within the credential boundary

Credentials SHALL originate in OpenBao or approved operator provisioning into OpenBao and SHALL never appear in public artifacts, command arguments, persisted survey/extra-variable payloads, unredacted output, or failure traces. Credential-bearing tasks SHALL suppress their sensitive output while emitting separate non-secret verdicts. Runtime AppRole material SHALL be accessible only to its owning runtime identity; permission checks SHALL verify the rendered file and containing directory. Agent permissions SHALL enumerate required paths without widening access to unrelated services. Non-secret health and verification output MUST remain visible.

#### Scenario: Credential failure is diagnosable without disclosure
- **WHEN** credential resolution, source authentication, or templating fails with a synthetic secret present
- **THEN** captured success and failure output contains no secret value, operators receive a stage-specific verdict, and runtime credential files have owner-only access

### Requirement: Every enabled source proves collection and ingestion

Recovery SHALL require, for every enabled source, a successful bounded collection with complete declared scope, correlated completed ingestion/reconciliation evidence, and a fresh read-back matching its expected NetBox object projection. Mere submission, a running container, existing aggregate object counts, or another source's recent write MUST NOT satisfy the gate. Partial collection, truncation, duplicate or ambiguous object identity, and unresolved ingestion errors SHALL fail acceptance. A successful empty collection SHALL be explicit and SHALL fail when configured expected objects are absent. Production qualification MUST include at least one source-attributed expected object per enabled source; configuration cannot waive this requirement.

#### Scenario: Collection succeeds but ingestion fails
- **WHEN** a worker collects expected entities but submission is rejected, reconciliation fails, or matching NetBox objects are absent after the deadline
- **THEN** collection is reported successful, ingestion or read-back fails separately, and overall recovery fails

#### Scenario: Existing objects do not prove this cycle ran
- **WHEN** old or manually created objects match the expected names but the current source has no fresh correlated collection and ingestion evidence
- **THEN** recovery remains unknown or failed and does not use those objects as proof of a completed cycle

#### Scenario: Unchanged successful cycle is healthy
- **WHEN** an unchanged source completes a new collection and ingestion cycle and its expected projection matches a fresh NetBox read-back
- **THEN** that source passes without forcing an object edit or relying on the object's modification timestamp advancing

#### Scenario: Partial or ambiguous results do not pass
- **WHEN** collection omits part of its scope, an API page is missing, identifiers collide, or a supposedly successful empty result lacks a required object
- **THEN** that source fails acceptance with the affected stage and no automatic deletion or reconciliation repair is attempted by verification

### Requirement: Freshness and alerting are independent per source

Monitoring SHALL retain source-specific last attempt, last complete collection, last completed ingestion, last successful expected-object check, and current verdict in bounded telemetry outside authoritative IPAM. Freshness SHALL use the source's configured cadence and bounded execution/ingestion allowance, never a global latest-write timestamp. Stale, missing, future-dated, or untrustworthy evidence SHALL be non-healthy. A scheduled read-only verifier SHALL detect expiry within its declared polling interval. State transitions into failed, stale, or unknown and back to healthy SHALL be delivered through an operator-approved notification route; repeated unchanged healthy state SHALL remain quiet. Missing verifier executions or unavailable telemetry MUST become observable rather than preserving a stale green status.

#### Scenario: Healthy pfSense cannot hide stalled Proxmox
- **WHEN** pfSense continues completing cycles while Proxmox passes its freshness deadline
- **THEN** Proxmox becomes stale, overall recovery is non-healthy, and a source-specific alert is delivered within one verifier polling interval plus the declared bounded notification delivery allowance

#### Scenario: Quiet success and meaningful recovery notifications
- **WHEN** a source remains healthy, then fails, then recovers with complete fresh evidence
- **THEN** unchanged healthy checks are quiet and the failure and recovery transitions are each observable through the configured route

#### Scenario: Lost monitor is not a permanent green
- **WHEN** scheduled verification stops executing or required telemetry becomes unreadable
- **THEN** an independent scheduler or existing monitoring check detects the missing heartbeat within its declared bound and reports monitoring unavailable without asserting pipeline health

### Requirement: Recovery is reproducible and scoped

Recovery operations SHALL run through version-controlled Semaphore workflows with OpenBao-managed credentials and the configured container runtime. They SHALL record the exact code/configuration revision and avoid repointing shared repository records. Repeated deployment and verification SHALL converge without duplicate objects or credentials. Rollback SHALL restore only the owned configuration and verified credential selection, preserving existing NetBox data and unrelated services. Native Linux network/SNMP qualification MUST NOT be replaced by macOS app-tier or mocked tests. Local container-to-VM ORM ingestion, local Semaphore conversion, inventory pruning, SNMPv3, LLDP, and production runtime migration MUST NOT be used as recovery shortcuts.

Environment declarations SHALL come from private site-config, while active credentials and VM access keys SHALL come from OpenBao. Semaphore SHALL perform target-host access; the workflow MUST NOT require direct workstation SSH or copy private inventory/credential backups into public artifacts. A missing access mechanism SHALL be reported and repaired through code before dependent execution.

#### Scenario: Target access uses the platform credential chain
- **WHEN** diagnosis requires reading the NetBox VM and site-config identifies that environment
- **THEN** Semaphore obtains the authorized VM key from OpenBao, executes the read-only check, and an unavailable key or access mechanism causes a named blocker rather than a workstation SSH workaround

#### Scenario: Recovery repeats and rolls back without data loss
- **WHEN** a validated recovery is deployed twice and its owned configuration is then rolled back through Semaphore
- **THEN** the second deployment creates no duplicate objects or credentials, rollback preserves unrelated fields and NetBox data, and the report identifies the executed revisions and resulting health honestly

#### Scenario: Local tests do not close the production gate
- **WHEN** fixture tests and the local app tier pass but an enabled network or SNMP source has not completed acceptance on a representative Linux host
- **THEN** production qualification remains incomplete with that uncovered source named
