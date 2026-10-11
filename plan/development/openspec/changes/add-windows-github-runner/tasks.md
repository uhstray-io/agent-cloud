## 0. Branch and working ground

- [ ] 0.1 Create one feature branch for the implementation from `dev`:
      `git checkout dev && git pull && git checkout -b feat/windows-github-runner`. All
      work below lands on it: one `feat` to `dev` PR, one `dev` to `main` promotion.
- [ ] 0.2 Confirm `make git-setup` has been run in this clone (`core.hooksPath` is
      `.githooks`, `merge.ours.driver` is set), so the secret-scanning hooks are active.
- [ ] 0.3 Read `add-github-actions-runners` (design D1 to D9 and its spec) and
      `platform/services/github-runner/CLAUDE.md`. This change extends that plane; it
      re-uses its group, credential chain and egress declaration and must not fork them.
- [ ] 0.4 Validation gate: the branch is `feat/windows-github-runner`, the hooks path is
      `.githooks`, and `openspec validate add-windows-github-runner --store agent-cloud`
      passes.

## 1. Spike the unknowns before anything is built on them

Everything here runs on a throwaway VM on the target node with the **evaluation** media,
and is destroyed afterwards. Each task writes its verbatim outcome into `design.md`
under the decision it settles.

- [ ] 1.1 Confirm the orchestrator's ansible-core version inside the Semaphore runner
      image (a read-only Semaphore task that prints `ansible --version`). It must be 2.18
      or later (D4). If it is not, the fix is an orchestrator image upgrade, sequenced
      before phase 2; WinRM is not the fallback.
- [ ] 1.2 Unattended install: write the first draft of the task 2.1 template branch and
      the `autounattend.xml.j2` on this branch, and run it from Semaphore's Dev variant
      against a throwaway vmid (UEFI, TPM state disk, VirtIO SCSI and network, the three
      ISOs from `SharedISOs`), so the spike is itself re-runnable and its output is a
      Semaphore task log. Record whether setup loads the VirtIO drivers,
      completes without interaction, installs the guest agent, enables OpenSSH with
      PowerShell as the default shell, and powers off after sysprep.
- [ ] 1.3 Clone-time identity: convert the spike VM to a template, clone it with a Proxmox
      cloud-init drive carrying a hostname, a static address and an SSH key, and record
      whether cloudbase-init applies all three. If not, prove the D3 fallback (setting
      address and hostname through the guest agent's command execution via the Proxmox
      API) instead, and record which path the design keeps.
- [ ] 1.4 Record whether a Dev Drive can be created on the second disk on this edition, and
      whether Defender runs in performance mode on it (D5).
- [ ] 1.5 From the orchestrator, connect to the spike VM over SSH with
      `ansible_shell_type: powershell` and the management key, and run
      `ansible.windows.win_ping`, `win_package` and `win_regedit` once each.
- [ ] 1.6 Validation gate: every unknown in the design's Risks section that phase 1 owns
      is resolved in `design.md` with recorded output, and spec scenario **"The template
      is rebuilt from code"** is shown to be achievable on this cluster (the spike built
      one with no hand interaction with the installer).

## 2. Extend the shared mechanisms (before any host depends on them)

- [ ] 2.1 `provision-template.yml`: add `template_os` (default `ubuntu`) and make
      `target_node` and `template_vmid` real per-template parameters. With
      `template_os: windows`, build from the Windows ISO, the VirtIO ISO and a seed ISO
      generated from `platform/hypervisor/proxmox/windows/autounattend.xml.j2` plus a
      first-logon script, wait for power-off, detach the ISOs, attach a cloud-init drive,
      convert to a template. Delete the seed ISO from storage in an `always` block.
- [ ] 2.2 The seed carries only the management public key and a random one-time
      administrator password generated in memory for that build (D3). Add a test that
      fails if the rendered answer file contains any value read from the secret store
      other than a `:public_key`.
- [ ] 2.3 BATS or pytest regression: with `template_os` absent, the requests
      `provision-template.yml` would send are identical to today's (the Ubuntu path is
      byte-identical).
- [ ] 2.4 `proxmox-validate.yml`: when the target declares Windows, check the Windows and
      VirtIO ISOs exist on the ISO storage and report their volume ids.
- [ ] 2.5 `provision-vm.yml`: add an OS-family branch read from the declaration
      (`vm_os_family`, default `linux`). For Windows: attach the declared second data
      disk, use the identity path chosen in 1.3, skip `cloud-init status --wait` and the
      Linux runner-environment write, and wait for the guest agent and SSH instead. Refuse
      a Windows host whose declared licence node differs from its declared node.
