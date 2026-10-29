## 0. Owner decisions, branch and working ground

- [ ] 0.1 Get the owner's answers to design Open Questions 1 to 5 (purchase, CPU bin,
      placement, FileVault, separate App). Nothing below that spends money or touches
      the organisation's GitHub settings starts before 0.1.
- [ ] 0.2 Create or reuse one feature branch from `dev`, safely re-runnable:
      `git checkout dev && git pull && { git show-ref --verify --quiet
      refs/heads/feat/macos-github-runner && git checkout feat/macos-github-runner ||
      git checkout -b feat/macos-github-runner; }`, and confirm `make git-setup` has been
      run in the clone.
- [ ] 0.3 Read `add-github-actions-runners` and `add-windows-github-runner`. This change
      reuses their group, labels, credential helper and egress declaration and must not
      fork them.
- [ ] 0.4 Validation gate: the branch exists, the hooks path is `.githooks`, and
      `openspec validate add-macos-github-runner --store agent-cloud` passes.

## 1. Bootstrap the host and spike the unknowns

- [ ] 1.1 Write `platform/services/macos-runner/bootstrap/bootstrap.sh` (D7): idempotent,
      takes the management public key as its argument, enables Remote Login, installs the
      key, sets power settings, prints the hardware address. Place the Mac mini (wired,
      UPS), complete the three Setup Assistant screens, run the script, and record the
      exact screens in `platform/services/macos-runner/CLAUDE.md` as the one step that
      cannot be encoded without MDM.
- [ ] 1.2 Headless operation: install Tart and a vanilla VM through the first draft of the
      task 2.3 playbook (on this branch, launched from Semaphore's Dev variant, so the spike
      is re-runnable), run the VM from a launchd service with no logged-in session, reboot the host (with FileVault per 0.1,
      using an authenticated restart if FileVault is on), and record whether the VM comes
      back without a human. Record the exact keychain handling that worked. This answer
      may reopen Open Question 4.
- [ ] 1.3 Softnet: from a VM on `--net-softnet`, attempt the secret store, the
      orchestrator, a hypervisor, another VM and the host (each must fail), then GitHub,
      the Rust distribution server, Homebrew and Apple's notarisation endpoint (each must
      succeed). Record what the reachable gateway forwards to.
- [ ] 1.4 Image: build a VM from Apple's IPSW with a vendored Cirrus Labs Packer template
      and the Tart plugin, then install an operator-downloaded Xcode 26 archive. Record the
      build time and image size. If IPSW automation fails, record why and fall back to the
      Cirrus Labs vanilla base (D5).
- [ ] 1.5 Just-in-time runner: mint a configuration with the first draft of the task 5.2
      helper (a pytest-covered script on this branch, not an ad-hoc command), start the runner in the spike VM with it, dispatch a trivial
      job, and confirm the runner disappears from the group after the job.
- [ ] 1.6 Confirm the third-VM limit: start a third macOS VM while two run, and record the
      error.
- [ ] 1.7 Validation gate: every unverified item in the design (headless reboot, Softnet
      reachability, IPSW build, JIT lifecycle, the two-VM limit) is resolved in `design.md`
      with recorded output, and spec scenario **"A runner accepts only one job"** is shown
      achievable (1.5).

## 2. Bring the host under automation

- [ ] 2.1 Declare the host in the private inventory as `macos_runner_svc`: address
      (allocated like every host: free in the inventory, silent to ICMP, outside the DHCP
      range), `ansible_connection: ssh`, the hardware facts (model, cores, memory, disk),
      the Tart licence tier and core count (D9), slots, per-slot VM size, the pinned
      macOS, Xcode (version and archive SHA-256), runner, Tart, Softnet and Packer
      versions, the labels (`uhstray-lan`; the runner adds `self-hosted`, `macOS`,
      `ARM64`), and membership of the parent group that carries the plane-wide vars and
      `firewall_deny_egress`.
- [ ] 2.2 Generate the per-service key (`generate-service-ssh-key.yml`), distribute it,
      prove key-only access with `verify-host-access.yml` (macOS branch), then
      `harden-ssh.yml` (macOS branch).
- [ ] 2.3 Add `platform/playbooks/configure-macos-runner-host.yml`: supervisor account,
      power settings (restart after power loss, no sleep), automatic software update
      policy (download only, install by playbook), Tart, Softnet and its helper, Packer
      with the Tart plugin, Python with the `cryptography` version the helper needs, all
      pinned and digest-checked.
- [ ] 2.4 Add a `pf` branch to `apply-firewall.yml` (or a macOS sibling sharing its rule
      planner and guards): inbound SSH from `firewall_ssh_cidrs` only, the shared
      `firewall_deny_egress` list for the host's traffic, anti-lockout ordering as on Linux.
