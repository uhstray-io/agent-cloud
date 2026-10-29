## ADDED Requirements

### Requirement: The runner lifecycle is a per-host declaration

Each Linux runner host SHALL declare its lifecycle, `persistent` or `ephemeral`, as
configuration, and `persistent` SHALL be the default. A change to the automation SHALL NOT
change the lifecycle, the runner unit, or the registration of a host whose declaration did
not change.

#### Scenario: Merging the ephemeral lifecycle changes no persistent host

- **GIVEN** a host with no lifecycle declared
- **WHEN** the runner deploy runs after this change merges
- **THEN** its runner unit renders identically, its persistent registration is kept, and no
  supervisor, stock or seed is created on it

#### Scenario: A host moves back to the persistent lifecycle

- **GIVEN** an ephemeral host whose declaration is set back to `persistent`
- **WHEN** the runner deploy runs
- **THEN** the supervisor, its stock, its seed and the host's ephemeral runner records are
  removed, and the host registers and serves as a persistent runner again

### Requirement: An ephemeral host serves each job with a runner that accepts only that job

On a host declaring the ephemeral lifecycle, every job SHALL be served by a runner started
from a just-in-time configuration minted for that runner, which the service binds to one
job, and the runner SHALL NOT be restarted after it exits. The host SHALL hold no persistent
runner registration.

#### Scenario: A runner accepts only one job

- **WHEN** an ephemeral host's runner completes a job, successfully or not
- **THEN** that runner no longer appears in the runner group, and the next job on the host is
  served by a different runner

#### Scenario: The persistent registration is retired

- **GIVEN** a persistent host switched to the ephemeral lifecycle
- **WHEN** the runner deploy completes
- **THEN** the host's persistent runner record no longer exists at the organisation

### Requirement: Ephemeral runner credentials are minted on the orchestrator and held root-only on the host

Just-in-time configurations SHALL be minted by orchestrator-launched automation from the
App credential in the platform secret store, and SHALL bind the runner's name, group and
labels from the declaration. The App credential SHALL NOT be written to a runner host. On
the host, a configuration SHALL be readable only by root until the runner that uses it
starts, SHALL be used for one runner only, and SHALL NOT appear on any command line or in
any visible task output.

#### Scenario: A job cannot read the next job's runner credential

- **GIVEN** an ephemeral host with configurations in its stock
- **WHEN** a job running on that host tries to read the stock
- **THEN** the read is denied, because the stock is owned by root with no access for others

#### Scenario: A configuration is never passed on a command line

- **WHEN** the supervisor starts a runner
- **THEN** no process's argument list on the host contains the configuration

#### Scenario: Runner records that can no longer take a job are removed

- **GIVEN** a minted configuration that was discarded, expired, or whose runner never started
- **WHEN** the scheduled stock automation runs
- **THEN** its runner record is deleted, and no runner that is running or stocked is deleted

### Requirement: Nothing of one job survives into the next job on an ephemeral host

Between every two jobs on an ephemeral host, every process of the runner account and of its
subordinate ids SHALL be ended, wherever it runs; every file those ids own on the host's
local filesystems SHALL be removed; their System V IPC objects SHALL be removed and the
account's keyrings cleared; and the runner's home SHALL be rebuilt from a root-owned seed
that holds no registration. This SHALL hold whether the job succeeded, failed or was
cancelled, and SHALL NOT depend on the workflow.

#### Scenario: A leftover process from a failed job does not reach a job of another repository

- **GIVEN** a job from one permitted private repository that writes a unique marker into its
  workspace and into a temporary directory, starts a uniquely named background process, and
  then fails
- **WHEN** a job from a different permitted private repository runs next on the same host
- **THEN** that job finds neither marker and cannot see, signal or attach to the process,
  because the process no longer exists

#### Scenario: A leftover process from a cancelled job does not reach a job of another repository

- **GIVEN** the same job, cancelled while its background process runs
- **WHEN** a job from a different permitted private repository runs next on the same host
- **THEN** the same holds as for the failed job

#### Scenario: A container a job left running is ended

- **GIVEN** a job that starts a detached rootless container and ends
- **WHEN** the next job starts
- **THEN** no process of that container exists and its image store is gone

### Requirement: An ephemeral host that cannot prove a clean reset takes no job

If the reset cannot verify that no process, owned file or IPC object of the account remains,
or if runners on the host repeatedly exit without taking a job, the host SHALL stop taking
jobs, SHALL NOT retry by itself, and the condition SHALL be reported by the scheduled
automation until a deploy verifies a clean reset.

#### Scenario: A failed reset stops the host

- **GIVEN** an ephemeral host whose reset finds a process it cannot end
- **WHEN** the reset finishes
- **THEN** no runner is started on that host, the scheduled stock template fails naming the
  reason, and the other host keeps serving jobs

#### Scenario: A deploy clears the stop only after a clean reset

- **GIVEN** a stopped ephemeral host
- **WHEN** the runner deploy runs and its own reset verifies clean
- **THEN** the host serves jobs again, and if the reset is not clean the deploy fails and the
  host stays stopped
