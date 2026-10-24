# Source-only primary-outage writer census

**Date:** 2026-10-24

**Status:** PROPOSED

**Context:** Planning evidence for existing PBS task 4.4c.1 and the
[primary-unavailable restore plan](primary-unavailable-restore-plan.md).
All source and line references are pinned to reviewed Dev commit
[`b0e7beeb9fcb6fa1e076b935aac39e86c9986f75`](https://github.com/uhstray-io/agent-cloud/tree/b0e7beeb9fcb6fa1e076b935aac39e86c9986f75).
Relative links provide repository navigation; re-pin line numbers and applicability
before implementation if source changes. Public YAML, catalog and named helper
inspection supplemented graph discovery where its coverage was stale or absent;
no index was refreshed. This document is not live census closure, implemented
fencing or activation approval.

## Result and counting rule

The bounded PBS/VM core below contains **21 distinct playbook entrypoints**:
**20 mutation-capable entrypoints and 1 read-only readiness entrypoint**.
This counts the PBS provisioning wrapper separately from its shared writer, counts
each playbook once despite multiple mutations, and does not count helper tasks as
independent launch entrypoints. There are **2 explicit autonomous PBS scheduled
job policies** (GC and weekly verification), **1 verify-new policy**, and **1
explicitly unscheduled protected-sync definition**. Installed job counts are unknown.

This is a source inventory of those bounded entrypoints, **not a closed live writer
census**. Current generic host operations create an additional unresolved scope
boundary described below. Private identity aliasing, installed legacy templates,
queued/running revisions, PVE/PBS jobs and effective privileges remain unverified.
No production API, SSH, container, task, grant, publication, or private push was used.

## Core matrix

Catalog refs are in [platform/semaphore/templates.yml](../../../../../../platform/semaphore/templates.yml) (T). Playbook refs use
[`platform/playbooks/`](../../../../../../platform/playbooks/) (P). All rows require future outage admission/exclusion unless
explicitly incorporated into the outage coordinator's owned transaction.

| # | Current entrypoint / catalog | Actual writer or relevant action, exact source | Existing guard/coordination | Outage exclusion gap |
|---|---|---|---|---|
| 1 | Provision VM; T:2969 | [provision-vm.yml](../../../../../../platform/playbooks/provision-vm.yml):638 clone, :728 migrate, :810/:838 config, :854 resize, :963 start | Reserved-target guard at :631/:721/:803/:831/:847/:956; inventory/VM validation | Guard allows unrelated VMIDs, including primary/destination; no admission retained across API effect. Preparatory clone/migration also need coverage. |
| 2 | Create VM Template; T:2941 | [provision-template.yml](../../../../../../platform/playbooks/provision-template.yml):136 upload, :162 create, :183 fallback create, :202 config, :244 start, :281 config, :304 template | Refreshed target guard at :129/:155/:176/:195/:237/:274/:297; seed-build/upload fallback wiring tested | Target-only GET guard does not exclude primary/destination or shared staging-store changes. |
| 3 | Provision PBS Debian Cloud Template (Dev); T:2161 | [provision-pbs-debian-template.yml](../../../../../../platform/playbooks/provision-pbs-debian-template.yml):251/:319/:400 guards before publication mutations; :407 template conversion | Exact reviewed checkout, private declaration, no-start publication contract | Template/staging identity and store overlap with transaction subjects must be resolved privately; target guard is not an outage claim. |
| 4 | Resize VM; T:3092 | [resize-vm.yml](../../../../../../platform/playbooks/resize-vm.yml):384 config, :406 resize, :456 shutdown, :481/:518 start | Guards :377/:397/:449/:474/:509; pending/config readback | Can power or resize declared primary/destination without outage admission. |
| 5 | Destroy VM; T:2947 | [destroy-vm.yml](../../../../../../platform/playbooks/destroy-vm.yml):168 stop, :220 delete; preparatory/destructive groups guarded :126/:161/:213/:285 | Inventory-first identity/confirmation and fixed-target reservation guard | Primary/destination remain ordinary permitted VMIDs; explicit confirmation is not concurrency exclusion. |
| 6 | Snapshot VM; T:3118 | [snapshot-vm.yml](../../../../../../platform/playbooks/snapshot-vm.yml):138 create snapshot; :183 readback | Target guard :131; live running/config checks | Snapshot task can overlap primary outage or backing-store operations; no participant lifecycle. |
| 7 | Attach PBS Data Disk (Dev); T:2249 | [attach-pbs-data-disk.yml](../../../../../../platform/playbooks/attach-pbs-data-disk.yml):211-226 allocates scsi1, :228 task wait, :249 readback | Target guard :205-209, exact disk layout, backing preflight and config digest | Disk allocation and eventual effect are not atomically excluded from outage; primary is not reserved target. |
| 8 | Provision PBS VM (Dev); T:2987 | [provision-pbs-vm.yml](../../../../../../platform/playbooks/provision-pbs-vm.yml):22-38 restricts PBS group then imports `provision-vm.yml` | Reviewed dependency pin includes shared target guard (:14-19); shared lifecycle | Wrapper must participate before shared preparations; protecting only catalog template name would miss direct shared entrypoint. |
| 9 | Prepare PBS Guest Data Disk (Dev); T:2261 | [prepare-pbs-guest-data-disk.yml](../../../../../../platform/playbooks/prepare-pbs-guest-data-disk.yml):254 installs helper, :403 formats proven blank disk, :446 mount/fstab, :457 datastore directory | Exact PVE disk serial/layout readback :155-166, guest mount/signature safety | No outage admission; filesystem/config effects on primary guest need exclusion even if idempotent. |
| 10 | Configure PBS Service (Dev); T:2273 | [configure-pbs.yml](../../../../../../platform/playbooks/configure-pbs.yml):163/:173 repository/key writes, :181 packages, :196 shared datastore reconciliation | Private PBS group, supported OS/repository, mounted-disk proof | Package/service/datastore/job changes can overlap outage; no outage guard. Its verify-only mode is read-only but cannot attest exclusion. |
| 11 | Configure PBS Backup Access (Dev); T:2285 | [configure-pbs-backup-access.yml](../../../../../../platform/playbooks/configure-pbs-backup-access.yml):548-566 identity task; :683 PVE storage create. Helpers [tasks/configure-pbs-backup-identity.yml](../../../../../../platform/playbooks/tasks/configure-pbs-backup-identity.yml) own user/token/ACL state | Exact identity state machine, credential CAS, storage/jobs snapshots :599-677 and final readback :703-791 | Credential/storage consistency controls do not exclude guest outage; existing backup-job preservation is not scheduler suspension. |
| 12 | Configure o11y PBS Copy Access (Dev); T:2297 | [configure-o11y-pbs-copy-access.yml](../../../../../../platform/playbooks/configure-o11y-pbs-copy-access.yml):726-768 source Reader, destination Manager and restore Reader identity task includes | Private inventory provenance :73-84; credential CAS before ACL, exact grants/effective permission readbacks in three identity helpers | Identity CAS protects credentials, not primary/destination outage. All three helper paths belong to this participant. |
| 13 | Register o11y PBS Restore Storage (Dev); T:2314 | [register-o11y-pbs-restore-storage.yml](../../../../../../platform/playbooks/register-o11y-pbs-restore-storage.yml):380 creates exact PVE storage with destination Reader | Private provenance :74-84, exact existing registration and preserved state; dedicated Reader | Registration/storage readback can race transaction; no outage exclusion. |
| 14 | Enable PBS Import Storage Content (Dev); T:2185 | [enable-o11y-pbs-import-content.yml](../../../../../../platform/playbooks/enable-o11y-pbs-import-content.yml):204-217 PUT cluster-wide storage content | Exact private import-store, preserved content, PVE digest | Digest prevents stale content update, not outage overlap or store aliasing. Include only when private store belongs to transaction scope. |
| 15 | Backup o11y to PBS (Dev); T:2343 | [backup-o11y-to-pbs.yml](../../../../../../platform/playbooks/backup-o11y-to-pbs.yml):241-258 POST node vzdump; :284 exact task poll; :312 onward artifact readback | Reviewed private provenance, exact source VM/storage, manifest/UPID receipt | Writes source snapshot into primary PBS; no outage admission; asynchronous vzdump must hold admission through terminal outcome. |
| 16 | Verify o11y PBS Artifact (Dev); T:2358 | [verify-o11y-pbs-artifact.yml](../../../../../../platform/playbooks/verify-o11y-pbs-artifact.yml):213-229 `snapshot-verify`; :254-274 poll; :286 manifest readback | Authenticated backup receipt :119 onward, exact snapshot/fingerprint, pinned TLS | Verification is a server task/metadata writer, not a harmless GET. No outage admission across server effect. Helper operation is POST: [files/inspect_pbs_backup_access.py](../../../../../../platform/playbooks/files/inspect_pbs_backup_access.py):87, :636-646. |
| 17 | Sync o11y Protected PBS Copy (Dev); T:2378 | [sync-o11y-protected-pbs-copy.yml](../../../../../../platform/playbooks/sync-o11y-protected-pbs-copy.yml):269 remote upsert, :335 sync upsert, :369 sync-run, :408 poll | Authenticated predecessor chain :114-176; pinned identities; verified-only pull, no prune; explicit `schedule: ''` :348-350 | Writes destination and reads primary; remote/job upserts and run all need one retained participant. No atomic outage exclusion. |
| 18 | Verify o11y Protected PBS Copy Readiness (Dev); T:2403 | [verify-o11y-protected-pbs-copy.yml](../../../../../../platform/playbooks/verify-o11y-protected-pbs-copy.yml):345 reads remote/job/datastore, :393 PVE storage/content, :452 manifests | Authenticated chain, exact ACLs/config/locators; readiness remains pending restore :513 | Read-only entrypoint, not a backup writer. Its reads require primary and can race; reject/defer during outage or keep outside acceptance interval. Its pre-outage evidence cannot replace fresh outage-time destination reads. |
| 19 | Restore o11y from Protected PBS Copy (Dev); T:2433 | [restore-o11y-isolated.yml](../../../../../../platform/playbooks/restore-o11y-isolated.yml):659 CAS reservation; :717 restore, :905 NIC/config scrub, :1052 start, :1244 guest exec, :1414 stop | Fixed-path schema-2 CAS action journal, exact target fingerprint, intent/handle/effect readback | Correct target transaction only; does not reserve/stop/observe primary. Must compose through reviewed subject-aware coordinator, not run concurrently beside it. |
| 20 | Cleanup o11y Isolated Restore (Dev); T:2470 | [cleanup-o11y-isolated-restore.yml](../../../../../../platform/playbooks/cleanup-o11y-isolated-restore.yml):441 CAS ownership/delete intent, :467 delete, :517 task poll, :607 deletion effect | Authenticated v3 receipt and exact stopped owned target/config, CAS journal | Must refuse active outage transaction and preserve legacy ordinary cleanup contract; no primary outage subject currently exists. |
| 21 | Reconcile o11y Backup Job (Dev); T:2630 | [reconcile-o11y-backup-job.yml](../../../../../../platform/playbooks/reconcile-o11y-backup-job.yml):730-745 PUT selected cluster backup job explicit VMID membership | Exact reviewed checkout, private selected job, fresh state comparison :689-727, preserved-option readback :759-779 | Not a pause mechanism: it preserves schedule/options and changes membership. Private selected destination may be PBS; no outage admission. Existing scheduled job continues independently. |

## Autonomous jobs and launch paths

`configure-pbs.yml`:33-34 and :212-213 fix weekly verification to `Sun 03:00`
and GC to `daily`. [tasks/configure-pbs-datastore.yml](../../../../../../platform/playbooks/tasks/configure-pbs-datastore.yml):175-210 creates/updates
the datastore with GC schedule and `verify-new=true`; :249-299 creates/updates
the weekly verify job; :319-325 verifies those settings. These are current
source-defined autonomous policies, not just hypothetical unknown schedules.
They are configured for `pbs_svc` (`configure-pbs.yml`:23); this source alone
does not establish destination deployment, installed job identity/count, timezone,
next run, active state, or scheduling behavior while the primary is stopped.

The protected-copy sync definition alone uses no schedule
(`sync-o11y-protected-pbs-copy.yml`:335-350). Its explicit API start remains
enabled at :369. Other installed sync/verify/prune/GC jobs are unobserved.
`configure-pbs-backup-access.yml`:381-406 reads all PVE backup jobs and preserves
them through registration; `reconcile-o11y-backup-job.yml`:274-307 reads scheduler
state for its diagnostic, which is not authoritative outage idle/exclusion proof.

The named catalog binds the rows above; generated Dev variants of generic
templates must also be covered. [platform/semaphore/setup-templates.yml](../../../../../../platform/semaphore/setup-templates.yml):370-376
states schedules bind base templates only, with no generated-Dev inheritance;
:503-532 upserts declared schedules. It does not globally remove undeclared
installed templates/schedules. No PBS core template declares a Semaphore cadence
in the inspected catalog; that is not evidence installed Semaphore has none.

[platform/workflows/service-onboarding/registry.yml](../../../../../../platform/workflows/service-onboarding/registry.yml):72-89 names `Provision VM`
as executor for provision and cloud-init steps: onboarding dispatch is an indirect
launch route to row 1, not a second independent writer implementation. Actual
external launcher/queued-task state was not inspected. Legacy
[platform/scripts/setup-project.sh](../../../../../../platform/scripts/setup-project.sh):279-303 publishes Create VM Template and six
service-specific Provision VM names; those six hardcoded unrelated services do
not establish PBS writers, but an installed legacy/modified binding cannot be
excluded from current catalog inspection alone. Admission at shared writer code,
plus publication/draining, avoids relying on template-name matching alone.

## Privileged host maintenance boundary (outside assigned writer implementation)

The assigned runtime-writer implementation scope is the core matrix, with subject
overlap decided from private declarations. The current source also includes
privileged host-maintenance operations that may target PBS guest groups without
going through the seven VM mutators. These require a defined trusted-admin
maintenance boundary; they are not a proposal to implement whole-platform host
fencing. Concrete examples:

| Current catalog operation | Current source evidence | Decision needed |
|---|---|---|
| Reboot Host (Dev), T:1409 | `reboot-host.yml`:46-69 targets caller group with exact host confirmation; :95-100 `ansible.builtin.reboot` | Include guest reboot as participant, or formally exclude all such maintenance during window. Confirmation does not fence concurrent primary reboot. |
| Configure Host Network, T:2999 | `configure-host-network.yml`:50-60 arbitrary declared group; network apply/revert timer design :13-28 | Include identity/network config and deferred revert effects when group overlaps subjects. |
| Apply Firewall, T:2805 | `apply-firewall.yml`:665-723 delete/rules/default/enable writes | Connectivity and identity/endpoint proof can change independently of VM lock. |
| Install QEMU Guest Agent, T:2918 | `install-qemu-guest-agent.yml`:22 group target, :52 package, :60 agent service start | Relevant to QGA proof and guest runtime; private target availability unknown. |
| Install Docker / Install Podman, T:2908/:2928 | `install-docker.yml`:15 target group, :45-102 package/repository/engine writes; `install-podman.yml`:24, :57-106 packages/host state | Host package/runtime mutation needs a boundary if these launch groups can include declared guests. |
| Harden SSH / Distribute SSH Keys, T:2791/:2770 | `harden-ssh.yml`:32 group, :206 handlers/sshd restart; `distribute-ssh-keys.yml`:102-198 group key/account changes | Changes guest access/control plane used for PBS configuration/recovery. |
| Instrument Host Observability, T:1396 | `instrument-host-o11y.yml`:103/:245 target group; :303-347 container mutation, :358 onward boot settings | Current guest writer; not assumed harmless during maintained outage. |
| Ensure Service Persistence, T:1449 | `ensure-service-persistence.yml`:35 caller group; :45-60 boot settings, :140-168 future container start unit | Deferred boot effects are relevant to explicit restart-behavior contract if private group supports this path. |

These are **10 named conditional privileged-maintenance entrypoints** (Docker and Podman,
and Harden SSH and Distribute SSH Keys counted separately), not an assertion that
all ten can successfully run against installed PBS guests. There is no private
inventory/privilege evidence to make that assertion. Further broad host/group
tools and shared-hypervisor/storage maintenance are outside this bounded read;
their applicability must be resolved before asserting live exclusion closed.
Explicitly declare that privileged administrators will not run these against the
subjects during the maintenance window, identify how that agreement is enforced
operationally, and retain that trusted-admin limitation in acceptance evidence.
If the desired guarantee includes these cooperating operations, add only the
applicable bounded participants through a separately reviewed scope decision.

## Minimum implementable exclusion design

There is no proven exclusion design in current source. The smallest defensible
next implementation is one versioned subject-aware CAS coordinator, extending
the existing mechanism rather than adding an unrelated lock:

1. Bind exact private subjects (primary, destination, source o11y, isolated target,
   declared stores/job selectors) and reviewed public/private revisions. Define
   the trusted-admin maintenance boundary separately from the assigned runtime
   participants; refuse unknown runtime-writer overlaps.
2. Add atomic admission to each assigned shared mutation path, before preparations,
   with admission and outage claim updating the same CAS state. Hold until exact
   authoritative task/effect completion, including deferred effects; uncertain
   outcome retains admission and requires recovery. No TTL takeover or GET-only
   check followed by an uncoordinated write.
3. Route owned outage restore/cleanup actions through the coordinator with typed
   subjects; preserve existing ordinary schema-2/v3 cleanup validation explicitly.
   A target lock alone cannot become primary power authority.
4. Treat autonomous server jobs as a separate required prerequisite. Controller
   admissions cannot fence PVE/PBS schedulers. Add complete read-only census and
   effective visibility first. Prove schedules cannot fire in the bounded window,
   or implement exact digest-bound journaled pause/resume and verify original
   state preservation. Refuse busy/queued/unaccounted jobs; never cancel them.
5. Publish compatible code/catalog through the normal reviewed mechanism, then
   authoritatively drain old queued/running variants and verify exact installed
   bindings. A source merge does not update already running code. Recheck idle
   after exclusion and immediately before the first primary power operation.

A demonstrably atomic declarative Semaphore maintenance gate could replace
per-writer admission only if it also blocks every indirect/direct relevant launch,
drains queues/running variants, and covers the server scheduler problem. No such
capability is proven here. Do not implement both speculatively.

The next bounded coding step is the **disabled-by-default coordinator/admission
contract with CAS race tests and an explicit participant manifest**, plus a
read-only complete job/visibility preflight contract. Tests must prove one loser
for admission-vs-outage races, unknown-outcome retention, wrong-subject refusal,
no-write check mode, and legacy ordinary cleanup compatibility. Activation stays
blocked on private subject applicability, installed schedules/queues and effective
privileges, available recovery, publication/drain evidence and reviewed maintenance
authorization. This artifact does not unblock the separately denied template-351
OpenBao grant or private site-config PR110 correction push.

## Limits

No production reads or writes occurred. No test suite was executed; source
inspection is not runtime validation. No installed API schema, complete PVE/PBS
task permissions/pagination, HA/watchdog/onboot state, server jobs, queue state,
current template bindings, filesystem backing identity, private declaration
distinctness, or availability of a maintenance gate was established. Shared
hypervisor/storage and privileged administrators remain explicit trust domains.
This census records source evidence only; it does not implement exclusion,
change policy or private approval gates, or authorize an outage.
