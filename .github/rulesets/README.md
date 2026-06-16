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
| [`protect-main.json`](./protect-main.json) | default branch (`main`) | no direct push / force-push / deletion; PR required; conversations resolved; squash-only linear history; required status checks |

### `protect-main` rules

- **Restrict deletions** + **block force pushes** — `main` history is never rewritten or removed.
- **Require a pull request** — `required_approving_review_count: 0` (solo maintainer: GitHub forbids self-approval, so a non-zero count would deadlock every PR). Raise to `1` only when a second human maintainer joins.
- **Require conversation resolution** — the enforceable CodeRabbit hook: unresolved review threads block the merge button.
- **Require linear history** + **squash-only** merges — clean monorepo history.
- **Required status checks** — `Static Analysis`, `Security Scan`, `Unit Tests` (the three jobs in `lint-and-test.yml` that run on **every** PR). The path-gated `Go *` jobs are deliberately **not** required: they don't report on non-Go PRs and would deadlock the merge. Contexts are pinned to the GitHub Actions app (`integration_id: 15368`).
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

## Rollout: evaluate → active

`protect-main.json` ships with `"enforcement": "evaluate"` — a dry-run mode
(available on this org's Enterprise plan) that **logs** would-be violations
without blocking anyone. Use it to confirm the rules behave as intended:

1. Apply with `apply.sh` (enforcement `evaluate`).
2. Open a test PR / push and watch **repo → Settings → Rules → Insights** for the
   recorded would-be-blocked events. Confirm no legitimate workflow is caught.
3. Edit `protect-main.json`: change `"enforcement": "evaluate"` → `"active"`.
4. Re-run `apply.sh`. The ruleset now blocks.

### Verification matrix (after flipping to `active`)

| Test | Expected |
|------|----------|
| `git push origin main` (trivial commit) | Rejected by ruleset |
| `git push --force origin main` | Rejected |
| Delete `main` via UI/API | Rejected |
| Merge a PR with an unresolved CodeRabbit thread | Merge button blocked |
| Merge a PR before `Static Analysis` / `Security Scan` / `Unit Tests` report | Merge button blocked |
| Resolve threads + checks green + merge | Succeeds |
| Semaphore deploy from `main` | Unaffected (read-only clone) |

See [`plan/development/MAIN-BRANCH-PROTECTION-PLAN.md`](../../plan/development/MAIN-BRANCH-PROTECTION-PLAN.md)
for the full design, decisions, and follow-up phases (release-tag protection,
CodeQL as a required check, signed commits, `site-config` protection).
