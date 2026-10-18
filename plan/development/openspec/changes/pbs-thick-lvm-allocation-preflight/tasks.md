## 1. Implement the read-only preflight

- [x] 1.1 Add the sanitized inspector operation, separate Dev-bound playbook/template, and focused synthetic coverage; reuse the protected survey payload and require a private serial or WWN pin without changing existing survey behavior.
- [x] 1.2 Validation gate — run focused tests, relevant Ansible syntax/lint, and strict OpenSpec validation to prove **Scenario: Pinned backing disk passes the proposed allocation preflight** and **Scenario: Incomplete, changed, or unhealthy backing evidence fails closed**. Focused suite passed (209 tests), Ruff passed, Ansible syntax/lint passed, YAML lint passed for the new playbook, and strict OpenSpec validation passed in an isolated temporary root.
