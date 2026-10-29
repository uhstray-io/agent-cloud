## Context

See `proposal.md`, Why. This section records the facts that shape the approach. Each is
either verified against the live estate or the upstream source named beside it, or is
marked as an assumption with the task that settles it.

**What the `zerds` Windows jobs need** (read from `zerds` `main`, `.github/workflows/`,
2026-10-11). Three jobs run on `windows-latest`: `ci.yml` `windows-check` (45 minute
timeout: `cargo check`, `cargo test` and `cargo clippy` on a few crates), `warm-cache.yml`
`windows` (a full-feature `dx build` plus the CLI, to seed the cache), and `release.yml`
`windows` (180 minute timeout: the Vulkan GPU umbrella `windows-all-gpu`, `dx bundle`
into an NSIS installer, the CLI, Authenticode signing when secrets exist, an MSIX package,
and artefact collection). Their dependencies, step by step:

| Need | Where it comes from in the workflow | Hosted image provides it? |
|---|---|---|
| MSVC toolchain (`cl.exe`, `link.exe`) and the Windows SDK | every Rust and CMake build; `signtool` (Authenticode) and `makeappx` (MSIX) are SDK tools located via `vswhere` or under `Windows Kits\10\bin` | yes |
| CMake | `llama-cpp-sys-2`, `whisper-rs-sys`, `opus` native builds | yes |
| LLVM / libclang | `llama-cpp-sys-2` runs `bindgen` (it is the crate in `Cargo.lock` that depends on it) | yes |
| Git for Windows, including its `bash`, `find`, `comm`, `sha256sum`, `cygpath`, `curl` | `actions/checkout`, and every `shell: bash` step | yes |
| Windows PowerShell 5.1 and PowerShell 7 (`pwsh`) | `powershell -NoProfile -Command Expand-Archive`, and every `shell: pwsh` step | yes |
| rustup | `rustup toolchain install` resolves the pinned nightly from `rust-toolchain.toml` | yes |
| NASM | vendored OpenSSL (`encryption` feature) | **no**: the job runs `choco install nasm -y` |
| Strawberry Perl | vendored OpenSSL's configure script | yes |
| Vulkan SDK (headers, import library, `glslc`) | `GGML_VULKAN=ON` in the GPU build | **no**: the job runs `choco install vulkan-sdk`, then exports `VULKAN_SDK` and its `Bin` |
| VC++ x64 runtime DLLs in `System32` | copied beside the exe by "Bundle ONNX Runtime + VC++ redist" | yes |
| WebView2 runtime | the Dioxus desktop app's renderer | yes |
| A `D:` volume | `CARGO_TARGET_DIR: D:\z`, chosen because the GPU build's nested CMake scratch path exceeds `MAX_PATH` (260) otherwise | yes, by accident of the image |
| Long paths enabled | paired with the short path | **no**: the job writes `LongPathsEnabled` into `HKLM` |
| Antivirus exclusions for the build | the job calls `Add-MpPreference`, "the dominant reason the Windows job runs 3 to 4 times the macOS one" | **no**: the job does it, because the hosted runner is an administrator |
| NSIS and WiX for `dx bundle` | fetched by the bundler itself into the account's local data directory on first use | fetched at run time; **here they are pinned in the toolchain manifest instead** (D5), and the release dry run proves the bundler fetched nothing (task 7.4) |

**Machine administration the jobs do today.** Four steps change the machine rather than
the workspace, and each depends on the hosted runner being an administrator:
`choco install nasm`, `choco install vulkan-sdk`, `Set-ItemProperty HKLM:\...\LongPathsEnabled`,
and `Add-MpPreference` (in both `release.yml` and `warm-cache.yml`). Each is written with
`-ErrorAction SilentlyContinue` or would simply fail for a non-administrator, so on a
correctly configured self-hosted host the right outcome is that the host already provides
what they were setting up, and the steps are skipped (D11).

