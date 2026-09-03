# Delta: tududi ↔ GitHub issue sync

## Purpose

The bidirectional bridge between tududi projects and GitHub repository issues:
which relationships exist, which work items cross, what propagates in each
direction, how conflicts resolve, and the identity and credential custody the
sync operates under.

## ADDED Requirements

### Requirement: Only declared project↔repo relationships sync

A tududi project SHALL sync with exactly one GitHub repository, and only when
that pair is explicitly declared in version-controlled configuration. A project
or repository absent from the declaration MUST never be read from or written to
by the sync, and removing a pair from the declaration MUST stop its sync without
deleting or altering existing tasks or issues.

#### Scenario: Undeclared project is untouched

- **WHEN** a tududi project exists that appears in no declared pair and a sync
  cycle runs
- **THEN** no task in that project is read, created, or modified, and no issue
  is created from it in any repository

#### Scenario: Unmapping is non-destructive

- **WHEN** a previously declared pair is removed from the configuration and the
  sync is re-provisioned
- **THEN** that pair stops syncing and every existing task and issue remains
  exactly as it was

### Requirement: A work item crosses only when tagged for it

A tududi task SHALL be represented as a GitHub issue only when it carries the
designated sync tag AND belongs to a project in a declared pair. A task that has
never carried the sync tag MUST never appear in GitHub in any form, regardless
of project. Removing the sync tag from a linked task SHALL stop propagation and
close its issue as not-planned with an audit note — never delete it — and the
linkage MUST survive so re-tagging reopens the same issue.

#### Scenario: Untagged task stays private

- **WHEN** a task in a declared project has no sync tag and a sync cycle runs
- **THEN** no issue is created for it and no trace of its content reaches GitHub

#### Scenario: Tagging a task creates its issue exactly once

- **WHEN** the sync tag is added to a task in a declared project
- **THEN** a subsequent cycle creates one issue in the paired repository carrying
  the task's title, description, status and tags, the pair is durably linked,
  and further cycles update rather than re-create it

#### Scenario: Interrupted creation recovers without a duplicate

- **WHEN** a cycle created the issue but failed before recording the link on the
  task, and a later cycle processes the same tagged task
- **THEN** the later cycle finds the existing issue by the task identity it
  carries, completes the linkage, and no second issue is created

#### Scenario: Removing the sync tag closes the issue, destroys nothing

- **WHEN** the sync tag is removed from a linked task
- **THEN** the next cycle closes the issue as not-planned with an audit note,
  the task itself is unchanged, and re-adding the tag later reopens the same
  issue rather than creating a new one

### Requirement: Linked pairs converge bidirectionally

For a linked task↔issue pair, changes to title, description, status, and
tags/labels on either side SHALL propagate to the other side within one sync
cycle. Status MUST map both ways between tududi's task statuses and the issue's
open/closed state under a single documented mapping table. Comments MUST NOT
propagate in either direction.

#### Scenario: GitHub edit reaches tududi

- **WHEN** an issue in a linked pair is retitled and closed on GitHub
- **THEN** the next cycle updates the linked task's title and sets its status to
  the documented closed-state equivalent

#### Scenario: tududi edit reaches GitHub

- **WHEN** a linked task's description and tags change in tududi
- **THEN** the next cycle updates the issue body and reconciles the issue's
  labels to match the task's tags per the documented tag↔label rules

#### Scenario: Comments never cross

- **WHEN** comments are added to a linked issue and a note is added to the
  linked tududi task
- **THEN** subsequent cycles copy neither: the issue's comments stay only in
  GitHub and the task's notes stay only in tududi

### Requirement: Conflicts resolve last-writer-wins without silent loss

When both sides of a linked pair changed since the last cycle, the side with the
newer update timestamp SHALL win. The overwritten side's conflicting values MUST
be preserved in an audit note on the issue — a conflict MUST never silently
destroy content.

#### Scenario: Both sides edited between cycles

- **WHEN** a linked task and its issue both changed the same field since the
  last cycle
- **THEN** the field converges to the value from the side with the newer
  timestamp, and the losing value is recorded in an issue comment naming the
  field and when it lost

### Requirement: The sync acts under its own identity and never echoes

All sync writes on both sides SHALL be made under a dedicated sync identity,
and a cycle MUST NOT treat the sync's own writes as fresh changes to propagate
back. Re-running a cycle with no external changes MUST make zero writes.

#### Scenario: Quiet cycle is a no-op

- **WHEN** two consecutive cycles run with no human change on either side
- **THEN** the second cycle performs no write on either side

#### Scenario: Propagated change does not bounce back

- **WHEN** a tududi edit is propagated to the issue in one cycle
- **THEN** the following cycle does not write that same change back onto the
  task (no ping-pong updates in either system's history)

### Requirement: Sync credentials live in the secret store, scoped to the job

The tududi token and the GitHub credential the sync uses SHALL be stored in the
secret store and provisioned into the workflow engine by automation. The GitHub
credential MUST be scoped to no more than the declared repositories, and no
credential value may appear in workflow definitions, task output, or logs.
Provisioning MUST validate both credentials against their live services before
changing the engine, and MUST remove engine objects it owns that the current
declaration no longer implies.

#### Scenario: Credentials provisioned as code

- **WHEN** the sync's provisioning automation runs against a clean workflow
  engine
- **THEN** both credentials exist in the engine, sourced from the secret store,
  and re-running the provisioning creates no duplicates

#### Scenario: No credential in any committed or logged artifact

- **WHEN** the workflow definitions, the mapping configuration, and the
  provisioning run's output are inspected
- **THEN** none contains a credential value — only secret-store path references

#### Scenario: Removal from the declaration removes the engine objects

- **WHEN** a workflow or credential the provisioning previously created is no
  longer implied by the declaration and provisioning re-runs
- **THEN** that object is removed from the workflow engine, while objects the
  provisioning did not create are left untouched

#### Scenario: Provisioning refuses dead credentials

- **WHEN** provisioning runs while either stored credential is absent or no
  longer accepted by its service
- **THEN** it fails with an error naming which credential failed, before
  changing anything in the workflow engine
