## Context

See `proposal.md` — Why. This section records only the facts that shape the approach,
all of them verified against the live cluster and against upstream rather than assumed.

**Repository visibility** (`gh repo view`, 2026-08-24): `agent-cloud` is **public**;
`zerds`, `atlas`, `zerds-website`, `weft`, `scientific-business` are all **private**.
That single fact drives the access-list decision below.

**Hypervisor capacity** (Proxmox `/api2/json/nodes`, read-only, 2026-08-24):

| Node | Threads / cores | CPU | RAM total | RAM used | Free RAM | CPU load |
|---|---|---|---|---|---|---|
| crabnebula | 40t / 20c | Xeon E5-2690 v2 @ 3.00 GHz | 141 G | 39 G | ~102 G | 0 % |
| alphacentauri | 32t / 16c | Xeon E5-2667 v2 @ 3.30 GHz | 125 G | 29 G | ~96 G | 1 % |
| mercier77 | 32t / 16c | Xeon E5-2665 @ 2.40 GHz | 94 G | 37 G | ~57 G | 11 % |
| apollo | 32t / 16c | Xeon E5-2665 @ 2.40 GHz | 94 G | 40 G | ~54 G | 2 % |
| primordial | 32t / 16c | Xeon E5-2690 @ 2.90 GHz | 46 G | 6 G | ~40 G | 0 % |
| milkyway | 24t / 12c | Ryzen 9 5900X | 31 G | 13 G | ~18 G | 0 % |
| andromeda, hydrus, starbirth | — | — | — | — | offline | — |

Image-capable storage headroom: crabnebula `vm-lvms` 558 G free and `crab-lvm` 1053 G
free; alphacentauri `vm-lvms` 911 G free. Disk is not a constraint anywhere.

**Allocation state.** Identifiers in use cluster-wide: 100–106, 200–215, 401, 501, 9000
(the base template, on alphacentauri). The next free pair in the service range is
**216, 217**. The inventory's declared addresses leave a gap immediately after the
current service block, and its first two free addresses were identified during
planning; both are silent to ICMP. The concrete values belong in the private
site-config inventory and are deliberately not written here — this repository carries
templates and placeholders only. These are *candidates* — the address
authority, not the inventory gap, is what makes them allocated, and that confirmation is
a task, not an assumption.

**Upstream facts.** Runner **v2.336.0** is current (published 2026-07-20). Its
`linux-x64` archive checksum is published in the release body and is machine-readable
from the release API, so pinning and integrity-checking need no hand-copied constant.
Registration credentials expire after **one hour**. The flags this design relies on —
`--labels`, `--runnergroup`, `--ephemeral`, `--no-default-labels`, `--disableupdate` —
are present in `actions/runner` v2.336.0 (`src/Runner.Common/Constants.cs`). The job
hook environment variables `ACTIONS_RUNNER_HOOK_JOB_STARTED` /
`ACTIONS_RUNNER_HOOK_JOB_COMPLETED` and the container-hook variable
`ACTIONS_RUNNER_CONTAINER_HOOKS` are upstream features, documented in that repository's
own design record for job hooks; hooks execute unconditionally when the variable is set.
`actions/runner-container-hooks` is at **v0.8.1**.

**Platform mechanisms that already exist and are reused unchanged.** Inventory-first VM
provisioning; declared-size convergence; host-access verification that refuses to pass on
password auth; per-host key issuance and distribution; SSH hardening ordered after key
verification; the inbound host firewall; container-runtime installation; secret retrieval
and env-file templating; user-session lingering for rootless services; the composable
per-service deploy shape (secrets → deploy → verify).

**Platform gaps this change must close properly rather than work around.** The host
firewall is inbound-only by design ("default-deny inbound, allow-outbound"), so
network-level *egress* denial has no mechanism today. Container-runtime installation
does not expose a Docker-compatible API socket. Neither gap is runner-specific, so both
are closed in the shared mechanism rather than in a runner-only script.

## Goals / Non-Goals

**Goals**

- Two runner hosts on separate hypervisor nodes, forming one interchangeable pool, so a
  single node reboot does not take continuous integration offline.
- Every step from bare allocation to a runner serving work is an orchestrator-launchable,
  re-runnable operation. Nothing in the path exists only as a command someone once ran.