**Hosted runner size** (GitHub's published reference, read 2026-10-11): for a private
repository `windows-latest` is **2 vCPU, 8 GB RAM, 14 GB SSD**, running Windows Server
2025. The release job's own comments record the cost: the GPU build was killed at a 120
minute timeout with roughly 118 minutes of step time (pre-build 24.2, bundle 69.9,
CLI 24.1), and the CPU-only job takes about 21 minutes.

**Usage** (organisation billing API, `zerds` only): Windows minutes 851 (August), 1,798
(September), 3,683 (October 1 to 11). Gross value 8.50, 17.98 and 36.83 US dollars; net
charge zero in every month, because the plan's included allowance covered it.

**Platform mechanisms reused unchanged.** The organisation runner group and its declared
access list (`manage-github-runner-group.yml`); the application-key credential chain and
the per-host registration-token mint on the orchestrator; the `firewall_deny_egress`
declaration shape and its single-host guard; inventory-first provisioning from a
declaration; declared-size convergence (`resize-vm.yml`); key issuance into the secret
store (`generate-service-ssh-key.yml`); the verify-before-harden ordering; the shared ISO
storage (`SharedISOs`) that every node can read.

**Platform facts that constrain the approach.**

- **Templates are node-local.** The only template (vmid 9000, Ubuntu 24.04) lives on
  alphacentauri on `vm-lvms`, which is node-local LVM that merely shares a name across
  nodes. Proxmox refuses a cross-node clone onto non-shared storage, which is why both
  Linux runners ended up on alphacentauri instead of on separate nodes as their design
  intended. A Windows template therefore has to be built **on the node that will run the
  Windows runner**, and this change makes the template playbook's node a real parameter
  for that reason. The same parameter is what the Linux pool needs to get its second
  node, so the work is shared rather than Windows-only.
- **The template is built by an unattended ISO install, driven by Ansible over the
  Proxmox API.** `provision-template.yml` creates a VM with the installer ISO and a
  generated seed ISO, waits for the guest to power itself off, detaches both, and
  converts it to a template. That is exactly the shape a Windows unattended install
  takes (installer ISO, answer-file ISO, VirtIO driver ISO), so no new tool is needed.
- **`provision-vm.yml` assumes Linux after boot.** It waits for the QEMU guest agent and
  SSH (both portable), then runs `cloud-init status --wait` and writes a Linux runner
  environment. Those last steps need an operating-system branch.
- **The firewall operation is UFW.** `apply-firewall.yml` drives the `ufw` CLI; it has no
  Windows path. The declaration it reads (`firewall_ssh_cidrs`, `firewall_deny_egress`)
  is operating-system neutral and is reused as is.
- **The controller has no Windows tooling.** Collections installed are
  `community.hashi_vault` and `ansible.posix`. Repository CI pins ansible-core 2.19.14;
  the version inside the Semaphore runner image is not recorded anywhere in this
  repository and is checked by task 2.1.
- **Hypervisor CPUs are Ivy Bridge Xeons on the candidate nodes** (crabnebula: E5-2690 v2,
  alphacentauri: E5-2667 v2). They have AVX but **not AVX2**. Windows Server 2025 runs on
  them (its processor requirement is x64 with SSE4.2 and POPCNT); Windows 11 lists them as
  unsupported processors. This matters for D1 and for one risk below.

## Goals / Non-Goals

**Goals**

- Every `zerds` Windows job can run on the platform with no step that administers the
  machine, and with the hosted runner kept as a one-variable fallback.
- The Windows host, its template, its toolchain and its runner are produced by
  orchestrator-launchable, re-runnable playbooks from a declaration. Nothing in the path
  exists only as a click in an installer or a command someone once ran.
- The Windows member satisfies the plane's existing requirements, with every place the
  Windows mechanism differs from the Linux one stated, including where it is weaker.
- A materially faster release build than the hosted 2 vCPU runner, measured, not assumed.

**Non-Goals**

- **macOS.** Covered by `add-macos-github-runner`, which needs Apple hardware.
- **Windows on ARM.** No `zerds` job targets it.
- **A GPU in the VM.** The Vulkan build needs the SDK's headers, import library and shader
  compiler, not a GPU; the shipped exe falls back to CPU when no Vulkan driver is present.
  No job executes GPU code. PCI passthrough is therefore unnecessary.
- **Per-job ephemeral VMs.** Recorded as follow-up (D10), not built here. A per-job
  **reset of the runner account** is in scope (D7): it is the Linux ephemeral lifecycle
  (`add-ephemeral-github-runners`) brought to Windows.
- **Windows containers.** The runner's `container:` and service-container support is
  Linux only upstream, so there is nothing to configure.
- **Active Directory.** The host is a standalone server with local accounts; nothing here
  needs a domain, and introducing one for a single runner would be a second platform.
- **Editing `zerds` in this repository.** The workflow edits are a cross-repository task
  that targets the label contract published here.

## Decisions

### D1: Windows Server 2025 Standard, licensed for the node it runs on

**Decision.** The guest is **Windows Server 2025 Standard (Desktop Experience)**. Phases 1
to 6 run on the **evaluation** edition (180 days, free) so the whole path is proven before
anything is bought. Before the runner serves a release, the evaluation is converted to a
licensed Standard edition with a key held in the secret store, and the licence's node
assignment is recorded in the host's declaration.

**Why.** Three facts point the same way:

1. **Parity with what the jobs were written against.** `windows-latest` is Windows Server
   2025. The same base OS removes a class of "works hosted, fails here" differences.
2. **The hardware.** The candidate nodes' Ivy Bridge Xeons are on Windows 11's
   unsupported-processor list; installing Windows 11 there means bypassing setup's checks
   and running an unsupported configuration. Windows Server 2025 supports them.
3. **The licence matches the use.** A build server is a server workload.

**How Windows Server licensing works here, stated so the cost is not a surprise.**
Standard is licensed by the **physical cores of the host**, not by the VM's vCPUs, with a
minimum of 16 core licences per server (8 per processor), and licensing every core of a
host permits two Windows Server VMs on it. A licence is assigned to one physical server
and may be reassigned no more often than every 90 days except on hardware failure, so a
licensed VM **cannot live-migrate freely between nodes**. Client access licences may also
apply; whether a build host with no human or device clients consuming its server
services needs any is a licensing judgement for the owner, not something this design can
settle.

**Alternatives considered.**

- *Windows 11 Pro (retail licence, about 200 US dollars).* Cheapest. Rejected on the
  unsupported-processor point above, and on parity. It becomes the right answer only if
  the runner moves to a node with a supported CPU, which none of the candidate nodes is.
- *Windows Server 2025 Datacenter.* Unlimited VMs per licensed host. Rejected: one runner
  does not need it, and it costs several times Standard.
- *Stay on the evaluation edition, re-arming or rebuilding every 180 days.* Rejected for
  anything beyond the proof phases: the evaluation licence is for evaluation, and the
  product's releases are production use. Recorded honestly so that nobody mistakes the
  proof phases' licensing for the steady state.
- *Windows 11 Enterprise or LTSC.* Requires volume licensing or a subscription the
  organisation does not have.

### D2: Placement on crabnebula, one runner, 16 vCPU / 32 GB / 150 G system + 250 G build

**Decision.** One Windows runner, `gh-runner-win-01`, on **crabnebula**: **16 vCPU** (CPU
type `host`), **32768 MB**, a **150 G** system disk (`C:`) and a **250 G** build disk
(`D:`), both on node-local storage, UEFI (OVMF) with a TPM state disk, VirtIO SCSI and
VirtIO network. Its vmid and address come from the ledger and the address check at
allocation time (task 3.1), never from this document.

**Why crabnebula.** It is the strongest node (40 threads, the largest memory) and the one
the most recent capacity report showed least committed. It also does **not** host the
Linux runners, which both sit on alphacentauri. A release run starts its Linux, Windows and
macOS jobs together, so placing Windows beside the Linux pool would put 8 + 8 + 16 vCPU of
simultaneous compiler load on alphacentauri's 32 threads at exactly the moment the release
is waiting on all three. Separate nodes also mean one node's maintenance does not stop
both Linux and Windows CI at once. The cost is that crabnebula has 20 physical cores, so it
needs 20 core licences rather than the 16 minimum (D1): about one quarter more licence
cost, recorded as the price of the separation.

**Why 16 vCPU.** The build is dominated by parallel C++ (llama.cpp, whisper.cpp, opus) and
parallel Rust codegen, both of which scale with cores until link time. The hosted runner
has 2. These cores are older and slower per core than the hosted runner's, so 16 is the
smallest count that makes a large improvement likely rather than marginal; it is the
same vCPU count as two Linux runners together, and leaves crabnebula over half its
threads.

**Why 32 GB.** The working rule for MSVC plus rustc thin-LTO is roughly 2 GB per
concurrent compile process; 16 parallel jobs at 2 GB is 32 GB. Less invites the linker or
a large translation unit to page, which is slower than having fewer cores. This is the
figure to re-measure (task 7.5) rather than to defend: memory is a declaration change.

**Why two disks, and why these sizes.** `D:` exists because the release job hard-codes
`CARGO_TARGET_DIR: D:\z` to stay under `MAX_PATH`, so the host must provide a `D:` volume
that a non-administrator can write. A separate disk also keeps build churn off the system
volume, so a runaway build fills `D:` and the OS stays healthy. `C:` holds Windows (about
30 G after updates), Build Tools and the SDK (about 15 to 20 G), LLVM, the Vulkan SDK and
the rest (about 10 G), and the runner account's rustup and cargo state (a few gigabytes
per pinned nightly, accumulating across toolchain bumps): 150 G is about twice the
expected peak. `D:` holds one GPU release's target directory at a time; that size is not
recorded for Windows, and the Linux hosts' experience (a 200 G disk with a 9.8 G root
volume that no Rust build fitted in) shows the cost of guessing small. 250 G is the
starting point, and the release dry run (task 7.4) records the real peak so the figure is
corrected with evidence. Both disks can only grow (`resize-vm.yml` refuses to shrink),
which is why the starting point is moderate rather than generous.

