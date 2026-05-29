---
name: coderabbit-rate-limits
description: CodeRabbit plan, hourly rate-limit behavior, and how to recover from "Insufficient review credits" without buying credits.
metadata:
  node_type: memory
  type: reference
---

The uhstray-io org's CodeRabbit subscription is **Pro+**, not free. Confirmed from the auto-generated PR comment metadata on 2026-05-29 (`Plan: Pro Plus`).

**Why:** When a PR shows CodeRabbit status "fail — Insufficient review credits", the instinct is to assume free-tier quota or to top up billing. Neither is usually correct.

**How to apply:**

- **Hourly rate limit on Pro+: 10 PR reviews per hour per developer.** Source: <https://docs.coderabbit.ai/management/plans>.
- When a developer hits the cap, CodeRabbit posts a rate-limit comment with an exact wait time (e.g., "More reviews will be available in 36 minutes and 10 seconds"). The window resets on a rolling hour, not at a fixed clock minute.
- The accompanying "Your organization has run out of usage credits" message refers to an **optional, opt-in usage-based add-on** for going beyond the hourly cap. The platform does **not** require buying credits to recover — waiting refills the regular quota automatically.
- After the quoted window elapses, retrigger by posting `@coderabbitai review` as a PR comment, or by pushing any new commit. CodeRabbit will re-review the changed files since its last run.
- The check on the PR shows up as "fail" while rate-limited — that is **not a code finding** and should not block merge on its own. Inspect the CodeRabbit comment to confirm it's a rate-limit vs an actual review.

**Quick triage of the CodeRabbit check status:**

| Check appearance | Meaning | Action |
|------------------|---------|--------|
| `pending` + "Review in progress" | Working | Wait |
| `pass` + "Review completed" | Done, see inline comments | Triage findings |
| `fail` + "Insufficient review credits" | Hourly cap hit | Wait the quoted minutes, then `@coderabbitai review` |
| `fail` + actual findings | Code issues found | Address inline comments |

**Bursting above 10/hour:** if a workflow regularly needs more than 10 reviews/hour per developer (e.g., a stack of small PRs being addressed quickly), buy the usage-based add-on. Otherwise pace the PRs.
