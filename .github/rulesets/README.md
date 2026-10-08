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
| [`protect-dev.json`](./protect-dev.json) | `refs/heads/dev` | no deletion, no force-push; deliberately **no** PR or status-check rule |

### `protect-main` rules

- **Restrict deletions** + **block force pushes** — `main` history is never rewritten or removed.
- **Require a pull request** — `required_approving_review_count: 0` (solo maintainer: GitHub forbids self-approval, so a non-zero count would deadlock every PR). Raise to `1` only when a second human maintainer joins.
- **Require conversation resolution** — the enforceable CodeRabbit hook: unresolved review threads block the merge button.
- **Allow merge commits (default) or squash; linear history NOT required** — `dev` → `main` promotions use merge commits so the long-lived `dev` branch shares ancestry with `main` and promotions never diverge (which is what used to force a manual back-merge). Squash a merge only to scrub a branch whose history accidentally contains sensitive content. (Superseded the 2026-06-16 squash-only+linear decision on 2026-06-26.)
- **Required status checks** — `Static Analysis`, `Security Scan`, `Unit Tests` (the three jobs in `lint-and-test.yml` that run on **every** PR), plus `Promotion source (dev -> main)` (see next bullet). The path-gated `Go *` jobs are deliberately **not** required: they don't report on non-Go PRs and would deadlock the merge. Contexts are pinned to the GitHub Actions app (`integration_id: 15368`).
- **Promotion source: only `dev` may PR into `main`.** GitHub rulesets can protect the *base* branch but cannot restrict a PR's *head* branch, so the `Promotion source (dev -> main)` required check ([`enforce-promotion-source.yml`](../workflows/enforce-promotion-source.yml)) is the enforcing half: it runs on every PR whose base is `main` and fails unless the head is exactly this repository's `dev` branch (`head_ref` is only a branch name, so a fork's branch called `dev` is refused by comparing the head repository with this one). It also fails unless the PR head already contains every commit on `main`: `sync-main-to-dev.yml` cannot push a `main`-only workflow-file change into `dev`, and a promotion from a `dev` that lacks it would revert it on `main` (`docs/MISTAKES.md` 10.23). Merge `main` into `dev` through a feature PR to clear that failure. Together the two halves make `feature -> dev -> main` a hard gate instead of a convention. Emergency-only: an Admin bypass actor (below) can merge a hotfix straight to `main` despite a failing check.
- **Bypass actors** — Repository admin role only (`actor_id: 5`), break-glass. AI agents (NemoClaw / Claude Code) and any automation PAT are intentionally **off** the bypass list. Prefer flipping `enforcement` to `disabled` over using bypass, so bypass events stay rare and meaningful in the audit log.

### `protect-dev` rules

- **Restrict deletions** — the repository setting `delete_branch_on_merge` deletes a merged PR's head branch, and a `dev` -> `main` promotion has `dev` as its head. Merging PR #447 on 2026-10-08 deleted `dev` that way, and `sync-main-to-dev.yml` then failed with "A branch or tag with the name 'dev' could not be found" (`docs/MISTAKES.md` 3.13). GitHub's docs state that "Branch protection rules and repository rules can also prevent branches being automatically deleted" ([docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-the-automatic-deletion-of-branches)); the `deletion` rule means "only users with bypass permissions can delete branches or tags whose name matches the pattern" ([docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)). Not verified against the live repo: that the automatic deletion is blocked in practice; confirm with the check below after the first promotion.
- **Block force pushes** — `sync-main-to-dev.yml` pushes a merge commit on top of `dev` (`git push origin HEAD:dev`), a fast-forward, so it is unaffected. A force-push of `dev` is now refused.
- **No pull request rule, no required checks** — the sync workflow pushes to `dev` directly with `GITHUB_TOKEN`; a PR or check rule would refuse it.
- **Bypass actors** — the same Repository admin role entry as `protect-main` (`actor_id: 5`), break-glass, so a deliberate delete or rewrite of `dev` stays possible. Prefer flipping `enforcement` to `disabled` over using bypass. Not verified: whether a merge performed by a bypass-listed admin also lets the automatic head-branch deletion through; check after the first promotion.
- The rule is pattern-based, so it does not recreate a missing `dev`: restore the branch from its last head before applying.

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
| Open a `dev` → `main` PR while `main` has a commit `dev` lacks | `Promotion source` check fails → merge blocked until `main` is merged into `dev` |
| Resolve threads + checks green + merge | Succeeds |
| Semaphore deploy from `main` | Unaffected (read-only clone) |

See [`plan/development/03-guardrails-governance.md`](../../plan/development/03-guardrails-governance.md)
for the full design, decisions, and follow-up phases (release-tag protection,
CodeQL as a required check, signed commits, `site-config` protection).

## Drift check (live vs JSON)

Applying is by hand, so a file can say one thing while GitHub enforces another —
`docs/MISTAKES.md` 10.21 records `protect-main.json` declaring `active` while the live
ruleset sat in `evaluate` with an extra rule, leaving `main` unprotected.
[`check-drift.sh`](./check-drift.sh) is the read-back half of `apply.sh`: it matches each
`*.json` here to the live ruleset by name, fetches it, and [`compare.py`](./compare.py)
fails naming every difference in the updatable fields `name`, `target`, `enforcement`, `conditions`,
`bypass_actors` and the rules (by type, with their parameters). The comparison runs both
ways: a declared key must match, and a key set only live is drift unless its value is an
empty default (`false`, `0`, `""`, `null`, `[]`, `{}`). Read-only response fields (ids,
links, timestamps) are ignored, list order does not count, a duplicated rule type is drift,
and a ruleset missing live is drift. Exit codes: `0` match, `1` drift, `2` API/auth/usage
error (never reported as drift).

```bash
.github/rulesets/check-drift.sh            # read-only; exit 1 on any drift
```

[`ruleset-drift.yml`](../workflows/ruleset-drift.yml) runs it daily and on
`workflow_dispatch`. Token: GitHub's fine-grained permission table lists reading
repository rulesets under the **Metadata** (read) permission
([docs](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens#repository-permissions-for-metadata)),
and the workflow uses `GITHUB_TOKEN` unless the optional `RULESET_READ_TOKEN` secret is
set. The API returns `bypass_actors` only to a caller with write access to the ruleset
([docs](https://docs.github.com/en/rest/repos/rules#get-a-repository-ruleset)), so with
`GITHUB_TOKEN` that field is reported as a warning (not compared). To compare it too,
store a fine-grained token scoped to this repository with the Administration permission
as `RULESET_READ_TOKEN`. Not yet verified: that `GITHUB_TOKEN` can read the endpoint at
all — if the first run fails with 403/404, add the secret. The token is passed only via
`GH_TOKEN` and is never printed. Tests: `platform/tests/test_ruleset_drift_compare.py`.
