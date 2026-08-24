## Purpose

Provides an organisation-scoped, self-hosted continuous integration execution plane so
that workflows needing to originate from inside the platform's own network — or from its
stable address — have a reproducible place to run, while confining the blast radius of
executing repository-authored code inside that network.

## ADDED Requirements

### Requirement: Runner availability is restricted to private repositories

The execution plane SHALL be offered to the organisation's private repositories only,
and SHALL NOT be reachable by a public repository's workflows.

A repository gains access by being named in the runner group's access list. The list
SHALL be explicit — access MUST NOT be granted by a default that admits every
repository in the organisation, because the organisation contains a public repository
whose forks can propose workflow code.

Public-repository continuous integration SHALL continue to run on hosted runners, and
this change MUST NOT retarget it.

#### Scenario: A private repository's workflow is dispatched to the plane

- **GIVEN** a private repository named in the runner group's access list
- **WHEN** one of its workflow jobs requests the plane by label
- **THEN** the job is dispatched to a runner in the group and executes

#### Scenario: The public repository cannot reach the plane

- **GIVEN** the organisation's public repository, which is absent from the access list
- **WHEN** a workflow in that repository — including one proposed from a fork —
  requests the plane by label
- **THEN** no runner in the group accepts the job, and the job does not execute on
  platform-owned hardware

#### Scenario: Access is enumerable

- **WHEN** an operator asks which repositories may use the plane
- **THEN** the answer is a finite, explicitly declared list, not "every repository in
  the organisation"

### Requirement: Workflows select a runner by declared label

A workflow SHALL select the execution plane by naming labels, and those labels SHALL be
declared as configuration alongside the runner's other allocation facts rather than
assigned by hand after the runner exists.

Labels SHALL distinguish the plane from hosted runners, and SHALL be sufficient to
distinguish one runner's characteristics from another's where those characteristics
differ in a way a workflow author would need to choose between.

#### Scenario: A workflow targets the plane

- **WHEN** a workflow job names the plane's labels
- **THEN** the job runs on a runner carrying all of those labels, and on no other
  runner

#### Scenario: Labels survive a rebuild

- **GIVEN** a runner host that is destroyed and re-provisioned from its declaration
- **WHEN** the runner re-registers
- **THEN** it carries the same labels it carried before, without an operator
  re-entering them

#### Scenario: A workflow naming an unmatched label is not silently misrouted

- **WHEN** a workflow job names a label no runner in the group carries
- **THEN** the job remains unstarted rather than executing on a runner that does not
  match

### Requirement: Runner identity is established from a stored organisation credential

Runner registration SHALL be performed by automation that reads a long-lived
organisation credential from the platform secret store and exchanges it, at the moment
of registration, for the short-lived credential the forge requires.

The short-lived registration credential SHALL NOT be persisted, logged, or passed on a
command line. The long-lived organisation credential SHALL exist only in the secret
store and SHALL NOT be written to the runner host's durable storage.

Registration SHALL be idempotent: re-running it against an already-registered runner
converges on the declared state rather than creating a duplicate identity or failing.

#### Scenario: A runner registers without an operator handling a token

- **GIVEN** the organisation credential is present in the secret store
- **WHEN** registration runs for a declared runner
- **THEN** the runner appears in the organisation's runner group with its declared name,
  labels, and group, and no registration credential appears in the run's output or on
  the host's disk

#### Scenario: Registration is re-runnable

- **GIVEN** a runner that is already registered and online
- **WHEN** registration runs again for that same runner
- **THEN** the run succeeds, the organisation still lists exactly one runner for that
  host, and its declared name, labels, and group are unchanged

#### Scenario: A missing organisation credential fails loudly

- **GIVEN** the organisation credential is absent or invalid
- **WHEN** registration runs
- **THEN** the run fails with a message naming the missing credential, and the runner
  host is left unregistered rather than half-configured

### Requirement: Runner identity is revocable independently of the host

An operator SHALL be able to remove a runner's ability to receive work without
destroying the host, and SHALL be able to revoke the organisation credential without
touching any runner host.

After revocation, a host that is still running SHALL NOT be able to obtain new work.

