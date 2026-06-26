# Linting and Testing Guide

This guide covers the automated quality gates that run on every pull request to main.

---

## CI Pipeline

Every PR triggers three GitHub Actions jobs:

### Static Analysis

| Tool | What it checks | Config file | Scope |
| ---- | -------------- | ----------- | ----- |
| **Ruff** | Python lint (style, imports, bugs) | `pyproject.toml` | All `.py` files |
| **ShellCheck** | Bash lint (quoting, unused vars, bugs) | Built-in rules | All `.sh` files (excludes `netbox-docker/`) |
| **ansible-lint** | Ansible playbook lint (syntax, best practices) | `.ansible-lint` | `platform/playbooks/` |
| **yamllint** | YAML lint (syntax, trailing spaces, newlines) | `.yamllint.yml` | All `.yml`/`.yaml` files (excludes `netbox-docker/`) |
| **hadolint** | Dockerfile lint (base images, layers, security) | Built-in rules | Custom Dockerfiles (excludes vendored) |
| **vault fmt** | HCL policy format check | Built-in rules | `platform/services/openbao/**/*.hcl` |

### Security Scan

| Tool | What it checks | Scope |
| ---- | -------------- | ----- |
| **TruffleHog** | Verified secrets (API keys, tokens, passwords) | Full repo history |
| **Bandit** | Python security lint (hardcoded passwords, eval, etc.) | Worker Python files |
| **IP/credential grep** | Leaked IPs (`192.168.*`) and credential patterns | PR diff only |

### Unit Tests

| Framework | What it tests | Test count | Scope |
| --------- | ------------- | ---------- | ----- |
| **pytest** | Python worker helper functions | 79 tests | `platform/services/netbox/deployment/tests/` |
| **BATS** | Bash common.sh functions (secrets, runtime detection) | 36 tests | `platform/tests/` |

---

## Running Locally

### Python (Ruff)

```bash
pip install ruff
ruff check .                    # Check all Python files
ruff check --fix .              # Auto-fix safe issues
ruff check --fix --unsafe-fixes .  # Fix all (review changes)
```

Configuration is in `pyproject.toml` under `[tool.ruff]`.

### Bash (ShellCheck)

```bash
# macOS
brew install shellcheck

# Ubuntu
apt install shellcheck

# Check all scripts (excludes netbox-docker/)
find . -name '*.sh' ! -path '*/netbox-docker/*' -exec shellcheck -S warning {} +
```

### Ansible (ansible-lint)

```bash
pip install ansible-lint
ansible-lint platform/playbooks/
```

Configuration is in `.ansible-lint`. Skips `command-instead-of-module` and `no-changed-when` (intentional patterns in deploy playbooks).

### YAML (yamllint)

```bash
pip install yamllint
yamllint -c .yamllint.yml .
```

### Dockerfiles (hadolint)

```bash
# macOS
brew install hadolint

# Check custom Dockerfile
hadolint platform/services/netbox/deployment/Dockerfile-Plugins
```

### HCL Policies (vault fmt)

```bash
# macOS
brew install vault  # or: brew install openbao

# Check all HCL files
find platform/services/openbao -name '*.hcl' -exec vault fmt -check {} +
```

### BATS (Bash tests)

```bash
# macOS
brew install bats-core

# Run all bash tests
bats platform/tests/
```

### Python Tests (pytest)

```bash
# Requires Python 3.11+
cd platform/services/netbox/deployment
PYTHONPATH=workers/proxmox_discovery:workers/pfsense_sync \
  python3.11 -m pytest tests/ -v
```

### Secret Scanning (TruffleHog)

```bash
# Install
brew install trufflehog  # or: pip install trufflehog

# Scan current branch
trufflehog git file://. --only-verified --branch HEAD
```

---

## Rules and Exceptions

### Ruff (Python)

**Selected rules:** E (errors), F (pyflakes), W (warnings), I (imports), UP (pyupgrade), B (bugbear), SIM (simplify), PIE (misc), C4 (comprehensions), BLE (blind except).

**Intentionally ignored:**

| Rule | Reason |
| ---- | ------ |
| BLE001 | Blind `except Exception` is intentional in worker error handling — workers must not crash the orb-agent |
| SIM105 | `try-except-pass` is clearer than `contextlib.suppress` for inline type coercion |
| C408 | `dict(key=value)` calls are preferred over `{"key": value}` for readability with many keyword args |

**Per-file overrides:** `platform/services/netbox/deployment/lib/pfsense-sync.py` allows E501 (long lines) because it's a legacy standalone script.

### ShellCheck (Bash)

Severity is set to **warning** — both errors and warnings fail CI.

