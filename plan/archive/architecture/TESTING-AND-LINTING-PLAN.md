# Testing and Linting Plan

**Date:** 2026-04-21
**Updated:** 2026-05-06
**Status:** ACTIVE — Phases 1-5 implemented for NetBox. Coverage gaps remain for 11 services and 3 agents.
**Contributors:** Architecture, Automation, Security, and Testing review agents

---

## Coverage Assessment (2026-05-06)

### What Exists

The CI pipeline (`.github/workflows/lint-and-test.yml`) runs 3 jobs with 8 linters on every PR to main. Python unit tests (79 cases) and BATS bash tests (39 cases across 3 files) provide functional coverage for NetBox workers and shared libraries.

| Component | Test Coverage | Notes |
|-----------|---------------|-------|
| `platform/lib/common.sh` | BATS (8 functions) | Shared library |
| `platform/services/netbox/deployment/lib/common.sh` | BATS (6 functions) | NetBox-specific library |
| `platform/playbooks/` | BATS (2 functions) | Heredoc/rendering validation |
| `platform/services/netbox/deployment/workers/` | pytest (79 cases) | Proxmox + pfSense helpers |

### What Is Missing

**11 services with zero tests:** caddy, inference, n8n, nextcloud, nocodb, o11y, openbao, postiz, semaphore, wikijs, a2a-registry

**3 agents with zero tests:** nemoclaw, netclaw, cowork

**No tests exist for:**
- Compose file validation (valid YAML, required services, no hardcoded credentials)
- deploy.sh structural validation (sources common.sh, uses CONTAINER_ENGINE variable)
- Environment template validation (Jinja2 templates use proper variable namespaces)
- Credential leak regression tests as standalone BATS tests (currently only CI grep)
- .gitignore coverage validation for sensitive patterns
- Tracked .env file detection

### New Testing Requirements

Every new service onboarded to agent-cloud must include tests before its PR can merge. See `plan/architecture/CI-TESTING-SPECIFICATION.md` for the full specification including:

- Test templates for compose files, deploy scripts, env templates, and credential leaks
- Service onboarding testing checklist
- CI pipeline extension recommendations

---

## Original State (2026-04-21)

The repository had **zero automated testing or linting infrastructure** at the start of this plan. No GitHub Actions, no pre-commit hooks, no pytest, no shellcheck, no ansible-lint. The only quality gates were:

- A manual `grep`-based pre-push audit for leaked IPs/credentials (CLAUDE.md)
- Runtime validation playbooks in Semaphore (`validate-all.yml`, `validate-secrets.yml`, `check-discovery.yml`)
- CodeRabbit automated code review on PRs

## Guiding Principles

1. **Automate what's manual** — the pre-push audit grep patterns should be a pre-commit hook, not discipline
2. **Static analysis before unit tests** — linting catches more bugs per hour of setup than writing tests
3. **Test pure functions first** — the discovery workers have ~15 helper functions with clear contracts
4. **Don't test what Semaphore already validates** — runtime health checks against live services stay in Semaphore
5. **Security scanning is non-negotiable for a public repo** — trufflehog must gate every PR

---

## Phase 1: Static Analysis (immediate, no test infrastructure needed)

### 1a. Python Linting — Ruff

**Scope:** 4 Python files (~1,800 LOC total)
- `workers/proxmox_discovery/proxmox_discovery/__init__.py`
- `workers/pfsense_sync/pfsense_sync/__init__.py`
- `lib/pfsense-sync.py`
- `configuration/plugins.py`

**Configuration:** Add `[tool.ruff]` to root `pyproject.toml`:
```toml
[tool.ruff]
target-version = "py311"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "SIM", "PIE", "C4", "BLE"]
ignore = ["BLE001"]  # blind except is intentional in worker error handling
```

### 1b. Shell Linting — ShellCheck

**Scope:** 28 shell scripts across the repo
- `platform/lib/common.sh`, `platform/lib/bao-client.sh`
- `platform/services/*/deployment/deploy.sh`
- `platform/services/netbox/deployment/lib/common.sh`, `lib/generate-secrets.sh`
- `agents/nemoclaw/deployment/deploy.sh`, `update.sh`, `validate.sh`

**Tool:** `shellcheck` (install via `brew install shellcheck` or CI action). Scans the entire repo (excluding `netbox-docker/`).

### 1c. Ansible Linting

**Scope:** 50+ YAML playbooks and task files
- `platform/playbooks/*.yml`
- `platform/playbooks/tasks/*.yml`
- `platform/semaphore/templates.yml`

**Configuration:** `.ansible-lint` at repo root:
```yaml
skip_list:
  - command-instead-of-module  # deploy.sh invocations are intentional
  - no-changed-when            # many shell tasks are check commands
exclude_paths:
  - netbox-docker/
  - .github/
```

### 1d. YAML Linting

**Scope:** All YAML files (playbooks, compose files, agent configs, templates.yml)
**Tool:** `yamllint` with relaxed rules for Ansible compatibility

### 1e. Dockerfile Linting — hadolint