- The credential path is: application identity in the secret store → short-lived
  installation credential → short-lived registration credential → discarded. No
  long-lived token on a runner host, none in a log, none on a command line.
- Standing up runner two exercises the same automation as runner one, with a different
  declaration and nothing else.

**Non-Goals**

- **Autoscaling.** A fixed pool of two. Elastic runners are a different architecture
  (a controller reconciling demand) and nothing here is being built in a way that
  blocks it later.
- **Kubernetes-hosted runners.** The platform's Compose→Kubernetes migration is planned
  separately; adopting the Kubernetes runner controller now would fork the effort.
- **Retargeting existing workflows.** Opting in is per-workflow and per-repository, done
  deliberately by whoever owns that workflow.
- **Serving the public repository.** Excluded by decision D2, and no partial measure is
  offered in its place — a half-gate would read as protection while not being any.
- **Windows or macOS runners, and GPU runners.** Linux x64 only.
- **Caching infrastructure.** No shared build cache, artifact proxy, or registry mirror.
  Deferred until a measured need exists.

## Decisions

### D1 — Placement: crabnebula and alphacentauri, one runner each

**Decision.** `gh-runner-01` on **crabnebula** (id 216), `gh-runner-02` on
**alphacentauri** (id 217). Both **8 vCPU / 16384 MB / 200 G**, identical.

**Why.** The brief asked for strong, *active* hardware. Four nodes qualify on headroom;
the discriminator is what a second runner is *for*. Two runners on one node share a
failure domain — one host reboot for maintenance takes the whole plane down, which
defeats the point of the second. crabnebula and alphacentauri are the two least
contended nodes (0–1 % CPU) with the two largest memory reserves (~102 G and ~96 G
free), so an 8-core, 16 G guest on each is a small fraction of either and leaves both
with substantial room for a third runner later.

Identical sizing is deliberate. A pool whose members differ in capacity makes job
runtime depend on which runner picked the work up, which is exactly the kind of
irreproducibility that is expensive to debug six months later. Homogeneous members mean
the pool has one performance characteristic, not two.

**Alternatives considered.**

- *Both on crabnebula* (40 threads, 102 G free — the single strongest node). Rejected:
  shared failure domain, and it wastes the fault-independence the second runner is
  mostly there to provide.
- *One on milkyway* (Ryzen 9 5900X — decisively the fastest per-core silicon in the
  fleet, roughly double the Xeons' single-thread throughput; continuous integration is
  often clock-bound, so this was genuinely attractive). Rejected on memory: milkyway has
  ~18 G free of 31 G total and already carries two guests, so a 16 G runner would leave
  the node at ~2 G. Recorded as the placement to revisit **if** a workload proves
  clock-bound — that is a measurement, not a guess, and moving a runner is a
  declaration change plus a rebuild.
- *Larger guests (16 vCPU / 32 G)*. Rejected as sizing ahead of evidence. 8 vCPU / 16 G
  already exceeds a standard hosted runner several times over; growing is one
  declaration change away and the convergence automation already exists, whereas
  shrinking an over-provisioned disk is not something that automation does.

### D2 — The public repository is excluded, with no partial gate

**Decision.** The runner group's access list names the five private repositories
explicitly. `agent-cloud` is absent and keeps hosted runners for its continuous
integration.

**Why.** These hosts sit inside the network perimeter. A fork of a public repository can
propose workflow code, and upstream's own guidance is unambiguous that self-hosted
runners belong with private repositories. Compensating controls exist — a fork-origin
condition on every job, plus mandatory approval for outside contributors — but they fail
open: one job that omits the condition is remote code execution on the local network,
and nothing in the review process reliably catches an omitted `if:`. A control whose
correct operation depends on nobody ever forgetting is not a control. Excluding the
repository is a property of the configuration instead of a property of everyone's future
diligence.

The cost is honest and small: `agent-cloud`'s continuous integration (static analysis,
security scan, unit tests) has no need to be inside the perimeter, and none of it
changes.

**Alternatives considered.** Fork-origin conditions plus approval gates (rejected above);
a separate, network-isolated runner exclusively for the public repository (rejected as
building a second execution plane for a need that has not been demonstrated — revisit if
`agent-cloud` ever grows a workflow that genuinely requires local reach).

