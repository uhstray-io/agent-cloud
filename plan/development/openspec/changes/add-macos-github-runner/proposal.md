## Why

The owner has decided that the target state for every GitHub Actions job of the private
repository `zerds` is hardware the organisation controls, with GitHub's hosted runners
kept only as a fallback for when a self-hosted member is unavailable: Linux on the existing pool
(`add-github-actions-runners`), Windows on a Proxmox VM (`add-windows-github-runner`), and
macOS here. The owner's constraint for macOS is explicit: a virtual machine or GitHub's
hosted runners, **never a personal workstation**.

macOS is the largest share of the hosted cost by value. From the organisation's billing
API (2026-10-11), `zerds` consumed **987** hosted macOS minutes in August, **3,039** in
September and **3,328 in the first eleven days of October**, worth 61, 188 and 206 US
dollars gross at 0.062 dollars a minute. The net charge so far is zero because the plan's
included allowance absorbed it, but the October run rate is roughly 9,400 minutes and
580 dollars of gross value a month, between five and six times the Windows figure by
value for a similar number of minutes. macOS jobs are
also the repository's main quality gate: lint and test on every pull request, the
release feature-set clippy that gates a tag, the weekly heavy-feature check, the cache
warmer, and the release build with signing and notarisation.

macOS cannot join the Proxmox estate. Apple's software licence permits macOS virtual
machines only on Apple-branded hardware, and the platform's hypervisor nodes are x86
servers. A macOS execution surface therefore means owning Apple hardware.

The hosted macOS runner a private repository gets is **3 vCPU (M1), 7 GB RAM, 14 GB SSD**.
That size is why the release workflow deletes simulator runtimes and every Xcode but the
active one before bundling: the disk does not otherwise fit the build.

## What Changes

- **New: one dedicated Apple Silicon Mac mini on the platform network,** declared in the
  private inventory and managed as code over SSH like every other host. It is not a
  workstation: no one logs in to use it, and it runs nothing but the build plane.
- **New: one fresh macOS VM per job.** The host runs at most two macOS VMs at a time (the
  limit Apple's licence and its virtualisation framework both impose), each cloned from a
  golden image with Tart, registered with GitHub as a **just-in-time runner** that accepts
  exactly one job, and destroyed when that job ends. No job can see another job's files,
  processes, keychains or toolchain changes.
- **New: a golden image built as code.** A Packer template (Tart plugin) builds the image on
  the host from Apple's own restore image, with Xcode (latest stable, the Xcode 26 SDK the
  ScreenCaptureKit bridge needs), the Command Line Tools, Homebrew, CMake, rustup and the
  pinned runner preinstalled. Rebuilt on a cadence and on every Xcode bump.
- **Reused: the runner group, the label vocabulary and the application credential.** The
  VMs join `uhstray-selfhosted` with labels `self-hosted`, `macOS`, `ARM64`, `uhstray-lan`.
  The credential chain is the existing one with its last link swapped: an installation
  token is exchanged for a single-job just-in-time configuration instead of a
  registration token.
- **Reused: the egress-denial declaration.** VM traffic is confined by the host's virtual
  network (no route to private addresses), and the host's packet filter enforces the same
  `firewall_deny_egress` list as a second layer.
- **Changed posture, stated plainly:** inside the ephemeral VM the job's account has
  passwordless administrative rights, as it does on a hosted runner, because the release
  workflow selects Xcode, deletes extra Xcode installs and creates and sets a default
  keychain. That is acceptable only because the VM is destroyed after the job; the
  isolation boundary moves from "the job is not an administrator" to "the job's whole
  machine is discarded". The host itself grants jobs nothing.
- **Interim fallback: GitHub-hosted macOS runners stay available** behind a repository
  variable, exactly as for the Linux and Windows jobs, until the host is proven, and after
  that whenever the host is down.

This is the third of the three build-fleet changes, alongside `add-github-actions-runners`
(Linux x64) and `add-windows-github-runner` (Windows x64). They share one runner group,
one credential chain, one label vocabulary and one egress-denial declaration.

## Capabilities

### New Capabilities

None. The plane is `platform/github-actions-runners`.

### Modified Capabilities

- `platform/github-actions-runners`: adds requirements for a macOS member hosted on
  Apple hardware. Each job runs in a VM created for that job and destroyed after it; the
  VM image is built by code; the host is a declared, code-managed physical host; the
  number of concurrent VMs per host never exceeds the licensed limit; and the hosted
  fallback is one variable away. The existing requirements apply unchanged except where
  the spec delta states otherwise (administrative rights inside the disposable VM).

## Impact

- **Hardware purchase**: one Mac mini (owner decision; design D1 and Open Questions).
- **Physical placement**: a wired port on the platform network, power (ideally on a UPS),
  and an address from the declaration.
- **New service directory**: `platform/services/macos-runner/` with the supervisor (a
  launchd service and its script), the Packer template, and agent-facing context, following
  the service layout.
- **New playbooks**: host baseline and hardening for macOS, golden-image build, supervisor
  deploy, verify. Shared host-access playbooks gain a macOS branch where their Linux
  commands differ (for example, `pf` in place of UFW).
- **Shared library**: the existing App-token helper (`platform/lib/github_app_token.py`) is
  reused on the host; a small just-in-time configuration step is added beside it.
- **Secret store**: one new location for a macOS-only App private key (recommended,
  D4) and the host's administrative bootstrap.
- **Private inventory**: one host declaration with its address, sizing, labels, VM
  concurrency, image pins (macOS, Xcode, runner) and the shared egress list.
- **`zerds`**: the macOS jobs select the runner by repository variable, and their
  disk-freeing steps become no-ops in a VM sized for the build (a cross-repository task;
  the workflow is otherwise unchanged).

## Rollback Plan

Staged, each stage independently sufficient:

1. **Stop routing work.** Set the `zerds` repository variable that selects the macOS runner
   back to the JSON array `["macos-15"]`. Every macOS job runs hosted again, unchanged. No platform change.
2. **Withdraw availability.** Stop the supervisor service on the host. No new VM registers,
   any running job finishes, and the just-in-time runners vanish from the group on their
   own after their one job.
3. **Revoke identity.** Revoke the macOS App's installation; it is separate from the Linux
   and Windows App (design D4), so that stops only macOS. After that the host
   cannot register a runner even if the supervisor is running.
4. **Retire the host.** Wipe the Mac mini and remove its declaration. The hardware is a
   general-purpose computer with resale value; nothing in the estate depends on it.

No schema, no shared datastore, no change to any existing host. The durable artefacts are
the Mac mini, its image store, one secret-store location and one App installation.