**Scope:** 1 custom Dockerfile
- `platform/services/netbox/deployment/Dockerfile-Plugins`
- (`netbox-docker/Dockerfile` is vendored upstream — excluded)

**Tool:** `hadolint` via CI action. Catches base image issues, layer inefficiencies, and security anti-patterns.

### 1f. Jinja2 Template Validation

**Scope:** 6 Jinja2 templates in `platform/services/netbox/deployment/templates/`
- `agent.yaml.j2`, `discovery.env.j2`, `dot-env.j2`, `hydra.yaml.j2`, `netbox.env.j2`, `postgres.env.j2`

**Tool:** `ansible-lint` validates Jinja2 syntax when checking playbooks that reference templates. Standalone `j2lint` available for direct template checking.

### 1g. HCL Policy Validation

**Scope:** 8 HCL files in `platform/services/openbao/deployment/config/`
- `openbao.hcl` (server config)
- `policies/*.hcl` (7 AppRole policies: semaphore-read, semaphore-write, orb-agent, nemoclaw-read, nemoclaw-rotate, nocodb-write, n8n-write)

**Tool:** `openbao policy fmt -check` or `terraform fmt -check` for syntax validation. These are security-critical files — invalid policy syntax could lock out services.

**Status:** Manual validation for now. CI integration planned when `openbao` CLI is available in GitHub Actions runners.

### 1h. Secret Scanning — trufflehog

**Scope:** All committed content + staged changes
**Tool:** `trufflehog` as pre-commit hook and CI gate
**Rationale:** The manual grep patterns in CLAUDE.md catch IPs and simple passwords but miss API tokens, SSH keys, base64 credentials, JWTs, PEM content. trufflehog covers all of these.

---

## Phase 2: Python Unit Tests

### Framework

**pytest** with a `conftest.py` providing shared fixtures and SDK mocks.

### Test Layout

```text
platform/services/netbox/deployment/
  tests/
    conftest.py              # SDK stubs (worker.backend, worker.models)
    test_proxmox_helpers.py  # 66 test cases via 14 parametrized functions
    test_pfsense_helpers.py  # 13 test cases via 1 parametrized function
```

### Mock Strategy

The `worker.backend.Backend` and `worker.models` modules are orb-agent runtime-only — not pip-installable. `conftest.py` stubs them via `sys.modules` injection. The real `netboxlabs-diode-sdk` is installed as a test dependency (entity constructors are pure data containers).

### Implemented Tests — Pure Logic (parametrized)

| Parametrized Function | Module | Test Cases |
|-----------------------|--------|------------|
| `test_int` | proxmox_discovery | 9 |
| `test_mb_to_gb` | proxmox_discovery | 4 |
| `test_bytes_to_gb` | proxmox_discovery | 3 |
| `test_should_skip_iface` | proxmox_discovery | 12 |
| `test_iface_type` | proxmox_discovery | 8 |
| `test_prefix_len` | proxmox_discovery | 8 |
| `test_returns_none` | proxmox_discovery | 3 |
| `test_strips_sensitive_lines` | proxmox_discovery | 7 |
| `test_preserves_words_containing_keywords` | proxmox_discovery | 3 |
| `test_clean_description_unchanged` | proxmox_discovery | 1 |
| `test_selection` | proxmox_discovery | 5 |
| `test_skips_non_routable` | proxmox_discovery | 3 |
| `test_is_valid_ip` | pfsense_sync | 13 |
| **Total** | **13 functions** | **79 test cases** |

### Future: Entity Builder Tests (mocked SDK/API)

These would test `_build_node()`, `_build_vm()`, `_build_lxc()`, `_build_seed_entities()`, and `_build_entities()` with mocked Proxmox API and SDK. Not yet implemented — prioritized pure function coverage first.

---

## Phase 3: Bash Script Testing

### ShellCheck (static, Phase 1)

Already covered in Phase 1b.

### BATS Unit Tests — IMPLEMENTED

