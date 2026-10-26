# github-runner — self-hosted GitHub Actions runners

Organisation-scoped CI execution on platform hardware, for workflows that must originate
inside the network perimeter or from its stable address.

**Two hosts, one interchangeable pool.** Verified serving work: a four-shard matrix landed
shards 1 and 3 on `gh-runner-01`, shards 2 and 4 on `gh-runner-02`.

## Using it from a workflow

```yaml
runs-on: [self-hosted, linux, x64, uhstray-lan]
```

`self-hosted`, `Linux` and `X64` are applied by the runner itself; `uhstray-lan` is the
label that actually selects this plane. A job naming a label no runner carries waits
indefinitely rather than failing — that is upstream matching behaviour, not something this
service can change, which is why the label contract matters.

## Which repositories may use it

Exactly seven, all private: `zerds`, `atlas`, `zerds-website`, `weft`,
`scientific-business`, `site-config`, and `skynet`. They are the declared access list of the
`uhstray-selfhosted` runner group (`visibility: selected`,
`allows_public_repositories: false`). `skynet` was added 2026-10-19 (operator decision)
because its GitHub-hosted jobs stopped starting.

**`agent-cloud` is deliberately excluded.** It is public, a fork can propose workflow code,
and these hosts sit inside the perimeter. Upstream's own guidance is to pair self-hosted
runners with private repositories only. Compensating controls — a fork-origin `if:` on
every job, mandatory approval for outside contributors — fail open on one omitted
condition, and nothing in review reliably catches an omitted `if:`. Exclusion is a
property of the configuration instead of a property of everyone's future diligence.

The access list is converged by `manage-github-runner-group.yml`, which **refuses to run**
if any declared repository resolves as public. In a web console the exclusion would hold
only until someone widened it in a hurry, with no review trail.

## What isolation you actually get

Verified from inside a running job, on both hosts:

| Control | Status |
|---|---|
| Workspace destroyed between jobs | **enforced** — a marker written by one job was absent in the next |
| No host administration from a job | **enforced** — `SUDO=no`; runs as `ghrunner`, no sudoers entry |
| Platform interior unreachable | **enforced** — secret store, orchestrator and every hypervisor denied at the network boundary |
| Per-job containerisation | **NOT enforced** — see below |
| Per-job process reaping | **NOT enforced** — the wipe removes files and containers, not a process an undeclared host job left running |
| Container engine out of a job container's reach | **NOT enforced**, by design (since 2026-10-11): `/var/run/docker.sock` in a job container is the runner account's own rootless podman socket, so a job container can do what the account can do. It can never reach root's socket. See "Container jobs under rootless podman" |
| Setuid programs out of a job's reach | **NOT enforced** (since 2026-10-11): the unit runs without `NoNewPrivileges`, because rootless podman needs the setuid `newuidmap`/`newgidmap`. The deploy strips the setuid/setgid bit from helpers a runner never uses (`chfn`, `chsh`, `passwd`, `gpasswd`, `newgrp`, `chage`, `expiry`, `su`, `crontab`, `ssh-keysign`, `polkit-agent-helper-1`); `sudo`, `mount`, `umount`, `fusermount3`, `newuidmap` and `newgidmap` stay executable by jobs |
| Foreign-architecture binaries out of a job's reach | **NOT enforced**, by design (since 2026-10-20): every host carries `qemu-user-static`, which registers user-mode emulators for arm64 and other architectures with the kernel's binfmt_misc. Any job on the pool can execute a foreign-architecture binary through it. The interpreter is an ordinary user-mode program and grants no privilege beyond what the runner account already holds. See "arm64 image builds" |

**Per-job containerisation is not a control here.** The container-hook mechanism manages
containers for a job that *declares* one; it does not place an undeclared job into a
container. A smoke job requesting nothing reported `ISOLATION=host`. Enforcing it for
every job needs an ephemeral runner whose own process and filesystem are containerised and
recreated per job — a lifecycle change, not a setting.

**Practical consequence:** a job can read whatever `ghrunner` can read on that host. Do not
place anything on a runner host that every repository declared in `manage-github-runner-group.yml` is not entitled to.