- [ ] 2.6 `verify-host-access.yml`, `distribute-ssh-keys.yml` and `harden-ssh.yml`: add a
      Windows branch each (D4). Distribution writes
      `C:\ProgramData\ssh\administrators_authorized_keys` with its required access
      control; hardening sets `PasswordAuthentication no` only after the key-only probe
      returns GO. Linux behaviour is unchanged (existing tests stay green).
- [ ] 2.7 `apply-firewall.yml`: add a Windows branch (D8) that enforces
      `firewall_ssh_cidrs`, inbound default block and `firewall_deny_egress` with Windows
      Defender Firewall rules carrying the same tag scheme, re-using the shared rule
      planner and single-host guard rather than re-implementing them. Same anti-lockout
      order: SSH allow, forced reconnect, then default block.
- [ ] 2.8 Add `ansible.windows` to `platform/playbooks/collections/requirements.yml` with
      a minimum version, and confirm the Semaphore runner installs it.
- [ ] 2.9 Tests: the Windows firewall branch emits one outbound block per declared denial
      and none when the list is empty; the guard refuses a denial broader than an SSH
      CIDR on Windows exactly as on Linux; the licence-node refusal in 2.5 fires.
- [ ] 2.10 Validation gate: the full BATS and pytest suites pass, and spec scenarios
      **"The Ubuntu template path is unchanged"** and **"No answer file is left on
      hypervisor storage"** are proven by tests, not by inspection.

## 3. Declare the host

- [ ] 3.1 Allocate the vmid and address: the next free vmid from the
      `site-config/proxmox/vm-specs.yml` ledger cross-checked against the hypervisor's
      own resource list, and an address that is free in the inventory, silent to ICMP and
      outside the DHCP range (`validate-address-free.yml`). The declaration is the
      allocation record, as for the Linux runners (their D9).
- [ ] 3.2 Declare `gh-runner-win-01` in the private inventory: a `github_runner_win_svc`
      group (or a host in the existing runner group with Windows vars, whichever keeps
      the shared group vars un-duplicated) carrying `vm_os_family: windows`, crabnebula,
      16 cores, 32768 MB, a 150 G system disk and a 250 G data disk, the address, tags,
      `ansible_connection: ssh`, `ansible_shell_type: powershell`, and a comment
      recording why this node and this size (design D2).
- [ ] 3.3 Declare the runner vars: pinned `github_runner_version`, the label list
      (`uhstray-lan`; the runner adds the rest), the shared runner group, the account
      name, `github_runner_windows_build_roots: ['D:\z']`, the toolchain manifest (D5:
      name, version, URL, SHA-256, arguments per tool), the licence edition, state and
      licensed node. Move `firewall_deny_egress` (and the other plane-wide vars: group,
      org, version) up to a parent group that both runner groups belong to, so Linux and
      Windows read one list rather than two copies; confirm with `ansible-inventory
      --host` that both Linux runners resolve an unchanged list afterwards.
- [ ] 3.4 Mirror the entry into `site-config/proxmox/vm-specs.yml`, and add a placeholder
      Windows entry to `platform/hypervisor/proxmox/vm-specs.example.yml` documenting the
      new fields.
- [ ] 3.5 Sync the inventory to Semaphore.
- [ ] 3.6 Validation gate: `ansible-inventory --graph` resolves the host with every
      declared var, and spec scenario **"The declaration is the allocation record"**
      holds for the Windows host.

## 4. Build the template, then provision and harden the host

- [ ] 4.1 Upload the evaluation ISO and the pinned VirtIO ISO to `SharedISOs` through the
      validated path (no hand upload outside the playbooks), with their digests recorded
      in the declaration.
- [ ] 4.2 Store the bootstrap administrator password for clones in the secret store
      (`store-ssh-password.yml` path) and generate the per-service key
      (`generate-service-ssh-key.yml`).
- [ ] 4.3 Run `provision-template.yml -e template_os=windows` targeting crabnebula from
      Semaphore. Confirm the template exists on crabnebula and the seed ISO is gone.
- [ ] 4.4 Run `provision-vm.yml` for `gh-runner-win-01`. Confirm the clone is local to
      crabnebula, has both disks, and comes up with its declared hostname and address.
- [ ] 4.5 Run `verify-host-access.yml`, `distribute-ssh-keys.yml`, `verify-host-access.yml`
      again, then `harden-ssh.yml`, then `apply-firewall.yml`.
- [ ] 4.6 From the host, attempt to reach the secret store, a hypervisor and the
      orchestrator; each must be blocked. Confirm the forge, the Rust distribution server
      and one toolchain vendor are reachable. From the network, enumerate the host's
      reachable ports.
- [ ] 4.7 Validation gate: spec scenarios **"Key access is proven before password access
      is withdrawn on Windows"**, **"Only SSH is reachable on the Windows host"** and
      **"The platform interior is unreachable from a Windows job"** verified against the
      live host, with the 4.6 output recorded.