**Framework:** [BATS](https://github.com/bats-core/bats-core) (Bash Automated Testing System)

```text
platform/tests/
  test_common.bats          # 8 functions testing platform/lib/common.sh
  test_netbox_common.bats   # 6 functions testing netbox lib/common.sh
```

**36 test cases across 14 composable functions:**

| Test File | Functions Tested | Test Cases |
|-----------|-----------------|------------|
| `test_common.bats` | gen_secret, needs_gen, get/put_secret, detect_runtime, info, warn | ~20 |
| `test_netbox_common.bats` | gen_secret, gen_django_key, get/put_secret, needs_gen, get_val, read_existing | ~16 |

Tests use multi-assertion patterns — each BATS function verifies multiple related behaviors (e.g., `put/get_secret` tests create, read, permissions, overwrite, and missing in one function).

---

## Phase 4: Security Testing

### 4a. Secret Scanning (Phase 1e)

Already covered. trufflehog as pre-commit + CI gate.

### 4b. Dependency Scanning — IMPLEMENTED

**Tool:** GitHub Dependabot (`.github/dependabot.yml`)
**Scope:** pip (3 directories), GitHub Actions, Docker base images. Weekly schedule.

### 4c. Python Security Linting — IMPLEMENTED

**Tool:** Bandit in CI security job. Skips B101 (assert) and B110 (try-except-pass).
**Scope:** Worker Python files and `lib/pfsense-sync.py`.

### 4d. Sanitization Regex Hardening

The `_sanitize_description()` regex covers `password|passwd|secret|token|key` but misses:
- `credential`, `apikey` (no underscore), `private`, `cert`, `bearer`, `auth`

Expand the pattern and add test cases. Balance false positives (stripping "keyboard" etc.) vs. credential leakage risk.

### 4e. TLS Verification Audit

Both `proxmox_discovery` and `pfsense_sync` default `verify_ssl=False`. Document this as an accepted risk for self-signed certs in uhstray.io datacenter, but add a config option to enable verification when proper CA infrastructure exists.

---

## Phase 5: CI Pipeline (GitHub Actions)

### Proposed Workflow

See `.github/workflows/lint-and-test.yml` for the actual implementation. The workflow runs two jobs on every PR to main:

**lint job:** ruff (Python), shellcheck (all `.sh` files, warning severity), ansible-lint (playbooks), yamllint (all YAML), hadolint (Dockerfiles)

**security job:** trufflehog (verified secrets), IP/credential grep audit

**test job** (Phase 2, planned): pytest with mocked SDK dependencies

### Semaphore Integration (unchanged)

Semaphore continues to own runtime validation:
- `validate-all.yml` — HTTP health checks
- `validate-secrets.yml` — credential testing against live services
- `check-discovery.yml` — entity count verification post-deploy

No changes needed to Semaphore. GitHub Actions handles pre-merge quality; Semaphore handles post-deploy verification.

---

## Implementation Priority

| Phase | Status | Tests/Tools |
|-------|--------|-------------|
| 1a. Ruff (Python lint) | Done | `pyproject.toml` config, violations fixed |
| 1b. ShellCheck | Done | Warning severity, full repo scan, violations fixed |
| 1c. Ansible-lint | Done | `.ansible-lint` config, CI step |
| 1d. YAML lint | Done | `.yamllint.yml`, trailing-spaces enforced |
| 1e. Hadolint | Done | Dockerfile linting in CI |
| 1f. Jinja2 validation | Done | Via ansible-lint |
| 1g. HCL validation | Done | `vault fmt -check` in CI |
| 1h. Trufflehog | Done | Secret scanning in CI |
| 2. Python unit tests | Done (NetBox only) | 79 test cases, 13 parametrized functions |
| 3. BATS bash tests | Done (shared libs only) | 39 test cases, 17 composable functions |
| 4a. Secret scanning | Done | Trufflehog in CI |
| 4b. Dependabot | Done | pip, GitHub Actions, Docker |
| 4c. Bandit | Done | Python security lint in CI |
| 5. CI pipeline | Done | 3 jobs: lint, security, test |
| 6. Credential leak tests | **New** | `platform/tests/test_credential_leaks.bats` |
| 7. Service onboarding tests | **Planned** | Per-service compose/deploy/template validation |
| 8. Compose dry-run validation | **Planned** | CI step using `docker compose config` |
| 9. Coverage reporting | **Planned** | pytest-cov + BATS TAP output |

---

## Challenges Identified by Review Team

1. **Orb-agent SDK not pip-installable** — `worker.backend.Backend` and `worker.models` are runtime-only. Tests must stub these via `sys.modules` in conftest.py.
2. **Shell scripts are platform-dependent** — `common.sh` detects Podman vs Docker at runtime. BATS tests need to mock or skip platform-specific paths.
3. **Ansible playbooks target live infrastructure** — Molecule would require extensive mocking. ansible-lint + `--check` mode are the pragmatic choices until a test environment exists.
4. **`_sanitize_description` false positive risk** — Expanding the keyword list risks stripping legitimate description content. Each new keyword needs negative test cases.
5. **TOCTOU in env file generation** — `generate_*_env()` functions in `common.sh` write files then chmod, leaving a brief window where secrets are world-readable. Fix: use `umask 077` subshells like `put_secret()` already does.
6. **Only NetBox has functional tests** — 11 services and 3 agents have zero test coverage. Service onboarding must require tests going forward.
7. **No compose validation in CI** — Compose files are YAML-linted but never checked for structural correctness (required services, volume mounts, network definitions).
8. **Credential leak tests are CI-only** — The IP/credential grep runs only in the security CI job diff. No standalone BATS tests exist for regression testing against the full committed tree.
9. **No Jinja2 template rendering tests** — Templates are validated only via ansible-lint syntax checks, not for correct variable namespace usage or rendering output.

---

## Related Documents

- `plan/architecture/CI-TESTING-SPECIFICATION.md` — Detailed specification for writing tests for new services
- `plan/development/OPENSSF-SCORECARD-PLAN.md` — OpenSSF Scorecard implementation plan
- `docs/LINTING-AND-TESTING.md` — Local setup and pre-PR checklist
- `plan/architecture/BRANCH-TESTING-WORKFLOW.md` — Branch deploy and validation workflow
