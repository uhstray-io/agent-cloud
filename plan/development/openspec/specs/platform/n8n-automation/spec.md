# platform/n8n-automation Specification

## Purpose
n8n as a composable, OpenBao-sourced workflow-automation service: its production
deployment, the preservation of its stateful secrets across redeploys, the custody
of its API key, and the code-managed provisioning of the nodes and credentials its
workflows depend on (Postiz first).
## Requirements
### Requirement: Stateful secrets survive the composable cutover

The migration of a live n8n instance from the legacy deploy path to the composable
deploy MUST NOT regenerate any stateful secret. The encryption key and both
database passwords MUST be captured from the live instance into the secret store
before the first composable deploy, and that deploy MUST fetch them rather than
generate new values. A cutover run whose rendered environment differs from the
live environment in any stateful value MUST NOT be applied.

#### Scenario: Pre-seed makes the first composable deploy a fetch

- **WHEN** the live n8n host's environment file is pre-seeded into the secret
  store and the composable deploy then runs against that host
- **THEN** every stateful secret in the rendered environment is byte-identical to
  the live value, and no stored workflow credential becomes undecryptable

#### Scenario: Cutover refuses to proceed on a stateful mismatch

- **WHEN** the rendered environment is compared against the live environment
  before containers are restarted and any stateful value differs
- **THEN** the cutover stops before touching the running service and reports
  which key differs, without printing either value

### Requirement: The n8n API key lives in the secret store, never in output

n8n's automation API key SHALL be stored in the secret store under the n8n
service's own path. No task output, log, or orchestrator run record may contain
the key value; capture and verification steps MUST reference it only by name.

#### Scenario: Key capture leaves no trace in the run record

- **WHEN** the API-key capture workflow runs to completion
- **THEN** the key is readable at the n8n service's secret path, and the
  orchestrator's task output contains only field names and confirmation counts

### Requirement: Community nodes are declared, not clicked

Every community node installed in n8n MUST be declared in configuration with a
pinned name and version, reconciled at instance startup. Installing or removing
nodes through the n8n UI SHALL be disabled; a node absent from the declaration is
removed on the next deploy.

#### Scenario: Declared node present after redeploy

- **WHEN** the Postiz community node is declared in the deployment configuration
  and the service is redeployed from scratch
- **THEN** the node is installed at the pinned version and usable in workflows,
  with no manual installation step

#### Scenario: Undeclared node removed by reconciliation

- **WHEN** a node exists in the instance but not in the declaration and the
  service restarts
- **THEN** the node is uninstalled and UI-based installation remains disabled

### Requirement: The Postiz credential is provisioned as code from shared custody

n8n's Postiz credential MUST be created by automation reading the Postiz API key
from the Postiz service's secret path (shared read — never a copy stored under
the n8n service's path) and MUST target the self-hosted Postiz host over the
public TLS-terminated route, per the Postiz automation contract.

#### Scenario: Credential works against the self-hosted instance

- **WHEN** the credential-provisioning workflow has run and a workflow node tests
  the Postiz connection
- **THEN** the connection test succeeds against the platform's own Postiz host,
  and the API key value exists in exactly one secret path

#### Scenario: Provisioning is idempotent

- **WHEN** the credential-provisioning workflow runs a second time with no
  changes
- **THEN** the existing credential is left in place or updated in place — a
  duplicate credential is never created

### Requirement: Every n8n lifecycle operation is runnable from the orchestrator

Each n8n lifecycle concern — deploy, stateful-secret pre-seed, destructive clean
deploy, API-key capture, and credential provisioning — SHALL be an independently
runnable orchestrator task defined as code. No lifecycle operation may require a
manual SSH session.

#### Scenario: Pre-seed and clean deploy exist as orchestrator tasks

- **WHEN** an operator opens the orchestrator's task list
- **THEN** the stateful-secret pre-seed and the destructive clean deploy are
  each present as declared templates, and the destructive one is clearly marked

### Requirement: Re-running the completed cutover changes nothing

After the cutover is validated, re-running the composable deploy against the
production host MUST converge with no changes: same secrets fetched, same
environment rendered, containers untouched unless the image pin changed.

#### Scenario: Idempotent redeploy after cutover

- **WHEN** the composable deploy runs twice in succession against the cut-over
  production host with no configuration change between runs
- **THEN** the second run reports no stateful change and every stored workflow
  credential remains decryptable
