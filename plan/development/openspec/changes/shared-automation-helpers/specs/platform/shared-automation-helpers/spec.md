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

### Requirement: Internal-leaf issuance separates validation from implementation
`tasks/issue-internal-leaf.yml` SHALL contain only input validation and the call into
`files/internal_leaf.py`; leaf classification MUST be a single filter plugin used by every
caller. Issued leaves MUST carry the same subject, SANs, profile and validity as before the
split.

#### Scenario: Leaf fields unchanged
- **WHEN** the same leaf declaration is issued before and after the split
- **THEN** the characterization test finds identical subject, SANs, profile and validity

#### Scenario: Invalid declaration refused before issuance
- **WHEN** a leaf declaration lacks a required field
- **THEN** the task fails naming the field and `internal_leaf.py` is never invoked

### Requirement: Renewal is driven by per-leaf declarations
Certificate renewal SHALL reload and prove each leaf from that leaf's own declaration, and DNS
records for server-profile leaves MUST be derived from those leaves rather than listed
separately.

#### Scenario: New server-profile leaf
- **WHEN** a server-profile leaf is added to the declarations
- **THEN** its DNS record is derived without a second edit, and renewal reloads and proves it

### Requirement: agentgateway readiness and compose helpers are shared
agentgateway readiness SHALL be probed through one `gateway-ready.sh` against the target
declared in `vars/agw-probe-target.yml`. `platform/lib/common.sh` SHALL provide
`compose_files()` and `compose_up_if_changed`; the latter MUST NOT restart containers when no
compose input changed.

#### Scenario: Unchanged inputs
- **WHEN** `compose_up_if_changed` runs with no changed compose file or env file
- **THEN** no container is recreated

#### Scenario: Gateway not ready
- **WHEN** the gateway readiness endpoint does not answer 200 within the bound
- **THEN** `gateway-ready.sh` exits non-zero and every consumer fails the same way

### Requirement: Shared guards are adopted
Playbooks requiring an exact reviewed commit SHALL include `require-reviewed-checkout.yml`,
and `preflight-target-group.yml` SHALL offer a `target_service` mode that resolves the group
from a literal service name.

#### Scenario: Unreviewed checkout
- **WHEN** a guarded playbook runs on a dirty or mismatched checkout
- **THEN** it refuses before any change

#### Scenario: Unknown target service
- **WHEN** the preflight is given a `target_service` with no populated group
- **THEN** it fails naming the service

### Requirement: Hygiene guards
`check-o11y-liveness.yml` SHALL revoke its OpenBao token on every exit path.
`caddy_probe_host` MUST come from inventory with no literal default. OPA `launch_branches` MUST
be tested against the Semaphore repository records. The Discord webhook-URL shape MUST be
checked by one shared task. Proxmox status waits MUST use one shared task.

#### Scenario: Liveness check fails
- **WHEN** the liveness check fails after login
- **THEN** the token is revoked before the play ends

#### Scenario: Probe host unset
- **WHEN** `caddy_probe_host` is not in inventory
- **THEN** the playbook fails naming the variable

#### Scenario: Branch lists diverge
- **WHEN** a branch is in `launch_branches` but in no Semaphore repository record, or the reverse
- **THEN** the sync test fails naming the branch

#### Scenario: Malformed webhook URL
- **WHEN** a stored webhook URL is not https Discord `/api/webhooks/`
- **THEN** the shared assert fails without printing the URL
