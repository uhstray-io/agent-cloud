# Dev PR Merge Guard

**Date:** 2026-10-10
**Status:** IMPLEMENTED
**Context:** PR #531 was merged while checks for its final head were still running. Dev intentionally has no required-check rule because the main-to-Dev reconciliation workflow pushes directly to `dev`.

## Problem

A pull-request status rollup can describe an earlier head. Since Dev has no required checks, `gh pr merge --auto` can arrange a merge before the final-head jobs finish. The operator needs one repeatable gate that checks the exact head, complete CI, independent review, and cleared feedback before any merge.

## Design principles

- Read-only validation is the default. A merge needs `--merge` and an explicit feedback-addressed attestation.
- Fail closed on missing, pending, failed, skipped-unconditionally, unknown, stale, or incompletely paginated evidence.
- Pin every gate and the final merge to one full expected head SHA.
- Use installed `gh` for authentication and API access; add no runtime dependency.
- Leave the Dev ruleset compatible with main-to-Dev sync. This CLI is an operator convention and can be bypassed by another merge path.

## Architecture

```mermaid
flowchart TD
    INPUT["PR number + expected full head SHA"] --> PR["Open, same-repo Dev PR and exact head"]
    PR --> POLICY["Dev workflow policy and complete changed-file evidence"]
    POLICY --> CI["Latest workflow attempt + exact-head job check runs"]
    CI --> REVIEW["Final-head approval + no changes request or unresolved thread"]
    REVIEW --> ACK["Operator attests actionable feedback is addressed"]
    ACK --> MODE{"Explicit --merge?"}
    MODE -->|No| READ["Print read-only PASS"]
    MODE -->|Yes| RECHECK["Repeat every read gate"]
    RECHECK --> MERGE["One gh pr merge --merge --match-head-commit SHA"]
    MERGE --> READBACK["Read PR state; never retry blindly"]
```

`scripts/merge-when-green.py` reads `.github/workflows/lint-and-test.yml` from the PR's pinned base and head commit SHAs. For each version it checks the Contents response's path, type, base64 encoding, size, and Git blob SHA, then checks the matching recursive tree entry is one regular `100644` blob with the same SHA and size; incomplete or truncated tree evidence refuses. The workflow policy keeps the original pre-partition blob `da998c35a6bc5d3ea7eedc72ea228e48f9fc6b38` and first partition blob `29020efdaa1747bd2dc42d4c6468a96e676d8b4b` pinned. The one allowed follow-up changes the first partition to the exact credential-safe workflow blob pinned in `scripts/merge-when-green.py`; later PRs must keep that workflow byte-identical. Each accepted state is validated against its required job names, trigger, and path-filter policy. Each transition requires one GitHub file record with status `modified` and no prior filename. Any other base/head pair, extra byte, rename/add/delete/copy, malformed evidence, different mode, or unsupported workflow edit refuses. The workflow path remains in changed-file classification, so all three Go jobs remain required when it changes. Once the transition merges, the new `Python Playbook Safety` check is mandatory for subsequent PRs.

The Python core job keeps pytest's configured root discovery active and deselects only the NetBox, Semaphore, and the two complete playbook-safety files. The new required Python Playbook Safety job runs `test_rollback_inference_route.py` and `test_executor_internal_overrides.py` explicitly with the same Python dependencies, Ansible collections, and xdist options. The `Unit Tests` aggregate runs under `if: always()` and requires a successful result from each shard. `platform/tests/test_ci_python_partition.py` derives collection commands and selectors from the workflow, then collects root, core, safety, NetBox, and Semaphore node IDs. Collection errors fail the test; the four shards must be pairwise disjoint, their union must exactly equal root, and the safety shard must be nonempty.

The gate requires the nine unconditional quality jobs, the `Detect changes` classifier, and all three Go jobs to appear in the latest workflow attempt. A Go job may be skipped only when paginated PR file evidence proves its path condition false; rename evidence includes both the prior and current path. Exact-head commit statuses are paginated and checked by context in GitHub's newest-first order; CodeRabbit is review-only and optional, while every other context must be successful. Workflow jobs must map through `check_run_url` to completed check runs for the exact expected SHA; the latest run/attempt is selected by workflow identity. Unknown workflow/check policy refuses.

