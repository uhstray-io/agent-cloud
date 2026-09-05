# Production Diagnosis Approval Scope

**Date:** 2026-09-05

**Status:** Request A approved by the user; metadata inspection partially completed and then blocked by the browser client. Task 1.1 remains incomplete. Request B and production repairs are not approved.

## Verified local preparation

The owning checkout is `feat/netbox-discovery-recovery`. Initial source evidence was inspected at `108c09009c8d04d56cf855deba12b3b0ca09c87e`; the planning-only commit was subsequently rebased onto integration revision `2077363e93d45c1b8e9984b7d12cfcc7eb32d981` for publication. OpenSpec context and actionContext resolve within this worktree's `plan/development`; the shared registered store was not selected. All four apply context artifacts were read. Implementation remains **0/25 tasks complete**; no checkbox is changed by this preparation.

Private site-config was inspected read-only. Its canonical inventory contains the NetBox service group and service/runtime declarations; its guidance identifies OpenBao as runtime credential authority and private credential files as backup/reference only. Existing private and public checkout changes were preserved. No private values or credentials are reproduced here.

The public [template declaration](../../../../../platform/semaphore/templates.yml) lines 550–551 binds `Check Discovery Pipeline` to `platform/playbooks/check-discovery.yml` without a repository override. The [repository declarations](../../../../../platform/semaphore/repositories.yml) lines 31–48 define separate main and dev records. [Inventory synchronization](../../../../../platform/semaphore/sync-inventory.yml) lines 129–178 preserves the live inventory record's key binding. These files establish intended configuration, not the current live template, inventory, key source, or executed revision.

The current checker is not suitable for a read-only request: [check-discovery.yml](../../../../../platform/playbooks/check-discovery.yml) lines 89–109 contain a Site GPS write. Do not run it as-is, including under an assumed safe label or generic check mode.

## Request A — approved metadata inspection, partial result

The approved scope is read-only inspection of the production Semaphore project's existing **Check Discovery Pipeline** template and its linked repository, inventory and access-key metadata, solely to resolve task 1.1. Use existing authorized Semaphore authentication; if obtaining a new credential is necessary, stop and request that exact read separately.

Allowed reads are the existing project endpoints used by the repository's configuration tooling: `GET /api/project/<project_id>/templates`, `/repositories`, `/inventory` and `/keys`, narrowing to the matching records and linked IDs. Resolve the project from the configured environment; do not assume the example/default numeric ID identifies production. Responses stay in memory and are reduced to record IDs, fixed branch/type labels, a checker-playbook match boolean, inventory group presence and key-reference metadata. Free-form labels are not echoed. Do not emit raw inventories, environments, key payloads, task extra-vars, private addresses or credentials.

Expected evidence: actual template ID and checker-playbook match, linked main/dev repository and branch, production inventory reference and target-group match, environment ID reference, key-reference type and whether metadata establishes the declared OpenBao-backed access mechanism. Metadata alone does not establish key validity or origin when those are not exposed; report that as unknown. Compare with the local declarations without changing either side.

Explicitly excluded: task launch, VM connection, OpenBao reads/writes or rotation, inventory/template/repository updates, access-key export, branch repointing, deploy, scans, publishing and source-device changes. Do not run `setup-templates.yml`, `sync-inventory.yml` or any bootstrap merely to inspect their state.

### Repeatable execution mechanism

[inspect-discovery-metadata.py](../../../../../platform/semaphore/inspect-discovery-metadata.py) now encodes Request A as an operator-side tool, following the adjacent Semaphore configuration tools. It requires existing `SEMAPHORE_URL`, `SEMAPHORE_TOKEN` and explicit `SEMAPHORE_PROJECT_ID` environment values; there is no default project or credential lookup. Dependencies are Python and the existing PyYAML dependency. After the live access restriction is resolved through the normal approval mechanism, run `python3 platform/semaphore/inspect-discovery-metadata.py` from the reviewed checkout with those values already supplied through the authorized environment. Never pass the token in command arguments or copy it from a backup.

The tool performs only the four listed GETs, requires an HTTPS origin, refuses redirects, bounds each response to 2 MiB and uses a 10-second socket timeout. It uniquely resolves the checker and linked records, safely parses static YAML without executing inventory plugins, and prints a bounded JSON evidence record. Unsupported inventory forms or failed reads produce a fixed failure code; unknown key provenance/validity and execution revision remain unverified. A mismatched playbook or missing group is reported explicitly, never as healthy. Retain only this sanitized output in the approved operator evidence location, not raw API responses. The offline test is `python3 -m pytest -q platform/services/netbox/deployment/tests/test_discovery_metadata.py` and uses no network.