## 5. Converge the build toolchain

- [ ] 5.1 Write `platform/playbooks/configure-windows-build-host.yml` (D5): download each
      manifest entry, verify its SHA-256 before executing it, install machine-wide,
      apply the machine settings table (NASM, Vulkan SDK and `VULKAN_SDK`, LLVM and
      `LIBCLANG_PATH`, `LongPathsEnabled`, Git `core.longpaths`, the `D:` volume and
      `D:\z` with modify rights for the runner account only, antivirus exclusions or a Dev
      Drive per 1.4, Git `bin` ahead of `System32`, automatic updates disabled).
- [ ] 5.2 Check in the Visual Studio Build Tools `.vsconfig` listing exactly the C++ build
      tools and Windows SDK components the jobs need, and install from a fixed-version
      bootstrapper declared in the manifest.
- [ ] 5.3 Add its Semaphore template, run it, then re-run it unchanged.
- [ ] 5.4 Smoke on the host as the runner account (not an administrator): `cl`, `link`,
      `cmake --version`, `nasm -v`, `perl -v`, `glslc --version`, `clang --version`,
      `pwsh -v`, `git --version`, `bash --version` (must report Git Bash, not WSL),
      `makeappx` and `signtool` located, `rustup --version`.
- [ ] 5.5 Run the playbook against a test fixture manifest (a copy with one wrong digest,
      selected by a test-only inventory, never by editing the declaration), confirm that
      install aborts and the installed version is unchanged. Keep the fixture and the
      command as a regression test.
- [ ] 5.6 Validation gate: spec scenarios **"Toolchain convergence is idempotent"** (the
      5.3 re-run reports no change) and **"A corrupted toolchain download aborts its
      install"** (5.5) verified.

## 6. Install and register the runner

- [ ] 6.1 Decide, by size, whether the Windows install lives in a branch of
      `deploy-github-runner.yml` or in a sibling playbook composed from the same
      credential task (`tasks/mint-github-runner-token.yml`). The credential part must be
      shared either way; record the decision in the playbook header.
- [ ] 6.2 Create the `ghrunner` account (not in `Administrators`, "Log on as a service"
      granted), its password generated if absent and stored in the secret store, never
      written to disk.
- [ ] 6.3 Install the pinned `win-x64` runner archive, verified against the checksum
      published for that release before extraction; abort on mismatch.
- [ ] 6.4 Configure with `--runasservice`, `--windowslogonaccount`, the declared name,
      labels and group, `--replace`, `--disableupdate`; the registration token and the
      account password supplied as `ACTIONS_RUNNER_INPUT_TOKEN` and
      `ACTIONS_RUNNER_INPUT_WINDOWSLOGONPASSWORD`, never as arguments. Install rustup for
      the runner account.
- [ ] 6.5 Render the job-completed hook (`.ps1`, D7): remove per-job work directories
      (keeping `_tool`, `_temp`, `_actions`), empty each declared build root and the
      account's `TEMP`, log every path it could not remove, write the quarantine marker
      when anything is left, and always exit 0. Render the job-started hook (D7): if the
      marker exists, retry the wipe; if anything is still left, fail the job before its
      first step, naming the quarantine.
- [ ] 6.6 Tests: the token never appears in an argument position; the hook empties the
      build roots on success and failure paths; the completed hook exits 0 and writes the
      marker when a path is locked; the started hook fails the job while the marker and the
      leftover remain, and passes once the retry succeeds.
- [ ] 6.7 Add or extend the Semaphore templates (deploy, de-register, verify), run the
      deploy from Semaphore, then re-run it unchanged.
- [ ] 6.8 Validation gate: spec scenarios **"A runner registers without an operator
      handling a token"** and **"Registration is re-runnable"** (from
      `add-github-actions-runners`) verified for the Windows runner, and the runner shows
      in the group with labels `self-hosted, Windows, X64, uhstray-lan`.

## 7. Prove it with real work

- [ ] 7.1 Extend the existing smoke workflow (the private repository chosen for the Linux
      plane) with a Windows job naming `[self-hosted, windows, x64, uhstray-lan]` that:
      prints the runner name; asserts the job is not elevated; asserts the secret store,
      a hypervisor and the orchestrator are unreachable; attempts a machine-wide install,
      an `HKLM` write and `Add-MpPreference`, asserting each fails; writes a marker into
      the workspace and into `D:\z`.
- [ ] 7.2 Run it twice; the second run asserts both markers are absent. Run a Linux job
      naming the plane label without the OS label and record where it lands, then confirm
      the Windows job never lands on a Linux runner.
