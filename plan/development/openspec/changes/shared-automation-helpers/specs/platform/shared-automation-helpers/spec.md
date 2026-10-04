## Purpose

Keeps one implementation of each recurring automation skeleton in the composable task
library, so credential, verdict and probe code cannot drift between playbooks.

## ADDED Requirements

### Requirement: OpenBao AppRole login goes through one task
Playbooks SHALL authenticate to OpenBao through `tasks/bao-login.yml`, which MUST assert the
transport first, MUST be the only `no_log` boundary for the login, and MUST set a single
token fact. A guard test MUST fail when the count of hand-rolled `auth/approle/login` sites
outside the task library rises.

#### Scenario: A new hand-rolled login is refused
- **WHEN** a playbook adds its own `auth/approle/login` request
- **THEN** the ratchet test fails naming the file

#### Scenario: Cleartext endpoint refused before login
- **WHEN** `bao-login.yml` runs against a public cleartext URL
- **THEN** it fails before any credential is sent

### Requirement: Step results fold per-host evidence and capture failures safely
`tasks/emit-step-result.yml` SHALL accept per-host errors and evidence and fold them into one
verdict. Rescue blocks MUST record failure through `tasks/capture-failure.yml`, which MUST NOT
emit the result of a `no_log` task.

#### Scenario: One host fails
- **WHEN** one of two hosts fails a step
- **THEN** the folded result is `fail` and names that host

#### Scenario: A no_log task fails
- **WHEN** a `no_log` task fails inside a rescued block
- **THEN** the captured message carries the task name and no result content

### Requirement: Shared secret reads and deploy-key fetch
The agentgateway secret SHALL be read through `tasks/agw-read-secret.yml`, and
`tasks/site-config-clone.yml` SHALL fetch its own deploy key, so no caller reads either path
directly.

#### Scenario: Callers keep their view
- **WHEN** the rollback and conformance playbooks use the shared read
- **THEN** their characterization tests pass unchanged

### Requirement: Refactors are behaviour-preserving
Each helper adoption MUST be preceded by a characterization test of the replaced behaviour,
and that test MUST pass after the refactor.

#### Scenario: Refactor PR gate
- **WHEN** a helper PR replaces a copy
- **THEN** the PR contains the characterization test and it passes before and after

### Requirement: Test suites run in parallel with cached dependencies
CI SHALL run BATS with parallel jobs and cache pip and Ansible collection installs.

#### Scenario: CI run
- **WHEN** the Unit Tests job runs
- **THEN** BATS runs with `--jobs` and dependency installs hit a cache key
