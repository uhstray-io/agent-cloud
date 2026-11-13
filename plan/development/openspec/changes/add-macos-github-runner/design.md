## Context

See `proposal.md`, Why. This section records the facts that shape the approach, each
verified against the source named beside it or marked as an assumption with the task that
settles it.

**What the `zerds` macOS jobs need** (read from `zerds` `main`, `.github/workflows/`,
2026-10-11). Five jobs run on `macos-15` (Apple Silicon): `ci.yml` "Lint + test" (45
minute timeout, every pull request), `ci.yml` "Release feature set" (90 minutes, the tag
gate), `ci.yml` "Heavy feature check" (120 minutes, weekly and on demand), `warm-cache.yml`
`macos`, and `release.yml` `macos` (120 minutes). Their dependencies:

| Need | Where it comes from | Note for an ephemeral VM |
|---|---|---|
| Xcode, latest stable, with the macOS 26 SDK | `maxim-lobanov/setup-xcode` `latest-stable`; the comment on `runs-on` says the ScreenCaptureKit Swift bridge needs the Xcode 26 SDK and `macos-14` tops out at Xcode 16 | the action runs `sudo xcode-select`; with one Xcode in the image it selects that one |
| Command Line Tools at `/Library/Developer/CommandLineTools` | `.cargo/config.toml` links `-L/Library/Developer/CommandLineTools/usr/lib/swift/macosx` for `aarch64-apple-darwin` | must exist in the image, not only inside Xcode |
| rustup | `rustup toolchain install` resolves the pinned nightly from `rust-toolchain.toml` | baked; the nightly itself is fetched per job |
| CMake | `brew install cmake` in three jobs | baked, so the step is a fast no-op; Homebrew itself is baked |
| `cargo-binstall`, `dx` | installed by the job | per job, as on hosted |
| `codesign`, `notarytool`, `security`, `hdiutil` | release signing, notarisation, DMG | part of macOS and Xcode |
| A temporary keychain made the default | release "Import signing certificate": `security create-keychain`, `security default-keychain -s` | changes machine state: acceptable only in a VM that is discarded |
| Free disk | release "Free up disk space" and "Free disk space before bundling": `sudo rm -rf` of simulator runtimes, caches and every non-active `/Applications/Xcode*.app` | administrative and destructive: acceptable only in a VM that is discarded |
| `MACOSX_DEPLOYMENT_TARGET=13.0` | job env and `.cargo/config.toml` | none |

**Machine administration the jobs do today**: `sudo xcode-select` (through the action),
`sudo rm -rf` of Xcode installs and simulator data, and replacing the default keychain.
None of it can be made host configuration the way the Windows change does it, because
these are the release workflow's own steps against its own machine. The right answer for
macOS is therefore a machine per job (D2), not a non-administrative account.

