# Contributing to agent-cloud

Thank you for your interest in contributing to the uhstray-io platform. This guide covers everything you need to get started.

**General contribution guidelines:** [uhstray.io/contributing](https://www.uhstray.io/en/contributing)

**Discord:** [discord.uhstray.io](https://discord.uhstray.io/)

**Contact:** bac@uhstray.io

---

## Development Workflow

All changes go through feature branches and pull requests — never push directly to main.

```text
1. Fork or create a branch     git checkout -b feat/<description>
2. Make your changes            edit files, add tests
3. Update documentation         README.md, relevant CLAUDE.md, sub-READMEs
4. Run linters locally          see "Pre-PR Checklist" below
5. Run /simplify + /security-review  (if using Claude Code)
6. Push the branch              git push -u origin feat/<description>
7. Create a PR                  gh pr create
8. Wait for CI checks           lint, security, test jobs must pass
9. Address review findings      CodeRabbit + human review
10. Merge after all checks pass
```

Step 3 ensures documentation stays current with every PR — update the top-level `README.md` if the change adds features or services, the most relevant sub-directory README/CLAUDE.md for the area changed, and the root `CLAUDE.md` if adding new conventions or plans.

### Branch Naming

- `feat/<description>` — new features or functionality
- `fix/<description>` — bug fixes
- `docs/<description>` — documentation only
- `ci/<description>` — CI/CD changes
- `refactor/<description>` — code restructuring without behavior change
- `chore/<description>` — maintenance, cleanup, dependency updates
- `security/<description>` — security fixes or hardening

### Commit Messages

Use conventional commit format:

```text
<type>(<scope>): <description>

Types: feat, fix, refactor, docs, chore, ci, security
Scope: service or component name (netbox, openbao, playbooks, lib, semaphore, k8s)
```

Examples:

- `feat(netbox): add primary IPv4 assignment to discovery workers`
- `fix(playbooks): resolve SC2155 shellcheck warnings`
- `ci(platform): add ansible-lint to CI pipeline`

---

## Pre-PR Checklist

Run these locally before creating a pull request:

```bash
# Python lint
ruff check .

# Shell lint
find . -name '*.sh' ! -path '*/netbox-docker/*' -exec shellcheck -S warning {} +

# Ansible lint
ansible-lint platform/playbooks/

# YAML lint
yamllint -c .yamllint.yml .

# HCL format check (if modifying OpenBao policies)
find platform/services/openbao -name '*.hcl' -exec terraform fmt -check {} +

# Python tests (requires Python 3.11+)
cd platform/services/netbox/deployment
PYTHONPATH=workers/proxmox_discovery:workers/pfsense_sync python3.11 -m pytest tests/ -v
cd -

# Bash tests
bats platform/tests/

# Go (required if touching platform/services/uhhcraft/deployment/**)
cd platform/services/uhhcraft/deployment
templ generate              # regenerate *_templ.go from .templ source
sqlc generate               # regenerate internal/db/sqlcdb/ from db/queries/*.sql
golangci-lint run ./...     # config in .golangci.yml
gosec -exclude=G104,G304,G704,G710 -exclude-dir=web/templates -exclude-dir=internal/db/sqlcdb ./...
go test -race -count=1 ./...
cd -

# Secret scan
git diff --staged | grep -iE '^\+.*192\.168\.' | grep -v 'target\|host:\|subnet\|scope\|example'
git diff --staged | grep -iE '^\+.*password\s*[:=]\s*[A-Za-z0-9]{8}|^\+.*secret_id[:=]\s*[a-f0-9-]{30}'
```

### Tool Installation

```bash
# macOS
pip install ruff ansible-lint yamllint bandit
brew install shellcheck bats-core hadolint

# Python 3.11 (required for tests)
brew install python@3.11
pip3.11 install pytest netboxlabs-diode-sdk proxmoxer requests

# HCL formatting (optional, for OpenBao policy changes)
brew install terraform

# Go toolchain (required for UhhCraft changes)
brew install go@1.23
go install github.com/a-h/templ/cmd/templ@v0.2.793
go install github.com/sqlc-dev/sqlc/cmd/sqlc@v1.27.0
go install github.com/pressly/goose/v3/cmd/goose@v3.21.1
brew install golangci-lint
brew install gosec
```

---

## CI Pipeline

Every PR triggers three GitHub Actions jobs automatically:

| Job | Tools | Fails on |
| --- | ----- | -------- |
| **Static Analysis** | ruff, shellcheck, ansible-lint, yamllint, hadolint, terraform fmt | Any lint error or warning |
| **Security Scan** | trufflehog, bandit, IP/credential grep | Leaked secrets, security issues, hardcoded IPs |
| **Unit Tests** | pytest (79 tests), BATS (36 tests) | Any test failure |

All three must pass before merging. See `docs/LINTING-AND-TESTING.md` for full details.

---

## Adding a New Service

Follow the [Service Integration Plan](plan/architecture/SERVICE-INTEGRATION-PLAN.md) for the complete onboarding checklist. At minimum:

1. Create `platform/services/<name>/deployment/` with `deploy.sh`, `compose.yml`, and Jinja2 templates
2. `deploy.sh` handles container lifecycle only — no secret generation or OpenBao interaction
3. Create `deploy-<name>.yml` playbook using composable tasks from `platform/playbooks/tasks/`
4. Add Semaphore template to `platform/semaphore/templates.yml`
5. Add tests for any new Python or Bash code
6. All secrets use `{{ variable }}` references — real values live in site-config (private repo)

---

## Code Standards

### Python

- Linted by **ruff** (`pyproject.toml` config)
- Target: Python 3.11+
- Line length: 120 characters
- No blind `except` without documented reason

### Shell Scripts

- Linted by **shellcheck** at warning severity
- Use single-quoted trap strings: `trap 'cleanup' EXIT`
- Declare and assign variables separately: `local x; x=$(cmd)`
- Export cross-file variables: `export DEFAULT_TIMEOUT=300`

### Ansible Playbooks

- Linted by **ansible-lint** (`.ansible-lint` config)
- Jinja2 variables in task names go at the end: `"Deploy service — {{ service_name }}"`
- Use `failed_when` instead of `ignore_errors: true`
- Add `set -o pipefail` to shell tasks with pipes

### YAML

- Linted by **yamllint** (`.yamllint.yml` config)
- No trailing spaces
- Newline at end of file
- Max 200 character lines

### Security

- Never commit IPs, passwords, API tokens, GPS coordinates, or physical location data
- All sensitive values go in site-config (private repo) as inventory variables
- Use `{{ variable }}` Jinja2 references in all templates

---

## Architecture Documentation

| Document | Purpose |
| -------- | ------- |
| `CLAUDE.md` | AI agent guidance (conventions, deployment rules, secrets management) |
| `plan/architecture/AUTOMATION-COMPOSABILITY.md` | Composable Ansible task architecture |
| `plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md` | Secret generation, storage, rotation |
| `plan/architecture/SERVICE-INTEGRATION-PLAN.md` | New service onboarding checklist |
| `plan/architecture/TESTING-AND-LINTING-PLAN.md` | Testing strategy and implementation status |
| `plan/architecture/BRANCH-TESTING-WORKFLOW.md` | Branch deploy and validation workflow |

---

## Code of Conduct

All contributors are expected to follow the [uhstray.io Code of Conduct](https://www.uhstray.io/en/code-of-conduct).
