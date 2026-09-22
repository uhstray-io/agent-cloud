## Purpose

Defines the agents that run the service deployment workflow, what each may do, how their
proposals are policed, and how the platform's inference edge and skynet divide
responsibility for the inference hostname.

## ADDED Requirements

### Requirement: Four least-privilege agent identities
The OPA catalog SHALL define `infra-agent`, `security-agent`, `o11y-agent` and
`service-agent`, each with an explicit list of Semaphore templates it may launch. A launch of
any template outside the caller's list MUST be denied. Destructive templates MUST still
require an attested human approval. `netclaw` and `nemoclaw` MUST remain in the catalog,
unchanged, and MUST NOT be used as workflow agents.

#### Scenario: Role launches only its own steps
- **WHEN** `o11y-agent` requests `run_task` on the firewall template
- **THEN** OPA denies with a reason naming the template allowlist

#### Scenario: Destructive template still needs a human
- **WHEN** any agent requests a `Clean Deploy` template without human approval
- **THEN** OPA denies

### Requirement: Proposal content is policed, not only its shape
OPA SHALL evaluate the proposal body for the firewall, access and service assessments. A
firewall proposal MUST be denied when it removes SSH access from the orchestrator's declared
source, or allows SSH from any source outside the declared SSH sources. A service assessment
MUST be denied when it proposes a runtime action naming a destructive template, or a VM
specification outside the service's declared tier bounds.

#### Scenario: Orchestrator keeps SSH
- **WHEN** a firewall proposal's allow set omits SSH from the orchestrator's source
- **THEN** OPA denies and the step records the denial

#### Scenario: SSH stays scoped
- **WHEN** a firewall proposal allows SSH from a source not in the declared SSH sources
- **THEN** OPA denies

### Requirement: The inference edge owns the hostname
agent-cloud's gateway SHALL be the only authority for `inference.uhstray.io`: client keys,
budgets, limits, telemetry and every route under it. skynet's orchestration capabilities
MUST be reached through a route on that gateway, gated by the gateway's key policy, and
skynet's own model calls MUST go through the gateway under one client identity per agent
role. skynet MUST NOT serve the model API on that hostname itself.

#### Scenario: Agent calls are attributable per role
- **WHEN** `security-agent` makes a model call
- **THEN** the gateway's telemetry and budget accounting attribute it to that role's identity

#### Scenario: Orchestration route is key-gated
- **WHEN** a request without an enrolled key reaches the skynet route
- **THEN** the gateway answers 401 and skynet never sees it

### Requirement: skynet runs on local-dev through the platform
skynet SHALL be deployed on local-dev by the local Semaphore through a deploy playbook like
any other local service, with its credentials from the local OpenBao, and its workflow state
MUST survive a restart of the skynet process. Its model traffic MUST reach the DGX Spark
vLLM through the local gateway; the upstream address MUST live only in the gitignored local
inventory.

#### Scenario: Restart mid-run
- **WHEN** skynet restarts during a workflow run
- **THEN** the run resumes from its last completed step

#### Scenario: No private address in the public repo
- **WHEN** the security scan runs on the change
- **THEN** no DGX address appears in any committed file

### Requirement: Reasoning quality is measured before trust
Each reasoning step SHALL have recorded eval cases replayed against the served model, and
skynet's CI MUST gate on schema validity, decision match against the recorded verdict and a
zero forbidden-action rate.

#### Scenario: Regression blocks a prompt change
- **WHEN** a prompt change lowers decision match below its threshold
- **THEN** skynet's CI fails