- [ ] 7.3 On a `zerds` branch, set the job's `runs-on` to the self-hosted labels and gate
      the four machine-administration steps with
      `if: runner.environment == 'github-hosted'` (D11). Run `ci.yml` `windows-check`.
- [ ] 7.4 On the same branch, run a full `release.yml` dry run (`workflow_dispatch`,
      platforms `windows`, GPU on). Record: whether `dx bundle` fetched NSIS and WiX on its
      own; that the installer contains every GPU DLL; that `llama.dll`, `ggml-base.dll`
      and `ggml.dll` import `VCRUNTIME140.dll`; and that the shipped `ggml` CPU variant set
      matches a hosted build of the same commit (the AVX2 risk).
- [ ] 7.5 Record wall time per step against the hosted figures in the design, peak memory,
      and peak `D:` use, and write them into D2. Adjust the declaration if the evidence
      says so, and converge with `resize-vm.yml`.
- [ ] 7.6 Licence before production use: convert the evaluation to the licensed edition
      the owner chose, with the key from the secret store, and update the declaration's
      licence state. No tagged release runs on the evaluation edition.
- [ ] 7.7 Validation gate: spec scenarios **"The release build runs without machine
      administration"** (7.4), **"A job cannot change the machine"** (7.1),
      **"A release's target directory does not survive into the next job"** (7.2),
      **"A Windows job lands only on a Windows runner"** (7.2) and **"A Windows job cannot
      remove the denial"** verified with recorded run output.

## 8. Verification, operations and documentation

- [ ] 8.1 Extend `verify-github-runners.yml` (or its equivalent) to cover the Windows
      runner: registered, online, labels, and quarantine state from 6.5, with a
      remediation mode that reboots the host to release locks, re-runs the wipe and clears
      the marker only when it succeeds. Prove the quarantine with a test job that holds a
      file open from a background process.
- [ ] 8.2 Add the patch playbook (D9): drains first (removes the plane label through the
      organisation API, waits until no job is in progress), then stops the service,
      `win_updates`, reboots if needed, starts the runner, restores the label and verifies
      it. Test that a job queued during the drain is not picked up. Add its Semaphore
      template and run it once.
- [ ] 8.3 Add the monthly template rebuild and host rebuild as scheduled Semaphore
      templates (D3), add the runner-release check (bump `github_runner_version` within
      14 days of each upstream release, shared with the Linux pool), and record queue time
      for Windows jobs over the first month.
- [ ] 8.4 Update `platform/services/github-runner/CLAUDE.md`: the Windows member, its label
      contract, its isolation table (including the three things that do not hold, D10),
      the toolchain manifest, and the gotchas found in phases 1 to 7.
- [ ] 8.5 Update root `AGENTS.md` (playbooks in the Independent Workflows table, the new
      secret-store fields, the Windows branches of host access and firewall) and
      `README.md`.
- [ ] 8.6 Ask the user before opening the `zerds` pull request (user-gated, like every
      PR); on approval, open the one that makes the self-hosted runner the default via
      `vars.WINDOWS_RUNNER` (D11), with the hosted fallback documented in the workflow.
- [ ] 8.7 Validation gate: spec scenarios **"An incomplete wipe quarantines the runner"** (8.1),
      **"Verification detects a runner that is registered but not online"** for the
      Windows runner, and **"A licensed host is not placed off its licensed node"** (2.5,
      re-run against the real declaration) verified.

## 9. Review and promotion

- [ ] 9.1 Run the full local gate: ruff, shellcheck, ansible-lint, yamllint, pytest, BATS.
- [ ] 9.2 Run the mandatory pre-push audit as its own step (staged-diff greps, then
      `trufflehog git file://. --since-commit HEAD --only-verified --fail`).
- [ ] 9.3 Run `/simplify` and `/security-review` over the branch diff.
- [ ] 9.4 Commit and push; ask before opening the PR (user-gated).
- [ ] 9.5 On the go-ahead, open the PR into `dev`, wait for CodeRabbit and every check,
      address findings, merge with a merge commit through `scripts/merge-when-green.py`.
- [ ] 9.6 Ask before the `dev` to `main` promotion (user-gated). On approval, open it,
      wait for CodeRabbit and every required check to complete and pass, address
      findings, and only then merge with a merge commit.
- [ ] 9.7 Archive the change and retain one Hindsight memory into `agent-cloud-750a33b9`:
      the outcome labelled worked / dead end / corrected, which clone-identity path won in
      1.3, the measured release time, and any constraint found the hard way.
- [ ] 9.8 Validation gate: the change is on `main`, the Windows runner is online per
      verification, and spec scenarios **"Two hosts installed at different times match"**
      and **"The running version does not drift"** hold for the Windows runner (declared
      version, self-update disabled).
