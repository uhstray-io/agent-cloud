# Production discovery diagnosis and execution evidence

**Updated:** 2026-09-05 session (continued across UTC midnight)

**Current authorization:** The user authorized non-destructive production diagnosis
and discovery-agent deployment through production Semaphore. Production deployment
is not waiting for a generic permission request. Configuration must be applied as
code; no manual template bootstrap, workstation SSH, backup credential export,
blind rotation or credential retirement is authorized. Preserve effective agent
configuration and historical logs before any restart/redeploy.

## Current implementation and execution boundary

PR #162, branch `feat/netbox-discovery-recovery`, carries the read-only diagnostic
replacement and controller metadata wrapper. The owner merged integration revision
`89274ba792fa91955dbd39a53dcd201692b11263` before this implementation. This is source
state, not an installed production revision. The original planning-only reference
was `108c09009c8d04d56cf855deba12b3b0ca09c87e`.

`check-discovery.yml` removes GPS mutation and requires a nonempty `netbox_svc`
group, explicit zoned log window, and a matching committed controller revision.
It collects Docker container identity/status, a sanitized projection of the actual
mounted agent configuration, bounded retained log signals, and fixed aggregate ORM
reads inside an explicitly read-only transaction. It neither updates the checkout
nor restarts containers. Each command is bounded to 45 seconds/16 MiB; configuration
is bounded to 256 KiB. A missing host PyYAML dependency is a visible configuration
read failure, not permission to install during diagnosis. Docker-only scope avoids
confusing rootless Podman application storage with privileged Orb storage.

The collector always ends with `discovery_acceptance_unverified`, including when
all baseline reads succeed. Text signals, submission messages and aggregate counts
do not prove complete collection, reconciliation or expected-object coverage.
Timeout units, installed package versions, loaded configuration identity and
historical completeness remain unverified unless separate evidence establishes them.

`inspect-discovery-metadata.yml` reuses the shared controller `runtime-access.yml`
helper: existing injected token or the controller's existing OpenBao AppRole, with
an exact fixed `http://127.0.0.1:3000` destination. Its inspector performs four
allowlisted GETs (templates, repositories, inventory, keys), refuses redirects,
bounds responses, and emits IDs/booleans/fixed labels only. No raw inventory,
credential payload or environment is printed. The original operator HTTPS mode
remains available for an already authorized injected environment.

Both entry points are declared in `templates.yml`. The shared scoped publisher
updates surveys on existing records only; it cannot create the new inspector
record. A verified code-managed first executor and the NetBox VM key provenance
remain unresolved. No API or VM credential is present in this task's shell.
The already-running Semaphore UI can be read normally; no browser credential is
extracted. No production job or configuration change has been performed by this task.

## Retained historical evidence inspected through ordinary Semaphore UI

All UI dates below have **unspecified UI timezone**. Embedded agent timestamps
retain their explicit `Z` or recorded offset. Do not equate these time domains.
No raw logs, private topology or credential values are copied into this document.

| Evidence | Observation | Limit |
|---|---|---|
| Check Discovery Pipeline, template 56, task 181 | UI start `04/23/2026 06:37`; exact task revision `907085ae9b0b457f390118373ea561fc813c1889`; log checkout branch `feat/semaphore-branch-testing-and-cleanup` | Historical executed checker revision, not current deployed agent/config revision |
| Task 181 retained last-100 agent lines | Proxmox reported submission of 18 entities/1 chunk at `2026-04-23T07:45:00.446051412Z`; pfSense reported 16/1 chunk at `2026-04-23T07:45:01.123680757Z` | Submission messages are not reconciliation proof; these are latest inspected messages, not a proven last successful cycle |
| Same Proxmox cycle | Eleven offline-node VM/LXC skips preceded the submission report | Partial collection was compatible with a green historical checker |
| Earlier retained cycle | Ingestion `UNAUTHENTICATED` retry at `2026-04-23T07:30:00.430316078Z`, followed by submission messages | A transient retry does not establish persistent credential failure or the August root cause |
| Deploy Orb Agent, template 47, task 167 | Latest listed run `04/22/2026 13:04`; exact task revision `d96fe694167cd9fe9c419d5a85e799b66f659248`; retained task sequence includes fresh credential creation, OpenBao capture, template and agent stop/start | Establishes that the historical workflow invoked creation; not a live credential inventory or compromise finding |
| Update NetBox, template 20 | History displayed no data | No retained update-run evidence available from this template |
| Deploy NetBox, template 18 | Latest listed run 89, `04/04/2026 09:24` | No August deployment in this template's displayed newest history; absence here is not proof no deployment occurred elsewhere |