- [ ] 2.5 Tests for the `pf` branch: one block per declared denial, none when empty, the
      broad-denial guard fires.
- [ ] 2.6 Run 2.3 and 2.4 from Semaphore, then re-run both unchanged.
- [ ] 2.7 Add `platform/playbooks/bootstrap-macos-host.yml` as the single entry point that
      chains 2.2 to 2.4 for a new or replacement host, and its Semaphore template.
- [ ] 2.8 Prove the bootstrap path is repeatable: Erase All Content and Settings on the
      host, then Setup Assistant, `bootstrap.sh` and `bootstrap-macos-host.yml` again, and
      confirm the host converges to the same state with no other step.
- [ ] 2.9 Validation gate: spec scenario **"Bootstrap access is withdrawn only after key
      access is proven"** (from `add-github-actions-runners`) holds for the macOS host,
      both 2.6 re-runs report no change, the 2.8 erase-and-rebuild converged with no extra
      step, and the host's reachable ports from the network
      are SSH from the declared ranges only.

## 3. Build the golden image as code

- [ ] 3.1 Check in the Packer template under
      `platform/services/macos-runner/deployment/image/` (vendored from Cirrus Labs'
      templates at a pinned commit, with their licence notice), parameterised by the
      declaration: macOS IPSW, Xcode archive path and digest, runner version and checksum.
- [ ] 3.2 The template sets a random account password generated per build (never the
      upstream default), installs the supervisor's public key, the Command Line Tools,
      Xcode, Homebrew, CMake, rustup without a toolchain, and the pinned runner verified
      against its published checksum; enables passwordless sudo for the job account (D8);
      disables automatic updates.
- [ ] 3.3 Add `platform/playbooks/build-macos-runner-image.yml`: verifies the Xcode
      archive's digest on the host first, runs `packer build` on the host, names the image
      by its inputs, and leaves the current image untouched on failure. Add its Semaphore
      template.
- [ ] 3.4 Run the build against a test fixture declaration with a wrong Xcode digest
      (never by editing the real declaration), confirm it aborts before installing Xcode
      and the current image is unchanged. Keep the fixture as a regression test.
- [ ] 3.5 Validation gate: spec scenarios **"The image is rebuilt from code"** and **"An
      Xcode archive with the wrong digest aborts the build"** verified.

## 4. Deploy the supervisor

- [ ] 4.1 Write the supervisor under `platform/services/macos-runner/deployment/` (D3):
      per slot, clone the declared image to a unique name, boot headless on Softnet with
      the declared size, mint a just-in-time configuration (task 5.2), pass it to the
      runner in the VM over SSH standard input, wait for the runner to exit, stop and
      delete the VM, repeat. Delete the VM on every exit path, including a supervisor
      restart (on start, delete any VM it owns that is not in a live slot).
- [ ] 4.2 Refuse a declaration with more than two slots, naming the licence limit.
- [ ] 4.3 Log each slot's VM name, runner name, job outcome and lifetime where the verify
      playbook can read them. No credential in any log.
- [ ] 4.4 Add `platform/playbooks/deploy-macos-runner.yml`: place the App key (`0400`,
      owned by the supervisor account) from the secret store, render the supervisor's
      configuration and launchd service, start it, verify both slots register.
- [ ] 4.5 Tests: the slot-limit refusal; the JIT configuration never appears in an
      argument position on the host or in the guest (the runner's command line inside the
      VM, read while a job runs), and is gone from the guest runner's environment once it
      has started; a VM is deleted when the runner exits non-zero and
      when the supervisor is killed mid-job and restarted.
- [ ] 4.6 Validation gate: spec scenarios **"An over-limit declaration is refused"** and
      **"A cancelled job's machine is destroyed"** verified (4.5 plus a live cancellation).

## 5. Establish the macOS credential

- [ ] 5.1 If the owner chose a separate App (0.1): check in its App manifest (name, the
      single permission organisation self-hosted runners read and write, no webhook) and a
      playbook that renders the manifest-flow URL from it, so the App's definition is code.
      An organisation owner completes the one approval click GitHub requires to create and
      install an App (it cannot be done with an API token, by GitHub's design), then the
      key is stored at `secret/services/github-runner-macos` through the additive seeding
      path. (The key never transits a chat or a log.)
- [ ] 5.2 Add a just-in-time step beside `platform/lib/github_app_token.py`: given an
      installation token, the group id, a runner name and labels, call
      `generate-jitconfig` and print only the encoded configuration. pytest for the
      request shape and for a clear failure naming a missing input.
- [ ] 5.3 Converge the runner group with `manage-github-runner-group.yml` (unchanged
      access list) and record the group id the supervisor reads.
- [ ] 5.4 Validation gate: spec scenarios **"A job cannot read the application key"**
      (searched from inside a job, 6.1) and **"Revoking the macOS credential stops only
      macOS"** (suspend the macOS App's installation, confirm no macOS runner registers
      while a Linux job still runs, then restore) verified.