**Alternatives considered.**

- *alphacentauri, beside the Linux pool.* Exactly 16 physical cores, so the cheapest
  licence, and slightly higher clock. Rejected for the failure-domain and contention
  reasons above.
- *milkyway (Ryzen 9 5900X).* The fastest cores in the fleet, and the only candidate with
  AVX2. Rejected on memory: about 18 G free of 31 G, so a 32 G guest does not fit. Recorded
  as the placement to revisit if the build proves clock-bound.
- *Two Windows runners.* Rejected for now. The Windows jobs are a weekly cache warm, one
  check per pull request, and a release; one runner serialises them. A second runner on
  crabnebula needs no further licence, because licensing all of a host's cores covers two
  Windows Server VMs there (D1); a second runner on another node would need that node
  licensed too. Growth is a declaration (the plane's declaration-only
  requirement), so this is cheap to revisit once queue time is measured.
- *8 vCPU / 16 GB, matching the Linux runners.* Rejected: the Linux sizing was chosen for
  test and lint workloads ahead of evidence, whereas the Windows release build has
  evidence, and it says the build is CPU-starved at 2 vCPU.

### D3: The Windows template is built by an unattended install, as code, with the existing template playbook

**Decision.** `provision-template.yml` gains an operating-system parameter. With
`template_os: windows` it builds the template from three ISOs on the shared ISO storage:
the Windows Server 2025 installation ISO, the VirtIO driver ISO, and a seed ISO generated
from a templated `autounattend.xml` (plus a first-logon PowerShell script). The answer
file loads the VirtIO storage and network drivers during setup, installs the QEMU guest
agent and cloudbase-init (below), enables the in-box OpenSSH server with
PowerShell as its default shell, installs the management public key, applies the
security baseline that belongs in every clone (no RDP, no SMB inbound, no WinRM
listener), and then runs `sysprep /generalize /oobe /shutdown`. The playbook waits for
the power-off as it does for Ubuntu, detaches the ISOs, deletes the seed ISO from the ISO
storage, attaches a cloud-init drive, and converts the VM to a template. With the
parameter absent, the Ubuntu path is byte-identical (a regression test pins that).