**Hosted runner size** (GitHub's published reference, read 2026-10-11): `macos-15` for a
private repository is **3 vCPU (M1), 7 GB RAM, 14 GB SSD**.

**Usage** (organisation billing API, `zerds` only): macOS minutes 987 (August), 3,039
(September), 3,328 (October 1 to 11); gross 61.19, 188.41 and 206.33 US dollars at 0.062
dollars a minute; net charge zero each month because the plan's included allowance
absorbed it.

**Licensing facts.**

- **macOS.** Apple's macOS software licence permits running up to **two additional copies
  or instances** of macOS in virtual operating system environments **on each Apple-branded
  computer** you own or control that already runs macOS, for software development, testing
  during development, macOS Server, or personal non-commercial use. It excludes service
  bureau and time-sharing use. Continuous integration of the organisation's own product is
  development and testing. Apple's virtualisation framework enforces the same limit: a
  third concurrent macOS guest on one host does not start.
- **Tart** (Cirrus Labs) is under a Fair Source licence: royalty-free on personal
  computers, and free for an organisation **up to 100 CPU cores** across its Tart server
  installations; above that a paid tier applies (Gold is 12,000 US dollars a year for up to
  500 cores). One Mac mini is at most 18 cores, so this plan is far inside the free tier,
  and the core count is recorded in the declaration so that growth past it is visible
  before it happens (D9).
- **Xcode** is downloaded by an Apple ID holder who accepts Apple's Xcode licence. That
  acceptance is a human act, which shapes the image strategy (D5).

**Upstream mechanics this design relies on.**

- **Just-in-time runners.** `POST /orgs/{org}/actions/runners/generate-jitconfig` returns an
  encoded configuration for a runner with a given name, runner group id and labels. A
  runner started with it accepts **one job** and is then removed by GitHub. It needs the
  same App permission the platform already holds (organisation self-hosted runners: read
  and write). No registration token, no `config.sh`, nothing to de-register.
- **Runner version enforcement.** A runner with automatic update disabled must be updated
  within 30 days of a new release, or the service stops queueing jobs to it, silently. This
  is enforced from 2026-09-25. It applies to the Linux and Windows members too (D5 here
  carries the cadence for the image; the same cadence must apply to the version pinned in
  the inventory for the other two).
- **Softnet** is Tart's optional VM network: each VM may send only from its assigned
  address, VMs cannot reach each other, the host is not reachable from the VM, and by
  default the VM may reach globally routable addresses and the gateway only, so private
  address ranges, the platform network included, are unreachable. It needs a set-uid
  helper or passwordless sudo on the host to create its interfaces.
- **Headless operation.** Since macOS 15 the virtualisation framework requires the login
  keychain of the user running the VM to exist and be unlocked; Tart documents unlocking it
  programmatically for headless hosts. How this behaves across an unattended reboot with
  FileVault on is **unverified** and is the first spike (task 1.2).

**Platform facts.** The plane's runner group (`uhstray-selfhosted`, access list declared in
`github_runner_repos`, private repositories only, public repository excluded) and its converging playbook; the
App credential at `secret/services/github-runner`; the App-token helper
`platform/lib/github_app_token.py` (standard library plus `cryptography`); the
`firewall_deny_egress` declaration; the verify-before-harden rule. There is no Apple
hardware in the estate today and no OCI image registry.

## Goals / Non-Goals

**Goals**

- Every `zerds` macOS job can run on organisation hardware, each in a VM created for it and
  destroyed after it, with the hosted runner one variable away.
- The host, its image and its supervisor are produced by orchestrator-launchable,
  re-runnable automation from a declaration. The only manual steps are the ones Apple's
  hardware makes unavoidable, and each is written down as an exception with its reason.
- Stronger job isolation than the Linux or Windows members achieve, because a VM per job is
  cheap on Apple hardware.
- A faster pull-request gate and release than the hosted 3 vCPU runner, measured.

**Non-Goals**

- **macOS on the Proxmox nodes.** Ruled out by Apple's licence (D1, rejected alternatives).
- **A personal workstation as a runner.** Ruled out by the owner.
- **Intel macOS.** No `zerds` job targets it.
- **iOS simulators or device testing.** No job needs them; the image carries none.
- **A second Mac or an image registry.** Designed not to block either, not built.
- **Editing `zerds` here.** The workflow edits are a cross-repository task.

## Decisions

### D1: A dedicated Mac mini (M5 Pro, 64 GB, 2 TB) on the platform network

**Decision.** Buy one **Mac mini with the M5 Pro chip, 64 GB unified memory, and a 2 TB
SSD**, wired to the platform network, on a UPS if one is available, with a declared static
address. It is a build host, never used interactively.

**Why this machine.** The binding constraint is that the host runs two macOS VMs at once,
each doing a large Rust plus C++ build:

- **Memory.** Each VM gets 24 GB, which is more than three times the hosted runner's 7 GB
  and enough for parallel rustc and clang without paging; two VMs plus about 16 GB for the
  host is 64 GB. The base Mac mini tops out at 32 GB, which would leave each VM under
  12 GB. The M5 Pro offers 24, 48 or 64 GB.
- **Cores.** Each VM gets just under half the host's cores (for example 7 of 18, or 6 of
  15), against the hosted runner's 3 M1 cores, on a much newer generation.
- **Disk.** The golden image is roughly 70 to 100 GB (macOS, Xcode, the Command Line Tools,
  Homebrew and toolchains). Clones are copy-on-write, so a running VM costs only what its
  job writes, but a release writes tens of gigabytes of target directory, and an image
  rollout keeps the previous image until the new one is proven. Two images, two busy
  clones, the Xcode archive and the host come to roughly 400 to 500 GB. The internal SSD
  **cannot be upgraded later**, and external storage for VM images is slower and one more
  thing to fail, so 2 TB is the size that does not become the next constraint.

