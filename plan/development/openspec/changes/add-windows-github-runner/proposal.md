## Why

The organisation's self-hosted execution plane (`add-github-actions-runners`) serves
Linux x64 only, and that change deliberately excluded Windows and macOS. The owner has now
decided that every GitHub Actions job of the private repository `zerds` runs on hardware the
organisation controls, so that the product's continuous integration and its releases do
not depend on hosted minutes. Linux is in progress on the existing pool. Windows has no
execution surface on the platform at all, so every Windows job (the compile check, the
cache warmer, and the two to three hour release build) still runs on a hosted runner.

Measured, from the organisation's billing API (2026-10-11): `zerds` consumed 851 hosted
Windows minutes in August, 1,798 in September, and 3,683 in the first eleven days of
October. All of it was absorbed by the plan's included allowance (net charge zero), so
the motive is not a bill paid today. It is that the trend is steep, that the release is
gated on a pool the organisation cannot size, and that the hosted Windows runner a private
repository gets is **2 vCPU and 8 GB**: the GPU release build already needed its timeout
raised from 120 to 180 minutes because it was killed at roughly 118 minutes of step time.

A second reason is control. The Windows release job today administers its own machine:
it installs packages with Chocolatey, edits the machine registry, adds antivirus
exclusions, and relies on a `D:` drive the hosted image happens to have. On a self-hosted
host those are exactly the actions a job must not be able to take, so moving the job is
also the moment to make the host's configuration a declared, reviewable thing instead of
something each job re-does.

## What Changes

- **New: one Windows x64 runner host, a Proxmox VM declared like every other host.** It
  joins the existing organisation runner group (`uhstray-selfhosted`) and is selected by
  label (`self-hosted`, `Windows`, `X64`, `uhstray-lan`). Same access list: the five
  private repositories, the public repository excluded, for the reasons recorded in the
  Linux change's D2.
- **New: a Windows VM template built once, as code.** The platform has only one template
  today, a node-local Ubuntu image built by an unattended ISO install. This change adds a
  second template built the same way: the Windows installation ISO plus a generated
  answer-file ISO, an unattended install, generalisation, conversion to a template. No new
  tool is introduced; it is the existing `provision-template.yml` pattern with an
  operating-system parameter.
- **New: Ansible manages a Windows host over OpenSSH, with the platform's existing key
  chain.** The administrative key pair lives in the secret store exactly as for Linux
  hosts, access is proven key-only before the bootstrap password is withdrawn, and the
  controller needs one added collection rather than a new transport library.
- **New: the build toolchain is host configuration, installed by a playbook.** MSVC Build
  Tools and the Windows SDK, CMake, Git for Windows, LLVM, NASM, Strawberry Perl, the
  Vulkan SDK, PowerShell 7, the WebView2 runtime, the VC++ redistributable, rustup, long
  paths enabled, a dedicated build volume mounted as `D:`, and antivirus exclusions for
  the build paths. All pinned, all re-runnable to convergence. A job installs nothing and
  edits nothing on the machine.
- **The runner runs as a Windows service under a dedicated, non-administrative local
  account.** Registration reuses the existing credential chain (application key in the
  secret store, minted on the orchestrator, registration token handed over in memory).
- **Each job's leftovers are destroyed by a host-configured hook**, including the build
  volume's short-path directory that the release job uses outside the workspace.
- **Egress denial reuses the existing declaration.** The same `firewall_deny_egress` list
  the Linux runners carry is enforced on Windows by the host firewall, through a Windows
  branch of the shared firewall operation rather than a runner-only script.
- **Cross-repository follow-up, not part of this change's diff:** the `zerds` workflows
  select the runner through a repository variable (the pattern its Linux jobs already
  use), and gate their machine-administration steps to hosted runners only. The
  label contract this change publishes is what those edits target.

This is one of three changes that together form the build-fleet plan:
`add-github-actions-runners` (Linux x64, in place), this change (Windows x64), and
`add-macos-github-runner` (macOS arm64, on Apple hardware). They share one runner group,
one credential chain, one label vocabulary and one egress-denial declaration.

## Capabilities

### New Capabilities

None. The execution plane is the capability `platform/github-actions-runners`
introduced by `add-github-actions-runners`.

### Modified Capabilities

- `platform/github-actions-runners`: adds requirements for a Windows member of the plane.
  Its host configuration is declared and applied by automation and never by a job; it is
  built from a template that is itself built by code; it is administered over key-only
  SSH; and its operating-system licence is a declared fact rather than an assumption. The
  existing requirements (private-only access, label selection, credential chain,
  revocation, isolation, egress denial, host-access baseline, declaration-only growth,
  verification, pinned version) apply to the Windows member unchanged, and the new spec
  delta states where the Windows mechanism differs.

## Impact

- **Template automation**: `provision-template.yml` gains an operating-system parameter
  and a Windows answer-file seed (rendered at build time, carrying no long-lived secret).
  The Ubuntu path is byte-identical when the parameter is absent.
- **VM provisioning**: `provision-vm.yml` gains a Windows branch for the post-boot step
  (it waits for `cloud-init` today, which a Windows guest does not run) and for a second
  data disk. `resize-vm.yml` and `proxmox-validate.yml` are reused; the latter learns to
  check for the Windows and VirtIO driver ISOs on the shared ISO storage.
- **Host access**: `verify-host-access.yml`, `distribute-ssh-keys.yml`, `harden-ssh.yml`
  and `apply-firewall.yml` gain a Windows branch each, selected by the host's declared
  operating-system family. No forked playbooks.
- **New playbooks**: a Windows build-toolchain playbook, and a Windows branch of
  `deploy-github-runner.yml` (or a sibling composed from the same credential task, if the
  branch would be larger than the shared part; decided in tasks phase 6).
- **Controller**: the `ansible.windows` collection is added to the collections
  requirements. The controller's ansible-core must be 2.18 or later, the first release
  that supports Windows over SSH as a non-experimental feature.
- **Secret store**: one new location for the runner account's generated password and the
  Windows administrative bootstrap password. The application key is reused unchanged.
- **Private inventory**: one host declaration, in the existing runner group or a sibling
  group carrying the Windows-specific vars.
- **Licensing**: one Windows Server licence decision, recorded in the declaration (see
  design, Open Questions).
- **No change to any workflow in this repository.** The `zerds` workflow edits are
  tracked as a cross-repository task.

## Rollback Plan

Staged, each stage independently sufficient, matching the Linux change:

1. **Stop routing work.** In `zerds`, set the repository variable that selects the Windows
   runner back to the hosted label (`"windows-latest"`). The admin steps that were gated to
   hosted runners run again there, so the workflow is complete on either surface. No
   platform change.
2. **Withdraw availability.** De-register the Windows runner. The Linux pool is untouched,
   because the runner group is shared but registration is per runner.
3. **Revoke identity.** Unchanged from the Linux change: revoking the application stops
   every runner in the group. Use stage 2 when only Windows must stop.
4. **Remove the host.** Destroy the VM and, if it is no longer wanted, the Windows
   template. Release the licence assignment so it can be reassigned. The template and
   provisioning branches stay in the code as dormant, tested paths; the Ubuntu path never
   depended on them.

No schema migration, no shared datastore, no change to an existing service's runtime. The
durable artefacts are the VM, the template, one secret-store location, and the licence
assignment.
