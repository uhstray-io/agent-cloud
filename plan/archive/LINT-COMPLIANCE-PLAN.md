# Lint Compliance Plan

**Date:** 2026-04-22
**Status:** COMPLETE — all violations fixed in PR #7
**Prerequisite:** Merge CI pipeline PR (PR #7) first

---

## Overview

The CI pipeline (ruff, shellcheck, yamllint, trufflehog) catches real issues. Several pre-existing files have lint violations that must be fixed before CI can gate PRs. This plan tracks the cleanup work needed to bring the existing codebase into full compliance.

---

## Shell Script Violations (ShellCheck)

**5 warnings across 4 files:**

| File | Line | Code | Issue | Fix |
| ---- | ---- | ---- | ----- | --- |
| `platform/services/semaphore/deployment/deploy.sh` | 53 | SC2064 | Trap expands variable now, not when signalled | Use single quotes around the trap string |
| `platform/services/semaphore/deployment/deploy.sh` | 57 | SC2034 | `login_response` appears unused | Remove or export the variable |
| `platform/services/n8n/deployment/deploy.sh` | 73 | SC2064 | Trap expands variable now, not when signalled | Use single quotes around the trap string |
| `platform/services/netbox/deployment/lib/common.sh` | 29 | SC2034 | `LIB_DIR` appears unused | Verify usage or remove |
| `platform/services/netbox/deployment/deploy.sh` | 41 | SC2034 | `DEFAULT_TIMEOUT` appears unused | Verify usage or remove |

**Effort:** Low — 5 one-line fixes across 4 files.

---

## YAML Violations (yamllint)

**1 file with trailing spaces + missing final newline:**

| File | Issues |
| ---- | ------ |
| `platform/services/postiz/deployment/compose.yml` | Trailing spaces, missing final newline |

**Effort:** Minimal — single file cleanup.

---

## Python Violations (Ruff)

All ruff violations were fixed in the CI pipeline PR. The following rules are intentionally ignored:

| Rule | Reason |
| ---- | ------ |
| BLE001 | Blind `except Exception` is intentional in worker error handling |
| SIM105 | `try-except-pass` is clearer than `contextlib.suppress` in this codebase |
| C408 | `dict()` calls are used intentionally for readability with keyword args |

---

## Implementation Order

1. Fix the 1 YAML file (postiz compose.yml) — trivial
2. Fix the 5 shellcheck warnings — low effort, high value
3. Run CI to confirm all green
4. Enable CI as a required check on PRs

---

## Future Compliance

All new code must pass CI before merge. The linting rules are:

- **Ruff**: Enforced on all Python files in the repo
- **ShellCheck**: Enforced at warning severity on all `.sh` files in `platform/`
- **yamllint**: Enforced with trailing-spaces and newline-at-eof enabled
- **trufflehog**: Scans for verified secrets on all PRs
- **IP/credential grep**: Catches leaked IPs and credential patterns

See `docs/LINTING-AND-TESTING.md` for onboarding details.
