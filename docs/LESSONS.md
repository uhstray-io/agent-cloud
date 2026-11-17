# Lessons

Author: Joseph A. Wisneski IV

This file holds the constraints learned from work that was accepted, one per entry, newest last, so the next plan starts where the last one ended. A failure goes in [`docs/MISTAKES.md`](MISTAKES.md), not here. Each entry is written into the pull request that taught it and lands only if that work merges. The file is append-only: an entry that stops holding is superseded by a new entry naming it. A session that plans work reads this file and the Index of `docs/MISTAKES.md` first, and cites the entries that apply in each brief. The entry format is the operator's lessons format (`~/.claude/docs/lessons-format.md`): a `### L-NNN · date · area` heading, then ACCEPTED (what was accepted, with its pull request), DERIVED (the constraint, stated so a planner can apply it without this context) and APPLIES TO (the work or paths it constrains).

### L-001 · 2026-11-17 · tests

- **ACCEPTED:** #1199 (AC-968): the PromQL runner's deadline and cleanup tests.
- **DERIVED:** A test that hands a wall-clock deadline to code which splits it into reserves sizes that deadline from the smallest window the split leaves, never from the total. Where the code has a production budget constant, the test passes that constant, so only the code's own per-call caps bind.
- **APPLIES TO:** tests that pass `deadline=`, `timeout=` or a budget to runner or helper code under `platform/tests/` and `platform/playbooks/files/`.

### L-002 · 2026-11-17 · tests

- **ACCEPTED:** #1199 (AC-968).
- **DERIVED:** A test that fails only under pre-push load is proven fixed by injecting the delay into the code path (a late wake-up, or a slow child start) and showing that the old budget fails and the new one passes. One CPU busy-loop per core did not reproduce the failure, so a run of N passes under such a loop is no evidence either way.
- **APPLIES TO:** fixes for timing-dependent tests.