The tool also requires an approval on the expected SHA from a non-author reviewer, or a structured operator-attested JSON receipt with `repository`, `pr`, `head_sha`, `reviewer`, `session`, `verdict: "APPROVED"`, and `evidence`. The receipt is not cryptographic proof of reviewer independence. All review pages are read in GitHub's chronological order, with the later item winning equal submission timestamps; any latest non-dismissed changes-requested verdict or unresolved review thread refuses. Thread pagination metadata and every `isResolved` value must be boolean, and a continuing page must provide a cursor. `--feedback-addressed` is a separate operator attestation for actionable feedback in reviews and ordinary PR comments; the CLI does not classify comment prose.

## Implementation phases

1. Implement a standard-library CLI using `gh api`; make validation read-only unless `--merge` is present.
2. Bind PR identity, CI, review, feedback, and merge command to the full expected head SHA.
3. Test success, pagination, workflow drift, stale/pending/failing evidence, skips, reruns, review/thread failures, and zero merge calls on refusal.
4. Document the safe operator sequence and retain the intentional Dev ruleset limitation.

## Python CI partition acceptance

The partition moves both complete test files into the required parallel job without reducing root discovery. Keep the Python core timeout at 30 minutes and preserve `-v -x -n auto --dist worksteal --durations=20` in both jobs. Do not raise timeouts or skip cases. Verify exact pytest node-ID equality and disjointness across root discovery, Python core, Python Playbook Safety, Python NetBox, and Python Semaphore; record collection errors as failures. Run focused guard/partition tests, Ruff, and whitespace checks before independent final-head review. Measure on GitHub after the transition: target Python core under 25 minutes and full CI under 40 minutes; report queue delay separately. A two-file timing improvement is unverified until those runs complete. Existing unrelated skips remain visible and must not be presented as zero skips.

## Safe invocation

Start with read-only validation:

```sh
python3 scripts/merge-when-green.py --pr 532 --head aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

After review and explicit authorization for that PR/head, attest feedback is addressed and request one merge:

```sh
python3 scripts/merge-when-green.py --pr 532 --head aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --feedback-addressed --merge
```

Use `--review-receipt PATH` only for a structured Sol/Claude review receipt when the approval is recorded outside GitHub. Do not use `--auto`, `--admin`, or another merge path for a feature-to-Dev PR.

## Validation criteria

| Check | Pass condition |
|---|---|
| PR identity | Open, not draft, same-repository head, base `dev`, exact requested SHA |
| Workflow policy | Base/head are the exact pre-partition-to-reviewed-partition transition or byte-identical reviewed partition; the matching schema is valid and tree mode is `100644` |
| CI | Every expected job is present on the latest PR workflow attempt and has a successful exact-head check run; every latest exact-head commit status context succeeds except optional CodeRabbit; only path-proven Go skips are allowed |
| Review | One independent exact-head approval or valid receipt; no latest outstanding changes request; no unresolved thread |
| Merge | Only explicit `--merge`; revalidate all reads and issue one `--merge --match-head-commit SHA`; read back ambiguous results without retry |
| Refusal | Every gate failure exits nonzero before a merge call |

## Security considerations and limits

The CLI does not change GitHub rulesets or the main-to-Dev sync. Direct use of `gh pr merge` can bypass this operator gate. Reads and the merge are separate API operations, so a concurrently started rerun, review, or base movement can race the final merge despite immediate revalidation and head SHA matching. The gate checks base/head identity before and after evidence collection and compares both validations, but cannot make those API reads atomic with the merge. The workflow transition trusts the independently reviewed candidate blob SHA; future edits need a reviewed merge-gate policy update. An operator-attested receipt and feedback flag are trust inputs, not cryptographic evidence. Pagination caps and truncated trees refuse rather than treating partial responses as complete. An unfamiliar workflow or check requires a reviewed policy update before it can pass.

## Cross-references

- [AGENTS.md](../../AGENTS.md) — feature-to-Dev merge procedure
- [docs/MISTAKES.md §5.16](../../docs/MISTAKES.md#516-merged-on-a-status-rollup-that-still-described-the-previous-head)
- [PRINCIPLES.md](../../PRINCIPLES.md)
- [Testing and CI architecture](../architecture/03-testing-ci-quality.md)

## Revision history

- 2026-10-10: Implemented exact-head operator gate after the PR #531 recurrence.
- 2026-10-11: Permit only a pinned-base byte-exact insertion of the Bash job's canonical 1–30 minute timeout; all other protected workflow edits still refuse.
