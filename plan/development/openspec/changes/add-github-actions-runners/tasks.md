## 0. Branch and working ground

- [ ] 0.1 Create the single feature branch for this whole change from `dev`:
      `git checkout dev && git pull && git checkout -b feat/github-actions-runners`.
      All work below lands on this one branch — one `feat → dev` PR and one
      `dev → main` promotion, per the user's explicit instruction to avoid extra
      branches and premature PRs.
- [ ] 0.2 Confirm `make git-setup` has been run in this clone (repo-local
      `merge.ours.driver` and `core.hooksPath`), so the graph artifact merges
      cleanly and the secret-scanning hooks are active.
- [ ] 0.3 Validation gate — `git branch --show-current` reports
      `feat/github-actions-runners`, `git config core.hooksPath` reports `.githooks`,
      and `openspec validate add-github-actions-runners --store agent-cloud` passes.

## 1. Settle the container runtime question before anything installs

- [ ] 1.1 On a scratch host (or a throwaway VM on crabnebula), install rootless podman
      via `platform/playbooks/install-podman.yml` and enable the rootless
      Docker-compatible API socket for an unprivileged user with lingering enabled.
- [ ] 1.2 Download `actions/runner-container-hooks` v0.8.1 and run its docker hook
      against that rootless socket with a trivial job payload. Record verbatim what
      works and what fails — in particular container create, exec, volume mount of the
      work directory, and cleanup.
- [ ] 1.3 Write the outcome into `design.md` under D4 as a resolved decision (replacing
      the "unverified" wording) — either "rootless podman compatibility socket works,
      platform norm holds" or "rootless podman insufficient because <verbatim failure>;
      this host declares the Docker engine, which the platform already supports for
      NetBox for the same class of reason".
- [ ] 1.4 If podman is insufficient, confirm `platform/playbooks/install-docker.yml`
      covers the runner host's needs unchanged; if it does not, extend it generically
      rather than adding a runner-only step.
- [ ] 1.5 Validation gate — the container hook demonstrably forces a job payload into a
      container on the chosen runtime with no workflow-side request, proving spec
      scenario **"Isolation does not depend on the workflow asking for it"** is
      achievable on the chosen engine, and D4 in `design.md` no longer says
      "unverified".

## 2. Close the shared-mechanism gaps (before any host depends on them)

- [ ] 2.1 Extend `platform/playbooks/apply-firewall.yml` with an optional declarative
      egress-denial list (`firewall_deny_egress`: entries of `to`, `port`, `proto`,
      `comment`), emitting `ufw deny out` rules. Default empty. Document it in the
      playbook's header var block alongside the existing vars.
- [ ] 2.2 Make the egress rules idempotent and ordered so they are applied before UFW is
      enabled, and guard the management path: refuse a denial whose target is equal to or
      **broader than** any declared SSH CIDR. Note the inverted-scope trap recorded as
      M-005 in `docs/MISTAKES.md` — the intended targets all sit *inside* the SSH CIDR, so
      a guard phrased as "covers an SSH CIDR member" rejects every legitimate declaration.
      Since the orchestrator installs no `ansible.utils` (hence no `ipaddr` containment
      filter), implement it as: a denial target must be a single host unless the entry
      explicitly opts into a broader mask.
- [ ] 2.3 Add BATS coverage in `platform/tests/` for the egress extension: rules emitted
      for a declared list; **no** rule emitted and byte-identical behaviour when the var
      is absent (regression guard for every existing service host); the management-path
      assertion aborts on a self-locking declaration.
- [ ] 2.4 Extend the chosen container-runtime playbook to enable the Docker-compatible
      API socket for a named unprivileged user, as an opt-in var (default off), reusing
      `platform/playbooks/tasks/enable-linger.yml` for the session lingering rather than
      re-implementing it.
- [ ] 2.5 Add BATS coverage for the socket extension, including the default-off case.
- [ ] 2.6 Prove the no-op WITHOUT touching a live service host. Running the shared
      firewall playbook against an existing host is the single highest-lockout-risk
      action in this change, and the user's standing instruction is that no machine
      loses access — so the regression is held by the default-empty structural test
      plus a first run against the *runner* host (itself new, and reachable
      out-of-band via the hypervisor console if anything goes wrong). Diff that host's
      rule set before and after declaring its egress list.
- [ ] 2.7 Validation gate — the new BATS tests pass alongside the existing suite
      (`platform/tests/`), the no-op run in 2.6 produced a zero diff, and the mechanism
      needed to satisfy spec scenarios **"The secret store is unreachable from a job"**
      and **"Legitimate workflow egress still works"** now exists in the shared
      playbook rather than in any runner-specific script.

## 3. Allocate the addresses and declare the hosts