## 6. Prove it with real work

- [ ] 6.1 Extend the smoke workflow with a macOS job naming
      `[self-hosted, macos, arm64, uhstray-lan]` that: prints the runner and VM names;
      asserts the platform interior and the host are unreachable; searches the VM for the
      App key; writes a file, starts a background process, creates a keychain and deletes
      an application. Run it twice; the second run asserts all four are gone or restored.
- [ ] 6.2 On a `zerds` branch, set `runs-on` to the `MACOS_RUNNER` pattern (D10) with the
      self-hosted default, and run `ci.yml` (lint and test, release feature set) and
      `warm-cache.yml` `macos`.
- [ ] 6.3 Run a `release.yml` dry run (platforms `macos`), signing and notarisation
      included if the secrets are configured. Record each step's time against hosted, and
      the VM's peak memory and disk.
- [ ] 6.4 Build a second image with a changed input, smoke it in one slot, promote it by
      declaration, and confirm the other slot picks it up on its next VM.
- [ ] 6.5 Set `MACOS_RUNNER` to `["macos-15"]` on the branch and confirm the same jobs pass
      hosted with no other edit.
- [ ] 6.6 Validation gate: spec scenarios **"A macOS job cannot see the previous job's
      machine"** (6.1), **"Administration inside the VM does not reach the host"** (6.1),
      **"The platform interior is unreachable from a macOS job"**, **"Build egress from a
      macOS job still works"** (6.2, 6.3), **"A third job queues"** (three jobs dispatched
      at once) and **"Switching to hosted runners is one change"** (6.5) verified with
      recorded run output.

## 7. Verification, operations and documentation

- [ ] 7.1 Add `verify-macos-runner.yml`: both slots registered and idle or busy, image
      age and inputs, runner version against the 30-day window and any upstream release older than 14 days
      that the image does not carry yet, Tart licence core total
      against the free tier (warn at 80, D9), FileVault and power settings as declared.
- [ ] 7.2 Add the image rebuild as a scheduled Semaphore template (at most every 21 days,
      and run within 14 days of each runner release, D5), and a host
      update playbook that drains the slots before installing macOS updates.
- [ ] 7.3 Record queue time and slot utilisation over the first month; revisit slot size
      and the second-host question with the numbers.
- [ ] 7.4 Write `platform/services/macos-runner/CLAUDE.md` and `context/`: the first-boot
      exception, the slot model, the isolation table (D8, including what does not hold),
      the credential flow, the image rollout, the fallback, and gotchas from phases 1 to 6.
- [ ] 7.5 Update root `AGENTS.md` (new playbooks, `secret/services/github-runner-macos`),
      `README.md`, and the plane's label contract in
      `platform/services/github-runner/CLAUDE.md` so the three operating systems are
      documented in one place.
- [ ] 7.6 Ask the user before opening the `zerds` pull request (user-gated, like every
      PR); on approval, open the one that makes the self-hosted macOS runner the default
      via `vars.MACOS_RUNNER`.
- [ ] 7.7 Validation gate: spec scenarios **"Verification confirms a healthy plane"** and
      **"Verification detects a runner that is registered but not online"** (stop the
      supervisor) hold for the macOS member.

## 8. Review and promotion

- [ ] 8.1 Run the full local gate: ruff, shellcheck, ansible-lint, yamllint, pytest, BATS.
- [ ] 8.2 Run the mandatory pre-push audit as its own step, then trufflehog.
- [ ] 8.3 Run `/simplify` and `/security-review` over the branch diff.
- [ ] 8.4 Commit and push; ask before opening the PR (user-gated).
- [ ] 8.5 On the go-ahead, open the PR into `dev`, wait for CodeRabbit and every check,
      address findings, merge with a merge commit through `scripts/merge-when-green.py`.
- [ ] 8.6 Ask before the `dev` to `main` promotion (user-gated). On approval, open it,
      wait for CodeRabbit and every required check to complete and pass, address
      findings, and only then merge with a merge commit.
- [ ] 8.7 Archive and retain one Hindsight memory into `agent-cloud-750a33b9`: the outcome
      labelled worked / dead end / corrected, the headless-reboot answer, the measured
      release time, and any constraint found the hard way.
- [ ] 8.8 Validation gate: the change is on `main`, both slots are serving work per
      verification, and spec scenario **"The running version does not drift"** holds in
      the sense the image cadence gives it: every running VM reports the runner version
      declared for its image.