**arm64 image builds.** The hosts run arm64 binaries through qemu's user-mode emulator, which
is what lets a multi-arch build produce `linux/arm64` on these amd64 hosts.
`deploy-github-runner.yml` installs `qemu-user-static` and proves the handler from
`/proc/sys/fs/binfmt_misc/qemu-aarch64`: the first line must be `enabled`, or the deploy
fails. On Ubuntu 24.04 the package ships `/usr/lib/binfmt.d/qemu-aarch64.conf`, so
`systemd-binfmt.service` registers the handler at boot
(https://www.freedesktop.org/software/systemd/man/latest/binfmt.d.html). The registration
flags are `OPF` (verified 2026-10-20 in Ubuntu noble's qemu packaging, `debian/binfmt-install`:
`:qemu-$fmt:M::magic:mask:/usr/libexec/qemu-binfmt/$fmt-binfmt-P:OP${FIXB}` with `FIXB=F` for
qemu-user-static, https://git.launchpad.net/ubuntu/+source/qemu/plain/debian/binfmt-install?h=ubuntu/noble-updates).
No `C` flag: credentials are not computed from the foreign binary, so emulation grants no setuid
path and no privilege beyond the runner account. `F` pins the interpreter at registration, which
is what lets rootless podman containers use it. The deploy prints the `interpreter` and `flags`
lines on success.
Enforced by `platform/tests/test_runner_arm64_emulation.bats`.

**Why `NoNewPrivileges` is off.** Rootless podman maps a container's users onto the
account's `/etc/subuid` range with `newuidmap`/`newgidmap`. A `uid_map` of more than the
writer's own id is accepted only from a writer holding `CAP_SETUID` in the parent namespace
(user_namespaces(7)), which the helpers get from their setuid bit. no_new_privs makes setuid
bits and file capabilities inert on `execve`, is inherited by every descendant, and cannot
be cleared (prctl(2), `PR_SET_NO_NEW_PRIVS`). With the unit's `NoNewPrivileges=true`, every
container job failed with `newuidmap: write to uid_map failed: Operation not permitted`
(2026-10-11). Turning it off concedes that a job can execute the setuid programs that
remain, so a local privilege-escalation bug in one of them (sudo has had two that needed no
sudoers entry: CVE-2021-3156, CVE-2025-32463) is reachable from repository code. Patching
these hosts promptly is part of the isolation, not hygiene. The same applies to every unit
option that sets no_new_privs implicitly in a user unit (all seccomp-backed ones) or implies
`PrivateUsers=`; the unit comment lists them and a test refuses them.

## Container jobs under rootless podman

A job that declares `container:` runs through the docker variant of the container hooks,
which run `docker` as `ghrunner`. That `docker` is Docker's CLI client (`docker-ce-cli`, a
client binary with no daemon), and `DOCKER_HOST` in the runner's `.env` points it at
`ghrunner`'s rootless podman API socket, so the engine is rootless podman and there is no
Docker engine on the host. Until 2026-10-11 every such job failed in "Initialize
containers", for three reasons that Docker hides and podman does not. The first two are
below; the third, the engine's environment, decided which `docker` the hooks get and is
in its own subsection after them.

**Missing bind sources.** The runner hands the hooks a fixed mount set, and the hooks pass
it to `docker create` unchanged (runner-container-hooks v0.8.1,
`packages/docker/src/dockerCommands/container.ts:56-65`). The set includes `_work/_actions`
and `_work/_tool` (actions/runner v2.336.0,
`src/Runner.Worker/ContainerOperationProvider.cs:301-318`). The runner deletes `_actions`
at the start of every job (`ActionManager.cs:93`), creates it again only to download an
action, and creates the tool cache only when a tool is installed. Docker creates a
missing source; podman refuses it (`statfs ...: no such file or directory`). The
`ACTIONS_RUNNER_HOOK_JOB_STARTED` hook, `job-started.sh`, creates the sources. It runs
after the action download and before "Initialize containers" (`JobExtension.cs:299-326`),
so it must run per job: a directory made at deploy time is gone after the first job.

**The Docker socket.** The runner adds `-v /var/run/docker.sock:/var/run/docker.sock` to
every job container (`src/Runner.Worker/Container/ContainerInfo.cs:57-60`) and every
`docker://` step (`Handlers/ContainerActionHandler.cs:193`). The path is hardcoded; no
runner or hook setting removes it (the hooks read only the Docker CLI variables,
`packages/docker/src/utils.ts:29-66`, and those do not change a mount source).
podman-docker linked the path to root's socket, `/run/podman/podman.sock`, in a directory
`ghrunner` cannot enter, so podman refused the mount with `permission denied`. With
podman-docker removed (below), nothing creates the path at all.

The options, and why this one:

| Option | Result |
|---|---|
| Give `ghrunner` access to root's socket | **Refused.** That is root on the host for every job. |
| Remove the mount by rewriting the hooks' input or the `docker` shim | **Rejected.** A patch on a pinned, digest-verified upstream component, which the design rules out ("the fallback is an existing supported path rather than a patch"). It would also break any job that expects a Docker API in its container. |
| Run the rootful Docker engine instead | **Rejected.** Its socket is root-equivalent. |
| Point the socket path at `ghrunner`'s own rootless podman socket | **Chosen.** The design's own plan for this gap (openspec `add-github-actions-runners`, tasks 2.4: the Docker-compatible API socket for a named unprivileged user). |

**Isolation impact.** A job container that uses the socket controls `ghrunner`'s rootless
podman, so it can do what `ghrunner` can do. A job that declares no container already runs
as `ghrunner` on the host, and a workflow chooses whether to declare one, so the container
was never an isolation boundary here (table above; design D4). No account gains access to
root's socket, and the link target is in `/run/user/<uid>` (mode 0700), so no other
unprivileged account can use the link. **Host-wide cost:** a root process that connects to
`/var/run/docker.sock` now reaches `ghrunner`'s engine, a job-controlled one. So, in
addition to the rule above: do not place on a runner host a root service that uses the
Docker API. Root's own socket is still at `/run/podman/podman.sock`.

**How it is applied.** `tasks/podman-user-docker-socket.yml` (shared, reusable) enables the
account's `podman.socket` user unit and writes `/etc/tmpfiles.d/podman-docker.conf`, which
replaces the package's file of the same name where podman-docker is installed and creates
the link on its own where it is not, so the link survives reboots and package upgrades.
The same socket is the API the hooks' Docker client reaches (next subsection). It reads back the effective tmpfiles.d configuration and the link. The deploy
then runs the hooks' `docker create`, with the full mount set and the deployed
`job-started.sh`, in a `systemd-run --user` transient unit carrying the runner unit's
`NoNewPrivileges` value (the reason is in `docs/MISTAKES.md` 2.29), and calls `_ping` on the
mounted socket path. The runner reads `.env` only at start, so a changed `.env` restarts it,
through the stop described in "Restarting the runner".

### The engine's environment

**The failure.** After the two fixes above were deployed, every container job still failed
with `cannot resolve /github/home: lstat /github: no such file or directory`. The hooks
start `docker` with an environment built from the job alone. For `create`, `exec` and `run`
it is exactly the job container's variables plus the runner's `DOCKER_*` variables; for
every other call (`pull`, `network`, `rm`) it is the `DOCKER_*` variables only
(runner-container-hooks v0.8.1, `packages/docker/src/utils.ts`, `optionsWithDockerEnvs`).
No PATH and no XDG directory of the runner's reaches the `docker` process. The job's
variables include `HOME=/github/home`: the CLI fills `-e HOME` from its own HOME, which is
how the container gets its home. The podman-docker shim is `exec /usr/bin/podman "$@"`, so
podman itself ran in that environment, and rootless podman reads three things from it:

| What | Source | With the hooks' environment |
|---|---|---|
| Configuration directory | `XDG_CONFIG_HOME`, else HOME resolved on the host (podman v4.9.3, `pkg/util/utils_supported.go:81-92`) | HOME is `/github/home`, which exists only in the container: the error above |
| Image store | `XDG_DATA_HOME`, else HOME, else the passwd entry (containers/storage v1.51.0, `pkg/homedir/homedir.go:28-37`, `pkg/unshare/unshare.go:17-23`) | `pull` (no HOME) and `create` (HOME=/github/home) would use two different stores |
| `newuidmap`/`newgidmap` | PATH (podman v4.9.3, `pkg/rootless/rootless_linux.go:104-106`) | No PATH: the mapping fails whenever no podman pause process is running |

The options, and why this one (decided under design D4, issue #609):

| Option | Result |
|---|---|
| Set the XDG variables in the job's environment | **Rejected.** Workflow-side, so it fails open, and the values would also enter the job container. |
| Make `/github/home` exist on the host (a link to the account's home) | **Rejected.** A new path at the host's filesystem root, made for one account's engine, and it would put the image store under a path name that only the hooks' calls use. |
| A runner-only `docker` wrapper first on the runner's PATH that rewrites podman's environment and the job's bare `-e` arguments, then runs `/usr/bin/podman` (PR #556) | **Replaced.** It worked in principle, but it is a patch on the CLI path, which D4 rules out ("the fallback is an existing supported path rather than a patch"), and it had to carry podman's own option tables to know where the image starts. Removed (`docs/MISTAKES.md` 10.29). |
| Run the Docker engine (D4's declared fallback) | **Not needed, and not wanted.** Its socket is root-equivalent, and the owner wants podman as the engine. |
| Docker's CLI client against the account's rootless podman API socket (`DOCKER_HOST`, which the hooks pass through) | **Chosen.** D4's own description of the runtime: "the container hooks drive a Docker-compatible API". The client only sends API calls; the account's `podman.socket` user unit answers them with the account's own environment, so the job's HOME never reaches podman, while the client still fills `-e HOME` from it. Nothing upstream is changed. |

**How it is applied.** `install-podman.yml` with `podman_docker_api_client: true` removes
`podman-docker`, adds Docker's apt repository (`tasks/add-docker-apt-repo.yml`, shared with
`install-docker.yml`) and installs only `docker-ce-cli`, pinned by
`podman_docker_api_client_version`, without its Recommends (the buildx and compose plugins
drive a Docker engine). It fails if `/usr/bin/docker` is not that package at that version,
or if any Docker engine package is installed. The runner's `.env` sets
`DOCKER_HOST=unix:///run/user/<uid>/podman/podman.sock` and no PATH, so the runner, its hooks
and every host step use `/usr/bin/docker` and the same engine and image store.
`tasks/podman-user-docker-socket.yml` enables that socket for `ghrunner` (lingering starts it
at boot) and links the Docker socket path to it. The deploy refuses a host where
`/usr/bin/docker` is not Docker's client, removes the #556 wrapper (`_bin/`) where an earlier
deploy left it, and restarts the runner because `.env` changed. Its mount probe resolves
`docker` from the user manager's PATH (the runner's), requires Docker's client, reads
`DOCKER_HOST` from the deployed `.env` and requires the account's socket, imports its image
under `env -i DOCKER_HOST=...` (as the hooks' `pull` runs), creates the container under
`env -i HOME=/github/home GITHUB_ACTIONS=true CI=true DOCKER_HOST=...` with `-e HOME`, and
requires the container to carry `HOME=/github/home` (`docs/MISTAKES.md` 10.25). It runs no
`podman system migrate`, which stops running containers and so could stop a job.

**Proving it off the hosts.** `deployment/hooks-smoke/run.sh` runs the real hooks (digest
verified against the release) through one job's whole cycle in a local container with the
hosts' podman 4.9.3 and the pinned client, twice: through a podman-docker style shim, where
it must fail with `cannot resolve /github/home`, and through the client, where
`prepare_job`, a `run_script_step` and `cleanup_job` must all pass. It needs only podman on
a workstation and touches no host (D4, "Runtime feasibility, RESOLVED").

**Verify on the live hosts** (the runbook; Semaphore, never SSH):

1. Merge this repository's change into `dev`, and the site-config change that declares
   `podman_docker_api_client: true` and `podman_docker_api_client_version` (and drops
   `podman_docker_cli`) for `github_runner_svc`. Sync the inventory into Semaphore
   (`platform/semaphore/sync-inventory.yml`).
2. Run **Install Podman (Dev)** with `target_service=github_runner_svc`. It removes
   `podman-docker` and installs the client; its last assert names the package and version.
3. Run **Deploy GitHub Runner (Dev)** with `service_branch=dev`,
   `target_service=github_runner_svc`. Expect the "Require Docker's CLI client" assert, the
   removal of `_bin`, a quiesce and start (`.env` changed), one Listener after `.env`, and the
   mount probe lines `client=docker-ce-cli`, `docker_host=...`, `create=ok`, `home=job`,
   `ping=OK`.
4. Run a job that declares `container:` on each runner: site-config's **Runner container
   smoke** workflow (`workflow_dispatch`), or re-run a failed zerds or weft container job.
   "Initialize containers" must pass and the steps must run in the image.
5. Close tasks 1.5 in the openspec change with the run links.

## Restarting the runner

**The failure.** On 2026-10-12 both hosts ran three `Runner.Listener` processes, all in the
unit's cgroup. The one that held the session, and so took every job, had started at boot
on 2026-09-19; the other two came from the deploy of that day. The `.env` on disk was
correct, but the serving Listener had read it at boot, so container jobs still resolved
`/usr/bin/docker` and failed with `cannot resolve /github/home`.

**Why.** The unit starts `run.sh`, which starts `run-helper.sh`, which starts
`bin/Runner.Listener`; the Listener holds the session and starts a `Runner.Worker` per job.
The unit had `KillMode=process`, so a stop signalled `run.sh` alone. Neither script passes a
signal on by default (actions/runner v2.336.0, `src/Misc/layoutroot/run.sh`, function
`run`), so `run.sh` died and the Listener kept running. Every `state: restarted` since the
unit was introduced stacked a new Listener beside the old one, which kept the session: a
new Listener retries a session conflict for up to four minutes and then exits
(`src/Runner.Listener/MessageListener.cs`), and `Restart=always` starts another. No unit or `.env` change after the boot-time start reached the
process that runs jobs, `NoNewPrivileges=false` included. The deploy did not see it: it read
`NoNewPrivs` from the unit's `MainPID`, which is the fresh `run.sh`, not the Listener
(`docs/MISTAKES.md` 10.27).

**What a signal does to the Listener** (actions/runner v2.336.0):

| Signal | Effect |
|---|---|
| SIGINT | Graceful stop (`src/Runner.Listener/Runner.cs`, `CtrlCHandler`): no new jobs, a running job is cancelled through the Worker and reported as cancelled, and the session is deleted, so the next Listener is not refused |
| SIGTERM | The same shutdown, but the Listener waits at most 30 s for it (`Runner_Unloading`, `ExitOnUnloadTimeout` in `src/Runner.Common/Constants.cs`) |
| SIGKILL | The job is lost and the session stays until the service expires it |

Any stop cancels a running job (`src/Runner.Listener/JobDispatcher.cs`, `ShutdownAsync`).
No signal lets a job finish; only waiting for the runner to be idle does.

The options, and why this one:

| Option | Result |
|---|---|
| GitHub's `svc.sh` shape: `runsvc.sh` with `KillMode=process` | **Rejected.** `runsvc.sh` turns TERM into SIGINT for its node host, which sends the Listener SIGINT and SIGKILL 30 s later (`src/Misc/layoutbin/RunnerService.js`), cutting off a Worker that is still cancelling its job. `KillMode=process` still leaves anything else in the cgroup running after the stop. It also replaces the process chain with node and `--startuptype service`. |
| `KillMode=control-group` | **Rejected.** Every process in the cgroup gets the stop signal at once, job processes included. With SIGTERM the Listener gets only its 30 s; leftovers that ignore the signal hold the stop for the whole `TimeoutStopSec`. |
| `KillMode=mixed` alone | **Rejected.** `run.sh` dies on SIGTERM without passing it on, then systemd sends SIGKILL to the Listener and Worker: no job is ever reported cancelled. |
| `KillMode=mixed` with `RUNNER_MANUALLY_TRAP_SIG=1` | **Chosen.** Upstream's own switch in `run.sh` (`runWithManualTrap`): it runs `run-helper.sh` as its own process group and turns TERM into SIGINT for that group, so the Listener gets its graceful stop. When `run.sh` exits, systemd sends SIGKILL to whatever is left in the cgroup (systemd.kill(5)), so a stop leaves nothing running. `TimeoutStopSec=5min` bounds it. |

The SIGINT reaches the whole process group, so a job's own step processes get it at the
same time as the Listener, as they would from Ctrl-C on an interactive `./run.sh`. The job
is cancelled either way. `RUNNER_MANUALLY_TRAP_SIG` is visible to jobs and means nothing to
anything but `run.sh`. `run.sh` exits 143 after the trapped TERM, so the unit declares
`SuccessExitStatus=143`.

**How the deploy applies it.** `deploy.sh procs` reads, from `/proc`, the unit's main
process and every `Runner.Listener` and `Runner.Worker` of the account, and prints one
verdict per line. The deploy restarts the runner when the unit or `.env` changed, or when
those lines are not exactly: one Listener, in the unit's cgroup, started after `.env` was
last written, no `NoNewPrivs` and no private user namespace on the main process, the
Listener or any Worker, and a `run.sh` that honours the trap variable. A restart is
`deploy.sh quiesce` and then a start, never `state: restarted`:

1. Wait up to `github_runner_drain_seconds` (default 600) for no Worker to run, so a job in
   flight can finish. On a busy runner the wait can end with a job still running; it is
   then cancelled and reported.
2. `systemctl --user stop`, before the daemon reload, so the stop uses the configuration
   the running unit was started with. On a host still on the `KillMode=process` unit, a
   reload first would make this stop send SIGKILL to the Listener that holds the job.
3. SIGINT to every Listener of the account, read from `/proc`, in the unit's cgroup or
   not (the ones `KillMode=process` left behind), and wait up to 300 s for them. The
   account exists only for this runner, so any Listener of it uses this registration.
4. SIGKILL to whatever is still in the cgroup and to any Listener that survived; the deploy
   fails unless both are gone. If no cgroup is found, only the Listeners are handled.

After the start, the deploy reads `deploy.sh procs` again and fails unless every line holds.
A dry run reads the processes and reports the restart decision; it stops and starts nothing,
so it does not judge the result.

## Credential chain

Each step is shorter-lived than the last, and the runner host never holds any of it:

```
App private key (OpenBao: secret/services/github-runner:app_private_key)
  -> RS256 assertion        (<=9 min, signed on the controller)
    -> installation token   (~1 hour)
      -> registration token (1 hour, single use, one per host)
```

The App's **client id** is the JWT issuer (upstream's recommended value; the app id also
works). Its OAuth **client secret plays no part** and is used nowhere.

Both playbooks read that key from OpenBao and nowhere else — not from a Semaphore
environment secret, because two channels for one private key means two places to rotate
and two places to leak.

The installation grant uses a dedicated classic `ghp_` PAT at
`secret/services/github-runner:installation_admin_token`. Create it with the `repo` scope;
its authenticated user must have admin permission on `uhstray-io/site-config`. The helper
accepts only a dedicated classic `ghp_` PAT with the `repo` scope, refuses `gho_` CLI OAuth
tokens, and requires the PAT's user to have admin permission on `uhstray-io/site-config`
before its additive PUT. It then confirms the repository appears through a fresh
installation token.
This dedicated credential keeps the shared `secret/services/github:pat` unchanged; that
PAT also serves GHCR. After the reviewed code merges into Dev, seed the dedicated PAT via
the governed `seed-openbao-key.yml` workflow using an encrypted isolated Semaphore
environment, never a survey value or log.

Minting runs on the orchestrator, not the host, because the host is firewalled away from
OpenBao by its own declaration — it could not fetch its own secret even holding a
credential. That is the design. Signing is `platform/lib/github_app_token.py`: standard
library plus `cryptography`, invoked with `ansible_playbook_python`. It deliberately shells
out to nothing — the first version used `openssl` and `jq`, and the orchestrator image has
neither.

A registration token is single use, so it is minted **per host**. Nothing here is
`run_once`.

## Playbooks

| Playbook | Purpose |
|---|---|
| `deploy-github-runner.yml` | Install or upgrade to the pinned versions, register, start. Idempotent: a re-run leaves an existing registration intact, and an unchanged pin changes nothing. |
| `manage-github-runner-group.yml` | Converge the group's repository access list. Read-only unless `-e dry_run=false`. |
| `grant-github-runner-app-repository.yml` | Add only private `uhstray-io/site-config` to the runner App installation. Directly Dev-bound in Semaphore; reads the dedicated repo-scoped administration token and App key from `secret/services/github-runner`, previews in check mode, and verifies a grant with a fresh App token. |
| `netbox-allocate-ip.yml` | Ask the address authority for free addresses. Read-only unless `-e reserve=true`. |

The installation grant is separate from runner-group convergence. Run its reviewed Dev
template first; a successful App installation grant allows the existing group playbook to
resolve `site-config`. The grant is additive and never removes access. Check mode and
`--tags verify` perform no repository-selection write.

Host lifecycle reuses the platform's existing playbooks unchanged: `provision-vm.yml`,
`verify-host-access.yml`, `distribute-ssh-keys.yml`, `harden-ssh.yml`,
`apply-firewall.yml`, `install-podman.yml` (with `podman_docker_api_client` declared).

## Upgrading the runner

The runners are registered with `--disableupdate`, so the declared version is the running
version and two hosts never drift apart. The cost: GitHub requires a runner with updates
disabled to be updated "within 30 days of a new version being made available", and "if you
do not perform a software update within 30 days, the GitHub Actions service will not queue
jobs to your runner" ([Self-hosted runners reference, "Runner software updates on
self-hosted runners"](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)).
Watch [actions/runner releases](https://github.com/actions/runner/releases) and move the pin
within that window.

An upgrade is a declaration change: raise `github_runner_version` (or
`github_runner_hooks_version`) in site-config, merge, then run the deploy. `deploy.sh install`
converges the host on the declared versions:

- **Same version: no-op.** The installed version is read from the binary
  (`bin/Runner.Listener --version`), never from a record of the last deploy. Nothing is
  downloaded or stopped.
- **Different version: swap.** The new tarball is fetched and its digest verified, then
  extracted into a staging directory and checked: it must report the declared version and
  must not ship any host-owned name (`.runner`, `.credentials`, `.credentials_rsaparams`,
  `.env`, `.path`, `config/`, `_hooks/`, `_work/`, `_diag/`, `job-cleanup.sh`, `.config/`).
  Only then is the service stopped, and the release's own entries (`bin/`, `externals/`,
  `config.sh`, `env.sh`, `run.sh`, `safe_sleep.sh`, the run-helper templates) are moved
  aside and replaced. The registration is never touched, so the host stays registered under
  the same name. The service is started again, the binary is re-read, and only then is
  "upgraded runner A -> B" logged, with the version the binary reports. A failure mid-swap
  restores the previous files.
- **Lower version: refused.** A declared version below the installed one is usually a stale
  declaration, not an intent, and silently acting on it could push a host behind the 30-day
  window. A real rollback passes `-e github_runner_allow_downgrade=true`.
- **Container hooks** follow the same rules. Their archive carries no readable version, so
  `deploy.sh` writes `_hooks/.hooks-version` beside the files it places; a host without the
  marker reinstalls the declared hooks once (they are stateless).

**An upgrade interrupts an in-flight job.** The unit uses `KillMode=mixed`, so stopping it
ends every process in its cgroup. `deploy.sh` also sends SIGINT to any `Runner.Listener` still
running, as one left by an older `KillMode=process` unit would be, and waits up to
`RUNNER_STOP_WAIT` seconds (default 60). If one survives, the swap is refused and the old
install stays as it was. Run upgrades while the runners are idle.

After the deploy, the playbook reads the binary's version and the hooks marker back and
fails unless both match the pin. The final report states those read-back values.

## Declaration

`github_runner_svc` in the private site-config inventory. **The declaration is the
allocation record**, including the address — deliberately, not as a shortcut. The IPAM
system's discovery pipeline has written nothing since 2026-04-23
(`plan/development/04-netbox-discovery.md`), and a hand-written entry inside a stale store
gets trusted more than it deserves. Once discovery is repaired it registers these hosts
itself, and IPAM becomes a cross-check rather than a second record to maintain.

**Where the code lives on the host.** `monorepo_deploy_path` is the path *within* the
repo — `platform/services/github-runner/deployment` — exactly as every other service uses
it and as `platform/playbooks/README.md` defines it. The repo root on the target comes
from `local_monorepo_dir`, defaulting to `/opt/agent-cloud`. That default is under `/opt`
rather than the peer services' `~ansible_user` deliberately: `deploy.sh` executes as the
unprivileged runner account, which cannot be relied on to read another user's home
directory. Reading `monorepo_deploy_path` as the root instead — an earlier bug here —
yields `<deploy-path>/<deploy-path>/deploy.sh` and `rc=127`.

Key vars: pinned `github_runner_version` / `github_runner_hooks_version` (an upgrade is a
declaration change and a review, never something that happens because time passed; see
"Upgrading the runner" above),
`github_runner_labels`, `github_runner_group`, `github_runner_account`,
`podman_docker_api_client: true` with a pinned `podman_docker_api_client_version` (Docker's
CLI client against the account's podman API; never `podman_docker_cli`, which the hooks
cannot use), `firewall_deny_egress`, and optionally
`github_runner_drain_seconds` (how long a restart waits for a running job, default 600).

`firewall_deny_egress` entries must name **single hosts**. A denial broader than
`firewall_ssh_cidrs` would cut the host off the management network, and the guard rejects
it — note that every legitimate target sits *inside* that prefix, so a guard phrased as
"reject anything within an SSH CIDR" would reject every valid entry.

## Gotchas that cost real time

- **`acl` is required.** Every step runs as the unprivileged account, and Ansible needs
  `setfacl` to hand temp files to a become target that is neither root nor the connecting
  user. Without it the register step dies in the become plumbing — raised outside the
  normal result path, so `failed_when: false` does not catch it, and under `no_log` it
  appears as a censored fatal with no cause.
- **`systemctl --user` needs `XDG_RUNTIME_DIR`.** sudo does not set it, so service checks
  fail with `Failed to connect to bus: No medium found` even while the runner is online.
  The uid is looked up, never assumed.
- **Container jobs need `uidmap`.** A job that declares `container:` runs through the
  container hooks, which call `docker` as `ghrunner` against its rootless podman.
  That needs `newuidmap` from the `uidmap` package and a `/etc/subuid` range for the
  account. Both hosts ran without `uidmap` until 2026-10-10: every container job failed
  with "command required for rootless mode with multiple IDs", and the job cleanup's
  container and volume prune failed with it. `install-podman.yml` now installs it.
- **A check run through `become_user` is not a job.** It is a sudo session from the SSH
  login, outside the runner unit, so it inherits none of the unit's properties. On
  2026-10-11 the deploy's `podman unshare` passed that way while every job, a descendant
  of a `NoNewPrivileges=true` unit, failed in `newuidmap`. `deploy-github-runner.yml` now
  reads `NoNewPrivs` and the user namespace from `/proc` for the unit's main process, its
  Listener and any Worker (the main process alone is `run.sh`, see "Restarting the
  runner"), runs
  the podman mapping check in a `systemd-run --user` transient unit carrying the unit's own
  `NoNewPrivileges` value, and requires at least 65536 ids in both `/etc/subuid` and
  `/etc/subgid` for the account.
- **A uid-map check does not prove a container job.** The 2.29 checks passed while every
  `container:` job still failed on its mounts (above). The deploy now creates a container
  with the hooks' own mount set, from inside the user manager (`docs/MISTAKES.md` 10.24).
- **A hook-shaped probe must copy the hooks' environment too.** The 10.24 probe used the
  hooks' arguments with the account's own environment, passed, and every container job
  still failed: the hooks give `docker` only the job's variables, with
  `HOME=/github/home`. The probe now runs `docker` under `env -i` with the hooks' HOME and
  `DOCKER_HOST` (`docs/MISTAKES.md` 10.25).
- **The hooks need Docker's CLI client, not podman as a CLI.** The podman-docker shim runs
  podman in the job's environment and fails on `HOME=/github/home`; Docker's client against
  the account's podman API does not (`docs/MISTAKES.md` 10.29). Declare
  `podman_docker_api_client`, not `podman_docker_cli`, for runner hosts.
- **The root disk is small until grown.** Both hosts came up with a 9.8 GiB root LV on a
  200 G disk, 878 MiB free, which no Rust build fits in. Run Grow GitHub Runner Root
  (`grow-github-runner-root.yml`) after provisioning; its dry run shows each host's chain.
- **A fresh VM holds the dpkg lock.** cloud-init is still doing its own apt work;
  `tasks/wait-for-apt.yml` waits for it.
- **`playbook_dir` is the controller's checkout.** Correct for `template:` (read on the
  controller), wrong for anything executed on the host — the two are not interchangeable.

## Known limitations

1. Per-job containerisation is not enforced (above), and neither is process reaping: a
   job may leave a process running after it ends. Both have the same fix — an ephemeral
   runner whose process and filesystem are containerised per job. A best-effort reaper is
   deliberately not shipped: one that cannot tell a leftover process from the runner's own
   is more dangerous than the gap it closes. A stop of the unit is not a per-job reaper:
   it ends every process in the unit's cgroup, a leftover of a finished job included,
   because none of them belongs to a running runner any more (`KillMode=mixed`).
2. Both hosts are on one hypervisor node. The cluster has no shared image storage and the
   only Ubuntu template is node-local, so Proxmox refuses a cross-node clone. Node
   separation needs a second template.
3. The pool is fixed at two. Growth is a declaration plus a run; there is no autoscaling.