- [ ] 3.1 Query NetBox — the platform's address authority — for the next unallocated
      addresses in the management prefix, through automation run from Semaphore (which
      holds OpenBao access), not an ad-hoc API call from a workstation. If no read-only
      path exists, add one as a playbook; do not mint a token by hand.
- [ ] 3.2 Confirm or replace the two candidate addresses identified during planning
      (their concrete values live in the private site-config inventory, never in this
      public repo) with what the authority reports free, and record both in NetBox as
      allocated to `gh-runner-01` / `gh-runner-02`.
- [ ] 3.3 Declare both hosts in the private site-config inventory as a
      `github_runner_svc` group, carrying the allocation fields the existing
      inventory-first provisioning reads (`vm_vmid` 216/217, `vm_node` crabnebula /
      alphacentauri, `vm_name`, `vm_cores` 8, `vm_memory` 16384, `vm_disk` "200G",
      `vm_ip`, `vm_gateway`, `vm_nameserver`, `vm_netmask`, `vm_net_bridge`,
      `vm_disk_storage`, `vm_tags`) — with a comment recording *why* those sizes and
      nodes, as the postiz declaration does.
- [ ] 3.4 Declare the runner-specific vars on the same group: pinned
      `github_runner_version`, runner group name, the label list, the runner account
      name, `container_engine`, the Docker-socket opt-in, `firewall_ssh_cidrs`, and the
      `firewall_deny_egress` list covering the secret store, every hypervisor management
      interface, and the orchestrator. No inbound `firewall_allow_rules` — the host
      publishes no service port.
- [ ] 3.5 Mirror the two entries into `site-config/proxmox/vm-specs.yml` so a workstation
      run stays consistent with the inventory declaration, and update
      `platform/hypervisor/proxmox/vm-specs.example.yml` with a placeholder runner entry
      documenting the schema for the next person.
- [ ] 3.6 Sync the updated inventory to Semaphore so the runner can see the new group.
- [ ] 3.7 Validation gate — `ansible-inventory --graph` resolves the new group with all
      declared vars, and spec scenario **"Address allocation comes from the authority"**
      holds: both addresses are recorded allocated in NetBox and neither was chosen by
      inspection.

## 4. Provision and harden runner host 01

- [ ] 4.1 Run `provision-vm.yml` for `gh-runner-01` (target the crabnebula declaration).
      Confirm it resolves the spec from inventory, and that a cross-node full clone from
      the template on alphacentauri completes.
- [ ] 4.2 Run `store-ssh-password.yml` (bootstrap credential into OpenBao via the
      encrypted-env-secret path), then `generate-service-ssh-key.yml` for the host.
- [ ] 4.3 Run `verify-host-access.yml`, then `distribute-ssh-keys.yml`, then
      `verify-host-access.yml` again — key auth must be proven before anything is
      withdrawn.
- [ ] 4.4 Run `harden-ssh.yml`, then `apply-firewall.yml` (inbound default-deny with
      administrative access only, plus the egress denials from 3.4).
- [ ] 4.5 Create the dedicated unprivileged runner account with no sudo entry and
      lingering enabled, and install the chosen container runtime with its
      Docker-compatible socket, all via the playbooks — nothing by hand on the host.
- [ ] 4.6 From the host, attempt to reach OpenBao, a hypervisor management interface, and
      the orchestrator; each must be refused or dropped. Then confirm the forge and a
      package source are reachable.
- [ ] 4.7 Validation gate — spec scenarios **"No service port is exposed"**,
      **"Bootstrap access is withdrawn only after key access is proven"**, **"The secret
      store is unreachable from a job"**, **"The hypervisor and orchestrator are
      unreachable from a job"**, and **"Legitimate workflow egress still works"** all
      verified against the live host, with the 4.6 command output recorded.

## 5. Establish the organisation identity and the runner group

- [ ] 5.1 Create the organisation-installed GitHub App with only *Organization
      self-hosted runners: read & write*, install it on `uhstray-io`, and generate a
      private key. (Operator action — the key must never transit a chat or a log.)
- [ ] 5.2 Store the app id, installation id, and private key at
      `secret/services/github-runner` using `seed-openbao-key.yml`-style additive
      placement, with the value supplied as a launch-time encrypted environment secret,
      not a Semaphore-persisted survey var.
- [ ] 5.3 Add `platform/lib/github_app_token.py`: reads app id, installation id, and the
      PEM from environment or stdin; signs the RS256 assertion using the cryptography
      library already present as a transitive dependency; exchanges it for an
      installation token; prints only the token to stdout. Never accepts the key on
      argv. If the library is absent, fall back to an `openssl`-based signature rather
      than adding a controller dependency.
- [ ] 5.4 Add pytest coverage for the helper: assertion header and claim shape, expiry
      bounds, refusal when the key is passed on argv, and non-zero exit with a message
      naming the missing input when a credential is absent.
