## Purpose

Simplifies how Semaphore separates production, integration and local-dev execution while
keeping each boundary enforced: one template catalog, a base template bound to `main` with a
`(Dev)` variant bound to `dev`, and separate controllers per environment.

## ADDED Requirements

### Requirement: One base template per playbook, with a dev-bound variant
The shared template catalog SHALL declare one base template per playbook, bound to `main`.
A playbook that must run from `dev` before promotion SHALL be given a generated `(Dev)`
variant of that base, bound to `dev`. The branch a task runs from MUST be fixed by the template
it launches. A template MUST NOT set `allow_override_branch_in_task`, and a launch MUST NOT name
a different branch for the task. The registry and OPA name base templates only.

#### Scenario: Integration run on the dev variant
- **WHEN** an operator or agent launches the `(Dev)` variant of a template
- **THEN** the task runs the `dev` tree of the same playbook the base template runs from `main`

#### Scenario: A task cannot choose its own branch
- **WHEN** a launch names a branch other than the one its template is bound to
- **THEN** the template does not honour it, because no template sets `allow_override_branch_in_task`

### Requirement: Branch choice is policed
OPA SHALL receive the requested branch on every agent launch, and agent launches on `main`
MUST be denied for templates whose registry entry is not yet reviewed.

#### Scenario: Unreviewed step cannot run from main
- **WHEN** an agent requests an unreviewed step's template on `main`
- **THEN** OPA denies

### Requirement: Environments are separate controllers
Local-dev and production SHALL each run their own Semaphore, and local-only templates MUST be
applied only to the local controller and MUST run the working tree of the local checkout.

#### Scenario: Local template cannot reach production
- **WHEN** the production catalog is applied
- **THEN** no local-only template is present in it