The script is implemented and offline-tested within task 1.1; the task remains unchecked because live bindings and revision are not yet proven. The historical browser read below is not the repeatable execution path. This code addition does not authorize using a different route around the client block: live execution still requires resolving the existing restriction through the normal approval/access mechanism. Obtaining any missing authentication credential is outside Request A and requires its own approved read scope.

### Execution evidence, 2026-09-05

The user approved the defined metadata-only request in the coordinating task. The current shell had neither Semaphore URL nor token configured. An existing authenticated browser session allowed the production project's repository page to be read without fetching or exporting credentials. The page confirmed separate `agent-cloud` / `main` and `agent-cloud dev` / `dev` repository records, both displaying the `local-none` repository-key reference. This is repository clone metadata; it does not prove the inventory's VM access mechanism.

The next approved request, `GET /api/project/<resolved_project_id>/templates`, failed with the browser-client result `net::ERR_BLOCKED_BY_CLIENT`. No template API payload was returned. The live phase stopped without retrying through a different route. The tool did not explain whether this was an approval restriction or another browser-client policy, so no cause is asserted.

Template, inventory and VM access-key bindings, key provenance/validity, and the executed revision remain unverified. No task was launched, no VM connected, no secret retrieved/exported, and no production configuration changed. Resolve the browser-client block through the normal approval/access mechanism before completing Request A; this partial result does not unlock Request B.

## Request B — production read-only diagnostic, conditional and not yet runnable

After request A and the local task-1.2 checker patch/offline tests, present a second concrete approval tied to the exact reviewed diff and execution revision. The eventual target is **production NetBox**, selected from private site-config through Semaphore. The user reports local Podman versus production Docker/low-level discovery requirements; verify the actual production runtime and capabilities rather than asserting that NetBox universally requires Docker. Do not build or deploy a local NetBox substitute for this gate.

The future workflow is the corrected `platform/playbooks/check-discovery.yml`, executed only through a verified Semaphore binding to its reviewed revision. If no such binding exists, request the exact code-managed binding/publishing operation separately; never repoint the shared template as a shortcut.

The bounded read set will be: deployed checkout revision; container image IDs/digests and running status without environment dumps; installed Orb/Diode/SDK version metadata; sanitized terminal events from existing scheduled cycles; read-only NetBox counts, timestamps and expected-object projections under a read-only database transaction. Read non-secret source schedule/configuration fields through a parser that excludes credential-bearing fields. Report missing evidence rather than forcing a cycle.

Credential diagnosis, if separately included in the final request, is limited to authentication and reads of enumerated existing credential paths/capabilities using the approved identities. It produces missing/denied/auth/transport verdicts and field-presence booleans, never values. A read-only source-authentication probe may be included only with its precise endpoint and scope specified at that time. No new credential mint, refresh, seed, policy update, or privilege expansion is implied.

The output must separate measured live evidence from the April–August operator report and distinguish vault access, source authentication, collection, reconciliation and expected-object failures. Unobserved stages remain unknown. Running containers or old objects never establish recovery.

Excluded mutations: GPS repair; NetBox create/update/delete; scans or forced worker runs; container restart/deploy; secret/policy/AppRole/key change; inventory/route/firewall change; source-device configuration; SNMP disablement; notifications to third parties; test/canary creation. Bounded normal task output is the only expected persisted result. Any additional operation requires a separately reviewed request.

## Conditional repair and validation after evidence

Classify faults before preparing a repair: missing fields require a verified authoritative source or approved operator provisioning; ACL failures require a scoped policy diff; rejected source auth requires identity/secret validation; collection errors require version-grounded worker/config fixes; reconciliation failures require ingestion evidence. Prepare and offline-test only the demonstrated repair, with exact owned fields/configuration, expected effects and rollback. Request that specific mutation separately. No unspecified production repair is approved by either diagnosis request.

Production acceptance remains the spec's per-source collection → completed reconciliation → expected-object read-back gates, with two scheduled cycles for every enabled source and tested monitoring delivery. Offline tests cannot close that gate. SNMP remains enabled unless the operator separately approves a reviewed disablement with a reason.

## Why execution stopped

The original approval-review outcome rejected broad delegation that could include VM/production operations. The user subsequently approved Request A only; its template metadata request was then blocked by the browser client as recorded above. Task 1.1 explicitly requires confirming the live access/binding/revision; task 1.3 requires a live baseline before later adapters and repairs. No task was skipped, redefined as offline-complete, or marked complete. Native Apply's pause rule also says: "Pause if you hit blockers or need clarification." Independent contract correction, metadata-inspector implementation/offline testing and publication are authorized; they do not satisfy implementation or production acceptance gates.
