## Purpose

Simplifies how Semaphore separates production, integration and local-dev execution while
keeping each boundary enforced: one template catalog, the branch as a gated launch choice,
and separate controllers per environment.

## ADDED Requirements

### Requirement: One template per playbook
The shared template catalog SHALL declare one template per playbook. The branch a task runs
from MUST be chosen at launch from `main` or `dev`, defaulting to `main`, and MUST NOT be
expressed by a duplicated template.

#### Scenario: Integration run without a twin
- **WHEN** an operator or agent launches a template on `dev`
- **THEN** the task runs the `dev` tree of that same template

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
