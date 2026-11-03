## Why

The Linux runner pool (`gh-runner-01`, `gh-runner-02`) keeps one long-lived runner
registration per host. The six permitted private repositories share it in turn, so the
isolation between their jobs rests on a workspace wipe. The service documentation records
what that does not cover: a process an undeclared job leaves running survives into later
jobs, and per-job process reaping is not enforced. A process left by one repository's job
can still be running when another repository's job checks out private data on the same
host (issue #545). The interim boundary is accepted for the private inventory workflow
(site-config PR #73) and recorded as follow-up in `add-github-actions-runners` tasks 12.1
and 12.2.

There is a second weakness of the same shape. A persistent registration stores the
runner's own credential (`.credentials`, `.credentials_rsaparams`) in the runner account's
home, and every job runs as that account. A job can read the credential and use it, from
anywhere, to take later jobs of any permitted repository. Nothing about the workspace wipe
touches it.

`--ephemeral` alone fixes neither. It ends a registration after one job; it does not stop a
process from outliving the job, and it does not remove files the job left outside its
workspace. The issue asks for both: a one-job runner whose process and filesystem are
isolated and recreated per job.

## What Changes

- **A second runner lifecycle, `ephemeral`, declared per host.** `github_runner_lifecycle:
  ephemeral` in the host's inventory switches it; `persistent` stays the default, so merging
  this changes no host until its declaration does.
- **One job per runner, from just-in-time configurations.** Each job is served by a runner
  started from a just-in-time configuration (`generate-jitconfig`): the service gives that
  runner one job and removes it. The configurations are minted on the orchestrator from
  the App key in OpenBao, exactly where the persistent design mints registration tokens,
  and stocked on the host in a root-only directory. The App key never reaches a runner
  host.
- **The runner account is destroyed and recreated between jobs.** A root supervisor on each
  host, between every two jobs, kills every process of the runner account and of its
  subordinate ids (container processes run as those) wherever it runs, removes every file
  those ids own on the local filesystems (the home included), removes their System V IPC
  objects and clears the account's keyrings, verifies all of it, then copies a root-owned
  seed of the deployed home into the empty home and starts the next runner. Failed and
  cancelled jobs are covered the same way: the reset runs whenever a runner exits.
- **A host that cannot prove a clean reset takes no job.** A failed verification, or
  runners that keep exiting without a job, quarantines the host: the supervisor stops, its
  unit does not restart it, and the scheduled stock template fails until a deploy proves a
  clean reset.
- **A scheduled Semaphore template keeps the stock full** (`Stock GitHub Runner JIT (Dev)`,
  every 10 minutes) and deletes runner records that can no longer take a job. It skips
  persistent hosts.
- **Unchanged:** the runner group and its access list (`manage-github-runner-group.yml`),
  the public-repository exclusion, the App credential path (OpenBao to the orchestrator),
  the unprivileged account with no sudo, the egress denial, the runner unit's process
  properties, and how container jobs reach an engine (that is #609's change).

## Capabilities

### Modified Capabilities

- `github-actions-runners`: adds the ephemeral lifecycle's requirements. The base
  requirement "Each job is isolated from the host and from the preceding job" keeps its
  wording for the persistent lifecycle; on an ephemeral host the stronger requirements in
  this change's spec delta apply.

## Impact

- **New:** `platform/services/github-runner/deployment/ephemeral-supervisor.sh` and its
  system unit and declaration templates; `platform/playbooks/tasks/github-runner-lifecycle.yml`;
  `platform/playbooks/tasks/github-runner-jit-stock.yml`;
  `platform/playbooks/stock-github-runner-jit.yml`; a scheduled Semaphore template.
- **Changed:** `deploy-github-runner.yml` (lifecycle branches; persistent behaviour
  unchanged), `actions-runner.service.j2` (ephemeral directives only when ephemeral; the
  persistent render is byte-identical), `platform/lib/github_app_token.py` (`jit-config`,
  `list-runners`, `prune-runners`).
- **site-config:** a host opts in with `github_runner_lifecycle: ephemeral`; the schedule
  needs `github_app_client_id` declared for the runner group, because a schedule cannot
  answer a survey. Both in a separate site-config PR.
- **Cost:** every job starts from a fresh home, so the runner's files are copied per job
  (a few seconds), and every container job pulls its image (no image store survives a job).

## Rollback Plan

Each stage is independent and sufficient on its own:

1. **One host:** set its `github_runner_lifecycle` back to `persistent` (or remove it) and
   run Deploy GitHub Runner. The deploy stops the supervisor, drains the runner, resets the
   account, removes the supervisor, its stock and its runner records, and registers the
   persistent runner again through the unchanged path.
2. **All Linux CI of a repository:** set its repository variable `LINUX_RUNNER` to a hosted
   runner label, the fallback the `zerds` workflows already read.
3. **Stop new runners at once:** disable the `Stock GitHub Runner JIT (Dev)` schedule. A host
   runs out of stock and takes no further jobs; nothing else changes.
4. **Withdraw stocked runners:** revoking the App stops all minting, as before. A
   configuration already in a stock stays usable until its runner record is deleted, so
   also delete the host's `<name>-jit-*` runner records (the leaving path in stage 1 does
   this); a configuration whose record is gone cannot start a runner.