Common fixes:

| Code | Issue | Fix |
| ---- | ----- | --- |
| SC2064 | Trap uses double quotes (expands now) | Use single quotes: `trap 'cleanup' EXIT` |
| SC2034 | Variable appears unused | Remove it, export it, or prefix with `_` |
| SC2086 | Unquoted variable | Quote it: `"$VAR"` |
| SC2155 | Declare and assign separately | Split: `local x; x=$(cmd)` |

### yamllint (YAML)

Uses the `relaxed` base with:
- Line length: 200 characters max
- Trailing spaces: **enforced** (remove all trailing whitespace)
- Newline at end of file: **enforced**
- Truthy values: only `true`, `false`, `yes`, `no` allowed

---

## Adding a New Service

When onboarding a new service, your code must pass all CI checks before merge:

1. **Python code**: Run `ruff check` on any `.py` files. Fix import ordering, unused imports, and f-string issues before pushing.
2. **Shell scripts**: Run `shellcheck -S warning` on your `deploy.sh`. Quote variables, use single-quoted traps, remove unused vars.
3. **Ansible playbooks**: Run `ansible-lint` on new or modified playbooks.
4. **YAML files**: Run `yamllint -c .yamllint.yml` on compose files, playbooks, and templates. Remove trailing spaces, ensure final newline.
5. **Dockerfiles**: Run `hadolint` on custom Dockerfiles.
6. **HCL policies**: Run `vault fmt -check` on any `.hcl` files. These are security-critical.
7. **Tests**: Run `pytest` (Python) and `bats` (Bash) to verify helper functions.
8. **Secrets**: Never commit real IPs, passwords, API tokens, or GPS coordinates. Use Jinja2 `{{ variable }}` references. Real values live in site-config (private repo).

### Pre-PR Checklist

```bash
# 1. Python lint
ruff check .

# 2. Shell lint
find . -name '*.sh' ! -path '*/netbox-docker/*' -exec shellcheck -S warning {} +

# 3. Ansible lint
ansible-lint platform/playbooks/

# 4. YAML lint
yamllint -c .yamllint.yml .

# 5. Dockerfile lint
hadolint platform/services/netbox/deployment/Dockerfile-Plugins

# 6. HCL format check
find platform/services/openbao -name '*.hcl' -exec vault fmt -check {} +

# 7. Python tests
cd platform/services/netbox/deployment
PYTHONPATH=workers/proxmox_discovery:workers/pfsense_sync python3.11 -m pytest tests/ -v
cd -

# 8. Bash tests
bats platform/tests/

# 9. Secret scan
git diff --staged | grep -iE '^\+.*192\.168\.' | grep -v 'target\|host:\|subnet\|scope\|example'
git diff --staged | grep -iE '^\+.*password\s*[:=]\s*[A-Za-z0-9]{8}|^\+.*secret_id[:=]\s*[a-f0-9-]{30}'
```

---

## Test Framework

### Python (pytest) — 79 test cases, 13 parametrized functions

Tests use `@pytest.mark.parametrize` for composability — each function covers multiple inputs in a single definition.

| Test File | Functions Covered | Test Cases |
| --------- | ----------------- | ---------- |
| `test_proxmox_helpers.py` | `_int`, `_mb_to_gb`, `_bytes_to_gb`, `_should_skip_iface`, `_iface_type`, `_prefix_len`, `_sanitize_description`, `_pick_primary_ipv4` | 66 |
| `test_pfsense_helpers.py` | `_is_valid_ip` | 13 |

**Requires:** Python 3.11+ and `pip install netboxlabs-diode-sdk proxmoxer requests pytest`.

The `conftest.py` stubs the orb-agent runtime modules (`worker.backend`, `worker.models`) that aren't pip-installable. The real Diode SDK is installed for entity constructor validation.

### Bash (BATS) — 36 test cases, 14 composable functions

Tests use multi-assertion patterns — each BATS function verifies multiple related behaviors in one test.

| Test File | Functions Covered | Test Cases |
| --------- | ----------------- | ---------- |
| `test_common.bats` | `gen_secret`, `needs_gen`, `get/put_secret`, `detect_runtime`, `info`, `warn` | ~20 |
| `test_netbox_common.bats` | `gen_secret`, `gen_django_key`, `get/put_secret`, `needs_gen`, `get_val`, `read_existing` | ~16 |

**Requires:** `brew install bats-core` (macOS) or `apt install bats` (Ubuntu).

### Total: 115 test cases across 27 composable test functions

See `plan/architecture/TESTING-AND-LINTING-PLAN.md` for the full testing roadmap.
