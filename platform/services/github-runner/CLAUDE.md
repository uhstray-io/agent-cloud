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

Exactly five, all private: `zerds`, `atlas`, `zerds-website`, `weft`,
`scientific-business`. They are the declared access list of the `uhstray-selfhosted`
runner group (`visibility: selected`, `allows_public_repositories: false`).

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

**Per-job containerisation is not a control here.** The container-hook mechanism manages
containers for a job that *declares* one; it does not place an undeclared job into a
container. A smoke job requesting nothing reported `ISOLATION=host`. Enforcing it for
every job needs an ephemeral runner whose own process and filesystem are containerised and
recreated per job — a lifecycle change, not a setting.

**Practical consequence:** a job can read whatever `ghrunner` can read on that host. Do not
place anything on a runner host that all five permitted repositories are not entitled to.

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
| `deploy-github-runner.yml` | Install, register, start. Idempotent: a re-run leaves an existing registration intact. |
| `manage-github-runner-group.yml` | Converge the group's repository access list. Read-only unless `-e dry_run=false`. |
| `netbox-allocate-ip.yml` | Ask the address authority for free addresses. Read-only unless `-e reserve=true`. |

Host lifecycle reuses the platform's existing playbooks unchanged: `provision-vm.yml`,
`verify-host-access.yml`, `distribute-ssh-keys.yml`, `harden-ssh.yml`,
`apply-firewall.yml`, `install-podman.yml`.

## Declaration

`github_runner_svc` in the private site-config inventory. **The declaration is the
allocation record**, including the address — deliberately, not as a shortcut. The IPAM
system's discovery pipeline has written nothing since 2026-04-23
(`plan/development/04-netbox-discovery.md`), and a hand-written entry inside a stale store
gets trusted more than it deserves. Once discovery is repaired it registers these hosts
itself, and IPAM becomes a cross-check rather than a second record to maintain.

Key vars: pinned `github_runner_version` / `github_runner_hooks_version` (an upgrade is a
declaration change and a review, never something that happens because time passed),
`github_runner_labels`, `github_runner_group`, `github_runner_account`,
`podman_docker_cli: true`, and `firewall_deny_egress`.

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
- **A fresh VM holds the dpkg lock.** cloud-init is still doing its own apt work;
  `tasks/wait-for-apt.yml` waits for it.
- **`playbook_dir` is the controller's checkout.** Correct for `template:` (read on the
  controller), wrong for anything executed on the host — the two are not interchangeable.

## Known limitations

1. Per-job containerisation is not enforced (above), and neither is process reaping: a
   job may leave a process running after it ends. Both have the same fix — an ephemeral
   runner whose process and filesystem are containerised per job. A best-effort reaper is
   deliberately not shipped: one that cannot tell a leftover process from the runner's own
   is more dangerous than the gap it closes.
2. Both hosts are on one hypervisor node. The cluster has no shared image storage and the
   only Ubuntu template is node-local, so Proxmox refuses a cross-node clone. Node
   separation needs a second template.
3. The pool is fixed at two. Growth is a declaration plus a run; there is no autoscaling.