### D3 — Identity: an organisation-installed application, exchanged at use time

**Decision.** An application installed on the organisation, holding only *self-hosted
runners: read & write*. Its identifier, installation identifier, and private key live at
one secret-store location. At registration time the automation signs a short-lived
assertion with the private key, exchanges it for an installation credential, exchanges
*that* for a registration credential, and hands the registration credential to the
runner configuration step over standard input. Nothing beyond the private key persists,
and the private key never reaches the runner host.

**Why.** Registration credentials live one hour, so registration is automated or it is
not repeatable — that much is forced. The remaining choice is what the automation holds
long-term. An application's credentials are narrowly scoped and its derived tokens
expire on their own, so a leak has a bounded lifetime and a bounded reach. It is also
revocable in one action, independently of every host, which is what makes rollback stage
3 in the proposal a real stop rather than a cleanup.

**Alternatives considered.**

- *Fine-grained organisation token scoped to self-hosted runners.* Nearly as narrow and
  markedly less work. Rejected because it is long-lived and expiry becomes a calendar
  event — the kind of dependency that fails at an inconvenient moment.
- *Classic token with organisation-administration scope.* Rejected: that scope confers
  authority over members, teams, and webhooks. Far too broad for a machine identity
  whose only job is registering runners.

**Implementation note.** Signing the assertion needs an RSA signature over two
base64url-encoded segments. The platform's controller already carries a cryptography
library as a transitive dependency of its secret-store integration, so this is a small
shared helper rather than a new dependency — and it belongs in the shared library
because organisation-level automation will need it again. If that library turns out to
be absent, the fallback is the same helper implemented over the `openssl` binary; adding
a new Python dependency to the controller image is the last resort, not the first move.

### D4 — Isolation: enforced by the host, not requested by the workflow

**Decision, CORRECTED after measurement.** The first layer below does not work as
originally designed, and the correction is recorded here rather than quietly dropped.

1. ~~**Per-job containerisation via the runner's container-hook mechanism.**~~ The
   original claim was that setting the container-hook variable makes every job's steps
   run in a container whether or not the workflow asked. **This is false, and was proven
   false by a job that asked for nothing and reported `ISOLATION=host`.** The hook
   mechanism manages containers for a job that *declares* one; it does not invent a
   container for a job that does not. Enforcing containerisation for every job needs an
   ephemeral runner whose own process and filesystem are containerised and recreated per
   job — a lifecycle change, not a setting. Recorded as follow-up.
2. **Workspace destruction via the job-completed hook.** A hook script removes the job
   workspace and prunes the job's containers and volumes after every job, always,
   including on failure and cancellation.

**Why.** The spec requires isolation that does not depend on the workflow asking. A
workflow-level container declaration is opt-in, so a job that omits it runs unconfined —
same failure-open shape rejected in D2. The container hook moves the decision to the
host, where it is a property of the runner rather than of each workflow author's
attention. The second layer exists because the first is a single mechanism protecting
multiple repositories from each other; a workspace wipe is cheap and catches whatever
leaks around it.

**What actually holds, verified on the live host:** the workspace wipe (a marker written
by one job was absent in the next), an unprivileged account with no sudo
(`SUDO=no` from inside a job), and network-level egress denial (the secret store,
orchestrator and hypervisors all unreachable from inside a job). What does not hold is
containerisation of an undeclared job.

**The runner service itself stays long-lived**, per the chosen lifecycle. This is the
trade-off being accepted knowingly: a persistent registration means the runner process
and its host state outlive any single job, so isolation rests on the container boundary
and the wipe rather than on process replacement. It is recorded as a risk below, and the
`--ephemeral` flag remains available as a later tightening because nothing in this design
depends on the registration being persistent.

**Alternatives considered.**

- *Ephemeral runners* (one job per registration, supervisor re-registers). Stronger — a
  poisoned job cannot persist at all — and rejected here only because it was not the
  chosen lifecycle. Explicitly reachable later.
- *Trusting each workflow's own container declaration.* Rejected: opt-in, fails open.