The April last-working and August outage dates remain separate operator-reported
leads. The inspected historical pages do not yet establish an August first failure,
current effective configuration, complete retained container history or current
collection/reconciliation health. Existing template labels showed production
inventory and a variable group labeled local-dev; labels alone cannot establish
actual values or runtime access. Repository clone key `local-none` says nothing
about the VM inventory key.

## Credential reuse and future retirement

Current source `tasks/manage-diode-credentials.yml` unconditionally invokes
`create_client` before every deployment. Its task labeled “Delete existing” merely
lists clients, and the creation command lacks scoped `no_log`. The standalone
agent deploy calls it before updating the target checkout. Do not launch this
unchanged path: it violates the user's no-mint-on-redeploy instruction and the
architecture's Subsequent Deploys secret-reuse contract.

Task 2.6 durably tracks consumer/ownership inventory and individual safe retirement
as future work. No bulk deletion or revocation is part of this incident. Preserve
non-secret audit/recovery references, identify selected credentials and all known
consumers, prove current/replacement consumers healthy, and review each retirement
candidate individually. Age or a matching name alone does not authorize retirement.
Fix routine credential reuse before agent redeployment; do not guess a replacement
API or rotate merely because a historical retry exists.

## Historical access restriction and superseding scope

The initial broad delegation was rejected by automatic approval review. The user
then approved a narrow metadata request; a direct browser navigation to the template
API returned `ERR_BLOCKED_BY_CLIENT`. That direct route was not retried or bypassed.
The user later explicitly authorized ordinary authenticated UI reads, continued
repository implementation, non-destructive diagnosis and Semaphore agent deployment.
Those later instructions supersede the old “Request B not approved” status. The
remaining blockers are executable binding/provenance and the unsafe deploy mechanism,
not missing broad permission. The controller wrapper is a declared platform workflow,
not a workstation alternate route around the browser-client restriction.

## Next execution gate

Establish the code-managed controller executor and exact reviewed diagnostic
revision, verify target inventory/VM key provenance, then collect historical windows
and current mounted configuration before any deployment. Preserve missing/rotated
history as unknown. Use that evidence to select the smallest demonstrated repair.
Acceptance still requires two scheduled complete collection → reconciliation →
expected-object cycles for every enabled source and tested monitoring delivery.
No local NetBox instance or synthetic success substitutes for this production gate.

## Local validation and limits

Task 1.2 is implemented; task 1.1 remains incomplete and the production baseline
(task 1.3) is not captured. The checklist now has 26 tasks, including the durable
credential reconciliation follow-up. Tests execute the collector with synthetic
container responses, real bounded subprocesses, and a disposable local Git/Ansible
preflight. The fixed ORM call/SQL surface is checked statically; these are not a live
PostgreSQL transaction test or production-side write audit. Historical UI reads do
not close any production acceptance gate.

An independent read-only Claude Opus 5 review completed; dependency failure reporting,
exit-code validation and revision/read-only test coverage were tightened afterward.
A missing configuration read still makes the overall baseline incomplete, while its
separate error leaves container/log evidence visible. No unconditional multi-host
abort was added to baseline collection: each target must preserve its own evidence,
and the final explicit refusal already prevents recovery acceptance.