#### Scenario: A single runner is withdrawn

- **WHEN** an operator de-registers one runner
- **THEN** that runner no longer appears in the organisation's runner group, receives no
  further jobs, and the other runner continues to serve work

#### Scenario: Credential revocation stops all registration

- **GIVEN** the organisation credential has been revoked
- **WHEN** registration is attempted for any runner
- **THEN** it fails, and no host can join the plane

### Requirement: Each job is isolated from the host and from the preceding job

The job workspace SHALL be destroyed when the job ends, so that no file, credential, or
process left by one job is visible to the next — including when the next job belongs to
a different repository. This SHALL be enforced by the runner host's configuration, not
requested by the workflow.

Job steps SHALL execute as an account holding no administrative privilege on the host,
so that a job cannot modify the host it runs on.

**Per-job containerisation is NOT claimed as an enforced control.** The container-hook
mechanism manages containers for a job that *declares* one; it does not place an
undeclared job into a container, so a workflow that requests no container executes
against the host filesystem as the unprivileged runner account. Enforcing
containerisation for every job requires a different lifecycle — an ephemeral runner
whose process and filesystem are themselves containerised and recreated per job — and
that is recorded as follow-up work rather than asserted here.

Consequently the isolation this capability guarantees is: workspace destruction between
jobs, no host administration from a job, and the egress containment required below. A
job CAN read anything the runner account can read on that host, so nothing may be placed
on a runner host that every permitted repository is not entitled to.

#### Scenario: A job cannot see the previous job's leftovers

- **GIVEN** a job that writes a file into its workspace and completes
- **WHEN** a subsequent job runs on the same runner
- **THEN** that file is not present in the subsequent job's workspace

#### Scenario: Workspace destruction does not depend on the workflow asking for it

- **GIVEN** a workflow whose job makes no cleanup request of its own
- **WHEN** the job runs on the plane and a later job follows it
- **THEN** the workspace the earlier job wrote is gone, because the wipe is configured on
  the host rather than requested by the workflow

#### Scenario: A job cannot escalate to host administration

- **WHEN** a job attempts to act as a privileged user on the runner host
- **THEN** the attempt fails, because the identity the runner executes as holds no
  administrative privilege on the host

### Requirement: The runner host is denied the platform's privileged interior

A runner host SHALL be treated as semi-trusted, because it executes code authored in a
repository.

The runner host SHALL be denied network access to the platform's secret store, its
hypervisor management interface, and its orchestrator. Denial SHALL be enforced at the
network boundary, not merely by withholding credentials, so that a credential leaked
into a workflow cannot be used from that host.

The runner host SHALL hold no platform service credential beyond what its own operation
requires.

#### Scenario: The secret store is unreachable from a job

- **WHEN** a job running on the plane attempts to reach the platform secret store
- **THEN** the connection is refused or dropped, independent of whether the job
  possesses any credential

#### Scenario: The hypervisor and orchestrator are unreachable from a job

- **WHEN** a job attempts to reach the hypervisor management interface or the
  orchestrator
- **THEN** the connection is refused or dropped

#### Scenario: Legitimate workflow egress still works

- **WHEN** a job fetches its repository, downloads a declared dependency, or reaches a
  platform service a workflow is legitimately intended to exercise
- **THEN** the connection succeeds

### Requirement: The runner host meets the platform host-access baseline

A runner host SHALL satisfy the platform's existing host-access baseline —
administrative credentials held in the secret store, key-only administrative access, and
a default-deny inbound firewall.

A runner host SHALL publish no inbound service port. It obtains work by reaching out to
the forge, so no inbound allowance beyond administrative access is warranted, and any
such allowance MUST be treated as a defect.

Hardening SHALL be ordered so that replacement access is proven to work before the
bootstrap access path is withdrawn.

#### Scenario: No service port is exposed

- **WHEN** the runner host's reachable inbound ports are enumerated from the network
- **THEN** only administrative access from the declared administrative ranges is
  permitted, and no runner-related service port is listening for inbound connections

#### Scenario: Bootstrap access is withdrawn only after key access is proven

