## Context

See `proposal.md`, Why. This section records the facts that shape the approach, each with
its source, or marked as unverified with the task that settles it.

**The issue binds four things** (#545, by the network architect): a one-job lifecycle using
`--ephemeral` with the runner's process and filesystem isolated and recreated per job; the
runner group and access list kept as code, and the OpenBao and Semaphore credential path
preserved; proof that a leftover process cannot survive into a later job or read its
workspace, with registration, completion, cleanup and replacement verified on each host;
rollout through reviewed configuration and Semaphore, with a bounded fallback when
registration or replacement fails. It also states that `--ephemeral` alone is not
isolation.

**What holds today** (service `CLAUDE.md`, "What isolation you actually get"): the workspace
wipe, no host administration from a job, and the egress denial. Not enforced: per-job
containerisation, per-job process reaping, the container engine out of a job container's
reach, and setuid programs out of a job's reach. A job runs as `ghrunner` on the host and
can read what that account can read.

**Upstream facts** (actions/runner v2.336.0 source, and GitHub's REST and runner
documentation, read 2026-10-18):

- `POST /orgs/{org}/actions/runners/generate-jitconfig` takes `name`, `runner_group_id`,
  `labels` (1 to 100) and `work_folder` (default `_work`), and returns the new runner's
  record and `encoded_jit_config`. The runner record exists from that moment, offline.
  It needs the App permission the platform already holds (organisation self-hosted
  runners, read and write); the macOS design (`add-macos-github-runner`, D4) relies on the
  same.
- A just-in-time runner is ephemeral: the service de-registers it after one job.
- "An ephemeral self-hosted runner is automatically removed from GitHub if it has not
  connected to GitHub Actions for more than 1 day."
- The runner accepts any command-line argument as `ACTIONS_RUNNER_INPUT_<NAME>`;
  `jitconfig` is in its secret list, so the value is masked, and the variable is removed
  from its environment (`Runner.Listener/CommandSettings.cs`). On start it decodes the
  configuration into `.runner`, `.credentials` and `.credentials_rsaparams` in its root
  directory (`Runner.Listener/Runner.cs`).
- `generate-jitconfig` has no field for `--disableupdate`. A runner records
  `DisableUpdate` at registration and passes it when it asks for messages
  (`ConfigurationManager.cs`, `MessageListener.cs`), so a just-in-time runner is expected
  to accept updates. GitHub's documentation warns that ephemeral runners in containers
  "can lead to repeated software updates". **Unverified here** whether a just-in-time
  runner on the pinned version updates itself when a newer release exists; task 6.4
  measures it.
- A job waiting for a matching runner stays queued for up to 24 hours.

**Platform constraints that bind the choice.** The runner hosts are firewalled away from
OpenBao by their own declaration (`firewall_deny_egress`), so a host cannot fetch a
credential; the App key is read on the orchestrator and never placed on a runner host
(`add-github-actions-runners` D3, spec "Runner identity is established from a stored
organisation credential"). Semaphore is a batch system: it can run a template on a
schedule, not hold a connection open per job. Hosts are one per Proxmox VM with no linked
clones on the storage in use (`add-windows-github-runner` D10).

**Work in flight on the same hosts.** #538 upgrades an installed runner to the declared
version. #609 settles how container jobs reach an engine (rootless podman behind the
account's API socket). `add-windows-github-runner` and `add-macos-github-runner` (#535)
plan the other two members. This change is kept to the Linux pool's lifecycle and
isolation, and composes with both #538 and #609 rather than changing what they change.

## Goals / Non-Goals

**Goals**

- Each job on an ephemeral host is served by a runner that accepts exactly that job.
- No process, file, System V IPC object or keyring entry of one job is present when the
  next job starts, including after a failed or cancelled job and when the next job is from
  another repository.
- The App key stays on the orchestrator; the OpenBao and Semaphore credential path is
  unchanged; the runner group and its access list are untouched.
- A host that cannot prove the above stops taking jobs, visibly, and the fallback is a
  declaration and a deploy.
- Persistent hosts are unaffected until their declaration changes.

**Non-Goals**

- **A VM per job.** Strongest, and what the macOS member does on Apple hardware. On this
  cluster a full clone per job is too slow (the Windows design's measurement). Recorded as
  the end state when linked clones exist.
- **Hiding the host from a job during the job.** A job still runs as `ghrunner` on the host
  and can read what the account can read (D2). This change makes nothing persist between
  jobs; it does not narrow what a job sees while it runs.
- **More than one job at a time per host.** One slot per host, as today. Concurrent slots on
  one host would let one job observe another's processes while both run; that needs
  `hidepid` and per-slot accounts, and is not built.
- **Autoscaling, Windows, macOS.** Separate changes.

## Decisions

### D1: Just-in-time configurations, not `config.sh --ephemeral` with registration tokens

**Decision.** Each runner is started from a just-in-time configuration minted for it. No
registration token reaches the host and `config.sh` is never run on an ephemeral host.

**Why.**

- *The controller fixes the identity.* A just-in-time configuration is bound, when it is
  minted, to one runner name, the runner group's id and the declared labels. A
  registration token lets whoever holds it register a runner with any name and labels in
  any group the App can reach. With configurations, a compromised host can only start the
  runners the orchestrator already named.
- *Lifetime fits a stock.* A registration token expires after an hour, so a stock of them
  would need refilling more often than hourly and would still need `config.sh` per job. A
  configuration's runner lasts a day offline, so a stock refilled every 10 minutes and
  discarded at 20 hours never holds an expired one.
- *Nothing to de-register.* The service removes the runner after its job; the stock
  template deletes records that never started.
- *Ephemeral by construction.* It is the `--ephemeral` behaviour the issue names, with the
  binding done by the server.

**Alternatives considered.**

- *`config.sh --ephemeral` per job, with registration tokens pushed by Semaphore.* Rejected
  for the identity and lifetime reasons above.
- *`run.sh --once` on a persistent registration.* Deprecated upstream (the runner prints the
  warning), and it leaves the persistent credential in the home, readable by every job:
  the weakness this change exists to remove.
- *A runner scale-set listener or Actions Runner Controller.* Creates runners on demand,
  but needs the App key, or a controller service holding it, next to the runners. Rejected
  for the same reason as D3's first alternative; the natural step if the plane moves to
  Kubernetes.

### D2: The isolation boundary is a full reset of the runner account between jobs

**Decision.** Between every two jobs a root supervisor destroys everything the runner
account and its subordinate ids own, verifies it, and rebuilds the home from a root-owned
seed. The runner itself runs as it does today: the account's `actions-runner` user unit,
under the account's user manager, with the same process properties the deploy already
verifies.

The reset, in order (`ephemeral-supervisor.sh`, `do_reset`):

1. Stop the account's user manager and its slice, so every unit of it (the runner,
   podman's API service, any unit or timer a job created) stops through systemd, and stop
   its runtime directory.
2. SIGKILL, from `/proc`, every process whose real, effective, saved or filesystem uid is
   the account's uid or in its `/etc/subuid` ranges, until none is left. Rootless podman
   runs a container's non-root users as subordinate ids, so a container a job left behind
   is found here even when its processes sit in another cgroup.
3. Remove the account's System V shared memory, message queues and semaphores (by owner
   or creator), which outlive their processes.
4. Clear the account's user, user-session and persistent keyrings, from a process of the
   account that the next kill removes. The persistent keyring outlives processes for days.
5. Remove the home, then every path owned by those ids on each local filesystem (`/`,
   `/tmp`, `/var/tmp`, `/dev/shm`, `/run`, `/dev/mqueue` and any other local mount),
   re-checking each path's owner before removing it.
6. Verify: no process, no IPC object, no owned path. Any finding quarantines the host.
7. Recreate the empty home, copy the seed into it as the account, and only then start the
   user manager, which reads its units and their enablement links (podman's socket among
   them) when it starts.

**Why a reset and not a container or VM per job.** The threat #545 names is persistence
across jobs: a process or file of one job reaching the next. The reset removes every
place the account can leave either, and its verification turns "we believe it is clean"
into "it was checked". It changes nothing about what a job runs on, so host jobs (the
`site-config` inventory workflow) see the host as today and container jobs keep whatever
engine path #609 settles: this change wraps that path rather than re-solving it.

**Why the reaper is safe now when the persistent design refused one** ("one that cannot
tell a leftover process from the runner's own is more dangerous than the gap it closes").
The reset runs only when the account should have no process at all: after its runner has
exited, or before the first one starts. Every process of the account is a leftover by
definition, so there is nothing to tell apart. The supervisor runs as root, executes
nothing from the account's files, and its own script is a root-owned copy under
`/usr/local/libexec`, not the checkout.

**Fail-safe deletion.** The file sweep builds its `find` predicate only from validated
numeric ranges, refuses an empty predicate and a range reaching uid 0, never removes a
sweep root or a mount point, removes with `--one-file-system`, and re-reads each path's
owner before removing it, so a fault in the predicate can only fail to remove, never
remove something that is not the account's.

**What still does not hold, stated plainly.**

1. **During a job**, the job can read what `ghrunner` can read on the host and execute the
   setuid programs that remain. Nothing may be placed on a runner host that every
   permitted repository is not entitled to. Unchanged from today.
2. **The kernel is shared.** A local privilege escalation from a job is a host compromise,
   which can read the stock (D3). Patching stays part of the isolation.
3. **The seed is shared by every job.** A defect in it reaches every job until the next
   deploy. It is written only by the deploy, owned by root and not writable by the account.

**Alternatives considered.**

- *The runner itself in a rootless container per job.* The runner's process and files would
  be confined during the job too, closing item 1 above. Rejected for now: container jobs
  would need an engine inside the runner container (rootless podman in rootless podman,
  unproven here) or the account's socket mounted into it (which hands the job the
  account's engine, and so the host files the account can read, so the gain is small),
  and it would change the environment of host jobs. It also overlaps #609, which is still
  choosing the engine path. Recorded as the next step if host-readable data must ever be
  hidden from jobs; this change's supervisor, stock and seed are its foundation.
- *A fresh account (new uid) per job.* Leaves the same files to sweep, adds subordinate-id
  allocation per job, and gains only that a leftover keyring or IPC object would belong to
  a different uid, which the reset already removes.
- *A per-job VM (Proxmox linked clone).* The end state (Non-Goals).

### D3: Configurations are minted on the orchestrator and stocked root-only on the host

**Decision.** A scheduled Semaphore template (`stock-github-runner-jit.yml`, every 10
minutes) reads the App key from OpenBao, mints configurations for each ephemeral host up
to its declared stock (`github_runner_jit_stock`, default 6), and writes each one to
`/var/lib/github-runner/jit/<runner-name>.jit` on the host: mode 0600, owner root, in a 0700
root directory. The supervisor takes the oldest, deletes the file, and passes it to the
runner through a 0600 file in the account's runtime directory that the unit reads with
`EnvironmentFile=` and removes in `ExecStartPost=`; never through argv. A configuration
older than `github_runner_jit_max_age_minutes` (default 1200, under the one-day removal) is
discarded. The same template deletes runner records with the host's `<name>-jit-` prefix
that are ephemeral, offline, idle, neither stocked nor running, and minted more than 30
minutes ago (the stock is read before the prune, and the deploy and the schedule can
overlap, so a younger record may be one the other run has just stocked). The first deploy
to ephemeral retires the host's persistent registration.

**Why.** It keeps the platform's credential rule intact: the App key stays where it is
today, the orchestrator mints, and the host receives single-use credentials over the same
Ansible path the persistent design uses for its registration token. The exposure is
bounded and smaller than today's:

| Who is compromised | Today (persistent) | Ephemeral |
|---|---|---|
| A job (runner account) | the runner's persistent credential: every later job of every permitted repository, from anywhere | its own runner's credential, already bound to the job it is running |
| The host as root | the same persistent credential | the stock: at most `github_runner_jit_stock` jobs, until the records are deleted |
| The orchestrator | the App key | the App key (unchanged) |

**Throughput.** One host serves one job at a time; a stock of 6 refilled every 10 minutes
covers more jobs than a host can run in that time unless jobs take under 100 seconds. An
empty stock queues jobs (they wait, they do not fail) until the next run; the template's
report shows the deficit, and the stock is a declaration.

**Alternatives considered.**

- *The App key on the host, minting per job* (the macOS design, D4). Rejected for Linux.
  There the job runs in a VM and the host is the hypervisor; here the job runs on the same
  kernel as the supervisor. The App's one permission also administers runner groups, so a
  key leaked from a runner host could widen the group's access list and undo the
  public-repository exclusion.
- *A token-vending service in the interior that hosts call per job.* Opens a path from a
  semi-trusted host into the interior that the egress denial exists to close.
- *Minting at deploy time only.* A deploy is not a schedule; the stock would run dry.

### D4: The lifecycle is a per-host declaration with a bounded fallback

**Decision.** `github_runner_lifecycle` (`persistent` by default, or `ephemeral`) in the
host's inventory. `deploy-github-runner.yml` reads it:

- *prepare*, before anything is rendered into the home: on a host that is or was
  ephemeral, stop the supervisor, drain and stop the runner (`deploy.sh quiesce`), and
  reset the account, so the deploy builds the home from nothing. On an ephemeral host it
  also installs the supervisor, its declaration and `keyutils`.
- The unchanged build of the home: runner install (`deploy.sh install`, never `configure`,
  on an ephemeral host), templates, hooks, the account's podman socket, and the deploy's
  own probes. The user unit is installed but neither enabled nor started.
- *activate*, after the probes: snapshot the home into the root-owned seed, reset again,
  stock and retire the persistent registration, start the supervisor, and verify one
  runner started, one Listener converged, and the runner online at the organisation.
- Leaving ephemeral, still in prepare: the reset, then remove the supervisor, its stock, its
  seed and its runner records, all before the persistent runner registers through the
  existing path, so a failed prune stops the play before the host serves under the other
  lifecycle.

**The bounded fallback.** A reset that cannot be verified, or `github_runner_max_start_failures`
(default 3) runners in a row that exit within two minutes without a job, quarantines the
host: the supervisor writes the reason, exits 3, and its unit (`RestartPreventExitStatus=3`)
does not restart it. The host takes no job; the other host and the hosted fallback
(`LINUX_RUNNER`) carry the work; the scheduled template fails every run with the reason
and the supervisor's recent log until a deploy clears it, which it does only after its own
reset verifies clean. A crash of the supervisor restarts it, and a restarted supervisor
waits for a running job before it resets, so a supervisor restart never cancels a job.

**Why a declaration.** One host can be moved first, compared with the other, and moved back
by a reviewed one-line change: the same pattern as every other host property here.

### D5: The seed is the deployed home, and every job starts from a copy of it

**Decision.** After the deploy has built the home from nothing, `ephemeral-supervisor.sh
snapshot` copies it to `<home>-seed`, removes what is per job or per registration (`_work`,
`_diag`, registration files, the image store, caches, the downloaded archives, the user
unit's enablement link), refuses outright if a registration is present, and makes it
root-owned and readable by the account's group. The supervisor copies it into the empty
home, as the account, before each job.

**Why.** Everything #538 and #609 put into the home (the runner version, hooks, `.env`, the
engine configuration and the podman socket enablement) reaches every job without this
change knowing about it, and nothing a job writes reaches the next. The image store is
per job on purpose: a shared store writable by jobs would let one job retag an image the
next job pulls. The cost is a copy of the runner (a few seconds) and an image pull per
container job.

**Constraint this adds for later work on these hosts:** every file the runner account needs
must live in its home (and so in the seed). A file the account owns anywhere else is
removed by the next reset.

### D6: Runner version under the ephemeral lifecycle

**Decision.** The declared `github_runner_version` is what every job's runner starts as,
installed and verified exactly as today. The 30-day rule (GitHub stops queueing jobs to a
runner with updates disabled that is more than 30 days behind) is met the same way as for
persistent hosts: #538 makes a pin bump converge, and the bump cadence of the macOS
design (within 14 days of a release) applies to this pin.

**Why this is enough, and what is unverified.** A just-in-time runner records no
`DisableUpdate`, so if it is behind it may update itself during a job. The update lands in
that job's home and is discarded with it, so the declared version is what the next job
starts as; the cost is a download per job while the pin lags. Task 6.4 measures whether
it happens.

## Risks / Trade-offs

- **The reset is new code that kills and deletes as root.** → It acts only on validated ids
  and paths, re-checks each owner, never crosses a mount, and is covered by behavioural
  tests against fabricated `/proc`, `/etc/subuid` and `/proc/sysvipc` trees. The first
  host's rollout reads its log for every cycle before the second host moves.
- **A quarantine takes a host out of the pool.** → Intended. The other host and the hosted
  fallback carry the work; the scheduled template turns red.
- **An empty stock queues jobs for up to 10 minutes.** → The stock size is declared; the
  template reports held and minted counts per run.
- **Each job pays for a seed copy and, for a container job, an image pull.** → Accepted for
  isolation; measured in rollout (task 6.3).
- **Per-job self-update while the pin lags** (D6). → Measured; #538 and the bump cadence
  keep the pin current.
- **Runner records accumulate if the template stops.** → GitHub removes an offline
  ephemeral runner after a day on its own.
- **The leftover-process proof is live work.** → Static checks and unit tests cover the
  mechanism; the acceptance in tasks phase 6 is the proof, run through Semaphore on one
  host first.

## Migration Plan and merge order

1. **#538 first** (runner upgrade path): it is the 30-day queueing deadline, it is older,
   and it touches `deploy.sh` and the playbook's install step, which this change only calls.
2. **#609 next** (container jobs under rootless podman): it changes what the home contains;
   this change snapshots whatever the home contains, so the order only decides which
   branch rebases, and #609 is the more urgent fix.
3. **This change**, rebased on both. Merging it changes no host (default `persistent`).
4. **site-config:** declare `github_app_client_id` for the runner group, then
   `github_runner_lifecycle: ephemeral` on `gh-runner-02` only. Deploy, run the
   acceptance (tasks phase 6), watch a working day of cycles.
5. **Then `gh-runner-01`.**

`add-windows-github-runner` and `add-macos-github-runner` are unaffected: Windows keeps the
persistent lifecycle (its D10 records the per-job VM as follow-up), and macOS mints
just-in-time configurations on its own host for the reasons in its D4.

## Open Questions

- **Stock size.** 6 per host and every 10 minutes are starting values; task 6.3 measures
  queue time and adjusts the declaration.
- **Self-update of a just-in-time runner while the pin lags** (D6, task 6.4).
- **Whether the `site-config` inventory workflow or any `zerds` host job relies on state
  that survives between jobs** (a warm cache in the home). Every such cache is gone under
  this lifecycle by design; a job that needs one should declare it as a workflow cache.