- [ ] 5.5 Add `platform/playbooks/tasks/mint-github-runner-token.yml` — a `no_log: true`
      credential task that calls the helper, exchanges the installation token for a
      registration token, and returns it in memory only. Scope `no_log` to this task
      only; the surrounding deploy, waits, and verification must stay loggable.
- [ ] 5.6 Add `platform/playbooks/manage-github-runner-group.yml` — converges the
      organisation's runner group to a declared name and repository access list (adds
      what is declared, **removes** what is not), reading the list from configuration.
      Support `-e dry_run=true` for a safe diff preview, mirroring
      `create-netbox-device.yml`.
- [ ] 5.7 Declare the access list as configuration: exactly `zerds`, `atlas`,
      `zerds-website`, `weft`, `scientific-business`. `agent-cloud` is absent, with a
      comment stating why (public repo; fork PRs; see design D2).
- [ ] 5.8 Inspect the organisation's current runner groups with the new credential
      before the first convergence, so a removal is not a surprise (design Open
      Question 2), then run the convergence.
- [ ] 5.9 Validation gate — spec scenarios **"Access is enumerable"** (the group's
      access list is the finite declared five) and **"A missing organisation credential
      fails loudly"** (the minting task fails with a message naming the missing
      credential and leaves nothing half-configured) both verified.

## 6. Install, register, and isolate the runner on host 01

- [ ] 6.1 Create `platform/services/github-runner/deployment/` following the existing
      service layout: install/lifecycle script handling the runner archive only, and
      `templates/*.j2` for the runner environment file and the hook scripts.
- [ ] 6.2 Install step: fetch the pinned runner version, read the published checksum for
      that release from the release metadata, verify the archive before extracting, and
      abort on mismatch. No hand-copied constant.
- [ ] 6.3 Configure the runner unattended with `--url` the organisation, the minted
      registration token supplied over stdin (never argv), `--name` from the
      declaration, `--labels` from the declaration, `--runnergroup` from the
      declaration, `--replace`, and `--disableupdate`.
- [ ] 6.4 Install the runner as a lingering user-session service under the unprivileged
      runner account, and template its environment file with
      `ACTIONS_RUNNER_CONTAINER_HOOKS` (per-job containerisation) and
      `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` (workspace and container/volume wipe).
- [ ] 6.5 Write the job-completed hook script so it always runs to completion —
      including on job failure and cancellation — and removes the job workspace plus the
      job's containers and volumes.
- [ ] 6.6 Add `platform/playbooks/deploy-github-runner.yml` composing the existing
      pattern: `manage-secrets` → mint token (task from 5.5) → place monorepo → install
      and register → verify. Add `clean-deploy-github-runner.yml` and
      `deregister-github-runner.yml` (de-registers one runner without destroying the
      host).
- [ ] 6.7 Add BATS coverage for the install/lifecycle script: checksum mismatch aborts
      without extracting; the token never appears in an argv-visible position; the hook
      script removes the workspace on both success and failure paths.
- [ ] 6.8 Add Semaphore templates in `platform/semaphore/templates.yml` for deploy,
      clean-deploy, de-register, verify, group-convergence, and token-mint, then run
      `setup-templates.yml`.
- [ ] 6.9 Run the deploy from Semaphore (not a workstation), then re-run it unchanged.
- [ ] 6.10 Validation gate — spec scenarios **"A runner registers without an operator
      handling a token"**, **"Registration is re-runnable"** (the re-run in 6.9 left
      exactly one runner with unchanged name, labels, and group), and **"A corrupted
      download aborts the install"** all verified.

## 7. Prove it end to end with real work

- [ ] 7.1 Choose the private repository that will carry the verification workflow
      (design Open Question 1) and confirm the choice with the user.
- [ ] 7.2 Add a `workflow_dispatch` smoke workflow there whose job names the plane's
      labels and: prints the runner name and OS; asserts it is **not** running as a
      privileged user; asserts the secret store, a hypervisor interface, and the
      orchestrator are unreachable; writes a marker file into the workspace; reaches one
      legitimately-permitted destination.
- [ ] 7.3 Run it. Confirm it is dispatched to `gh-runner-01`, that every assertion
      passes, and that its steps ran inside a container despite the workflow declaring
      no container of its own.
- [ ] 7.4 Run it a second time and assert the marker file from the previous run is
      absent.
- [ ] 7.5 From `agent-cloud` (public), add a temporary `workflow_dispatch` job naming the
      plane's labels, confirm it is **not** picked up by any runner, then remove it. This
      is the exclusion test — it must be run, not assumed.
- [ ] 7.6 Validation gate — spec scenarios **"A private repository's workflow is
      dispatched to the plane"**, **"A job cannot see the previous job's leftovers"**,
      **"Isolation does not depend on the workflow asking for it"**, **"A job cannot
      escalate to host administration"**, and **"The public repository cannot reach the
      plane"** all verified with recorded run output.