- **GIVEN** a newly provisioned runner host still reachable by its bootstrap credential
- **WHEN** hardening runs
- **THEN** key-only access is verified to work before the bootstrap path is disabled, and
  a failure of that verification aborts before anything is disabled

### Requirement: Runner capacity and placement are declared, and adding a runner is declaration-only

Each runner host's allocation — its identifier, its placement on the hypervisor, its
cores, memory, disk, and address — SHALL be declared as configuration and SHALL be the
single source that both provisioning and later resizing read.

Its address SHALL be recorded in that same declaration, which is the allocation record
for these hosts. The declaration — not an operator's inspection of the network — is what
makes an address taken.

**The IPAM system is deliberately NOT the record of record here.** Its discovery pipeline
has written nothing since 2026-04-23 (diagnosed in `plan/development/04-netbox-discovery.md`),
so it currently reflects neither what is allocated nor what exists. Recording these two
addresses there by hand would create an entry that looks authoritative while the system
around it is stale — worse than a gap, because a stale record is trusted. Once discovery
is repaired it will register these hosts on its own, and at that point IPAM becomes a
cross-check against the declaration rather than a second thing to maintain.

Adding a further runner SHALL require only a new declaration plus a run of the existing
automation against it. If adding one requires editing the automation, the automation
does not satisfy this requirement.

#### Scenario: A second runner is added without changing the automation

- **GIVEN** one runner already stood up by the automation
- **WHEN** a second runner is declared and the same automation is run against it
- **THEN** the second runner is provisioned, hardened, registered, and serving work,
  and no automation file needed to change to make that happen

#### Scenario: The declaration is the allocation record

- **WHEN** a runner's address is assigned
- **THEN** it is written into that host's declaration, and any later reader of the
  declaration can see which address belongs to which host without consulting anything
  else

#### Scenario: A stale authority is not treated as a record

- **GIVEN** an address-discovery pipeline that has not written for an extended period
- **WHEN** a new host's address is allocated
- **THEN** it is NOT hand-written into that pipeline's store, because an entry that looks
  current inside a stale store is trusted more than it deserves; the declaration carries
  it instead, and the pipeline is repaired separately

#### Scenario: Declared size is convergeable

- **GIVEN** a live runner host whose declared cores or memory have been changed
- **WHEN** the convergence automation runs
- **THEN** the host's actual allocation is brought into line with the declaration, and a
  run against an already-matching host changes nothing

### Requirement: Runner readiness is verifiable before work is routed to it

The automation SHALL provide a verification that reports, without an operator inspecting
the forge's interface by hand, whether each declared runner is registered, online, and
carrying its declared labels.

Verification SHALL be read-only and safe to run at any time.

#### Scenario: Verification confirms a healthy plane

- **WHEN** verification runs against a fully stood-up plane
- **THEN** it reports each declared runner as registered, online, and carrying its
  declared labels

#### Scenario: Verification detects a runner that is registered but not online

- **GIVEN** a runner whose host is powered off
- **WHEN** verification runs
- **THEN** it reports that runner as not online rather than passing

#### Scenario: Verification detects label drift

- **GIVEN** a runner whose labels no longer match its declaration
- **WHEN** verification runs
- **THEN** it reports the mismatch, naming the declared and actual labels

### Requirement: Runner software version is pinned and its integrity verified

The runner software version SHALL be declared as configuration rather than resolved to
"latest" at install time, so that two hosts installed at different times run the same
version and an upgrade is a deliberate, reviewable change.

The downloaded runner software SHALL be verified against a published checksum before it
is executed, and a mismatch MUST abort the install.

Automatic self-update SHALL be disabled, so that the running version remains the
declared one.

#### Scenario: Two hosts installed at different times match

- **GIVEN** a declared runner version
- **WHEN** two runner hosts are installed weeks apart from the same declaration
- **THEN** both run the declared version

#### Scenario: A corrupted download aborts the install

- **WHEN** the downloaded runner software does not match its published checksum
- **THEN** the install fails without executing the downloaded artefact

#### Scenario: The running version does not drift

- **GIVEN** a runner running the declared version and a newer version published upstream
- **WHEN** the runner continues to serve jobs
- **THEN** it still reports the declared version, and upgrading requires changing the
  declaration
