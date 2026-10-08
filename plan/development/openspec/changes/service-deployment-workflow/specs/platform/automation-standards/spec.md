## Purpose

Holds every playbook in the repository to Ansible's documented standards, and in particular
to its native check mode, so that any automation can be dry-run and verified without making
changes.

## ADDED Requirements

### Requirement: Automation conforms to a recorded Ansible standard
The platform SHALL keep a reference in `plan/architecture/` that cites the official Ansible
documentation for check mode, diff mode, variables, inventory, error handling, roles and
lint, and states the platform's conventions on top of it. Automation documentation in the
repository MUST NOT contradict that reference.

#### Scenario: A reader finds the standard
- **WHEN** a contributor looks for how playbooks must behave in a dry run
- **THEN** the reference names check mode as the mechanism, links the official page and
  states the platform rules

### Requirement: Every playbook supports check mode
Every playbook SHALL complete under `ansible-playbook --check` without changing any target.
Read-only tasks whose module lacks check-mode support MUST run in check mode, and every task
that would change state and cannot be simulated MUST be skipped in check mode. Bespoke
dry-run variables MUST NOT be introduced; existing ones MUST map onto check mode.

#### Scenario: Dry run changes nothing
- **WHEN** any playbook runs with `--check` against a converged target
- **THEN** it exits zero and the target is unchanged

#### Scenario: Read-only probe is not skipped
- **WHEN** a playbook's verification issues an HTTP read in check mode
- **THEN** the read executes and its assertion is evaluated rather than reported as skipped

#### Scenario: Legacy dry-run argument still works
- **WHEN** a playbook that accepted `-e dry_run=true` is launched that way during the
  transition
- **THEN** it changes nothing, as under `--check`, and a playbook whose `dry_run` defaulted to
  false prints that the argument is deprecated

#### Scenario: A dry-by-default playbook keeps its safety default
- **WHEN** a playbook whose `dry_run` defaults to true (it replaces or deletes shared state)
  is launched with no arguments
- **THEN** it changes nothing, and it changes state only on an explicit `-e dry_run=false`
  outside check mode

### Requirement: Verification is a first-class run
Every playbook that changes state SHALL tag its verification tasks so the verification alone
can run against a live target, and that run MUST make no changes and MUST record a step
result.

#### Scenario: Verify-only run
- **WHEN** a playbook runs with only its verification tag
- **THEN** it performs only read-only checks and records `pass` or `fail`

### Requirement: The check-mode contract is enforced mechanically
A CI test SHALL fail when a playbook contains a state-changing command, shell or non-GET HTTP
task with neither a check-mode guard nor an explicit check-mode setting, or a read-only HTTP
probe without check mode enabled.

#### Scenario: Unguarded write is caught
- **WHEN** a new playbook adds an unguarded `ansible.builtin.command` that writes state
- **THEN** the check-mode test fails naming the file and task
