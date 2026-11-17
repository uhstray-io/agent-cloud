# Lessons

Author: Joseph A. Wisneski IV

Constraints learned from accepted work, one per entry, newest last, so the next plan starts where the last one ended; failures go in [`docs/MISTAKES.md`](MISTAKES.md). An entry lands in the pull request that taught it and is never rewritten: a later entry supersedes it, and the old one gains a `Superseded by L-NNN` line. The format lives here: a `### L-NNN · date · area` heading, then ACCEPTED (the work and its pull request), DERIVED (the constraint, stated so a planner can apply it without this context) and APPLIES TO (the work or paths it constrains).

### L-001 · 2026-11-17 · tests

- **ACCEPTED:** #1199 (AC-968).
- **DERIVED:** Size a test's wall-clock deadline from the smallest window the code under test splits it into, not from the total; where that code has a production budget constant, pass the constant instead.
- **APPLIES TO:** tests that pass `deadline=`, `timeout=` or a budget to runner or helper code under `platform/tests/` and `platform/playbooks/files/`.

### L-002 · 2026-11-17 · tests

- **ACCEPTED:** #1199 (AC-968).
- **DERIVED:** Prove a fix for a test that fails only under load by injecting the delay into the code path (a late wake-up, a slow child start): the old budget must fail and the new one pass. Passes under a CPU busy-loop are not evidence; one loop per core did not reproduce such a failure.
- **APPLIES TO:** fixes for timing-dependent tests.
