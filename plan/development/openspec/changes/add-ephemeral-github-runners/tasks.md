## 1. Code and static proof (this PR)

- [x] 1.1 `ephemeral-supervisor.sh` (reset, seed, take, run, quarantine, snapshot, status),
      its system unit and declaration templates, and the ephemeral directives of
      `actions-runner.service.j2` rendered only for an ephemeral host.
- [x] 1.2 `github_app_token.py`: `jit-config`, `list-runners`, `prune-runners`, key on
      stdin only, prefix-bounded deletion of ephemeral, offline, idle, unheld runners.
- [x] 1.3 `tasks/github-runner-lifecycle.yml` (prepare and activate), `tasks/github-runner-jit-stock.yml`,
      `stock-github-runner-jit.yml`, the lifecycle branches of `deploy-github-runner.yml`, and
      the scheduled `Stock GitHub Runner JIT (Dev)` template.
- [x] 1.4 Tests: `test_github_runner_ephemeral.bats` (the reset against fabricated trees),
      `test_github_app_token_jit.py`, `test_github_runner_ephemeral_lifecycle.py`; the
      existing runner suites still pass.
- [ ] 1.5 Validation gate: ruff, shellcheck, yamllint, ansible-lint, the pytest and bats
      suites and final-head CI green; spec scenario **"Merging the ephemeral lifecycle
      changes no persistent host"** shown by the byte-identical persistent unit render
      and the persistent-only guards.

## 2. Merge order (before any rollout)

- [ ] 2.1 #538 (runner upgrade path) merged into `dev`.
- [ ] 2.2 #609 (container jobs under rootless podman) merged into `dev`.
- [ ] 2.3 This branch rebased on both; its suites rerun.
- [ ] 2.4 Validation gate: spec scenario **"Merging the ephemeral lifecycle changes no
      persistent host"** confirmed live: a dry run, then a real run, of Deploy GitHub Runner
      (Dev) against both hosts reports no unit change and no restart.

## 3. Declarations (site-config, separate PR)

- [ ] 3.1 Declare `github_app_client_id` (not a secret) in the runner group's vars, for the
      schedule.
- [ ] 3.2 Declare `github_runner_lifecycle: ephemeral` on `gh-runner-02` only.
- [ ] 3.3 Publish the Semaphore catalog (`setup-templates.yml`) so the scheduled template
      exists.
- [ ] 3.4 Validation gate: a dry run of Stock GitHub Runner JIT (Dev) skips `gh-runner-01`
      and lists `gh-runner-02`'s (empty) stock without writing.

## 4. First host

- [ ] 4.1 Dry run, then run, Deploy GitHub Runner (Dev) limited to `gh-runner-02`.
- [ ] 4.2 Confirm the deploy's own checks: reset clean twice, seed snapshot, stock filled,
      supervisor active, one Listener converged, runner `gh-runner-02-jit-*` online, the
      persistent `gh-runner-02` record retired.
- [ ] 4.3 Validation gate: spec scenarios **"The persistent registration is retired"** and
      **"A runner accepts only one job"** (two consecutive jobs show two runner names and the
      first is gone from the group).

## 5. The schedule

- [ ] 5.1 Enable the `Stock GitHub Runner JIT (Dev)` schedule; watch three runs.
- [ ] 5.2 Validation gate: spec scenario **"Runner records that can no longer take a job are
      removed"**, and the stock returns to its declared size after jobs consume it.

## 6. Acceptance (the #545 proof), on `gh-runner-02`

- [ ] 6.1 From a permitted private repository, a job writes a unique marker into its
      workspace and into `/tmp` and `/dev/shm`, starts a uniquely named background process
      and a detached container, and fails. Then a job from a different permitted private
      repository on the same host looks for both markers, the process (`pgrep`, `/proc`) and
      the container, and records the result.
- [ ] 6.2 Repeat 6.1 with the first job cancelled while its process runs.
- [ ] 6.3 Measure the reset-and-seed time per cycle and the queue time with the declared stock.
- [ ] 6.4 Record whether a just-in-time runner on the pinned version updates itself when a
      newer runner release exists (design D6).
- [ ] 6.5 Validation gate: spec scenarios **"A leftover process from a failed job does not
      reach a job of another repository"**, **"A leftover process from a cancelled job does
      not reach a job of another repository"**, **"A container a job left running is
      ended"**, **"A job cannot read the next job's runner credential"** and **"A
      configuration is never passed on a command line"**, with the job logs as evidence.

## 7. Fallback drill

- [ ] 7.1 Confirm the schedule's report: each run's success message for `gh-runner-02` names
      `quarantine=no`, the stock count and the current runner. (The quarantine itself, exit
      3 and no restart by systemd, is covered by the bats suite in 1.4; forcing one on a live
      host needs a fault the platform has no reviewed way to inject, so it is not drilled.)
- [ ] 7.2 Set `gh-runner-02` back to `persistent`, deploy, confirm the supervisor, stock, seed
      and `gh-runner-02-jit-*` records are gone and the persistent runner registers and
      serves; then set it to `ephemeral` again and deploy.
- [ ] 7.3 Validation gate: spec scenario **"A host moves back to the persistent
      lifecycle"** live, and **"A failed reset stops the host"** and **"A deploy clears the
      stop only after a clean reset"** by the 1.4 tests and the deploy's reset gate.

## 8. Second host and close-out

- [ ] 8.1 Declare `github_runner_lifecycle: ephemeral` on `gh-runner-01`; deploy; repeat 4.2.
- [ ] 8.2 Update the service `CLAUDE.md` isolation table from "planned" to measured, and
      `add-github-actions-runners` tasks 12.1 and 12.2 to done with the evidence.
- [ ] 8.3 Validation gate: both hosts ephemeral, spec scenario **"A runner accepts only one
      job"** holds on each.
