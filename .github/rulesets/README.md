# Repository Rulesets (config-as-code)

GitHub branch/tag protection for `agent-cloud`, stored as JSON and applied with
[`apply.sh`](./apply.sh). The documented branch workflow in the root `CLAUDE.md`
("never push directly to `main`, never merge before checks pass") is enforced
here mechanically rather than by convention.

Why this matters for this repo: **production deploys clone `main` directly**
(`service_branch | default('main')`), so anything that lands on `main` is one
Semaphore task away from production. `main` must never be force-pushed, deleted,
or merged before checks pass.

## Rulesets

| File | Target | Protects |
|------|--------|----------|
| [`protect-main.json`](./protect-main.json) | default branch (`main`) | no direct push / force-push / deletion; PR required; conversations resolved; merge-commit or squash merges; required status checks; **PRs into `main` must originate from `dev`** |

### `protect-main` rules

- **Restrict deletions** + **block force pushes** — `main` history is never rewritten or removed.
- **Require a pull request** — `required_approving_review_count: 0` (solo maintainer: GitHub forbids self-approval, so a non-zero count would deadlock every PR). Raise to `1` only when a second human maintainer joins.
- **Require conversation resolution** — the enforceable CodeRabbit hook: unresolved review threads block the merge button.
- **Allow merge commits (default) or squash; linear history NOT required** — `dev` → `main` promotions use merge commits so the long-lived `dev` branch shares ancestry with `main` and promotions never diverge (which is what used to force a manual back-merge). Squash a merge only to scrub a branch whose history accidentally contains sensitive content. (Superseded the 2026-06-16 squash-only+linear decision on 2026-06-26.)
- **Required status checks** — `Static Analysis`, `Security Scan`, `Unit Tests` (the three jobs in `lint-and-test.yml` that run on **every** PR), plus `Promotion source (dev -> main)` (see next bullet). The path-gated `Go *` jobs are deliberately **not** required: they don't report on non-Go PRs and would deadlock the merge. Contexts are pinned to the GitHub Actions app (`integration_id: 15368`).
- **Promotion source: only `dev` may PR into `main`.** GitHub rulesets can protect the *base* branch but cannot restrict a PR's *head* branch, so the `Promotion source (dev -> main)` required check ([`enforce-promotion-source.yml`](../workflows/enforce-promotion-source.yml)) is the enforcing half: it runs on every PR whose base is `main` and fails unless the head branch is exactly `dev`. Together the two halves make `feature -> dev -> main` a hard gate instead of a convention. Emergency-only: an Admin bypass actor (below) can merge a hotfix straight to `main` despite a failing check.
- **Bypass actors** — Repository admin role only (`actor_id: 5`), break-glass. AI agents (NemoClaw / Claude Code) and any automation PAT are intentionally **off** the bypass list. Prefer flipping `enforcement` to `disabled` over using bypass, so bypass events stay rare and meaningful in the audit log.

## Applying

`apply.sh` is idempotent (create-or-update by ruleset name) and requires `gh`
authenticated as a **repository admin**, plus `jq`.

```bash
# Inventory current state first (expect empty / 404 on a clean repo)
gh api repos/uhstray-io/agent-cloud/rulesets
gh api repos/uhstray-io/agent-cloud/branches/main/protection

# Create or update every ruleset in this directory
.github/rulesets/apply.sh

# Show the effective, aggregated rules on main (what actually applies)
gh api repos/uhstray-io/agent-cloud/rules/branches/main
```

## Rollout: enforcement `active`

`protect-main.json` ships with `"enforcement": "active"` — the ruleset **blocks**
(no longer just logs). `"evaluate"` remains available as a dry-run mode (logs
would-be violations to **repo → Settings → Rules → Insights** without blocking);
drop back to it if you need to observe behavior before enforcing.

**Apply order matters (avoid a merge deadlock).** The `Promotion source (dev -> main)`
required check only reports once its workflow (`enforce-promotion-source.yml`) exists on
the branches a `main`-targeting PR is built from. Run `apply.sh` **after** this change has
reached `dev`/`main` — otherwise a `dev` → `main` PR blocks waiting for a required check
that never runs. If that happens, either merge the workflow first or temporarily set
`enforcement` back to `evaluate`, re-apply, land the workflow, then re-activate.

To (re)apply after editing:

1. Edit `protect-main.json` (e.g. `"active"` ⇄ `"evaluate"`).
2. Run `apply.sh` (idempotent create-or-update by name). The new state takes effect immediately.

### Verification matrix (after flipping to `active`)

| Test | Expected |
|------|----------|
| `git push origin main` (trivial commit) | Rejected by ruleset |
| `git push --force origin main` | Rejected |
| Delete `main` via UI/API | Rejected |
| Merge a PR with an unresolved CodeRabbit thread | Merge button blocked |
| Merge a PR before `Static Analysis` / `Security Scan` / `Unit Tests` report | Merge button blocked |
| Open a PR into `main` from a `feature/*` branch (head != `dev`) | `Promotion source` check fails → merge blocked |
| Open a `dev` → `main` PR | `Promotion source` check passes |
| Resolve threads + checks green + merge | Succeeds |
| Semaphore deploy from `main` | Unaffected (read-only clone) |

See [`plan/development/03-guardrails-governance.md`](../../plan/development/03-guardrails-governance.md)
for the full design, decisions, and follow-up phases (release-tag protection,
CodeQL as a required check, signed commits, `site-config` protection).
