# Site-Config Runner Group Access

**Date:** 2026-10-11
**Status:** ACTIVE
**Context:** Add the private `site-config` repository to the declared access list for the existing `uhstray-selfhosted` runner group so its workflow can use the platform runners.

## Problem

The runner group declaration admits the existing private repositories but does not admit `site-config`. Changing the declaration causes convergence to replace the group's repository grants, so the code change, dry run, convergence, grant check, and workflow switch must happen in order.

## Design Principles

1. Keep group membership declared and reviewable in Agent Cloud.
2. Preserve the public-repository guard and keep public `agent-cloud` outside the group.
3. Keep private site values and inventory data out of this public repository.

## Architecture

```mermaid
flowchart LR
    REVIEW["Reviewed merge to Dev"] --> DRY["Semaphore dry run"]
    DRY --> APPLY["Semaphore converge"]
    APPLY --> VERIFY["Verify site-config group grant"]
    VERIFY --> SWITCH["Switch site-config workflow to runner group"]
```

The playbook remains the authority for the selected repository list. Its public-repository check must pass before group membership is changed.

## Implementation Phases

1. Update the declared list, its exact structural test, and runner documentation. **Acceptance:** focused BATS test passes; `agent-cloud` remains excluded.
2. Merge the reviewed change to Dev. **Acceptance:** the reviewed Dev revision contains the allowlist change.
3. Run `Manage GitHub Runner Group (Dev)` in Semaphore dry-run mode, then converge. **Acceptance:** dry run reports the intended list; convergence completes.
4. Verify `site-config` is granted to `uhstray-selfhosted`, then switch its workflow to that group. **Acceptance:** the grant is visible before the workflow is changed.

## Validation Criteria

| Check | Pass condition |
|---|---|
| Focused structural test | `bats platform/tests/test_manage_github_runner_group.bats` passes |
| Public-repository protection | Playbook still refuses any declared repository whose visibility is public |
| Semaphore dry run | Reports the intended access list without changing it |
| Grant verification | `site-config` appears in the selected group before workflow switch |

## Security Considerations

The group remains selected-only with public repositories disabled. `agent-cloud` stays excluded, and the playbook must continue validating repository visibility before applying grants. Runner jobs can access files available to the runner account, so access is limited to the named private repositories. No site-specific values or inventory data belong in this public repository.

## Cross-references

- [Runner service operating notes](../../platform/services/github-runner/CLAUDE.md)
- [Runner group playbook](../../platform/playbooks/manage-github-runner-group.yml)
- [Ansible automation standards](../architecture/08-ansible-automation-standards.md)
- [Root operating guidance](../../CLAUDE.md)
