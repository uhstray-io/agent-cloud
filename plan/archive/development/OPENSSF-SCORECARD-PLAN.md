# OpenSSF Scorecard Implementation Plan

**Date:** 2026-04-22
**Status:** TODO
**Reference:** [ossf/scorecard](https://github.com/ossf/scorecard), [scorecard-action](https://github.com/ossf/scorecard-action)

---

## Overview

The [OpenSSF Scorecard](https://scorecard.dev/) evaluates open-source repositories against 20 security health checks. It runs as a GitHub Action, produces a score (0-10 per check), publishes results to the Security tab, and enables a badge for the README.

Free for all public repositories.

---

## Current State Assessment

Mapping each Scorecard check against the agent-cloud repository's current state:

| Check | Current State | Expected Score | Action Needed |
| ----- | ------------- | -------------- | ------------- |
| **Binary-Artifacts** | No binaries in repo | 10 | None |
| **Branch-Protection** | No branch protection rules configured | 0 | Configure branch protection on main |
| **CI-Tests** | GitHub Actions CI pipeline (PR #7) | 10 | Already implemented |
| **CII-Best-Practices** | No OpenSSF badge | 0 | Register at bestpractices.coreinfrastructure.org |
| **Code-Review** | CodeRabbit reviews PRs | ~7 | Already in practice |
| **Contributors** | Single-org project | ~3 | Low priority — community growth over time |
| **Dangerous-Workflow** | No `pull_request_target`, no `workflow_run` with untrusted input | 10 | Already safe |
| **Dependency-Update-Tool** | Dependabot configured | 10 | Already implemented |
| **Fuzzing** | No fuzzing configured | 0 | Consider OSS-Fuzz for Python workers |
| **License** | No LICENSE file | 0 | Add LICENSE file |
| **Maintained** | Active commits weekly | 10 | Already active |
| **Packaging** | No package publishing | 0 | Low priority — internal platform |
| **Pinned-Dependencies** | GHA actions use tags not SHAs | ~5 | Pin to commit SHAs |
| **SAST** | Ruff + Bandit + ShellCheck | 10 | Already implemented |
| **SBOM** | No SBOM generation | 0 | Add SBOM generation to CI |
| **Security-Policy** | No SECURITY.md | 0 | Add SECURITY.md |
| **Signed-Releases** | No releases or signing | 0 | Low priority — no published releases |
| **Token-Permissions** | Workflow uses default token permissions | ~5 | Add top-level `permissions: read-all` |
| **Vulnerabilities** | Dependabot monitors deps | ~8 | Already scanning |
| **Webhooks** | No webhooks configured | N/A | Not applicable |

**Estimated initial score: ~5/10.** With the quick wins below: ~7-8/10.

---

## Implementation Phases

### Phase 1: Quick Wins (30 minutes)

These improve the score with minimal effort:

#### 1a. Add GitHub Actions Scorecard Workflow

```yaml
# .github/workflows/scorecard.yml
name: OpenSSF Scorecard
on:
  branch_protection_rule:
  schedule:
    - cron: '0 6 * * 1'  # Weekly Monday 6am UTC
  push:
    branches: [main]

permissions: read-all

jobs:
  analysis:
    name: Scorecard analysis
    runs-on: ubuntu-latest
    permissions:
      security-events: write
      id-token: write
      contents: read
      actions: read
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false

      - name: Run Scorecard
        uses: ossf/scorecard-action@v2.4.0
        with:
          results_file: results.sarif
          results_format: sarif
          publish_results: true

      - name: Upload SARIF
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: results.sarif
```

#### 1b. Add SECURITY.md

Create `SECURITY.md` at repo root with vulnerability disclosure guidance.

#### 1c. Add LICENSE file

Add appropriate open-source license (the project's design principles state "use open-source and free systems first").

#### 1d. Pin GitHub Actions to commit SHAs

Replace tag references with pinned SHAs in all workflow files:

```yaml
# Before
- uses: actions/checkout@v4
# After  
- uses: actions/checkout@<sha>  # v4.x.x
```

#### 1e. Set top-level workflow permissions

Add `permissions: read-all` at the top level of all workflow files, with job-level write permissions only where needed.

### Phase 2: Medium Effort (1-2 hours)

#### 2a. Configure Branch Protection

Set up branch protection rules on `main`:
- Require PR before merging
- Require status checks to pass (lint, security, test jobs)
- Require CodeRabbit review
- Disallow force pushes to main

#### 2b. Add SBOM Generation

Add a step to the CI pipeline that generates a Software Bill of Materials:

```yaml
- name: Generate SBOM
  uses: anchore/sbom-action@v0
  with:
    format: spdx-json
    output-file: sbom.spdx.json
```

#### 2c. Register for CII Best Practices Badge

Register the project at [bestpractices.coreinfrastructure.org](https://bestpractices.coreinfrastructure.org/) and work through the self-assessment. Many criteria are already met (version control, CI, issue tracking, documentation).

### Phase 3: Longer Term

#### 3a. Fuzzing (optional)

Consider adding fuzzing for the Python worker parsing functions (`_pick_primary_ipv4`, `_sanitize_description`, LLDP JSON parser). Tools: `atheris` (Python fuzzer) or OSS-Fuzz integration.

#### 3b. Signed Releases (optional)

If the project publishes releases in the future, add Sigstore signing via `cosign` or SLSA provenance generation.

---

## Expected Score Progression

| Phase | Estimated Score | Checks Improved |
| ----- | --------------- | --------------- |
| Current | ~5/10 | — |
| Phase 1 (quick wins) | ~7/10 | Security-Policy, License, Pinned-Dependencies, Token-Permissions |
| Phase 2 (medium effort) | ~8/10 | Branch-Protection, SBOM, CII-Best-Practices |
| Phase 3 (long term) | ~9/10 | Fuzzing, Signed-Releases |

---

## Badge

After the Scorecard workflow runs, add the badge to README.md:

```markdown
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/uhstray-io/agent-cloud/badge)](https://scorecard.dev/viewer/?uri=github.com/uhstray-io/agent-cloud)
```
