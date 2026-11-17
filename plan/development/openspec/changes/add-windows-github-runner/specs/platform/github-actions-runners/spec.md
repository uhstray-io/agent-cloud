## ADDED Requirements

### Requirement: A Windows member's machine configuration is applied by automation, never by a job

The Windows runner host's build toolchain and machine settings (tools, machine
environment, long paths, antivirus exclusions, build volume) SHALL be declared as
configuration and applied by an orchestrator-launched, re-runnable operation.

A job SHALL need no administrative right to build, and the host SHALL NOT grant one: a
step that changes the machine MUST fail or be skipped on this host.

#### Scenario: The release build runs without machine administration

- **GIVEN** the Windows runner host converged to its declared toolchain
- **WHEN** a release workflow whose machine-administration steps are gated to hosted
  runners runs on it
- **THEN** the build completes, and no step of the job ran with administrative rights

#### Scenario: A job cannot change the machine

- **WHEN** a job on the Windows runner attempts to install software machine-wide, write
  the machine registry, or add an antivirus exclusion
- **THEN** the attempt fails, because the runner account holds no administrative right

### Requirement: The Windows toolchain is pinned and integrity-checked

Each tool installed on the Windows runner host SHALL be pinned to a declared version and
verified against a declared digest before it is executed, and a mismatch MUST abort that
install. Re-running the toolchain operation against a converged host SHALL change nothing.

#### Scenario: Toolchain convergence is idempotent

- **GIVEN** a Windows runner host already converged to its declared toolchain
- **WHEN** the toolchain operation runs again unchanged
- **THEN** it reports no change

#### Scenario: A corrupted toolchain download aborts its install

- **WHEN** a downloaded installer does not match its declared digest
- **THEN** that install fails without executing the installer, and the host keeps its
  previous version

### Requirement: The Windows member is built from a template that is itself produced by code

The Windows template SHALL be produced by an orchestrator-launched operation from
declared inputs (installation media, driver media, and a generated answer file), and
SHALL be re-buildable by that operation alone on any declared node.

The answer-file media SHALL carry no long-lived secret, and SHALL be removed from
hypervisor storage when the build ends, including when it fails.

Adding the Windows path SHALL NOT change the existing Ubuntu template path.

#### Scenario: The template is rebuilt from code

- **GIVEN** the Windows template has been deleted
- **WHEN** the template operation runs with the Windows parameters for a declared node
- **THEN** a Windows template exists on that node, built without an operator touching the
  installer

#### Scenario: No answer file is left on hypervisor storage

- **WHEN** a Windows template build finishes, successfully or not
- **THEN** the generated answer-file media is absent from hypervisor storage

#### Scenario: The Ubuntu template path is unchanged

- **WHEN** the template operation runs without the Windows parameters
- **THEN** it issues the same hypervisor requests it issued before this change

### Requirement: Windows administration meets the host-access baseline over key-only SSH

The Windows runner host SHALL be administered over SSH with the platform's issued keys,
and SHALL satisfy the existing host-access baseline: administrative credentials held in
the secret store, key-only administrative access after hardening, and a default-deny
inbound firewall that permits only administrative access from the declared ranges.

The host SHALL expose no remote-management listener other than SSH.

#### Scenario: Key access is proven before password access is withdrawn on Windows

- **GIVEN** a newly provisioned Windows host still reachable by its bootstrap password
- **WHEN** hardening runs
- **THEN** a key-only connection is verified before password authentication is disabled,
  and a failed verification aborts before anything is disabled

#### Scenario: Only SSH is reachable on the Windows host

- **WHEN** the Windows host's reachable inbound ports are enumerated from the network
- **THEN** only SSH from the declared administrative ranges is permitted, and no remote
  desktop, file-sharing or remote-management listener is reachable

### Requirement: Egress denial on Windows comes from the same declaration as on Linux

The Windows runner host SHALL enforce the same declared egress-denial list as the Linux
runner hosts, through the host firewall, so that the secret store, the orchestrator, the
address authority and every hypervisor are unreachable from a job. One declaration SHALL
serve both operating systems; a Windows-only copy of the list MUST NOT exist.

#### Scenario: The platform interior is unreachable from a Windows job

- **WHEN** a job on the Windows runner attempts to reach the secret store, the
  orchestrator, or a hypervisor management interface
- **THEN** the connection is blocked, independent of whether the job possesses any
  credential

#### Scenario: A Windows job cannot remove the denial

- **WHEN** a job on the Windows runner attempts to delete or disable a firewall rule
- **THEN** the attempt fails for lack of administrative rights

### Requirement: Job cleanup covers every declared build root, not only the workspace

On a runner whose workflows build outside the job workspace, every such directory SHALL
be declared. Before every runner start, a supervisor that is not the job account SHALL
stop every process of the runner account, remove its profile, its work directory and
every declared build root, verify that nothing of the account remains, and rebuild the
profile from a machine-owned seed. A runner SHALL NOT start until that verification
passes.

#### Scenario: A release's target directory does not survive into the next job

- **GIVEN** a job that writes into a declared build root outside its workspace
- **AND** the reset before the next runner start verifies clean
- **WHEN** the next job runs on the same runner host
- **THEN** that build root is empty at the start of the next job

#### Scenario: A tool replaced by one job is not run by the next

- **GIVEN** a job that replaces an executable in the runner account's profile
- **WHEN** the next job runs on the same runner host
- **THEN** the next job runs the seed's copy of that executable, not the replacement

#### Scenario: An abrupt reboot does not skip the reset

- **GIVEN** a host that loses power while a job is running
- **WHEN** the host starts again
- **THEN** the supervisor resets the runner account before any runner starts, so no job
  sees what the interrupted job left

### Requirement: An unverifiable reset quarantines the runner host

A reset that cannot be verified SHALL make the host take no job: the supervisor MUST NOT
start a runner, its quarantine state SHALL be stored where the runner account cannot
write, and verification SHALL report the host as quarantined. Only a reset that verifies
clean MAY clear the quarantine.

#### Scenario: A surviving locked file quarantines the host

- **GIVEN** a job that leaves a file locked by a process the reset cannot stop, so the
  reset does not verify
- **WHEN** the supervisor would start the next runner
- **THEN** no runner starts, no job is dispatched to the host, the leftover file is never
  readable by a later job, and verification reports the host as quarantined

#### Scenario: The job account cannot clear the quarantine

- **GIVEN** a quarantined host
- **WHEN** a process running as the runner account attempts to remove or rewrite the
  quarantine state
- **THEN** the attempt fails for lack of rights, and the host stays quarantined

### Requirement: A workflow selects the operating system explicitly

Because every member of the plane carries the plane's shared selection label, a workflow
job that requires a particular operating system SHALL name that operating system's label
alongside the plane label, and the label contract SHALL document this.

#### Scenario: A Windows job lands only on a Windows runner

- **WHEN** a workflow job names `self-hosted`, `windows`, `x64` and the plane label
- **THEN** it runs on the Windows runner and on no Linux runner

### Requirement: The Windows member's licence is a declared fact

The operating-system edition, its licensing state, and the physical node a licence is
assigned to SHALL be recorded in the host's declaration. Provisioning SHALL refuse to
place a licensed Windows host on a node other than the one its licence is declared for.

#### Scenario: A licensed host is not placed off its licensed node

- **GIVEN** a Windows host whose declaration assigns its licence to one node
- **WHEN** provisioning is asked to place it on a different node
- **THEN** provisioning refuses, naming both nodes
