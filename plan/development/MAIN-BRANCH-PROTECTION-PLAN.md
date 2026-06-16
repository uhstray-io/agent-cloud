# Branch Protection & Rulesets Plan — `uhstray-io/agent-cloud`

**Date:** 2026-06-12 · **Updated:** 2026-06-16
**Status:** IN PROGRESS — config-as-code built on branch `ci/main-branch-protection`; live apply pending (see Status Update).
**Scope:** Protect `main` in the public agent-cloud monorepo using GitHub repository rulesets, with a phased path from baseline protection to full CI-gated merges.

---

## Status Update (2026-06-16)

Two original assumptions were corrected against the live repo, and the open questions were decided:

- **Org tier is GitHub Enterprise, not Free.** The "Plan-tier constraints" section below no longer binds: `evaluate` (dry-run) enforcement, org-level rulesets, metadata restrictions, and private-repo (`site-config`) protection are all available. The repo-level ruleset here still stands; org-level is a future option.
- **CI already exists** (`.github/workflows/lint-and-test.yml`). The Phase 2 prerequisite is met, so Phases 1 and 2 are **collapsed**: the initial ruleset includes required status checks.
- **Decisions locked:** rollout is **evaluate → active**; **squash-only + linear history**; required checks **included now**; `strict_required_status_checks_policy` kept `false`.
- **Real check contexts** are the job `name:` values — `Static Analysis`, `Security Scan`, `Unit Tests` (not the `lint`/`security`/`test` job-ids guessed in the original JSON below). Pinned to the GitHub Actions app (`integration_id: 15368`). The path-gated `Go *` jobs are intentionally **not** required (they don't report on non-Go PRs → would deadlock the merge).
- **Artifacts built:** `.github/rulesets/protect-main.json` (canonical), `.github/rulesets/apply.sh` (idempotent create-or-update), `.github/rulesets/README.md`, `.coderabbit.yaml` (`request_changes_workflow`). The ruleset is **applied live in `evaluate`** (id `17752539`); the remaining step is the `evaluate → active` flip after Insights verification.
- **Bonus finding:** CodeQL **default-setup** code scanning is already active (`Analyze (...)` checks from app 15368). Candidate additional required check once confirmed it reports unconditionally on every PR — see Open Questions.

---

## Problem

The branch workflow in `CLAUDE.md` ("never push directly to main, never merge before checks pass") is currently **convention, not enforcement**. Nothing on the GitHub side prevents:

- A direct push to `main` (human, NemoClaw, Claude Code, or a misconfigured playbook)
- A force push that rewrites `main` history that production VMs pull from
- Deleting `main` or merging a PR before CodeRabbit/CI complete
- Merging with unresolved CodeRabbit findings

This matters more than usual here because **production deploys clone `main` directly** (`service_branch | default('main')` in every deploy playbook). Anything that lands on `main` is one Semaphore task away from running on production VMs. The branch-testing workflow's rollback story ("re-deploy from main") also assumes `main` is always a known-good, non-rewritten ref.

## Goal

Make the documented workflow mechanically enforced: `main` only changes via PRs that have passed all checks, history is never rewritten, and AI agents have no path around the gate — while keeping solo-maintainer friction near zero.

---

## Decision: Rulesets over Legacy Branch Protection

Use **repository rulesets** as the enforcement mechanism, not classic branch protection rules. Both are available on public repos under GitHub Free for organizations, but rulesets win for this repo:

| Factor | Rulesets | Legacy branch protection |
|---|---|---|
| Multiple rules layered, most-restrictive wins | Yes | One rule per pattern |
| Visible to anyone with read access (auditable on a public repo) | Yes | Admin-only |
| Enable/disable without deleting (enforcement status) | Yes | No |
| JSON export/import → config-as-code in the repo | Yes | API only, clunkier schema |
| Bypass actors scoped per-ruleset | Yes | Coarse admin toggle |

> **Superseded (2026-06-16):** uhstray-io is on **GitHub Enterprise**, so none of the four constraints below bind — `evaluate` mode, org-level rulesets, metadata restrictions, and private-repo protection are all available. Retained for historical context and in case the tier ever changes.

Plan-tier constraints originally assumed (uhstray-io was assumed to be on **GitHub Free for organizations**):

1. **Org-level rulesets** (one ruleset targeting many repos) require Team/Enterprise. This plan uses a repo-level ruleset; if the org ever upgrades, the same JSON migrates up.
2. **"Evaluate" enforcement status** (dry-run mode) is Enterprise-only. We compensate with a short manual test matrix (see Verification).
3. **Metadata restrictions** (commit message / author email rules) are Enterprise-only — branch-name conventions like `feat/*` stay convention-only in `CLAUDE.md`.
4. **Important side-effect for site-config:** rulesets and branch protection on **private** repos require Pro/Team. If the org is on Free, the private `site-config` repo **cannot be protected the same way**. Mitigation: site-config has a tiny collaborator set (you + agents with read-only deploy keys), which is the real control there. Flag for re-evaluation if the org upgrades.

---

## Design Constraints Specific to agent-cloud

**Solo maintainer + AI agents.** GitHub does not allow a PR author to approve their own PR, so `required approvals ≥ 1` would deadlock every PR you open. The gate therefore cannot be human review count — it must be **automation**: required status checks and CodeRabbit conversation resolution. Required approvals is set to **0** now, raised to 1 only if a second human maintainer joins.

**CI now exists (corrected 2026-06-16).** `.github/workflows/lint-and-test.yml` is deployed and runs on every `pull_request` to `main`/`dev`. Required status checks can only reference contexts that actually report, and these now do — so they are included in the **initial** ruleset rather than deferred. The three unconditional jobs are required (`Static Analysis`, `Security Scan`, `Unit Tests`); the path-gated `Go *` jobs are not. (Original text assumed CI was unbuilt and deferred checks to a separate Phase 2.)

**CodeRabbit is a reviewer, not a status check, by default.** GitHub App reviews don't count toward required-approval counts, so CodeRabbit can't satisfy an approval rule. The enforceable hook is different: enable CodeRabbit's *request-changes workflow* (`reviews.request_changes_workflow: true` in `.coderabbit.yaml`) so it opens review threads that must be resolved, and turn on the ruleset's **required conversation resolution** rule. Result: unresolved CodeRabbit findings mechanically block the merge button. (Verify in CodeRabbit's current docs whether it also offers a commit-status/check-run mode — if so, that check can additionally be added to required status checks in Phase 2.)

**Agents must have no bypass path.** NemoClaw / Claude Code / any GitHub App or PAT used by automation stays **off** the bypass list. The only bypass actor is the Repository admin role (you), reserved for break-glass. Even then, prefer flipping the ruleset to `disabled` over routinely using bypass, so bypass events stay rare and meaningful in the audit log.

**Deploy keys are unaffected.** The OpenBao-stored deploy key (`secret/services/github:deploy_key`) is read-only clone access; rulesets only constrain writes, so no playbook changes are needed.

---

## Ruleset Design

### Ruleset 1: `protect-main` (Phase 1, immediately)

Target: default branch (`~DEFAULT_BRANCH`), enforcement `active`.

| Rule | Setting | Rationale |
|---|---|---|
| Restrict deletions | On | `main` is the production deploy source; deletion = outage of every deploy/rollback path |
| Block force pushes | On | VMs `git pull` from main; rewritten history breaks sparse checkouts and rollback guarantees |
| Require a pull request | On | Mechanizes the CLAUDE.md branch workflow |
| → Required approvals | 0 | Solo maintainer; self-approval impossible (see constraints) |
| → Dismiss stale approvals on push | On | Future-proofing for when approvals > 0 |
| → Require conversation resolution | On | The CodeRabbit enforcement hook |
| → Allowed merge methods | Squash only | Decided 2026-06-16: squash-only for clean linear monorepo history |
| Require status checks | Included (initial ruleset) | CI exists; `Static Analysis` / `Security Scan` / `Unit Tests`, pinned to the GitHub Actions app |
| Require linear history | On | Decided 2026-06-16: squash-only adopted, so linear history is enforced |
| Require signed commits | Off | Agents would each need signing keys provisioned; revisit in Phase 4 |

Bypass actors: `Repository admin` role, mode `always` — break-glass only, never for routine work.

### Ruleset 2: `protect-release-tags` (Phase 4, optional)

Target: tags matching `v*`. Rules: restrict creation/update/deletion to admins. Becomes relevant once the Harbor image-promotion pipeline from `IMPLEMENTATION_PLAN.md` pins prod deploys to version tags — at that point a moved tag is equivalent to a force-pushed main.

---

## Configuration as Code

Store the ruleset JSON in the repo and apply via `gh api`, consistent with the documentation-first / auditable-infrastructure pattern. CodeRabbit can then review changes to the protection rules themselves.

`.github/rulesets/protect-main.json` (canonical — this block mirrors the committed file; keep them in sync):

```json
{
  "name": "protect-main",
  "target": "branch",
  "enforcement": "evaluate",
  "conditions": {
    "ref_name": {
      "include": ["~DEFAULT_BRANCH"],
      "exclude": []
    }
  },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "required_linear_history" },
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true,
        "allowed_merge_methods": ["squash"]
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          { "context": "Static Analysis", "integration_id": 15368 },
          { "context": "Security Scan", "integration_id": 15368 },
          { "context": "Unit Tests", "integration_id": 15368 }
        ]
      }
    }
  ],
  "bypass_actors": [
    {
      "actor_id": 5,
      "actor_type": "RepositoryRole",
      "bypass_mode": "always"
    }
  ]
}
```

(`enforcement: evaluate` is the dry-run rollout — flip to `active` after verification. `actor_id: 5` = the built-in Repository admin role. `integration_id: 15368` = the GitHub Actions app, pinning each required context to it.)

The `required_status_checks` rule is **already included in the initial ruleset above** (corrected 2026-06-16 — CI exists). The contexts are the job `name:` values, **not** the job-ids — pinned to the GitHub Actions app:

```json
{
  "type": "required_status_checks",
  "parameters": {
    "strict_required_status_checks_policy": false,
    "do_not_enforce_on_create": false,
    "required_status_checks": [
      { "context": "Static Analysis", "integration_id": 15368 },
      { "context": "Security Scan", "integration_id": 15368 },
      { "context": "Unit Tests", "integration_id": 15368 }
    ]
  }
}
```

`strict_required_status_checks_policy: false` (branch need not be up to date with main before merge) keeps solo-dev friction low; flip to `true` if concurrent agent-authored PRs start landing semantically-conflicting changes.

### Commands

```bash
# Inventory current state (expect 404 / empty on first run)
gh api repos/uhstray-io/agent-cloud/branches/main/protection
gh api repos/uhstray-io/agent-cloud/rulesets

# Create the ruleset
gh api -X POST repos/uhstray-io/agent-cloud/rulesets \
  --input .github/rulesets/protect-main.json

# Update later (get {id} from the list call)
gh api -X PUT repos/uhstray-io/agent-cloud/rulesets/{id} \
  --input .github/rulesets/protect-main.json

# Show the effective, aggregated rules on main (what actually applies)
gh api repos/uhstray-io/agent-cloud/rules/branches/main
```

---

## Required-Check Pitfall: Path-Filtered Workflows

If the CI workflow uses `paths:` filters, a docs-only PR would never trigger the `lint`/`security`/`test` jobs, the required contexts would sit in "expected" forever, and the PR could never merge. Two safe patterns:

1. Run the workflow on every `pull_request` with no path filter (jobs are cheap at this repo's size), or
2. Add a single always-running `gate` job that `needs:` all real jobs and is the **only** required context; conditionally-skipped jobs report `skipped`, which `gate` treats as success via `if: always()` + result inspection.

Option 1 is recommended initially for simplicity; the gitleaks/IP-audit job should run on every PR regardless.

---

## Rollout Phases

**Phase 0 — Prerequisites — DONE (2026-06-16).** Org tier confirmed (**Enterprise**). Inventory run: no pre-existing rulesets, no legacy branch protection on `main` (clean slate — nothing to remove). Merge-method decided (squash-only).

**Phase 1 + 2 — Baseline ruleset *with* CI gating (this branch).** `.github/rulesets/protect-main.json` is committed via this feature-branch PR. Apply it with `.github/rulesets/apply.sh` (run by a repository admin) in `enforcement: evaluate`; watch repo → Settings → Rules → Insights for false positives; then flip the one field to `active` and re-run `apply.sh`. From active: no direct/force pushes, no deletion, PR required, conversations resolved, and `Static Analysis` / `Security Scan` / `Unit Tests` must pass. This is the point where "never merge before checks pass" stops being discipline and becomes physics.

**Phase 3 — CodeRabbit enforcement — DONE (this branch).** `.coderabbit.yaml` now sets `reviews.request_changes_workflow: true`, so unresolved CodeRabbit findings hold the merge via the conversation-resolution rule (demonstrated on the promotion PR, which CodeRabbit reviewed `CHANGES_REQUESTED`). Still open: whether current CodeRabbit also exposes a check-run that can be added as a required status check (see Open Question 2).

**Phase 4 — Hardening (later, optional).** Add the `protect-release-tags` ruleset when version-pinned deploys arrive. Add the CodeQL `Analyze (...)` checks as required once confirmed they report on every PR. Protect the private `site-config` repo (now possible on Enterprise). Revisit signed commits (provisioning signing keys to every agent identity — meaningful effort, real provenance benefit on a public repo). Linear history is already enabled (squash-only adopted).

---

## Verification Matrix (run after Phase 1, repeat after Phase 2)

| Test | Expected result |
|---|---|
| `git push origin main` with a trivial commit | Rejected: ref update blocked by ruleset |
| `git push --force origin main` | Rejected |
| Delete `main` via UI/API | Rejected |
| Open PR, attempt merge with an unresolved CodeRabbit thread | Merge button blocked |
| (Phase 2) Open PR, attempt merge before `lint`/`security`/`test` report | Merge button blocked |
| Open PR, resolve threads, checks green, merge | Succeeds |
| Semaphore deploy from `main` post-merge | Unaffected (read-only clone) |

Also confirm the agents' happy path end-to-end once: NemoClaw/Claude Code pushes a feature branch, opens a PR, and can do everything *except* merge early.

---

## Open Questions

1. **Merge method:** ~~squash-only vs. keeping merge commits~~ — **DECIDED 2026-06-16: squash-only + linear history.** `"merge"` removed from `allowed_merge_methods`; `required_linear_history` enabled.
2. **CodeRabbit status check:** does the current CodeRabbit offer a commit status / check-run that can be a required check, or is conversation-resolution the only enforceable hook? Verify against current CodeRabbit docs during Phase 3. *(Still open.)*
3. **Strict up-to-date checks:** ~~leave `strict: false`?~~ — **DECIDED: keep `false`** until concurrent PR volume (multiple agents) makes stale-branch merges a real risk; revisit then.
4. **site-config protection:** ~~accept "unprotectable on Free plan"~~ — **RESOLVED 2026-06-16: the org is Enterprise, so the private `site-config` repo *can* be protected the same way.** Tracked as a Phase 4 follow-up (separate repo, out of this branch's scope).

---

## Documentation Updates on Completion

- `CLAUDE.md` — Branch Workflow section: **DONE** — notes the workflow is enforced by the `protect-main` ruleset; bypass is break-glass only.
- `plan/architecture/BRANCH-TESTING-WORKFLOW.md` — PR Merge Rules section: **DONE** — checks are now mechanically required, not just procedural.
- `.github/rulesets/README.md` — **DONE** — config-as-code reference (rules, idempotent apply, evaluate→active, verification matrix).
- ~~`plan/NEXT-STEPS.md`~~ — file does not exist; the Phase 2 hook is moot because required status checks ship in the initial ruleset.