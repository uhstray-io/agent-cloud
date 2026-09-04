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

#### Scenario: Finished work is not exported

- **WHEN** a task in a declared project already holds a finished status
  (done, cancelled or archived) when the sync tag is added, and no issue is
  linked to it
- **THEN** no issue is created for it — the mirror of "closed issues are not
  imported" — while a task that finishes AFTER it was linked still closes its
  issue with the matching reason

#### Scenario: Interrupted creation recovers without a duplicate

- **WHEN** a cycle created the issue but failed before recording the link, and a
  later cycle processes the same tagged task
- **THEN** the later cycle adopts the existing open issue carrying the task's
  title (see "An item exists once, whichever side it started on"), completes the
  linkage, and no second issue is created

#### Scenario: Removing the sync tag closes the issue, destroys nothing

- **WHEN** the sync tag is removed from a linked task
- **THEN** the next cycle closes the issue as not-planned with an audit note,
  the task itself is unchanged, and re-adding the tag later reopens the same
  issue rather than creating a new one

### Requirement: Work can start on GitHub

An open issue in a paired repository that no task is linked to, filed by anyone
other than the sync identity, SHALL become a task in the paired tududi project on
the next cycle, carrying the issue's title, body, labels as tags, and the sync
tag, and the two SHALL be durably linked so the pair converges like any other.
A closed unlinked issue MUST NOT be backfilled — finished work is not imported.
An unlinked open issue authored by the sync identity is an orphan of an
interrupted tududi-origin creation and MUST be reported, never re-imported as a
new task.

#### Scenario: Issue filed on GitHub becomes a task

- **WHEN** a person opens an issue in a paired repository and a cycle runs
- **THEN** one task exists in the paired project with the issue's title, body,
  labels and the sync tag; the issue carries the linkage to that task; and the
  next cycle updates rather than re-creates

#### Scenario: Closed issues are not imported

- **WHEN** a paired repository holds closed issues that were never linked
- **THEN** no task is created for any of them

#### Scenario: Missing project is a named refusal

- **WHEN** the paired tududi project does not exist on the instance and a
  creation is due
- **THEN** the cycle records a recovery error naming the project and the issue,
  writes nothing to either side, and succeeds otherwise

### Requirement: An item exists once, whichever side it started on

Creation from either origin SHALL never produce a second copy of an item that
already exists on the other side. Before creating, the cycle MUST look for the
same canonical title among the unlinked open issues (for a tagged task) or the
tasks of the paired project (for an unlinked issue). Exactly one match SHALL be
adopted and linked in place, with the linkage announced once and every
differing field resolved last-writer-wins with the losing value preserved. More
than one candidate, or a candidate that is not tagged for sync, SHALL block
creation with a recovery error naming every artifact involved — the cycle never
guesses. A linkage that names a task the project does not hold, or a linkage
carried by more than one issue, SHALL likewise be reported rather than silently
skipped or resolved by re-creation.

#### Scenario: Same item created on both sides is linked, not duplicated

- **WHEN** a tagged task and an open issue with the same title exist unlinked in
  a pair when a cycle runs
- **THEN** the issue is linked to the task, no new issue and no new task is
  created, the link is announced once on the issue, and any differing field
  resolves last-writer-wins with the losing value preserved on the issue

#### Scenario: Ambiguous match refuses to guess

- **WHEN** more than one unlinked open issue carries a tagged task's title
- **THEN** nothing is created or linked — on EITHER side: no issue for the
  task, and no task for any of the candidate issues — and a single recovery
  error names the task and every candidate issue

#### Scenario: Untagged twin blocks import

- **WHEN** an unlinked open issue's title matches a task in the paired project
  that is not tagged for sync
- **THEN** no task is created and a recovery error names the task and the
  issue, so a person decides whether the private task should be published

#### Scenario: Dangling or duplicated linkage is reported

- **WHEN** an open issue's linkage names a task the paired project does not hold,
  or two issues carry the same linkage
- **THEN** no task or issue is created for them and a recovery error names
  every artifact involved

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

#### Scenario: A human's close on GitHub is never reverted

- **WHEN** a linked issue is closed as not-planned on GitHub while its tagged
  task is still open in tududi
- **THEN** the next cycle sets the task to the documented not-planned
  equivalent and leaves the issue closed — it MUST NOT be mistaken for the
  sync's own un-tag close and reopened; the two are told apart by which side
  the marker's status baseline says wrote the closed state

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

On GitHub the identity is a dedicated App. On tududi it is a labelled API
token belonging to the user who OWNS the mapped projects — not a separate
service account — because tududi scopes every list per user and a task
created by another user is invisible to the project's owner. Provisioning
MUST refuse an enabled pair whose project the token cannot see.

#### Scenario: Token that cannot see a mapped project is refused

- **WHEN** provisioning runs with a tududi token whose user neither owns nor
  is shared an enabled pair's project
- **THEN** it fails naming that project before changing anything in the
  workflow engine

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