**Clone-time identity: cloudbase-init, with a guest-agent fallback.** A clone must get its
own hostname, static address and administrative SSH key from its declaration, which on
Ubuntu is cloud-init's job. On Windows the one agent this design uses is
**cloudbase-init** (the Cloudbase Solutions MSI, pinned in the toolchain manifest and run
as its own Windows service), configured with its ConfigDrive metadata service to read the
Proxmox cloud-init drive (Proxmox's `configdrive2` format), and with exactly three plugins
enabled: hostname, static network configuration, and SSH public keys (which it writes to
`administrators_authorized_keys`). Proxmox's own cloud-init drive is written by
`provision-vm.yml` from the declaration, as it is for Ubuntu clones. Whether this
combination works on this cluster's Proxmox version is **unverified**, so it is settled by
the first spike (task 1.3). If it does not work cleanly, the fallback needs no new guest
software: the Windows branch of `provision-vm.yml` sets the hostname and address and
writes the administrative public key through the QEMU guest agent's command execution,
which the Proxmox API already exposes. The spike records which path is kept, and only
that one ships.

**Why this, not Packer.** Packer would build the same image by the same unattended
install, but it is a second tool with its own state, plugin versions and credentials,
doing a job `provision-template.yml` already does for Ubuntu. Adopting it for one image
forks the template path into two mechanisms. The platform's rule is to extend the shared
mechanism, and the existing one fits the Windows install without strain.

**What the seed ISO may contain.** It is written to hypervisor storage, so it must hold no
long-lived secret. It carries the management **public** key and a random one-time
administrator password generated for that build only. The password is never stored, it
stops mattering when sysprep runs, and each clone's administrator password is set from
the secret store at provisioning (task 4.2). The seed ISO is deleted from storage when the
build ends, including on failure.

**Rebuild cadence.** Because the template is code, rebuilding it is cheap, and a rebuilt
template carries the latest cumulative update. The template is rebuilt monthly (a
scheduled orchestrator template), and the runner VM is rebuilt from it on the same
cadence. It keeps the machine patched; what a job leaves in the runner account is removed
far sooner, by the reset before every job (D7).

**Alternatives considered.** *A hand-built golden image* (rejected: it cannot be rebuilt
by the next person, which is the defect this platform exists to remove); *Packer*
(rejected above); *an Azure or Hyper-V image converted to qcow2* (rejected: licensing
for those images is tied to their cloud, and conversion is a one-off step that does not
encode).

### D4: Ansible reaches Windows over OpenSSH with the existing key chain, not WinRM

**Decision.** The Windows host is managed with `ansible_connection: ssh`,
`ansible_shell_type: powershell`, and the `ansible.windows` collection. The administrative
key pair is the one the platform already issues (`secret/services/ssh` and the per-service
key from `generate-service-ssh-key.yml`). OpenSSH on Windows reads an administrator's keys
from `C:\ProgramData\ssh\administrators_authorized_keys`, not from the profile, so
`distribute-ssh-keys.yml` gains a Windows branch that writes there with the file's
required access control. `verify-host-access.yml` runs its key-only probe unchanged in
principle (password and keyboard-interactive disabled), and `harden-ssh.yml` gains a
Windows branch that sets `PasswordAuthentication no` in
`C:\ProgramData\ssh\sshd_config` only after that probe returns GO.

**Why.** It is the transport the platform already runs on, with the credential chain the
platform already has:

- **One credential model.** Keys in the secret store, a key-only proof before the
  bootstrap password is withdrawn, hardening as the last step. WinRM would replace all
  of that with passwords: Basic or NTLM over HTTP, or an HTTPS listener whose certificate
  becomes one more thing to issue and renew, or Kerberos, which needs a domain the
  platform does not have.
- **No new controller dependency.** WinRM needs `pywinrm` (and its own dependencies) in the
  orchestrator's Python environment, which is the kind of addition this platform treats
  as a last resort. SSH needs only a collection, installed through the existing
  requirements file.
- **Supported, not experimental.** ansible-core 2.18 made Windows over SSH an officially
  supported connection. Task 2.1 confirms the orchestrator runs 2.18 or later; if it does
  not, upgrading the orchestrator image is the fix, not falling back to WinRM.
- **Smaller exposed surface.** The host listens on one administrative port (22), the same
  as every Linux host, and the firewall rules stay identical in shape.

**Alternatives considered.** *WinRM over HTTPS with a certificate from the internal CA.*
Workable, and the platform has a CA. Rejected because it still authenticates with a
password, needs `pywinrm`, and adds a second administrative port and a renewal path, for
no capability SSH lacks. *PSRP.* Same objections as WinRM.

### D5: The build toolchain is host configuration: pinned, checksummed, installed by a playbook

**Decision.** A new playbook, `configure-windows-build-host.yml`, converges the host to a
declared toolchain manifest in the private inventory (each entry: name, version, download
URL, SHA-256, install arguments). It downloads each installer, verifies its digest before
executing it, installs it machine-wide under `Program Files` (not writable by the runner
account), and records what it did. It also applies the machine settings the jobs used to
apply themselves:

| Host setting | Replaces the job step | How |
|---|---|---|
| NASM | `choco install nasm` | pinned installer; `nasm` on the machine `PATH` |
| Vulkan SDK | `choco install vulkan-sdk` | pinned installer; machine `VULKAN_SDK` and its `Bin` on `PATH` |
| `LongPathsEnabled = 1` | `Set-ItemProperty HKLM:\...\FileSystem` | registry, plus `core.longpaths=true` in Git's system config |
| Antivirus exclusions | `Add-MpPreference` | exclusions for the runner work directory, `D:\z`, the runner account's `.cargo`, `.rustup` and `TEMP`, and the compiler processes |
| `D:` build volume | the hosted image's `D:` | the second disk initialised, formatted and lettered `D:`; `D:\z` created with modify rights for the runner account only |
| Visual Studio Build Tools + Windows SDK | preinstalled on the hosted image | a fixed-version bootstrapper with a checked-in `.vsconfig` listing exactly the workloads and components (C++ build tools, the Windows 11 SDK, CMake is installed separately) |
| CMake, Git for Windows, LLVM, Strawberry Perl, PowerShell 7, WebView2 runtime, VC++ x64 redistributable | preinstalled on the hosted image | pinned installers; LLVM sets machine `LIBCLANG_PATH` |
| NSIS and WiX (for `dx bundle`) | the bundler downloads them on first use | pinned installers with their digests, installed machine-wide, and placed in the bundler's tool directory in the seed (D7) so it finds them and downloads nothing; task 7.4 fails the dry run if the bundler fetched anything |
| rustup and the declared Rust toolchains | preinstalled | installed for the runner account at deploy time and captured in the seed (D7), so each job starts with the declared nightly and `rustup toolchain install` in a job works without elevation; whatever a job installs or replaces is discarded with its account |

Git for Windows' `bin` is placed ahead of `System32` on the machine `PATH`, and WSL is not
installed, so `shell: bash` resolves to Git Bash rather than to the WSL launcher, a
well-known self-hosted Windows trap.

**Why pinned installers rather than a package manager.** It is the Linux change's D7
applied to the toolchain: what runs is what was declared, two hosts built weeks apart are
identical, and an upgrade is a reviewed diff. Chocolatey would add a third-party package
repository whose community packages are scripts that themselves download from vendors,
so the trust chain is longer and the pin is weaker; winget on Windows Server is usable but
has a history of awkward unattended operation under the system account. Vendor installers
plus a declared digest are the most explicit option, and `ansible.windows.win_package`
already does the install part.

**Why exclusions rather than turning antivirus off.** The host executes repository code,
so real-time protection stays on for the rest of the machine. The exclusions are the same
set the job already applies on hosted runners, scoped to build output and toolchains.
Whether a Dev Drive (a ReFS volume on which Defender runs in its performance mode) is
available for `D:` on this edition is checked in the spike (task 1.4); if it is, `D:` is
made a Dev Drive and the path exclusions on `D:` are dropped, which is the stronger
posture.

**Divergence from the hosted image is intended.** `windows-latest` changes monthly; this
host changes when the manifest does. That is a feature for reproducibility and a cost
when a workflow starts depending on something the hosted image added. The fallback
variable (D11) is the escape hatch, and the manifest is the place the fix lands.

### D6: The runner is a Windows service under a dedicated non-administrative account

**Decision.** A local account `ghrunner` is created by the playbook, is **not** a member
of `Administrators`, holds the "Log on as a service" right, and has modify rights only on
the runner home, the runner work directory and `D:\z`. Its password is generated once in
Ansible memory and stored in the secret store (generate if absent, reuse always), never
on disk. The runner is installed from the pinned `win-x64` archive, verified against the
checksum published with that release before extraction (the Linux change's D7), into the
seed (D7). It is **never configured** with `config.cmd`: each runner starts from a
just-in-time configuration (D7), passed as the `ACTIONS_RUNNER_INPUT_JITCONFIG`
environment input, never as an argument, for the reason the Linux `deploy.sh` records: an
argument is visible to every local user for the life of the process.

**The credential chain is reused, not re-built.** The orchestrator reads the application
key from `secret/services/github-runner`, signs the assertion, exchanges it for an
installation token and mints just-in-time configurations **for this host's runner names**
with the scheduled stock template from `add-ephemeral-github-runners` (its D3), writing
them to the host's root-only stock. The Windows host is denied the secret store by its
egress declaration (D8), exactly as the Linux hosts are, so it could not fetch the key
even if it held a credential.

**Labels.** The runner applies `self-hosted`, `Windows` and `X64` itself; the declaration
adds `uhstray-lan`. The Linux runners also carry `uhstray-lan`, so a job that names only
`uhstray-lan` could land on either operating system. The label contract therefore states
that a job names the OS label too: `runs-on: [self-hosted, windows, x64, uhstray-lan]`.
Label matching is case-insensitive upstream.

**Why not the default service identity.** The runner's default is `NT AUTHORITY\NETWORK
SERVICE`, a built-in identity shared with other Windows services. A dedicated account is
the only way to make "the job can read what the runner account can read" a small,
known set, which is the guarantee the plane's isolation requirement actually makes.

### D7: Every job starts from a reset runner account (the Linux ephemeral lifecycle, on Windows)

**Decision.** The Windows runner uses the lifecycle `add-ephemeral-github-runners` built
for Linux, with Windows mechanisms in place of the Linux ones:

- **One job per runner, from a just-in-time configuration** (that change's D1 and D3).
  The orchestrator mints configurations from the application key and stocks them on the
  host in `C:\ProgramData\github-runner\jit\`, whose access control admits only
  `SYSTEM` and `Administrators`. No registration token reaches the host, and `config.cmd`
  never runs on it.
- **A supervisor that is not the job account.** A Windows service running as
  `LocalSystem`, whose script lives under `Program Files` (not writable by `ghrunner`),
  takes the oldest configuration, deletes it from the stock, starts one runner as
  `ghrunner` with the configuration in `ACTIONS_RUNNER_INPUT_JITCONFIG` (never an
  argument; the runner removes the variable from its environment), and waits for it to
  exit after its one job.
- **A reset before every runner start**, in this order:
  1. stop every process whose owner is `ghrunner` (CIM `Win32_Process` owner, killed from
     the supervisor), until none is left; a surviving process is what held files locked
     under the hook design;
  2. unload the account's registry hive and delete its profile directory, the runner
     work directory, every declared build root (`github_runner_windows_build_roots`,
     `D:\z` by default) and every file `ghrunner` owns under the declared sweep roots;
  3. verify: no process, no profile, no owned path, each declared root empty;
  4. recreate the profile from a machine-owned **seed** (the account's home as the deploy
     built it, with the declared Rust toolchains and the bundler's pinned tools, root-only
     writable, as in that change's D5), and only then start the runner.

  The reset runs **before** each runner start, not after a job, so a host that rebooted
  in the middle of a job resets on the supervisor's first start before it takes another.
- **Quarantine is the supervisor's state, not the account's.** A reset that cannot be
  verified, or `github_runner_max_start_failures` runners in a row that exit without a job,
  makes the supervisor write its reason under `C:\ProgramData\github-runner\state\`
  (`SYSTEM` and `Administrators` only) and stop starting runners. A quarantined host takes
  no job because no runner is running to take one: nothing the job account can write
  decides it. Verification (task 8.1) reports it; a deploy clears it only after its own
  reset verifies clean, as on Linux. The job hooks are not part of the isolation and are
  not used for it.

**Why this, and not the hooks.** The hook design had the job account clean up after
itself, so the account under suspicion also held the evidence: a surviving job process
could delete the quarantine marker, and a reboot before the completed hook ran left no
marker at all, so the next job skipped the wipe. Moving the reset to a separate account,
running it before every runner start, and keeping quarantine outside the job account's
reach removes all three gaps by construction, and it reuses a lifecycle already reviewed
for Linux instead of designing a second one.

**What it costs.** Every job starts cold: the profile and `D:\z` are rebuilt, so the
workflow's `rust-cache` restore (from the forge's cache service) does the warming, as on a
hosted runner. The seed copy takes seconds; a job that installs extra tools pays for them
each time.

**Spike first.** How the supervisor starts a process as `ghrunner` with the configuration
in its environment (a per-job service entry with an `Environment` value only
administrators can read, or a scheduled task), and whether a loaded profile hive can always
be unloaded after the kill, are unverified on this edition and settled by task 1.6 before
anything depends on them.

### D8: Inbound default-deny and egress denial on the Windows firewall, from the same declaration

**Decision.** `apply-firewall.yml` gains a Windows branch that enforces the same declared
inputs with Windows Defender Firewall rules: inbound default block, SSH (22) allowed only
from `firewall_ssh_cidrs`, and one outbound block rule per `firewall_deny_egress` entry
(the secret store, the orchestrator, the IPAM authority, every hypervisor on all ports).
Rules carry the same tag scheme the UFW path uses, so convergence (add declared, prune
tagged but undeclared) works the same way. The single-host guard on denials is shared,
not re-implemented. The anti-lockout ordering is the same: SSH allow first, a fresh
connection forced to prove it, and the Proxmox console as the out-of-band path.

**Why the job cannot undo it.** Changing firewall rules requires administrative rights,
which the runner account does not have (D6), so the denial is a property of the host,
not of each workflow's restraint.

**Limitation, the same as Linux.** A host firewall is only as strong as the host's
integrity. Gateway-enforced segmentation for semi-trusted hosts remains the recorded
follow-up from the Linux change, and this host is one more reason to do it.

### D9: Updates are applied by automation, in a window, with the runner drained

**Decision.** Automatic Windows Update is disabled by policy. A patch playbook
(`ansible.windows.win_updates`) is launched from the orchestrator. It first **drains** the
host: it tells the supervisor (D7) to start no further runner, then waits until the
running one, if any, has finished its job (bounded by the longest job timeout, 180
minutes). Only then does it install updates, reboot if required, let the supervisor reset
the account and resume, and verify a runner is online with its declared labels. A run that
fails after draining leaves the supervisor drained, so the host stays out of rotation
rather than taking work half-patched, and verification reports it. The monthly template
rebuild (D3) is the other half: a freshly built host starts patched.

**Why.** An automatic reboot in the middle of a three hour release turns into a failed
release with no obvious cause. Patching on a schedule the platform controls keeps the host
current without that failure mode, and draining before stopping makes the playbook
safe to launch at any time.

### D10: Isolation, and what does not hold on Windows

**What holds**, to be verified from inside a job exactly as on Linux: the runner account,
its profile (tools included), the workspace and the declared build roots are destroyed and
rebuilt from the seed before every job (D7); the job runs as an account with no
administrative rights; the platform interior is unreachable from the host.

**What does not hold, stated plainly.**

1. **No job runs in a container.** Upstream's container jobs and service containers are
   Linux only, so on Windows every job runs on the host as `ghrunner`. A job can read
   whatever that account can read. Nothing may be placed on this host that all five
   permitted repositories are not entitled to.
2. **Within a job, the job owns its tools.** `~\.cargo\bin` (where `dx` and
   `cargo-binstall` land), the cargo registry and the rustup toolchains are writable by
   the job that is running, so a step can replace a tool a later step of **the same job**
   executes. No later job sees it: the reset discards the profile, and the next job's tools
   come from the root-only seed (D7). Machine-wide toolchains (D5) are not writable by the
   account at all.
3. **A signing secret is exposed to its own job only.** The release job writes `cert.pfx`
   into the workspace. Every step of that job, including steps of actions it calls, can read
   it, which is equally true on a hosted runner. A later job cannot: the file is removed by
   the next reset or, if the reset fails, the host is quarantined and runs nothing.
   Signing is still kept off this host until the lifecycle has passed its acceptance
   (Open Question 5).

**The strongest lifecycle** is a fresh VM cloned from the template per job,
registered with a just-in-time configuration and destroyed afterwards. That is what
`add-macos-github-runner` builds on Apple hardware, where clones are cheap. On this
cluster a full clone of a 400 G Windows VM per job is too slow to be practical until
linked clones are available on the chosen storage, so it is recorded as follow-up, with
the template work in D3 as its foundation.

### D11: The contract the `zerds` workflows target

**Decision.** This change publishes, and the `zerds` repository adopts in its own pull
request, the following:

- **Runner selection by repository variable**, the pattern its Linux jobs already use:
  `runs-on: ${{ fromJSON(vars.WINDOWS_RUNNER || '["self-hosted","windows","x64","uhstray-lan"]') }}`.
  Setting `WINDOWS_RUNNER` to the JSON array `["windows-latest"]` (it must parse with
  `fromJSON`) sends the jobs back to hosted runners.
- **Machine-administration steps run on hosted runners only**, gated with
  `if: runner.environment == 'github-hosted'`: the NASM and Vulkan SDK installs, the
  `LongPathsEnabled` write and the antivirus exclusions.
- **The Vulkan step is split** into an install step (hosted only) and a locate-and-verify
  step (both), so `VULKAN_SDK` is still exported and `glslc --version` still proves the SDK
  before an hour of build depends on it.
- **`CARGO_TARGET_DIR: D:\z` is unchanged**, because the host provides `D:` and `D:\z`.

**Why gate rather than delete.** Deleting the steps would make the hosted fallback
incomplete, and the fallback is rollback stage 1. Gating keeps one workflow that is
correct on both surfaces, which is the same property the Linux jobs got from running in
an `ubuntu:22.04` container on both.

## Risks / Trade-offs

- **The CPUs lack AVX2.** Compilation is unaffected, but any test or build step that
  *executes* AVX2 code would fail with an illegal instruction here and pass on a hosted
  runner. Today's Windows tests are pure-logic crates. The release must also not tune the
  shipped CPU code to the build host: llama.cpp's native-CPU option, if on, would bake
  this host's instruction set into the binary. → Task 7.4 checks the shipped `ggml` CPU
  variant set against a hosted build of the same commit, and the CPU type is `host` so
  the guest sees AVX at all.
- **Licence compliance depends on placement.** A Standard licence is tied to a physical
  host, so migrating the VM to another node is a licensing event, not just an operation.
  → The licence's node is in the declaration, and `provision-vm.yml` refuses to place a
  host whose declared licence node differs from its declared node.
- **One runner is a queue.** A release and a pull request's check on the same day wait
  for each other. → Accepted; queue time is measured (task 8.3), and a second runner is a
  declaration plus a licence decision.
- **The account reset is new on Windows.** Killing a user's processes, unloading its hive
  and deleting its profile are standard operations, but the combination has not run on this
  edition. → Spike first (task 1.6); any reset that cannot be verified quarantines the host
  (D7), so a fault in it fails closed, not open.
- **The toolchain manifest drifts from the hosted image.** → Intended (D5). The fallback
  variable covers a job that needs something the manifest lacks, and the manifest is
  where the fix lands.
- **The answer-file and cloudbase-init path is new to this platform.** → Proven by a
  spike before anything depends on it (phase 1), with a guest-agent fallback that needs no
  new guest software.
- **Antivirus exclusions widen what malware could hide in.** → Scoped to build output and
  toolchains; replaced by a Dev Drive if the edition supports it.
- **A pinned runner with self-update disabled has a 30-day shelf life.** Since 2026-09-25
  GitHub stops queueing jobs, silently, to a runner more than 30 days behind a newer
  release. The Linux pin (2.336.0) is already behind 2.337.0 (2026-08-26) and 2.338.0
  (2026-10-06). → The plane-wide `github_runner_version` is bumped within 14 days of every
  runner release, a deadline independent of the monthly template rebuild (D3), and
  verification (task 8.1) reports a pending release and any runner within a week of the
  window closing. The rule is shared with `add-macos-github-runner`.
- **GitHub could start charging for self-hosted minutes.** It announced a per-minute
  charge on self-hosted runners in private repositories for 2026-03-01, then postponed
  it without cancelling. → Outside this platform's control; recorded so that "no GitHub
  billing" is understood as the policy of the day, not a guarantee. Artefact and cache
  storage are billed separately and are unchanged by this change.

## Migration Plan

Nothing is migrated. The sequencing is risk ordering:

1. **Spike the unknowns first** (phase 1): the answer-file install on this cluster's
   Proxmox, cloudbase-init against the Proxmox drive, Windows over SSH from the
   orchestrator's actual ansible-core, and Dev Drive availability. Each can change what
   gets built.
2. **Extend the shared mechanisms** (phase 2) with their regression tests before any host
   depends on them: the template playbook's OS and node parameters, the provisioning
   post-boot branch, the Windows branches of host access and firewall.
3. **Build the template on crabnebula, then provision and harden the host** (phases 3 and
   4), in the platform's existing order: declaration, provision, key issuance, key-only
   proof, hardening, firewall.
4. **Converge the toolchain** (phase 5), then **register the runner** (phase 6).
5. **Prove it with real work** (phase 7): a smoke workflow, then a full release dry run
   from a `zerds` branch with the variable pointed at the self-hosted runner.
6. **Verify, document, and hand over the contract** (phase 8), then the `zerds` pull
   request makes the self-hosted runner the default.
7. **License before the first production release** (task 7.6).

Rollback is in `proposal.md`, Rollback Plan.

## Open Questions (owner decisions)

Each has a recommendation. None changes the specs; each is a parameter of a task that
exists either way.

1. **Licence.** Windows Server 2025 Standard for crabnebula's 20 cores (a 16-core pack plus
   two 2-core packs; list price is about 1,176 US dollars per 16 cores, so roughly 1,470
   US dollars at list, less through resellers), versus Windows 11 Pro on an unsupported
   CPU, versus staying on evaluation. **Recommendation: Server 2025 Standard**, bought
   after phase 6 proves the path on the evaluation edition. Also for the owner: whether
   client access licences apply to a build host with no interactive users.
2. **Node.** crabnebula (failure-domain separation, most headroom, 20 core licences)
   versus alphacentauri (16 core licences, shares the Linux pool's node). **Recommendation:
   crabnebula.**
3. **Sizing.** 16 vCPU / 32 GB / 150 G + 250 G. **Recommendation: start there**, record the
   release dry run's wall time, peak memory and peak `D:` use (task 7.5), and adjust by
   declaration. The figure to watch is memory: if peak use is under 20 GB, 24 GB frees room
   on the node; if the build pages, 48 GB is the next step.
4. **One runner or two.** **Recommendation: one**, revisited when queue time is measured.
   A second runner on crabnebula costs no further licence (Standard covers two VMs on a
   fully licensed host); one on any other node needs that node licensed as well.
5. **Release signing on this host.** The Authenticode certificate is a repository secret
   today and is only used when configured. **Recommendation: not on this host until the
   reset lifecycle (D7) has passed its acceptance** (task 8.7): until then the signing step
   runs only on hosted runners (`if: runner.environment == 'github-hosted'`, the D11
   pattern), so self-hosted release builds are unsigned. After acceptance, allow it, knowing
   that a self-hosted signer is a host the certificate's private key passes through.