**Runtime feasibility — must be proven, not assumed.** The container hooks drive a
Docker-compatible API. The platform's norm is rootless Podman, which offers a
Docker-compatible socket, but **whether the container hooks operate unmodified against
rootless Podman's compatibility socket is unverified.** This is settled by a spike before
any dependent work, with two outcomes: rootless Podman works and the platform norm holds;
or it does not, and this host uses the container engine the platform already supports for
exactly this class of reason. Both outcomes are acceptable and declared per-host in the
inventory — the important thing is that the answer is measured, not that it comes out a
particular way, and that the fallback is an existing supported path rather than a patch.

### D5 — Runner group membership is configuration, not a console click

**Decision.** The group's name and its repository access list are declared in
configuration, and an orchestrator-launched operation converges the organisation's actual
group to that declaration — adding what is declared and removing what is not.

**Why.** This is the control that implements D2. If it lives in a web console, D2 holds
only as long as nobody widens it in a hurry, and there is no review trail when someone
does. Declared and converged, widening the access list is a reviewable diff. This is the
same pattern the platform already uses for its edge configuration, applied to the forge.

Removal must be part of convergence, not just addition — an access list that only ever
grows cannot enforce an exclusion.

### D6 — Egress denial is added to the shared firewall mechanism

**Decision.** Extend the existing host-firewall operation with an optional declarative
egress-denial list. It defaults to empty, so no existing host's behaviour changes. The
runner hosts declare denials covering the secret store, the hypervisor management
interfaces, and the orchestrator.

**Why.** The spec requires denial at the network boundary rather than by withholding
credentials, so that a credential that leaks into a workflow is useless from that host.
The mechanism is generic — "this host may not reach that" is a platform-wide concern, and
every future semi-trusted host needs it — so it belongs in the shared operation. A
runner-only egress script would be precisely the one-off this platform treats as a
defect.

**Limitation, stated plainly.** A host-level rule is enforced by the host, so it is only
as strong as the host's integrity. Job code runs unprivileged inside a container with no
path to host administration, which is what makes the rule meaningful — but the
authoritative boundary is the network, at the gateway. Gateway-enforced segmentation for
semi-trusted hosts is the correct end state and is recorded as follow-up work, not
silently assumed to be already true.

### D7 — Version pinned, integrity verified, self-update disabled

**Decision.** The runner version is a declared value. The install verifies the archive
against the checksum published with that release before extracting it, and disables the
runner's automatic self-update.

**Why.** All three serve one property: what runs is what was declared. An unpinned
install makes two hosts built weeks apart quietly different, which turns "works on one
runner, fails on the other" into a debugging session. Self-update would undo the pin
after the fact. The checksum is published in machine-readable form alongside the release,
so verification needs no hand-copied constant that could go stale.

### D8 — Service shape follows the platform's existing service pattern

**Decision.** The runner is a platform service directory with deployment automation and
agent-facing context, deployed by a playbook composed of the existing tasks —
secrets, place, deploy, verify — and driven by the orchestrator. It runs as a dedicated
unprivileged account with no administrative privilege, as a lingering user-session
service.

**Why.** Deliberately unremarkable. Every deviation from the established service shape is
something the next person has to learn, and there is nothing about this service that
justifies one. The dedicated unprivileged account is the concrete thing that makes the
isolation requirement's "cannot escalate to host administration" scenario true.

### D9 — The declaration is the allocation record; IPAM is a cross-check, once repaired

**Decision.** Each runner's address lives in its inventory declaration, and that is the
allocation record. The addresses are not hand-written into NetBox.

**Why, and why this is not a shortcut.** The original design named the address authority
as the source, on the reasonable assumption that it was current. It is not: checking
whether the two new hosts had been auto-registered turned up an IPAM store whose newest
discovery-written record is dated 2026-04-23, because the agent fails to resolve six
vault references every cycle and is left with no usable policy.

Writing two entries by hand into that store would produce records that look authoritative
inside a system that is four months stale — and a stale record is worse than an absent
one, because it gets trusted. The declaration is already read by both provisioning and
size convergence, so it is a real record rather than a placeholder, and it is versioned.

**What this defers, not abandons.** Repairing discovery (see
`plan/development/04-netbox-discovery.md`) makes IPAM register these hosts without anyone
recording anything. At that point it becomes a *cross-check* on the declaration —
divergence between the two is information — instead of a second record to keep in step by
hand.

