# Architecture Review Follow-Up Items

**Date:** 2026-05-07
**Status:** TODO
**Context:** Remaining issues identified during the docs/architecture-review branch work that were deferred as nice-to-have or require larger effort.

---

## Quality Issues

### 1. README.md Four-Layer Guardrails Model — Not Mermaid

CLAUDE.md was converted to a mermaid diagram but README.md still uses a plain text code block for the same diagram. Should be consistent.

**File:** `README.md` (lines 12-19 approximately)
**Fix:** Replace the text code block with the same mermaid `graph TD` diagram used in CLAUDE.md.
**Effort:** 5 minutes

### 2. NETCLAW-INTEGRATION-PLAN.md — Old Repository Structure

The plan references the pre-monorepo directory structure (`deployments/agent-cloud/vms/netclaw-network/`) throughout. The "Files to Create/Modify" section (Section 15) is entirely based on old paths. The deploy.sh pattern still references the old 5-step pattern with secret generation (Step 1), which contradicts the composable pattern.

**File:** `plan/development/NETCLAW-INTEGRATION-PLAN.md`
**Fix:** Rewrite directory references to use `platform/services/netclaw/deployment/`, update deploy.sh pattern to container-lifecycle-only, align with composable task library.
**Effort:** 1-2 hours (significant rewrite)

### 3. Missing Context Fields in Frontmatter

Two new architecture documents are missing the `Context` field required by the architecture-reference.md template.

**Files:**
- `plan/architecture/CADDY-REVERSE-PROXY.md` — has `Contributors` instead of `Context`
- `plan/architecture/CI-TESTING-SPECIFICATION.md` — has `Scope` instead of `Context`

**Fix:** Add a one-paragraph `Context` field to each file's frontmatter.
**Effort:** 5 minutes each

### 4. NETCLAW-INTEGRATION-PLAN.md Status Casing

Status says `Proposed` (mixed case) instead of `PROPOSED` (all caps per taxonomy).

**File:** `plan/development/NETCLAW-INTEGRATION-PLAN.md`
**Fix:** Change `Proposed` to `PROPOSED` in the status line.
**Effort:** 1 minute

## Cross-Reference Issues

### 5. AUTOMATION-COMPOSABILITY.md Stale Cross-References

Three internal references use paths without subdirectories:
- Line ~65: `plan/SPARSE-CHECKOUT-MIGRATION.md` → should be `plan/development/SPARSE-CHECKOUT-MIGRATION.md`
- Line ~354: `plan/CREDENTIAL-LIFECYCLE-PLAN.md` → should be `plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md`
- Line ~609: `plan/CREDENTIAL-LIFECYCLE-PLAN.md` → should be `plan/architecture/CREDENTIAL-LIFECYCLE-PLAN.md`

**File:** `plan/architecture/AUTOMATION-COMPOSABILITY.md`
**Fix:** Update the three path references.
**Effort:** 5 minutes

### 6. OPA-INTEGRATION-PLAN.md Reference to Archived Document

References `UNIFICATION-PLAN.md` without noting it's archived. The reference says "OPA is listed as P0 priority alongside Kyverno in the 'Recommended Additions' section."

**File:** `plan/development/OPA-INTEGRATION-PLAN.md` (line ~851)
**Fix:** Add "(archived)" annotation or update reference to IMPLEMENTATION_PLAN.md.
**Effort:** 2 minutes

### 7. README.md Architecture Documentation Table Incomplete

The table lists 9 documents but omits 5 new architecture docs created in this review: `architecture-reference.md`, `ACCESS-BOUNDARIES.md`, `CI-TESTING-SPECIFICATION.md`, `PODMAN-VS-DOCKER-COMPOSE.md`, `SECURITY-TESTING-STANDARDS.md`.

**File:** `README.md`
**Fix:** Add the missing documents to the Architecture Documentation table.
**Effort:** 10 minutes

## Pre-Existing Security Items

### 8. agent.yaml.j2 Hardcoded Subnet

`platform/services/netbox/deployment/templates/agent.yaml.j2` lines 47 and 107 contain literal `192.168.1.0/24`. Should be `{{ discovery_target_subnet }}` with the value in site-config inventory.

**File:** `platform/services/netbox/deployment/templates/agent.yaml.j2`
**Fix:** Replace hardcoded subnet with inventory variable. Update site-config inventory to define `discovery_target_subnet`.
**Effort:** 15 minutes (includes inventory update and testing)

### 9. Historical .env in Git History

Commit `aecd47d` added `projects/nocodb/.env` containing a Postgres password. While it appears to be a template value (`abc123456xyz`), it demonstrates that .env files were committed historically.

**Fix:** Consider running `trufflehog` against full git history. If real credentials are found, use `git filter-repo` to clean history.
**Effort:** 30 minutes for audit, variable for cleanup

## Cross-References

- `plan/architecture/architecture-reference.md` — master index of all documents
- `plan/architecture/SECURITY-TESTING-STANDARDS.md` — security testing requirements
- `plan/architecture/CI-TESTING-SPECIFICATION.md` — testing standards for new services
