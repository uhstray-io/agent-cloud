## Purpose

Provides AI-assisted drafting of social posts for the instance's brands, with a person's approval in front of every scheduled or published post. Activepieces flows generate and check drafts through the inference gateway, hand them to the publishing service as drafts, and schedule a draft only after a person has approved that exact revision. The publishing service keeps scheduling and publishing, as the `platform/postiz-publishing` capability describes.

## ADDED Requirements

### Requirement: Drafts need no approval; scheduling and publishing need a recorded approval

Generation MAY create a draft without an approval. The platform MUST NOT schedule or publish a post unless a recorded approval names the approver and the revision of that post.

An approval SHALL apply only to the revision it names. An edit to an approved post creates a new revision, and that revision MUST NOT be scheduled or published on the strength of the earlier approval.

#### Scenario: Draft created without approval

- **WHEN** a generation run produces a post for a brand
- **THEN** the post exists as a draft, and no approval is needed for it to exist

#### Scenario: Schedule refused without a recorded approval

- **WHEN** a handoff attempts to schedule a draft and no approval naming the draft's current revision is recorded
- **THEN** the draft stays a draft, nothing is scheduled, and the refusal is recorded

#### Scenario: Approval of one revision does not schedule the next revision

- **GIVEN** revision 1 of a draft has a recorded approval
- **WHEN** the draft is edited to revision 2 and a handoff attempts to schedule it
- **THEN** revision 2 is not scheduled until it has its own recorded approval

### Requirement: Flows never publish immediately

No flow SHALL create a post that publishes immediately. Every post a flow creates is a draft, and a post becomes scheduled only through the status change that follows a recorded approval.

#### Scenario: Flow definition requests immediate publication

- **WHEN** a flow definition is checked before it is applied and the definition requests a post type that publishes immediately
- **THEN** the check fails and the flow is not applied

#### Scenario: Generated draft is not published at creation

- **WHEN** a generation run completes and creates a post
- **THEN** the post is in draft state and has been neither scheduled nor published

### Requirement: Handoff to the publishing service is exactly once

A handoff SHALL create at most one publishing-service post for a draft, and SHALL schedule each approved revision at most once. A retried handoff MUST find the post it already created and MUST NOT create another.

#### Scenario: Retried handoff creates no second post

- **GIVEN** a handoff created a publishing-service post for a draft and then failed before it recorded the result
- **WHEN** the handoff is retried
- **THEN** the existing post is found, and no second post is created for that draft

#### Scenario: Retried schedule creates no second schedule

- **GIVEN** an approved revision has already been scheduled
- **WHEN** the schedule step runs again for the same revision
- **THEN** the publishing service holds exactly one schedule for that revision

### Requirement: Guardrail checks run before approval

A draft MUST pass the deterministic guardrail checks before it is offered for approval. A draft that fails a check MUST be returned with the reason for the failure and MUST NOT reach the approval step.

The checks cover the length limit of each target platform, a list of banned terms declared for the brand, links that resolve to allowed domains, and internal hostnames and credential-shaped strings.

#### Scenario: Failing draft returned with its reason

- **WHEN** a draft exceeds the length limit of its target platform
- **THEN** the draft is returned with the failing check named, and it does not reach the approval step

#### Scenario: Draft naming an internal hostname is refused

- **WHEN** a draft contains a hostname that resolves only inside the platform's internal zones
- **THEN** the draft is refused and does not reach the approval step

#### Scenario: Passing draft reaches the approval step

- **WHEN** a draft passes every guardrail check
- **THEN** it is offered for approval, and the guardrail step does not schedule it

### Requirement: Each draft targets exactly one mapped account of its brand

A draft MUST name exactly one target account. A handoff MUST refuse a target integration that the declared brand mapping does not assign to the draft's brand, before it creates any post. A post to any other account MUST have its own recorded approval.

#### Scenario: Target integration not mapped to the brand is refused

- **WHEN** a handoff names a publishing-service integration that the declared brand mapping does not assign to the draft's brand
- **THEN** the handoff is refused before any post is created

#### Scenario: A draft names exactly one target

- **WHEN** a draft is created
- **THEN** it names one target account, and a draft that names no target or more than one target is refused

#### Scenario: Fan-out to another account requires its own approval

- **WHEN** an approved draft is to be posted to a second account
- **THEN** that second post has its own recorded approval before it is scheduled

### Requirement: Flows and connections are declared as code and applied by playbook

Flow definitions and connections SHALL be declared in the repository and applied by a playbook that reads credentials from the secret store. A re-run of the apply MUST converge: when the declarations are unchanged, the re-run MUST report no change. The apply MUST NOT delete a flow it does not own. Flow definitions MUST NOT contain a credential value.

#### Scenario: Re-run of the apply playbook converges

- **GIVEN** the flows and connections were applied from declarations that have not changed
- **WHEN** the apply playbook runs again
- **THEN** it reports no change

#### Scenario: Unowned flow is never deleted

- **GIVEN** a flow exists in Activepieces that the repository does not declare
- **WHEN** the apply playbook runs
- **THEN** that flow is left in place

#### Scenario: Flow definition holds no credential value

- **WHEN** the committed flow definitions are inspected
- **THEN** they refer to connections by name and contain no literal credential value

#### Scenario: Connection credential written from the secret store

- **WHEN** the connections are applied
- **THEN** each connection's credential is read from the secret store during the run and written into the connection, and the value is not printed

### Requirement: Generation reaches the model only through the inference gateway under its own identity

Text generation MUST reach the model only through the inference gateway, authenticated as the client identity `activepieces`. A flow MUST NOT call a model provider or an inference backend directly.

#### Scenario: Generation calls the gateway under the activepieces identity

- **WHEN** a generation step calls the model
- **THEN** the request is authenticated with the client key for the `activepieces` identity and is served through the gateway

#### Scenario: Reply outside the requested schema is not drafted

- **WHEN** the model's reply does not match the structured output schema that the flow requested
- **THEN** no draft is created from that reply, and the run is recorded as failed

### Requirement: A failed content flow run raises an alert

A content flow run that ends in failure MUST raise an alert through the platform's existing observability alert route. A run that completes without failure MUST NOT raise a failure alert.

#### Scenario: Failed run raises an alert

- **WHEN** a content flow run ends in failure
- **THEN** an alert is raised through the platform's observability alert route

#### Scenario: Successful run raises no alert

- **WHEN** a content flow run completes without failure
- **THEN** no failure alert is raised for that run
