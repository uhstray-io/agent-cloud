## Why

Workflows for the organisation's private repositories increasingly need to reach
services that exist only inside the platform's own network — the orchestrator, the
secret store, the IPAM authority, the hypervisor API — or to originate from an
address the target service allow-lists. A hosted runner cannot do either: its
address is neither stable nor inside the perimeter, so any workflow that needs the
platform's own control plane currently has no execution surface at all and the work
falls back to an operator running commands by hand. That fallback is the exact
failure mode the platform exists to remove: unreproducible, unaudited, and invisible
to the next person.

A second, lesser reason is throughput. The organisation owns idle capacity with far
more cores and memory than a hosted runner offers, so the same workflows finish
faster on it once the execution surface exists.

## What Changes

- **New capability: an organisation-scoped self-hosted CI execution plane.** Two
  runner hosts are provisioned on the platform's own hypervisor, registered against
  the organisation (not per-repository), and made selectable from workflows by label.
- **Runner availability is scoped to private repositories only.** The organisation's
  one public repository is deliberately excluded from the runner group. A fork of a
  public repository can propose a workflow that executes arbitrary code, and these
  hosts sit inside the network perimeter; the upstream guidance is to pair
  self-hosted runners with private repositories only, and this change follows it
  rather than trying to out-engineer it. Public-repository CI continues on hosted
  runners, unchanged.
- **Runner registration becomes automated and credential-backed.** Registration
  credentials issued by the forge are short-lived (one hour), so registration cannot
  be a hand-pasted step if it is to be repeatable. An organisation-installed
  application identity is held in the platform secret store, and the automation
  exchanges it for a registration credential at the moment it is needed.
- **Each job is isolated from the host and from the job before it, by host
  configuration.** The workspace is destroyed after every job, jobs execute as an
  unprivileged account with no administrative rights, and the host is denied the
  platform's interior at the network boundary. All three are configured on the host, so a
  workflow cannot opt out of them.

  **Per-job containerisation is NOT among the controls.** It was in the first draft of
  this proposal, on the belief that the runner's container-hook mechanism confines every
  job. It does not: it manages containers for a job that *declares* one, and a job
  declaring none executes against the host filesystem as the runner account. Enforcing it
  universally needs an ephemeral runner whose own process and filesystem are containerised
  per job — a different lifecycle, recorded as follow-up rather than claimed here. The
  consequence is stated in the spec: nothing may sit on a runner host that all permitted
  repositories are not entitled to read.
- **Runner hosts are denied the platform's privileged interior.** A runner executes
  code authored in a repository. It is therefore treated as semi-trusted: it may
  reach what a workflow legitimately needs, and is denied the secret store, the
  hypervisor API, and the orchestrator.
- **New automation, no manual steps.** Host provisioning, credential placement,
  runner installation, registration, label assignment, de-registration, and
  verification all become playbooks driven by the orchestrator, declared in the
  inventory, and re-runnable to convergence.
- **A second runner is produced by declaration, not by repetition.** Standing up
  runner two must be an inventory entry plus a playbook run against it — if it
  requires editing the automation, the automation was not general enough.

## Capabilities

### New Capabilities

- `platform/github-actions-runners`: An organisation-scoped, self-hosted continuous
  integration execution plane — which repositories may use it, how a workflow selects
  a runner, how runner identity is established and revoked, how a job is isolated from
  the host and from the previous job, and what the runner host is permitted to reach
  inside the platform.

### Modified Capabilities

None. The host-access baseline this change relies on (credentials in the secret store,
key-only administrative access, default-deny host firewall) is already specified and
is consumed unchanged; the containment requirements that are specific to hosts running
repository-authored code belong to the new capability rather than to the general
baseline.

## Impact

- **New service under the platform tree**: a runner service directory carrying its
  deployment automation and its agent-facing context, following the existing service
  layout.
- **New playbooks**: provisioning and lifecycle for the runner hosts; a credential
  exchange step that mints a registration credential from the stored application
  identity; register, de-register, and verify operations. Existing composable tasks
  are reused for host access, secret retrieval, container runtime installation, and
  firewalling — this change should add orchestration, not re-implement primitives.
- **New orchestrator templates**: one per operation above, so every action is
  launchable and auditable from the orchestrator rather than a shell.
- **Private inventory**: two host declarations carrying their allocation (identifier,
  hypervisor node, cores, memory, disk, address) and their runner-specific settings
  (labels, runner group). The allocation is the declaration; provisioning makes
  reality match it.
- **Secret store**: one new location holding the organisation application identity and
  its private key.
- **Forge-side organisation configuration**: an application installed on the
  organisation with permission to administer self-hosted runners, and a runner group
  whose repository access list contains only private repositories.
- **IPAM**: two addresses recorded as allocated, taken from the authority rather than
  guessed.
- **No change to any existing workflow file.** Workflows opt in by naming a label;
  nothing is retargeted implicitly, and the public repository's CI is untouched.

## Rollback Plan

Rollback is staged, and each stage is independently sufficient — nothing here requires
unwinding the stage below it.

1. **Stop routing work** — no platform change at all: a workflow reverts to a hosted
   runner by changing the label it names. Because no existing workflow is retargeted
   by this change, the default state is already "nothing depends on the runners".
2. **Withdraw availability** — remove repositories from the runner group, or take the
   group offline. Queued jobs targeting it stop being dispatched; the hosts stay up
   and nothing is destroyed.
3. **Revoke identity** — de-register the runners and revoke the organisation
   application's installation. This is the security stop: after it, the hosts cannot
   receive work even if they are still running, and the credential they held is dead.
4. **Remove the hosts** — destroy the two virtual machines and release their
   allocation back to the inventory declaration, which is the allocation record for
   these hosts.

The change introduces no schema migration, no shared datastore, and no modification to
an existing service, so there is no state to reconcile on the way back — the only
durable artefacts are the two hosts, the forge-side application, and two address
allocations, each removable on its own.