**Cost.** The M5 Pro Mac mini starts at 1,699 US dollars (24 GB, 512 GB). The memory and
storage upgrades are priced on Apple's configurator; if they follow the M4 Pro generation's
steps, 64 GB and 2 TB come to roughly 2,900 US dollars. Confirm at purchase. Against the
October run rate (about 580 dollars a month of hosted value) that is about five months of
gross value. Today's net bill is zero, so this is not a saving yet: it is the price of
not depending on the included allowance, of a faster gate, and of control.

**Alternatives considered.**

- *A macOS VM on the Proxmox x86 nodes.* **Rejected, plainly: Apple's licence permits macOS
  virtual machines only on Apple-branded hardware**, and running macOS on x86 hardware also
  needs unsupported boot loaders. Not a trade-off; not available.
- *A personal workstation.* Rejected by the owner, and rightly: a machine someone uses is a
  machine whose state the builds depend on and whose files the jobs can reach.
- *The base Mac mini (M6, up to 32 GB).* Cheaper, rejected on memory: two VMs at under 12 GB
  each would be the hosted runner's problem again.
- *Mac Studio.* More cores and memory than two VMs can use, at about twice the price, and
  still limited to two macOS VMs by the licence.
- *A used M1 or M2 Mac mini.* Cheap, but 16 or 24 GB (32 GB on M2 Pro) and a smaller SSD:
  one VM at a reasonable size, not two.
- *A rented dedicated Mac in a hosting provider.* No capital cost and no physical
  placement, but a recurring cost of the same order as the hosted value, a host outside the
  platform's network and controls, and a third party with physical access. Recorded as
  the fallback if buying hardware is not acceptable.
- *Hosted runners only.* The status quo, and the interim fallback (D10).

### D2: One fresh VM per job, at most two at a time

**Decision.** The host runs two **slots**. Each slot loops: clone the current golden image
to a uniquely named VM, boot it headless on Softnet, start the runner inside it with a
just-in-time configuration minted for that VM, wait for the runner to exit after its one
job, then stop and delete the VM. A slot keeps one VM booted and registered while idle, so
a queued job starts in seconds rather than waiting for a boot.