## Risks / Trade-offs

- **A persistent runner serves several repositories in turn, so isolation carries the
  whole load.** → Two independent host-enforced layers (D4), a dedicated unprivileged
  account with no administrative privilege, and network-level egress denial (D6). A
  verification confirms the wipe actually happens between jobs rather than assuming it.
  `--ephemeral` remains available as a tightening, and nothing here blocks it.
- **The container hooks may not work against rootless Podman's compatibility socket
  (unverified).** → Settled by a spike before any dependent work, with the platform's
  already-supported alternative engine as the declared fallback. This is why the spike
  is sequenced first: discovering it late would be the moment someone reaches for a
  patch.
- **Host-level egress denial is weaker than gateway segmentation.** → Accepted and
  stated (D6), paired with unprivileged containerised execution so the rule is not the
  only thing standing between a job and the interior. Gateway segmentation recorded as
  follow-up.
- **A secret placed into a repository's continuous-integration configuration becomes
  readable by any job on these hosts, including one from another repository if the wipe
  fails.** → Wipe verification, plus egress denial limiting what a captured secret can
  reach from that host. Worth stating for whoever later considers what to put in those
  configurations.
- **The application's private key is the single high-value credential this change
  introduces.** → Secret store only, never on a runner host, never in a log, narrowly
  scoped to runner administration, revocable in one action independently of the hosts.
- **Two runners is a small pool; one node's maintenance halves capacity and a queue can
  form.** → Accepted for now. Both hosts sit on nodes with room for more, so growth is a
  declaration; the spec's declaration-only requirement is what makes that cheap.
- **A workflow naming a label no runner carries waits indefinitely rather than
  failing.** → Inherent to how label matching works upstream, not fixable here. The
  mitigation is that the label contract is documented and readiness is verifiable
  (spec), so the failure is diagnosable rather than mysterious.
- **Extending the shared firewall operation touches a path every service host runs.** →
  The addition is opt-in and empty by default, so an unchanged host emits an unchanged
  rule set; a regression test pins that.

## Migration Plan

There is nothing to migrate — no existing runner, no state, no service being replaced.
The sequencing that matters is *risk ordering*, not data movement:

1. **Settle the container-runtime question first** (D4 spike). It is the one decision
   that can change what gets installed on the hosts, so it precedes anything that
   installs.
2. **Close the two shared-mechanism gaps** (egress denial, Docker-compatible socket)
   with their regression tests, before any host depends on them. Fixing the mechanism
   first is what keeps the runner work from becoming the place a workaround lands.
3. **Allocate, provision, harden, verify** — the existing path, in the existing order:
   address from the authority, declaration in inventory, provision, key issuance, access
   verification, hardening only after key access is proven, firewall.
4. **Establish identity, then register runner one.** Application installed, key stored,
   group declared and converged, then one runner registered and verified.
5. **Prove it end to end with real work** before building the second. A green job on
   runner one is the gate; the second runner is not started until the first has actually
   run something.
6. **Declare and stand up runner two** using the same automation. If anything in the
   automation needs editing to make it work, that is the declaration-only requirement
   failing, and the fix belongs in the automation.
7. **Verify the pool**, then document the label contract for workflow authors.

Rollback is in `proposal.md` — Rollback Plan: four independent stages, each sufficient on
its own, no state to reconcile.

## Open Questions

- **Which private repository hosts the end-to-end verification workflow.** A
  manually-triggered smoke workflow has to live in a repository that is allowed to use
  the plane, and `agent-cloud` is deliberately not one (D2). This changes neither the
  specs, the approach, nor the task breakdown — the repository name is a parameter of a
  task that exists either way — so it is safely answered when that task is reached.
- **Whether an organisation-wide default runner group already exists and what it
  currently admits.** The credential available while planning lacked organisation
  administration scope, so the current state could not be read. The convergence
  operation (D5) handles either case, but the existing state should be inspected before
  the first convergence so that a removal is not a surprise.
- **Whether the two candidate addresses are free in the address authority.** They are
  free in the inventory and silent to ICMP, which is suggestive, not authoritative.
  Confirmed by the allocation task; if taken, the authority supplies the next free pair
  and only the declaration changes.