## 8. Stand up runner two by declaration only

- [ ] 8.1 Freeze the automation: record the current commit of every playbook, task,
      template, and script touched above. Any edit needed from here to make runner two
      work is a declaration-only violation, and the fix belongs in the automation, not in
      a per-host special case.
- [ ] 8.2 Run the same sequence for `gh-runner-02` (alphacentauri, id 217): provision →
      credentials → key distribution → access verification → harden → firewall →
      runtime → deploy/register, all from Semaphore.
- [ ] 8.3 Confirm no automation file changed between 8.1 and 8.2 (`git diff` over the
      frozen set is empty). If it did, fix the automation generically and re-run from
      8.1.
- [ ] 8.4 Re-run the smoke workflow enough times to land on both runners, or target each
      explicitly, confirming both serve work with identical behaviour.
- [ ] 8.5 De-register `gh-runner-02`, confirm `gh-runner-01` keeps serving work, then
      re-register it — proving withdrawal is per-runner and reversible.
- [ ] 8.6 Validation gate — spec scenarios **"A second runner is added without changing
      the automation"** (zero diff in 8.3), **"A single runner is withdrawn"**, and
      **"Labels survive a rebuild"** all verified.

## 9. Verification, convergence, and documentation

- [ ] 9.1 Add `verify-github-runners.yml`: read-only, reports each declared runner's
      registered / online / label state from the organisation, and flags label drift by
      naming declared versus actual. Add its Semaphore template.
- [ ] 9.2 Prove its failure modes: power off one host and confirm it reports not-online;
      change one runner's labels out of band and confirm it reports the mismatch. Restore
      both.
- [ ] 9.3 Confirm `resize-vm.yml` converges a runner host from a changed declaration and
      is a no-op against a matching one.
- [ ] 9.4 Write `platform/services/github-runner/CLAUDE.md` (deployment, credential
      flow, isolation model, egress posture, exclusion of the public repo and why) and
      `context/` for agent use, matching the depth of the postiz and honcho service docs.
- [ ] 9.5 Document the label contract for workflow authors — the exact `runs-on` value,
      which repositories may use it, what the runner does and does not provide, and that
      job steps run containerised. Put it where a workflow author in another repository
      will actually find it.
- [ ] 9.6 Update the root `AGENTS.md`/`CLAUDE.md`: new service doc pointer, the new
      playbooks in the Independent Workflows table, the new
      `secret/services/github-runner` row in the secrets layout, and the new firewall
      egress capability.
- [ ] 9.7 Update the top-level `README.md` for the new service.
- [ ] 9.8 Validation gate — spec scenarios **"Verification confirms a healthy plane"**,
      **"Verification detects a runner that is registered but not online"**,
      **"Verification detects label drift"**, and **"Declared size is convergeable"** all
      verified, with 9.2's evidence recorded.

## 10. Review and promotion

- [ ] 10.1 Run the full local gate: ruff, shellcheck, ansible-lint, yamllint, the pytest
      suite, and the BATS suite. All green.
- [ ] 10.2 Run the mandatory pre-push audit as its own step — the staged-diff greps for
      private addresses, secret shapes, and machine paths, then
      `trufflehog git file://. --since-commit HEAD --only-verified --fail`. Review the
      output before committing.
- [ ] 10.3 Run `/simplify` and `/security-review` over the branch diff and address what
      they surface.
- [ ] 10.4 Commit and push `feat/github-actions-runners`. **Stop.** Ask the user before
      opening the PR — opening one is user-gated, every time.
- [ ] 10.5 On the user's go-ahead, open the PR into `dev`. Wait for CodeRabbit's review
      to **complete** (rate-limited is not reviewed) plus all CI checks, address every
      finding, push fixes, and confirm the checks pass again.
- [ ] 10.6 Merge into `dev` with a merge commit (never squash — it preserves `dev`↔`main`
      ancestry), then validate on `dev`.
- [ ] 10.7 Ask before opening the `dev` → `main` promotion PR; on approval, open it, wait
      for CodeRabbit and all checks, then merge with a merge commit.
- [ ] 10.8 Archive the change (`openspec archive add-github-actions-runners --store
      agent-cloud`) and retain one Hindsight memory into `agent-cloud-750a33b9`: the
      outcome labelled worked / dead end / corrected, the container-runtime spike's
      actual answer, and any constraint discovered the hard way.
- [ ] 10.9 Validation gate — the change is on `main`, both runners are online per
      `verify-github-runners.yml`, and spec scenarios **"Two hosts installed at
      different times match"** and **"The running version does not drift"** are verified
      (both hosts report the declared pinned version with self-update disabled).
