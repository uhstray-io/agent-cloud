## Purpose

Agent-initiated Semaphore launches go through one gate outside the agent: it validates exactly what a run
will receive, asks OPA server-side, and launches with the only launch-capable identity, so a compromised
or mistaken agent cannot make a run do anything the template and policy do not allow.

## ADDED Requirements

### Requirement: Only the launch gate holds a launch-capable identity for agents
No agent service (skynet, the Dispatch Console) SHALL hold a Semaphore token whose project role can
create tasks. The launch gate's own identity SHALL be the only non-human Semaphore member with a role
that can run tasks, and every agent-initiated launch MUST be created by that identity.

#### Scenario: skynet's own token cannot launch
- **WHEN** skynet's Semaphore token sends `POST /api/project/{p}/tasks`
- **THEN** Semaphore answers 403 and no task is created

#### Scenario: Declared team matches
- **WHEN** Manage Semaphore Access runs in check mode
- **THEN** the only non-human member with a role that can run tasks is the launch gate's identity

### Requirement: Callers are authenticated and bound to their agent identities
The launch gate SHALL require a bearer token per caller and MUST map each token to a fixed set of
agent identities that caller may act as. A request naming an agent outside that set, or carrying no
valid token, MUST be refused before any call to OPA or Semaphore.

#### Scenario: Missing token
- **WHEN** a request arrives without a valid bearer token
- **THEN** the launch gate answers 401 and calls neither OPA nor Semaphore

#### Scenario: Caller claims another identity
- **WHEN** the console's token sends a launch request naming agent `infra-agent`, which only skynet's
  token may act as
- **THEN** the launch gate answers 403 and calls neither OPA nor Semaphore

### Requirement: A launch carries only declared, literal survey values
The launch gate SHALL accept a launch only for a template on its allowlist, and only with values for
survey fields that template declares. It MUST refuse a missing required field, an undeclared key, a
value that is not a string, and any value containing `{{`, `{%` or `{#`. The task it creates MUST
contain exactly the validated survey values (with declared defaults filled in for omitted optional
fields) and no other extra variable, inventory, limit, tag or argument override.

#### Scenario: Undeclared key is refused
- **WHEN** a request for template `Deploy skynet (Dev)` includes a key the template does not declare
- **THEN** the launch gate answers 400 naming the key, and calls neither OPA nor Semaphore

#### Scenario: Templated value is refused
- **WHEN** a request carries a survey value containing `{{`
- **THEN** the launch gate answers 400 and calls neither OPA nor Semaphore

#### Scenario: Template off the allowlist
- **WHEN** a request names a template that is not on the launch gate's allowlist (for example
  `Destroy VM`)
- **THEN** the launch gate answers 403 and calls neither OPA nor Semaphore

#### Scenario: The created task holds only validated values
- **WHEN** an allowed launch is created
- **THEN** the task's recorded environment holds exactly the validated survey values plus declared
  defaults, and nothing else

### Requirement: OPA decides server-side before every launch
For every request that passes validation, the launch gate SHALL query OPA's decision endpoint itself,
with the caller's agent identity, action `run_task`, the template name, the step, the Git branch the
template runs from and the exact survey values to be launched, and MUST launch only when OPA answers
`allowed: true`. An OPA deny, an error, an unreachable OPA or a response without `allowed` MUST
result in no launch.

#### Scenario: OPA denies
- **WHEN** OPA answers `allowed: false` for a valid request
- **THEN** the launch gate answers 403 with OPA's reason and creates no task

#### Scenario: OPA unreachable
- **WHEN** OPA cannot be reached or answers without `allowed`
- **THEN** the launch gate answers 503 and creates no task

#### Scenario: OPA allows
- **WHEN** OPA answers `allowed: true`
- **THEN** the launch gate creates exactly one task and answers with its id

### Requirement: Launches are idempotent and recorded
The launch gate SHALL refuse to create a second task for a template whose previous task for the same
workflow and step is still running, SHALL tag every task it creates with a message naming the caller,
workflow and step (skynet's tasks keep the `skynet <workflow_id> <step_id>` form the console reads),
and MUST log each decision and launch (caller, agent, template, step, OPA verdict, task id) without
any secret value.

#### Scenario: Duplicate launch while running
- **WHEN** a request repeats a launch whose earlier task for that step is still running
- **THEN** the launch gate answers 409 naming the running task and creates no task

#### Scenario: Task message for skynet
- **WHEN** skynet launches step `ssh-keys` of workflow `onboard-x`
- **THEN** the created task's message is `skynet onboard-x ssh-keys`

### Requirement: Check-mode launches stay check mode
The launch gate SHALL support a dry-run flag and MUST create a check-mode task when it is set, refusing
the request instead of creating a real run on a Semaphore version whose check-mode parameter shape is
not verified.

#### Scenario: Dry run
- **WHEN** a request sets the dry-run flag
- **THEN** the created task records check mode, or no task is created

### Requirement: The gate is reachable only by its callers over TLS
The launch gate SHALL serve HTTPS only, from an internal-CA server leaf renewed without a restart, and
its host MUST admit inbound API traffic only from the skynet and console hosts.

#### Scenario: Plain HTTP
- **WHEN** a client sends plain HTTP to the launch gate's port
- **THEN** no HTTP response with status 200 is returned

#### Scenario: Another LAN host
- **WHEN** a LAN host that is neither skynet nor the console connects to the launch gate's port
- **THEN** the connection is refused
