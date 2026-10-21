## ADDED Requirements

### Requirement: Each macOS job runs in a virtual machine created for it and destroyed after it

Every job dispatched to the macOS member SHALL execute in a virtual machine cloned for that
job from the current golden image, registered as a runner that accepts exactly one job,
and destroyed when that job ends, whether it succeeded, failed or was cancelled.

No file, process, keychain, installed software or configuration change made by one job
SHALL be present when any other job starts.

#### Scenario: A macOS job cannot see the previous job's machine

- **GIVEN** a macOS job that writes a file, starts a background process, adds a keychain
  and deletes an application
- **WHEN** a later macOS job runs on the same host
- **THEN** the file, the process and the keychain are absent and the application is
  present, because the later job runs in a different machine

#### Scenario: A runner accepts only one job

- **WHEN** a macOS runner completes its job
- **THEN** it is absent from the runner group and its virtual machine no longer exists

#### Scenario: A cancelled job's machine is destroyed

- **GIVEN** a macOS job cancelled while it runs
- **WHEN** the cancellation completes
- **THEN** the virtual machine that ran it no longer exists

### Requirement: Administrative rights inside a disposable machine are a scoped exception

The job account inside a macOS job's virtual machine MAY hold administrative rights over
that virtual machine, because the machine is destroyed after the job. This exception SHALL
apply only to a machine that is destroyed after a single job. The physical host SHALL grant
jobs no access of any kind, and a job SHALL NOT be able to reach the host over the network.

#### Scenario: Administration inside the VM does not reach the host

- **WHEN** a macOS job uses its administrative rights inside its virtual machine and then
  attempts to connect to the physical host
- **THEN** the connection fails, and nothing on the host changed

### Requirement: Concurrent macOS virtual machines never exceed the licensed limit

A macOS host SHALL run at most two macOS virtual machines at the same time, the number
Apple's licence permits per Apple-branded computer. The limit SHALL be enforced by the
supervisor's declared slot count, which MUST NOT be declarable above two.

#### Scenario: A third job queues

- **GIVEN** two macOS jobs running on one host
- **WHEN** a third macOS job is queued
- **THEN** it waits for a slot, and no third virtual machine is started

#### Scenario: An over-limit declaration is refused

- **WHEN** a macOS host is declared with more than two slots
- **THEN** the supervisor deploy refuses, naming the limit

### Requirement: The macOS golden image is built by code from pinned inputs

The golden image SHALL be produced by an orchestrator-launched operation from a checked-in
template and declared, pinned inputs: the macOS version, the Xcode version and the digest
of its archive, and the runner version and its published checksum. A digest mismatch MUST
abort the build. A new image SHALL be smoke-tested before it is promoted, and promotion
SHALL be a change to the declared image name.

#### Scenario: The image is rebuilt from code

- **GIVEN** the golden image has been deleted
- **WHEN** the image operation runs from the declaration
- **THEN** an image with the declared macOS, Xcode and runner versions exists, built with no
  interactive step beyond the operator-supplied Xcode archive

#### Scenario: An Xcode archive with the wrong digest aborts the build

- **WHEN** the Xcode archive on the host does not match its declared digest
- **THEN** the image build fails before installing it, and the current image is unchanged

### Requirement: A macOS job cannot reach the platform's private network

A macOS job's virtual machine SHALL have no route to private address ranges, the platform
network included, nor to other virtual machines or the host, while still reaching the
public services a build needs. The host SHALL also enforce the plane's shared egress-denial
declaration for its own traffic.

#### Scenario: The platform interior is unreachable from a macOS job

- **WHEN** a macOS job attempts to reach the secret store, the orchestrator, a hypervisor,
  or any other private address
- **THEN** the connection fails

#### Scenario: Build egress from a macOS job still works

- **WHEN** a macOS job fetches its repository, the Rust toolchain, a Homebrew package, or
  submits to Apple's notarisation service
- **THEN** the connection succeeds

### Requirement: The macOS host's runner credential never enters a job's machine

The application credential used to mint macOS runner configurations SHALL be held only by
the host's supervisor, SHALL be placed there by automation from the secret store, and SHALL
NOT be present in any virtual machine. Each runner configuration SHALL be minted per
virtual machine, used once, and not persisted.

#### Scenario: A job cannot read the application key

- **WHEN** a macOS job searches its virtual machine for the application private key
- **THEN** it is not present

#### Scenario: Revoking the macOS credential stops only macOS

- **GIVEN** a macOS member using its own application credential
- **WHEN** that credential is revoked
- **THEN** no new macOS runner can register, and the Linux and Windows members keep serving
  work

### Requirement: The macOS member has a one-variable hosted fallback

A workflow targeting the macOS member SHALL select it through a repository variable whose
alternative value selects GitHub's hosted macOS runner, and the workflow SHALL complete on
either without other edits.

#### Scenario: Switching to hosted runners is one change

- **GIVEN** a macOS workflow running on the self-hosted member
- **WHEN** the selecting variable is set to the hosted label
- **THEN** the next run executes on a hosted runner and completes, with no workflow edit