**Teardown is bounded.** Waiting for the runner is not open-ended: once a VM has taken a
job, the slot allows it at most the declared `macos_runner_max_job_minutes` (default 210,
the longest workflow timeout plus 30 minutes for the runner's own cancellation grace).
At that deadline, or when the runner has exited but the guest still runs any process of
the job account, the slot force-stops the VM (`tart stop --timeout 0`) and deletes it.
The VM is the job's whole machine, so destroying it ends every process the job started,
wedged or not. The deadline bounds the **job**; it does not bound the slot. A VM that
cannot be deleted marks its slot failed: the slot clones nothing more, retries the
deletion every 5 minutes, and recovers by itself once the deletion succeeds and no VM of
the slot remains. Verification reports a failed slot and how long it has been failed; one
failed for longer than an hour is an operator's to clear (the deploy's slot-recovery step,
which force-deletes the slot's VMs and restarts it). The other slot keeps serving
throughout.

**Why.** It is the strongest isolation available anywhere in the fleet, and on this
hardware it is cheap: a clone is an APFS copy-on-write copy that takes seconds, and a boot
is under a minute. Nothing a job does (files, processes, keychains, a changed Xcode, a
`sudo rm -rf`) can survive into another job, because the next job runs in a different
machine. It is also what makes the release workflow's administrative steps acceptable
without rewriting them (D8).

**Why two.** Apple's licence and the framework both cap it at two macOS guests per host. A
release runs one macOS job; a pull request's gate runs one; two slots let them overlap. A
third concurrent job queues.

**VM size.** Declared per slot: just under half the host's cores and 24 GB each, with the
remainder kept for the host. One VM may be given more when the other slot is disabled, by
declaration.

**Alternatives considered.** *A persistent runner directly on the host* (rejected: the
release workflow would delete the host's Xcode and replace its default keychain, and every
job could see the last one's leftovers); *a persistent VM wiped between jobs* (rejected:
weaker than a fresh clone and no cheaper here).

### D3: A thin supervisor, owned in this repository, over third-party runner managers

**Decision.** The slot loop in D2 is a small script in
`platform/services/macos-runner/deployment/`, run by launchd, rendered and installed by
Ansible, with its configuration (slots, VM size, image name, labels, group) read from the
declaration. It calls `tart` and the platform's App-token helper, and nothing else.

**Why.** The loop is a few dozen lines of logic, and every part of it is something the
platform already has a rule for: the credential chain, pinned versions, declared
configuration, and logs that are not hidden. Owning it keeps it reviewable and keeps the
App key in the platform's own code path.

**Alternatives considered.**

- *Tartelet or Cilicon.* Open-source Mac apps that do exactly this loop. Rejected because
  their configuration lives in an app's settings (Tartelet) or a file the app owns, they
  hold the App key in their own storage, and they are GUI-first, so "declared and applied by
  a playbook" would be a wrapper around someone else's state. Recorded as the fastest path
  if the supervisor proves harder than expected.
- *Orchard* (Cirrus Labs' orchestrator for Tart). Free up to four hosts, built for fleets.
  Rejected for one host: it adds a controller service to run, secure and upgrade. The
  natural step if the fleet grows to several Macs.
- *A runner scale-set listener* (GitHub's scale-set protocol, as used by Actions Runner
  Controller). Creates a VM only when a job is assigned, instead of keeping one warm.
  Rejected for now as a larger moving part; the warm-slot model is simpler and the idle VM
  costs only memory.

### D4: Credentials: just-in-time configurations minted on the host, from a macOS-only App

**Decision.** The supervisor mints a just-in-time configuration per VM: it signs the App
assertion with the platform helper, exchanges it for an installation token, then calls
`generate-jitconfig` with the VM's name, the runner group's id and the declared labels.
The configuration is passed into the VM over SSH standard input and used once. Inside the
guest the runner is started with the configuration in the `ACTIONS_RUNNER_INPUT_JITCONFIG`
environment input, never with `--jitconfig`: the runner removes that variable from its
own environment once read, whereas an argument stays in the process's command line, which
every local user can read for the life of the process. This is the input the Linux
ephemeral supervisor uses (`add-ephemeral-github-runners`). The image pins a runner
version that clears the variable (2.336.0 or later). The guest's launcher, not `run.sh`,
starts `bin/Runner.Listener run`: it reads the configuration from standard input into a
shell variable it does **not** export, and hands it to the listener's environment alone,
so no wrapper keeps it in its own environment. It keeps the stock wrapper's one other duty:
when the listener exits for a self-update (exit codes 3 and 4), it waits for the update to
finish and relaunches the listener with the same configuration, so an update during the
job does not drop it. Task 4.5 tests the guest's process arguments, the environment of
every guest process, and an update exit followed by a completed job. The App
private key is placed on the host by Ansible (secret store to Ansible memory to a `0400`
file owned by the supervisor account), and **never enters a VM**.

**A separate App for macOS, required**, installed on the organisation with the same
single permission, its key at `secret/services/github-runner-macos`. Sharing the Linux and
Windows App is not an option: this host holds the key it mints with, so with a shared App
a Mac compromise could mint registrations for every member of the plane, and revoking it
would stop Linux and Windows CI too. The spec requires that revoking the macOS credential
stop only macOS.

**Why the key is on this host when the Linux design keeps it off runner hosts.** Minting
must happen per job, continuously, which the orchestrator (a batch system) cannot do.
On this host the code that executes repository-authored work is the **VM**, not the host:
the host plays the role the Proxmox node plays for the Linux runners, and the VMs are the
semi-trusted runners. The Linux rule's intent (the key never reaches where job code runs)
holds. What changes is that the host is a higher-value target, so: the VM cannot reach the
host (Softnet), the host accepts only administrative SSH from the declared ranges, and a
separate App means a compromised Mac can be revoked without stopping Linux and Windows CI.

**Why not a registration token.** A registration token creates a persistent registration
that must later be removed, and its lifetime outlasts the job. A just-in-time
configuration is bound to one runner and one job, and GitHub removes the runner itself.

### D5: The golden image is built by Packer from Apple's restore image, with pinned inputs

**Decision.** A Packer template using Cirrus Labs' Tart plugin builds the golden image **on
the host**, launched from the orchestrator through Ansible. It starts from Apple's own
restore image (IPSW) for the declared macOS version, automates Setup Assistant the way
Cirrus Labs' public templates do (vendored and pinned), and installs: the Command Line
Tools; Xcode from an archive the operator downloaded under the Xcode licence, verified
against a declared SHA-256 and kept on the host; Homebrew and CMake; rustup without a
toolchain; the pinned runner, verified against its published checksum; and the runner
account's SSH key for the supervisor. It sets a fresh random password for the image's
account (not the upstream templates' well-known default), disables automatic updates and
Spotlight indexing of build paths, and enables passwordless sudo for the job account (D8).
The image is named by its inputs (macOS version, Xcode version, build date), so a slot
always knows which image it runs.

**Rollout.** A new image is built beside the current one, smoke-tested in one slot
(task 6.3), then promoted by changing the declared image name. The previous image is kept
until the new one has run a release, then deleted.

**Cadence.** GitHub stops queueing jobs to a runner 30 days after a newer runner release,
so a calendar month is too slow. The image is rebuilt **within 14 days of every runner
release** (the verify playbook watches the upstream release feed and reports a pending
bump), and in any case at least every 21 days, which leaves a week for smoke test and
promotion inside the window. It is also rebuilt on every Xcode or macOS bump, by a controlled operation:

- **Detection is automated.** The verify playbook reads Apple's developer releases feed and
  reports a stable Xcode or macOS newer than the declared `macos_xcode_version` and
  `macos_version`, and how long it has been pending.
- **The download stays an operator step.** Xcode's archive is downloadable only with an
  Apple ID, under the Xcode licence, so a human downloads it and declares its version and
  SHA-256; the playbook refuses an archive whose digest differs.
- **The promotion is the image rollout above.** Build beside the current image, smoke-test
  in one slot (task 6.3), run a release dry run, then promote by changing the declared
  image name. Acceptance: the smoke workflow and the dry run pass on the new image, and
  `xcodebuild -version` in a job reports the declared version.

Until a new Xcode is promoted, `latest-stable` in a workflow resolves to the newest Xcode
the image carries, so jobs keep working; what they lag is the hosted image. The same
14-day rule applies to the plane-wide `github_runner_version` that the Linux and Windows
members read. The host's own macOS is kept at or above the guest's version.

**Why Packer here when the Windows change rejects it.** For Windows the platform already had
a template mechanism that fits (`provision-template.yml`). For macOS there is none, the
Proxmox path does not apply, and Packer's Tart plugin is the established, maintained way to
build Tart images. Using it is adopting the standard tool where no platform mechanism
exists, not forking one.

**Why build from the IPSW rather than pull Cirrus Labs' prebuilt images.** Their images are
convenient, and the vanilla ones are a reasonable start, but their Xcode images
redistribute Xcode, and the platform would be trusting a third party's build of the whole
toolchain that signs its product. Building from Apple's bits with a checked-in template
keeps the chain short. Pulling Cirrus Labs' vanilla image as the base is the documented
fallback if IPSW automation proves fragile (task 1.4).

### D6: Network: Softnet confines the VMs; the host enforces the shared egress list

**Decision.** VMs run with `--net-softnet`, so they can reach the internet and their
gateway but not each other, not the host, and not private address ranges. The host's packet
filter (`pf`) enforces the plane's `firewall_deny_egress` list for the host itself and
admits inbound SSH only from `firewall_ssh_cidrs`. The host publishes no other service.

**Why two layers.** Softnet is the boundary that matters for job code, and it is broader
than the Linux denial list: it denies the whole private network rather than a list of
known hosts. The host-level list is defence in depth for the host's own traffic, from the
same declaration the Linux and Windows members read. Softnet's set-uid helper is installed
by the playbook and pinned.

**To verify, not assume** (task 1.3): that a VM on Softnet cannot reach the platform's
secret store, orchestrator, hypervisors, or the host; that it can reach GitHub, the Rust
distribution server, Homebrew and Apple's notarisation service; and what the "gateway" a
Softnet VM may reach actually forwards to.

### D7: The host is managed as code over SSH; first boot is a written exception

**Decision.** After first boot the host is managed only through playbooks over SSH, with the
platform's issued keys: users, launchd services, `pf`, power settings (restart after power
loss, never sleep), software update policy, Tart and Softnet installation, the supervisor,
and image builds. `verify-host-access.yml` and `harden-ssh.yml` gain a macOS branch.

**The first boot is a short, encoded bootstrap, and the part that cannot be encoded is
named.** A new or replacement Mac must pass Setup Assistant before anything can reach it,
and without device management (MDM) Apple provides no way to script Setup Assistant. That
irreducible part is kept to three screens: language and region, the administrator
account, and skipping every optional service. Everything after it is code:

1. **`bootstrap.sh`**, checked in under `platform/services/macos-runner/bootstrap/`, run once
   in Terminal by the operator with the management public key as its only argument. It is
   idempotent: it enables Remote Login, installs the key for the administrator account,
   sets power settings so the host survives a power loss, and prints the host's hardware
   address for the declaration. Re-running it changes nothing on a host it already ran on.
2. **`bootstrap-macos-host.yml`**, launched from the orchestrator, takes over over SSH and
   converges everything else (the list at the top of this decision, and phase 2 of
   `tasks.md`). It is the same playbook for
   a first host and a replacement, so replacing failed hardware is "Setup Assistant,
   `bootstrap.sh`, launch the playbook".
3. **Erase All Content and Settings** returns a host to the start of this path, which is
   how the path itself is tested (task 2.8).

This is the same class of step as installing Proxmox on a new node. Enrolling the Mac in
Apple Business Manager with an MDM would remove even the three screens through automated
device enrolment; that is unnecessary for one host and is the recorded next step if the
fleet grows to several Macs.

**FileVault and unattended restart.** With FileVault on, a Mac that loses power waits at a
pre-boot unlock screen. With it off, the App private key on the disk is protected only by
the disk's physical security. The spike (task 1.2) measures whether the supervisor can run
headless after a reboot with FileVault on (planned reboots can use an authenticated
restart). The decision is an owner question (Open Questions 4).

### D8: Administrative rights inside the disposable VM; what holds and what does not

**Decision.** The job account inside the VM has passwordless `sudo`, as on a hosted
runner, so `setup-xcode`, the release's Xcode deletion and the keychain steps run
unmodified.

**Why this does not contradict the plane's "no host administration from a job" control.**
That control exists so that a job cannot change the machine the next job runs on. Here the
next job never runs on this machine. The control that carries the weight moves to the
boundary: the VM is destroyed after one job, it cannot reach the host or the private
network, and the host grants the VM nothing. The spec delta states this as a scoped
exception, not as a silent relaxation.

**What holds:** a fresh machine per job (files, processes, keychains, installed software);
no route from a job to the host or to the platform's private network; the App key never in
a VM; at most two concurrent VMs.

**What does not hold, stated plainly:**

1. **Signing secrets are in the VM during the release job**, as they are on a hosted
   runner, and an administrator in that VM can read them. Only the release's own job is in
   that VM, so this is the same exposure as hosted, not a new one.
2. **The golden image is shared by every job.** A defect or a compromise in the image
   reaches every job until the image is rebuilt. Hence pinned inputs, a checked-in template
   and a smoke test before promotion.
3. **A VM escape is a host compromise.** Apple's hypervisor is the boundary, and an escape
   would reach the host and the App key. Mitigated by keeping the host patched and the App
   separate, not eliminated.

### D9: Tart's licence is a declared fact

**Decision.** The declaration records the host's CPU core count and the Tart licence tier
in use (free, under 100 organisation cores). The verify playbook sums declared cores across
every Tart host and warns at 80, so that a second or third Mac crossing the free tier is
seen when it is planned, not when someone reads the licence.

### D10: The contract the `zerds` workflows target, and the hosted fallback

**Decision.** The `zerds` macOS jobs select the runner with
`runs-on: ${{ fromJSON(vars.MACOS_RUNNER || '["self-hosted","macos","arm64","uhstray-lan"]') }}`,
the same pattern as `LINUX_RUNNER` and `WINDOWS_RUNNER`. Setting `MACOS_RUNNER` to
the JSON array `["macos-15"]` (it must parse with `fromJSON`) sends every macOS job back
to GitHub. No other step needs to change: the
disk-freeing steps succeed (and free little) in a VM sized for the build, `setup-xcode`
finds the one Xcode, `brew install cmake` is already satisfied, and the keychain steps
work in the VM.

**The fallback is the interim plan.** Until the host is proven (phase 6), the default stays
hosted; the variable flips per repository when it is. After that, the variable is the
documented response to a host outage.

## Risks / Trade-offs

- **One host is a single point of failure for macOS CI.** → The hosted fallback is one
  variable. A second Mac is a declaration plus a purchase.
- **Two slots is a queue on a busy day.** → Accepted; measured (task 7.3). The licence caps
  slots per host, so more capacity means more hosts, not bigger VMs.
- **Headless operation across reboot is unverified with FileVault on** (D7). → First spike;
  the owner decides the FileVault trade-off with the measured answer.
- **Setup Assistant automation from the IPSW can break with a macOS release.** → Pinned
  macOS version; a new version is a reviewed bump; the Cirrus vanilla base is the fallback.
- **Xcode needs a human download.** → Accepted and written down: the operator downloads the
  archive under the licence, the automation verifies its digest and does the rest.
- **The App key lives on this host** (D4). → Separate App, `0400` file, no VM route to the
  host, revocable independently.
- **The 30-day runner version window applies fleet-wide.** → The release-triggered rebuild
  (within 14 days, at most every 21) covers macOS; the same rule must be adopted for the
  Linux and Windows pins, which
  today are fixed declarations (Linux is pinned to 2.336.0 while 2.337.0 and 2.338.0 have
  been published).
- **GitHub could start charging for self-hosted minutes.** It announced and then postponed
  a per-minute charge on self-hosted runners in private repositories. → Outside the
  platform's control, recorded so the cost case is read correctly.

## Migration Plan

1. **Buy and place the host** (owner decision), then the documented first boot.
2. **Spike** (phase 1): headless Tart under launchd across a reboot, Softnet reachability,
   a just-in-time runner in a hand-made VM, an IPSW-based Packer build with Xcode.
3. **Bring the host under automation** (phase 2): declaration, key access, hardening, `pf`.
4. **Build the golden image** (phase 3), **deploy the supervisor** (phase 4) with the
   macOS App (phase 5).
5. **Prove it** (phase 6): smoke, then the CI jobs and a release dry run from a `zerds`
   branch with `MACOS_RUNNER` pointed at the host.
6. **Make it the default** in `zerds`, keep the fallback documented (phase 7).

Rollback is in `proposal.md`, Rollback Plan.

## Open Questions (owner decisions)

1. **Purchase.** Mac mini M5 Pro, 64 GB, 2 TB, about 2,900 US dollars (confirm on Apple's
   configurator). **Recommendation: buy it.** If buying hardware is not acceptable, the
   recommendation is to stay on hosted macOS runners (behind the variable, so nothing else
   changes) rather than rent a Mac, because a rented Mac costs about the same as the hosted
   value and sits outside the platform's controls.
2. **CPU bin.** The 15-core or the 18-core M5 Pro. **Recommendation: 18-core** if Apple's
   price difference is modest, since both VMs compile in parallel and cores are the one
   thing that cannot be added later; otherwise 15-core.
3. **Placement.** Where the Mac mini sits physically: wired to the platform network, on the
   UPS if one exists, in the server room rather than an office. **Recommendation: the
   server room, on the UPS**, with a declared address from the same allocation process
   as every host.
4. **FileVault.** On (protects the App key at rest; an unplanned power loss needs a human to
   unlock) or off (unattended recovery; the key is protected only physically).
   **Recommendation: on, plus the UPS**, unless the spike shows the supervisor cannot run
   headless after an authenticated restart; then off, with the key's exposure accepted
   explicitly and the separate App making revocation cheap.
5. **Slot sizing.** **Recommendation: two slots at just under half the cores and 24 GB
   each**, re-measured after the first month (task 7.3).
