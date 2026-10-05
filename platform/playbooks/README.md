# Playbooks

Ansible playbooks for deploying, updating, validating, and hardening agent-cloud services via Semaphore.

## Conventions

### Dry run, verify and results

Standard: [`plan/architecture/08-ansible-automation-standards.md`](../../plan/architecture/08-ansible-automation-standards.md).

- **Dry run is Ansible check mode**: `ansible-playbook --check`, or Semaphore's "Dry run"
  option, which passes `--check`. Do not add a `dry_run` variable. The three playbooks that
  still accept `-e dry_run=true` (`create-netbox-device.yml`, `cleanup-netbox.yml`,
  `manage-github-runner-group.yml`) are migrating to check mode.
- **Every task is in one check-mode class.** Read-only probes on modules without full
  check-mode support (`uri` GET, a reading `command`) carry `check_mode: false`; writes that
  cannot simulate carry `when: not ansible_check_mode`; modules with full support need
  nothing. Under `--check`, a `uri` task without `check_mode: false` is skipped.
- **Verify is `--tags verify`**, and changes nothing.
- **Machine-read results use `ansible.builtin.set_stats`** through
  `tasks/emit-step-result.yml`, not `debug`.

### Thin Wrappers

There are two deployment patterns in use:

**Legacy (thin wrapper):** Services that have not yet migrated to the composable pattern use a thin wrapper that imports `deploy-service.yml` with `target_service` set. This is required because the Semaphore version in use does not support `extra_cli_arguments`.

```yaml
# deploy-nocodb.yml (legacy pattern)
- name: "Deploy NocoDB"
  import_playbook: deploy-service.yml
  vars:
    target_service: nocodb_svc
```

**Composable (multi-phase):** Services migrated to the composable pattern have their own multi-phase playbook that uses the composable task library directly. NetBox is the reference implementation.

```yaml
# deploy-netbox.yml (composable pattern — abbreviated)
# Phase 1: Clone repo, manage-secrets.yml (OpenBao fetch/generate, Jinja2 templates)
# Phase 2: Run deploy.sh (container lifecycle only)
# Phase 3: Application bootstrap
# Phase 4: Diode credential sync
# Phase 5: Login endpoint verification
```

See `plan/architecture/01-automation-model.md` for the full composable pattern specification.

### Variable Sources

| Variable | Source | Notes |
|----------|--------|-------|
| `ansible_user` | Inventory (private) | Declare explicitly; individual playbooks may supply fallback values |
| `service_name` | Inventory per-host | e.g., `nocodb`, `openbao` |
| `monorepo_deploy_path` | Inventory per-host | Path within monorepo to deploy.sh |
| `monorepo_repo` | Inventory global | Git SSH URL |
| `openbao_addr` | Inventory `all.vars` | OpenBao API URL. Declared for every host, `localhost` included (production since 2026-09-25); an environment extra var of the same name overrides it, which the seed playbooks refuse as drift (`tasks/assert-bao-addr-declared.yml`). Isolated seed environments carry no extra vars |
| `bao_role_id` / `bao_secret_id` | Environment | AppRole credentials |
| `target_service` | Wrapper playbook vars | Inventory group name (e.g., `netbox_svc`) |

### Become (sudo)

`become` is **not** set in the inventory. Each playbook declares its own:
- `distribute-ssh-keys.yml` — `become: false` (writes to user-owned `~/.ssh/`)
- `harden-ssh.yml` — `become: true` (modifies `/etc/ssh/sshd_config`)
- `deploy-service.yml` — `become: false` (runs deploy.sh as the service user)
- `provision-vm.yml` — runs against the Proxmox API; its post-boot play logs in to the new VM over Ansible's own connection and uses `become: true` only to write the Semaphore runner environment and enable `semaphore-runner`, after resolving the sudo password

Before privileged tasks, use `tasks/resolve-become-password.yml` to read the
bootstrap sudo password from OpenBao. Disable automatic fact gathering when it
would escalate before this resolver. Do not introduce a second password copy in
Semaphore environment variables.

### Delegate Tasks

Tasks that run on the Semaphore runner (e.g., fetching keys from OpenBao, writing temp files) use `delegate_to: localhost` with explicit `become: false` — the runner container does not have sudo.

### Secrets

**No credentials, IPs, or usernames in playbooks.** All sensitive values come from:
- **Inventory** (private repo) — IPs, usernames, host vars
- **OpenBao** — SSH keys, API tokens, passwords (fetched at runtime via `community.hashi_vault`)
- **Semaphore environment** — AppRole credentials for OpenBao access

### SSH Keys

SSH keys are fetched from OpenBao at runtime and put on the runner only for the probe that
needs them, through the shared `tasks/materialise-ssh-key.yml`, and wiped by
`tasks/remove-ssh-key.yml` from `always` — so a failed probe does not leave the key behind.
Do not hand-roll `tempfile` + `copy` again: `tempfile` has no check-mode support, so that
pattern cannot pass a dry run (`platform/tests/test_materialise_ssh_key.py` holds the closed
list of files allowed to write a private key).

**Internal names cannot come from extra vars.** An extra var outranks anything a play
registers or sets, so `-e` could forge a probe result or a scratch path. A play whose gates or
cleanup read such names includes `tasks/refuse-var-overrides.yml` first, passing each name as
the loop variable `_rvo_name` (a loop variable, unlike an include var, is not outranked by
`-e`); it refuses any name an extra var holds. The materialise and pin tasks guard their own
internal names the same way.

```yaml
- name: "Fetch key"
  ansible.builtin.set_fact:
    _key: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/services/ssh:private_key', ...) }}"
  no_log: true

- name: "Probe with a runner-local copy of the key"
  block:
    - name: "Materialise the key (runner-local, 0600)"
      ansible.builtin.include_tasks: tasks/materialise-ssh-key.yml
      vars:
        ssh_key_content: "{{ _key }}"
        ssh_key_result_var: _probe_key

    # ... ssh -i {{ _probe_key.key }} ..., delegate_to: localhost, check_mode: false ...

  always:
    - name: "Remove the runner-local key"
      ansible.builtin.include_tasks: tasks/remove-ssh-key.yml
      vars:
        ssh_key_result_var: _probe_key
```

## Playbook Reference

### Deployment
| Playbook | Pattern | Purpose |
|----------|---------|---------|
| `deploy-service.yml` | Legacy | Generic deploy: clone monorepo, run deploy.sh, health check |
| `deploy-all.yml` | Legacy aggregate | Covers OpenBao, NocoDB, n8n, NetBox and NemoClaw through the legacy clone task; not a complete current-platform deployment or recovery path |
| `deploy-openbao.yml` | Legacy | Deploy OpenBao (self-bootstrapping, special case) |
| `deploy-nocodb.yml` | Legacy | Retained for retired NocoDB; decommissioning is separate, not a new composable migration |
| `deploy-n8n.yml` | Composable | Stateful-secret cutover guard, readiness-gated app/worker, verification and owner setup; use the service README for upgrades |
| `deploy-semaphore.yml` | Legacy | Deploy Semaphore (new VM only) |
| `deploy-netbox.yml` | Composable | Deploy NetBox (5-phase: secrets, containers, bootstrap, Diode creds, verify) |
| `recover-netbox-runtime.yml` | Dev-bound recovery | Default preflight reports each core service's start/recreate/noop action; explicit apply converges only existing NetBox core containers through Compose and verifies health |
| `deploy-nemoclaw.yml` | Legacy | Deploy NemoClaw |
| `deploy-orb-agent.yml` | Composable | Deploy Orb Agent (standalone: Diode creds + agent.yaml + start) |
| `deploy-uhhcraft.yml` | Composable | Deploy UhhCraft (5-phase: secrets, containers, post-deploy migrations, caddy fragment, verify) |
| `deploy-inference-comfyui.yml` | Composable | Deploy ComfyUI sidecar (GPU prereqs + secrets + containers + verify) |
| `deploy-inference-hunyuan3d.yml` | Composable | Deploy Hunyuan3D sidecar (GPU prereqs + weights check + secrets + containers + verify) |
| `store-postiz-api-key.yml` | Composable | Read the org's API key from postiz's own Postgres (minted by the app on the first authenticated request; the stored value IS the bearer token) and place it at `secret/services/postiz:postiz_api_key`, no_log throughout |
| `deploy-authentik.yml` | Composable | Deploy Authentik IdP (secrets → containers → blueprints assembled from inventory → live-state verify: every placed blueprint applied, declared accounts present/active/in-group, retired accounts gone → Caddy fragment) |
| `audit-authentik-retirements.yml` | Read-only | Count the live Authentik accounts named by private `*_legacy_username` declarations before a Dev blueprint deploy; prints counts, not usernames or credentials |
| `recover-authentik-audit-runtime.yml` | Dev-bound recovery | Default read-only preflight checks existing container identity, restart policy, and exact Postgres/Redis data volumes; explicit apply starts only the existing database, cache, and server, leaving the blueprint worker stopped for the retirement audit |
| `clean-deploy-netbox.yml` | Composable | Destructive: wipe volumes + fresh NetBox deploy |
| `clean-deploy-uhhcraft.yml` | Composable | Destructive: wipe volumes + fresh UhhCraft deploy |
| `rollback-inference-route.yml` | Composable | Take the inference gateway out of the public path or put it back, as code (`-e mode=gateway-config\|direct\|restore`): the previous gateway config, or `direct_<name>` copies of the current vLLM key published before Caddy is pointed at vLLM, then withdrawn on restore |

### Updates
| Playbook | Purpose |
|----------|---------|
| `update-service.yml` | Generic update: pull images, restart compose, health check |
| `update-nocodb.yml` | Update NocoDB |
| `update-n8n.yml` | Legacy generic update wrapper; use the composable `deploy-n8n.yml` and backup/restore runbook for current n8n |
| `update-semaphore.yml` | Update Semaphore |
| `update-netbox.yml` | Legacy generic update wrapper; current full service configuration uses `deploy-netbox.yml`, with Orb Agent deployed separately |
| `update-uhhcraft.yml` | Update UhhCraft |
| `update-inference-comfyui.yml` | Update ComfyUI sidecar |
| `update-inference-hunyuan3d.yml` | Update Hunyuan3D sidecar |

### SSH & Security
| Playbook | Purpose |
|----------|---------|
| `distribute-ssh-keys.yml` | Deploy SSH keys from OpenBao, verify key auth (no sudo) |
| `harden-ssh.yml` | Pre-hardening key-only login proof (refuses, nothing edited, on failure) + NOPASSWD sudo + sshd lockdown + post-lockdown verification (requires sudo) |
| `generate-service-ssh-key.yml` | Generate + store a per-service ed25519 key in OpenBao (never rotates); backs the pair up to site-config in the same run when `site_config_dir` is set |
| `backup-service-ssh-key.yml` | Copy an existing per-service keypair OUT of OpenBao into site-config on a NEW branch per run, cloning with the deploy key read from `secret/services/ssh/site-config`. Read-only against the store; refuses a mismatched pair and a differing existing file (`force_overwrite` to replace) |
| `backup-credentials-to-site-config.yml` | Copy named credential fields out of OpenBao into site-config on a NEW branch per run, pushed with the deploy key read from `secret/services/ssh/site-config` over pinned GitHub host keys (`tasks/site-config-clone.yml` + `site-config-push.yml`); no value ever reaches task output |
| `print-platform-user-credentials.yml` | GATED stopgap that PRINTS first-login passwords into task output (`-e i_understand_this_prints_secrets=true`); Semaphore cannot restrict it per-template, so prefer the backup playbook above |
| `verify-host-access.yml` | Prove KEY-ONLY SSH works BEFORE `harden-ssh.yml` withdraws password auth. Refuses to pass on password auth — a false green here is the lockout it exists to prevent |
| `apply-firewall.yml` | Default-deny inbound (SSH from admin CIDRs, service ports from their intended source, and `firewall_allow_groups`: one rule per member of an inventory group, from its `ansible_host`, refused unless every member's address is a single IPv4 address) plus optional declarative `firewall_deny_egress` for a semi-trusted host (each entry a single host unless `broad` with a `reason`, never a supernet of or equal to an SSH CIDR even when broad — containment computed by stdlib `ipaddress` in `ufw_egress_problems` — `port` when given one port 1-65535, `proto` tcp/udp/any; refused before any rule is added). Anti-lockout: SSH allows are added first and every rule precedes enable, then a fresh handshake is forced. Converges: the declared rule set is built once (`filter_plugins/ufw_rules.py`, the only definition of the add and delete command forms and the tags), and every rule it adds carries an `agent-cloud:` ufw comment tag naming its peer as ufw stores it (`192.0.2.10/32` and `192.0.2.10` are one rule and one tag; a rule an earlier version tagged in the declared spelling is re-tagged in place, never pruned for it). Each run reads `ufw show added` and `ufw status verbose` first and adds only the declared rules the host does not hold (same tag AND spec; a rule carrying a declared tag over another rule is kept, reported, and not counted), plus every rule naming no address, re-asserted each run because `show added` prints both IP-family halves as one line. A converged host costs those two reads plus one skipped add per rule naming no address — two per podman bridge on a rootful podman host (its 53/udp and 53/tcp DNS allows), one per rule from `any` — which ufw answers "Skipping adding existing rule" and the run reports as `changed=0`; only a host whose rules all name an address costs the two reads alone. When an add changed something it re-reads, and it refuses to delete while any declared rule is missing; it then deletes (by rule spec) the tagged rules the inventory no longer declares, planned from the rules as they stand after the adds. Untagged rules are kept and listed in the report; an untagged copy of a declared rule is tagged in place. The `agent-cloud:` comment prefix is reserved: a hand rule carrying an undeclared `agent-cloud:` tag is pruned. Never prunes an SSH allow from a declared CIDR, and prunes no rule that admits inbound SSH (read from the stored rule: a port list, range or missing port covering 22, over tcp or any protocol) unless `firewall_controller_cidr` is declared, compared in ufw's stored spelling; holds stale detected-port rules when detection finds nothing (`firewall_prune_when_detection_empty: true` overrides); after a delete, re-reads and asserts the declared SSH allows survived. Default policies and enable run only when the live state differs. Check mode reports would-add/would-delete/held and changes nothing |
| `issue-internal-leaf.yml` | Issue, remove or inspect ONE leaf certificate declared in site-config `internal_leaves`, on its consumer host; the key is generated on the consumer and never leaves it |
| `renew-internal-certs.yml` | Renew every declared leaf per consumer host when any has less than `renew_threshold` (default a third) of its lifetime left, reload once, and prove each renewed leaf is the one in use; runs daily as `Renew Internal Certs (Dev)` |
| `backup-step-ca-to-site-config.yml` | Copy the CA's certificates, encrypted keys and configuration into site-config `secrets/step-ca/volume/` on a NEW branch per run; the plaintext key password is not copied; output carries file names, counts and the pushed `backup/step-ca` branch name, never values |

### Secrets & Policies
| Playbook | Purpose |
|----------|---------|
| `check-secrets.yml` | Read-only secret inventory from OpenBao (present/missing/empty) |
| `validate-secrets.yml` | Active credential testing (DB, Redis, HTTP auth) |
| `seed-discovery-credentials.yml` | Copy/migrate discovery credentials to new vault paths |
| `seed-openbao-key.yml` | Merge ONE operator-held key into a secret path (created if absent, siblings preserved). Runs in its own isolated Semaphore environment; the value arrives as the encrypted `BAO_VALUE` input staged by `scripts/semaphore-seed-input.py`. `bao_verify_access_only=true` is the read-only access check |
| `seed-postiz-secrets.yml` | Merge the operator's social-platform credentials into `secret/services/postiz`. Runs in its own isolated environment; values arrive as encrypted `SEED_*` inputs staged by `scripts/postiz-seed-input.py`. `postiz_verify_access_only=true` is the read-only access check |
| `manage-semaphore-access.yml` | Hold the Semaphore project team, system admins and integrations to the private declarations `semaphore_project_members` and `semaphore_admin_users`: launch rights are runner rights. Check mode reports; a real run converges the team and reads it back; an undeclared admin or any integration fails by name |
| `provision-seed-environment.yml` | Give ONE seed template that declares `isolated_environment` its own environment holding an encrypted copy of the controller AppRole, then bind it. Controller loopback API only; wraps `platform/semaphore/provision-seed-environment.yml` |
| `provision-o11y-watcher-token.yml` | Mint the liveness watcher's read-only Grafana service-account token inside the Grafana container and store it at `secret/services/o11y:watcher_token`; a stored token Grafana still accepts is reused |
| `sync-secrets-to-openbao.yml` | Push VM-local secrets to OpenBao (recovery/migration) |
| `sync-netbox-secrets.yml` | Sync NetBox-specific secrets to OpenBao |
| `update-proxmox-token.yml` | Update Proxmox API token in OpenBao |
| `apply-openbao-policies.yml` | Apply all OpenBao policies from .hcl files |
| `apply-policy-orb-agent.yml` | Apply orb-agent policy |
| `provision-orb-agent-approle.yml` | Provision the dedicated orb-agent AppRole + policy (from `orb-agent.hcl`) and store creds at `secret/services/approles/orb-agent` |
| `apply-policy-semaphore.yml` | Apply Semaphore policy |
| `apply-policy-nemoclaw.yml` | Apply NemoClaw policy |
| `apply-policy-uhhcraft.yml` | Apply UhhCraft policy (reserved) |
| `apply-policy-inference-comfyui.yml` | Apply ComfyUI sidecar policy (reserved) |
| `apply-policy-inference-hunyuan3d.yml` | Apply Hunyuan3D sidecar policy (reserved) |

### Validation & Provisioning
| Playbook | Purpose |
|----------|---------|
| `validate-all.yml` | Health check all services (HTTP only, no SSH commands) |
| `verify-o11y-metrics-target.yml` | Dev-bound, read-only Prometheus receipt for one exact healthy target and one named exporter series; refuses an altered controller checkout |
| `drill-o11y-active-alert-delivery.yml` | Dev-bound production drill against already-active Grafana rules/contact; adds one failed scrape, reuses the named Discord receipt path, then removes only its scrape declaration and verifies recovery |
| `recover-o11y-active-alert-drill.yml` | Separate idempotent recovery for an interrupted active alert drill; clears its marker only after scrape, rule, and contact readback |
| `verify-o11y-production-budgets.yml` | Dev-bound read-only production receipt for Prometheus/Loki/Tempo retention, Alloy sample limit, active Prometheus series, and the Semaphore task ID |
| `survey-o11y-backup-readiness.yml` | Dev-bound, read-only aggregate survey of backup-capable storage and matching o11y VM artifact counts; does not select an artifact or prove immutability/restore |
| `survey-o11y-backup-artifact.yml` | Dev-bound, read-only artifact and restore-feasibility survey. Reports allow-listed source format/size/time/protection and disk-layout facts, whether source sizes and backup-inclusion flags are complete, whether one supported artifact candidate exists, and distinct active non-shared image-storage IDs on online nodes other than the source VM's node with Proxmox-reported capacity for the complete source disk layout and 30% remaining headroom. A current VMID candidate is reported only as available-now and is not reserved. No identifiers or selections are shown; physical disk/LVM/filesystem readiness is not assessed, and immutability/restore are never claimed |
| `reconcile-o11y-backup-job.yml` | Dev-bound Proxmox reconciliation that adds the declared o11y VM to one existing enabled explicit-VMID backup job, refuses alternate selectors or changed state, and verifies exact readback |
| `check-o11y-liveness.yml` | External liveness watcher (10-minute schedule from the Semaphore host): Grafana `/api/health` and Prometheus readiness through Grafana's datasource health API at the public front door; one Discord message on failure |
| `diagnose-o11y-grafana-auth.yml` | Dev-bound, read-only: classify Grafana's recent Authentik OAuth failures into one fixed category, never log text; requires the reviewed commit SHA |
| `probe-reachability.yml` | From a LAN host that is not the controller, TCP-probe declared ports of a target against `open`/`closed` expectations; a resolution or network error is a probe error, never `closed` |
| `run-agw-conformance.yml` | Send the same requests to the inference gateway and straight to vLLM from the gateway VM and report where they differ; keys travel as 0600 files, never arguments |
| `require-reviewed-checkout.yml` | Import first in a Dev-bound playbook: refuse a checkout other than `expected_repository_sha`, or one with uncommitted files. Read-only, runs under `--check` |
| `check-discovery.yml` | Read-only Docker incident evidence with exact revision/log-window guards; no GPS writes, restart or mint. Always refuses recovery acceptance; verify installed revision |
| `inspect-discovery-metadata.yml` | Controller-only allowlisted metadata read using existing runtime authentication and fixed loopback destination; no VM access or template writes |
| `cleanup-netbox.yml` | Clean up orphaned NetBox objects |
| `provision-vm.yml` | Clone Proxmox template, configure cloud-init, provision VM |
| `provision-template.yml` | Create Proxmox VM template with cloud-init |
| `proxmox-validate.yml` | Validate Proxmox cluster readiness (tolerates an offline node — a guest on a downed node returns no name), and report each online node's capacity (live use, configured guest commitment, free VM storage), most free memory first, for placing a new VM |
| `preflight-target-group.yml` | Assert a target group resolves and its hosts are reachable before a deploy touches them |
| `netbox-allocate-ip.yml` | Ask NetBox for free addresses and report the recorded state of named ones. Read-only unless `-e reserve=true`; reserving takes explicit static addresses and checks live pfSense DHCP configuration first |

The NetBox API endpoint comes from the single private `netbox_svc` host's
`service_url` for both report and reserve mode. The shared transport guard
allows only HTTPS or internal HTTP before credentials are read. Launch-time
`netbox_url`, `service_url`, and `_netbox_url` overrides are refused.
For reserve mode, private `netbox_svc` inventory declares `pfsense_dhcp_api_url`
and `pfsense_dhcp_interface`, selecting the router and interface that serve the
requested prefix. Certificate validation defaults to on; a site-owned router
may declare boolean `pfsense_dhcp_validate_certs: false` on its private NetBox
host while its certificate is being renewed. The playbook refuses launch-time
overrides of the router URL, interface, or TLS setting, and refuses a non-boolean
TLS value. It reads that interface's DHCP configuration
through the pfSense REST API on every reservation run; its API key comes from OpenBao's
`secret/services/discovery/pfsense:api_key`, shared with the discovery worker.
`reconcile-pfsense-api-key.yml` seeds that field from the fixed private
`site-config` backup through a Dev-bound Semaphore task. It preserves a
different live key until the replacement is verified, and never passes the
backup value as a task parameter. The reservation refuses missing or malformed data,
addresses in the primary or additional DHCP pools, and existing static mappings
before any NetBox write. The candidate must be a static IP outside DHCP's ranges.
The router URL must use HTTPS; certificate validation is the default and the
private exception applies only to this pfSense read. Restore validation once
the router certificate and executor trust are ready.
The singular DHCP endpoint selects the interface by `id` and checks the returned
`id`; pfREST may render the `interface` field as a display name. A failed TLS or API read
refuses the reservation. Verify that source with a read-only refusal run before
reserving production addresses.
Report mode does not contact pfSense and remains read-only.

### Service Deployment Workflow

Plan 15, change `service-deployment-workflow`. The step registry is
`platform/workflows/service-onboarding/registry.yml`; each executor and snapshot records one
step result through `tasks/emit-step-result.yml`. All of these run with `-e target_service=<group>`
except the collector and the custom-fields converger, and all are read-only except where noted.

| Playbook | Purpose |
|----------|---------|
| `lookup-service-inventory.yml` | Step lookup-inventory: the declared VM spec is complete, NetBox records the declared address as reserved or active, and no other host claims it. Takes `target_service` as the service name, like `provision-vm.yml` |
| `validate-address-free.yml` | Step validate-address: the address is not live in the pfSense ARP table (`skip` when every MAC the ARP table holds for the address is on a NIC of the service's own running VM), and the NetBox VM record exists: exactly one, in a Proxmox cluster discovery maintains. It writes that record, `planned`, in the Proxmox cluster discovery maintains, with the scoped `vm-recorder` token |
| `snapshot-service-assessment.yml` | The one input to the service assessment: the committed compose services (image and ports, no environment), running containers, practices |
| `snapshot-firewall.yml` | The one input to the firewall assessment: listening sockets, ufw state, published container ports, the declared firewall vars |
| `snapshot-access.yml` | The one input to the role and access assessment: Authentik app-catalog entries, blueprints and OpenBao policy files for the service |
| `verify-service-persistence.yml` | Step systemd-enablement: every container has restart policy `always` (podman's boot unit starts nothing else), or `"no"` when it is a declared one-shot (`agent-cloud.one-shot` label) that exited 0; rootless podman has linger and its boot unit; rootful podman (`podman_rootful: true`) has the system `podman-restart.service`, and a container off `always` passes only when the service's enabled `agent-cloud-boot-<service>.service` names it (read back from the unit file). Fails on an empty container selection. A service running from outside the monorepo declares `compose_working_dir` |
| `verify-service-health.yml` | Step service-validate: HTTP 200 from the declared `health_url`, or `service_url` + `health_path`. Probed from the executor, or from the host itself when the host sets `health_probe_on_host: true` (for a port published on loopback only or firewalled to the Caddy host). A probe that cannot connect records "answered -1"; one whose module could not run at all records "answered no response" |
| `ensure-service-persistence.yml` | The setup half of systemd-enablement: linger plus podman's user boot unit (rootless), the system `podman-restart.service` (rootful) or `docker.service` (Docker). A rootful service whose containers are off `always` (chosen by policy; a declared one-shot on `"no"` excepted) also gets `agent-cloud-boot-<service>.service`, a oneshot `podman start <names>` unit, enabled and never started, removed once none needs it; a rootless one is refused by name. The Ubuntu 24.04 podman (4.9.3) cannot change a restart policy in place. Boot settings only; restarts nothing. An empty or failed container listing fails the run |
| `inspect-host-containers.yml` | Read-only: the containers on exactly one host, per engine (the connecting user's and any declared `linger_user`'s rootless podman, rootful podman, Docker), with the engine version, each container's state, restart policy and compose working directory; for onboarding a host with no service group and for finding which engine and account own a service's containers. The inspect format is passed as a `command` argv, because production Semaphore's Ansible re-templates `environment:` values |
| `inspect-service-runtime.yml` | Read-only Semaphore diagnostic for one populated `*_svc` inventory group; reports existing container names, states, exit codes, and restart counts, including stopped containers |
| `provision-netbox-custom-fields.yml` | Converge the workflow's NetBox custom fields to their declaration through the Django shell. Writes, and refuses to retype a field |
| `collect-service-conformance.yml` | The ONLY writer of workflow status: per-template Semaphore history (newest 1000 tasks each; a full window is reported, and marks the services it can hide) → newest result per service and step, with the status NetBox already holds merged underneath so an older result is kept everywhere → NetBox custom fields (scoped view/change-VM token) and Loki. Declared on a 15-minute schedule on its base template, which production does not have until `dev` is promoted (`main` has no collector); the `(Dev)` copy is unscheduled and launched by hand. Its dry run is the read-only failure report |

### Infrastructure
| Playbook | Purpose |
|----------|---------|
| `install-docker.yml` | Install Docker CE from official repo (idempotent) |
| `install-qemu-guest-agent.yml` | Install `qemu-guest-agent` on existing VMs (idempotent); refuses a VM without the Proxmox guest-agent channel. Enable that channel first with `resize-vm.yml`, which converges `agent=1` (a running VM needs `allow_reboot=true` to pick it up) |
| `resize-vm.yml` | Converge a live VM's cores/memory/disk and its guest-agent option (`agent=1`, per-host opt-out `vm_agent: false`) to its declaration; grow-only disk, opt-in reboot, and a run without `allow_reboot` is a safe preview |
| `install-podman.yml` | Install Podman + podman-compose (idempotent); optional `podman_docker_cli` adds the `docker` CLI shim for consumers that shell out to a docker binary |
| `deploy-github-runner.yml` | Install + register one self-hosted GitHub Actions runner. Registration token minted on the CONTROLLER — the host is firewalled away from OpenBao by design |
| `manage-github-runner-group.yml` | Converge the org runner group's repository access list as code. REFUSES to run if any declared repo is public. Read-only unless `-e dry_run=false` |
| `grow-o11y-root.yml` | Grow the o11y receiver's LVM ext4 root into its virtual disk online (partition, PV, LV, filesystem); refuses any other layout, plan-only under `--check` |
| `reboot-host.yml` | Reboot ONE declared host and wait for it; refuses unless the group holds exactly one host named in `confirm_reboot` |
| `mount-caddy-certs.yml` | Add a read-only `/etc/caddy/certs` volume to the central Caddy's hand-maintained compose file (after proving Caddy runs from `caddy_compose_dir`); recreate only on change, restoring the previous file on failure |

### Local-Dev Conventions (Phase 0A, `plan/archive/development/LOCAL-DEV-DEPLOYMENT.md`)

- **Path vars:** playbooks reference `local_monorepo_dir` (clone location) and `local_home_dir` (convenience-symlink base) with `/home/{{ ansible_user }}` defaults — unset means byte-identical prod behavior; local inventories override them for macOS paths.
- **Compose overlay:** `lib/common.sh`'s `compose()` appends `compose.local.yml` only when `LOCAL_MODE=true` **and** the overlay file exists; covered by `platform/tests/test_common.bats`.

### Local-Dev Bootstrap

`bootstrap-local-dev.yml` (run with `--tags bootstrap`; the only playbook
permitted to run unorchestrated — Critical Rule #1 bootstrap exemption via
`_bootstrap_play: true` + the tag) provisions the local control plane:
persistent-file OpenBao + local AppRole + `LOCAL_FAKE_` fixture seeds, a pinned single-container
Semaphore (SQLite), an automatically-created API token, project resources, and
the full template catalog (`templates.yml` + `templates-local.yml`).
State is under `~/.agent-cloud-local/` (including `credentials.env` and local
OpenBao initialization material). See [`docs/LOCAL-DEV.md`](../../docs/LOCAL-DEV.md).

### Composable Task Library

These reusable tasks are the building blocks for all playbooks. See `plan/architecture/01-automation-model.md` for the full architecture. (That content
used to live in `AUTOMATION-COMPOSABILITY.md`, which is now under `plan/archive/`.)

| Task | Status | Purpose |
|------|--------|---------|
| `tasks/manage-secrets.yml` | Implemented | Fetch/generate secrets from OpenBao, template env files via Jinja2 |
| `tasks/manage-approle.yml` | Implemented | Create/update AppRole + HCL policy, store credentials in OpenBao |
| `tasks/manage-diode-credentials.yml` | Implemented | Create fresh Diode orb-agent OAuth2 credentials via NetBox plugin API |
| `tasks/deploy-orb-agent.yml` | Implemented | Start privileged orb-agent with vault-integrated agent.yaml config |
| `tasks/clean-service.yml` | Implemented | Destroy containers, volumes, runtime dir, and clone for full rebuild |
| `tasks/clone-and-deploy.yml` | Legacy | Clone monorepo, symlink, run deploy.sh, health check (used by legacy services) |
| `tasks/apply-openbao-policy.yml` | Implemented | Apply a single OpenBao policy from an .hcl file |
| `tasks/seed-discovery-credential.yml` | Implemented | Copy/update one credential set at a discovery/* vault path |
| `tasks/update-vault-field.yml` | Implemented | Read a vault secret, update a specific field, write back |
| `tasks/run-migrations.yml` | Implemented | Generic goose migration runner; container or host execution |
| `tasks/install-nvidia-toolkit.yml` | Implemented | NVIDIA Container Toolkit + CDI for Podman on GPU hosts |
| `tasks/install-podman-compose.yml` | Implemented | Verify podman + `podman compose` / `podman-compose` is callable (Linux apt / macOS Homebrew) |
| `tasks/place-monorepo.yml` | Implemented | Put the monorepo on the target (clone in prod, copy the working tree in local-dev) — the shared Phase-1 preamble |
| `tasks/enable-linger.yml` | Implemented | Linger plus podman's user boot unit, so rootless `restart: always` containers survive a reboot. Included by `place-monorepo.yml`; optional `linger_user` for a dedicated service account |
| `tasks/assert-bao-transport.yml` | Implemented | Refuse to send secret material over public cleartext. Included by every play reaching OpenBao, and by other token-receiving endpoints via `_assert_url_label` |
| `tasks/assert-seed-inputs-declared.yml` | Implemented | Refuse, before the AppRole login, a seed run whose environment carries a seed input (`BAO_VALUE`, `SEED_*`) its template does not declare. Lists variable names only. Included by both isolated seed playbooks |
| `tasks/assert-bao-addr-declared.yml` | Implemented | Refuse, before the AppRole login, an OpenBao address other than the one the run's inventory file declares under top-level `all.vars` (an extra var would override it). Included by both isolated seed playbooks. Catches drift, a plain stray address; it does not stop a templated extra var, so launch and environment-edit permission is the boundary (`docs/MISTAKES.md` 1.15) |
| `tasks/assert-bao-seed-access.yml` | Implemented | Prove, from the token's own capabilities and without reading a value or writing, that it may seed one KV-v2 path: `create` for a missing path, `patch` for an existing one. Names the missing capability. Included by both isolated seed playbooks |
| `tasks/wait-for-apt.yml` | Implemented | Wait for cloud-init and the dpkg lock on a freshly provisioned host, so an install right after provisioning does not fail on a transient lock |
| `tasks/site-config-clone.yml` | Implemented | Clone site-config into a scratch dir on a fresh `<prefix>-<UTC>-<6hex>` branch with the deploy key the caller read from OpenBao — written 0600 inside that dir, `IdentitiesOnly`, pinned GitHub host keys |
| `tasks/site-config-push.yml` | Implemented | Stage ONE path, commit if changed, push the branch, report names and counts — never values. Pairs with the clone task; the caller wipes the scratch dir in an `always:` |
| `tasks/backup-ssh-key-to-site-config.yml` | Implemented | Write one SSH keypair into the site-config clone (0600/0644), idempotent, refuses to clobber a differing key. The single implementation shared by the generator and the backup playbook |
| `tasks/materialise-ssh-key.yml` | Implemented | Put ONE SSH private key on the runner for an `ssh -i` probe: a 0700 scratch dir holding the key at 0600 plus the `known_hosts` path every probe passes as `UserKnownHostsFile`, so no run writes the runner's own, result fact of paths only, only the write `no_log`. Runs under `--check` too (the runner-scratch class, plan 08), so dry runs still test key auth. The single implementation used by `distribute-ssh-keys`, `harden-ssh` and `verify-host-access` |
| `tasks/remove-ssh-key.yml` | Implemented | Wipe what `materialise-ssh-key.yml` made. Include it from the `always:` of the same block (an include cannot carry `always`); refuses any directory the materialise task did not create |
| `tasks/pin-ssh-host-key.yml` | Implemented | Read the target's own SSH host key over the existing Ansible connection and write it (`[host]:port` off 22) into a materialised scratch `known_hosts`, so every probe runs `StrictHostKeyChecking=yes`. Semaphore disables Ansible host-key checking, so the pin binds a probe to the host this run reached, not to a long-lived trust root. Used by `distribute-ssh-keys`, `harden-ssh` and `verify-host-access` |
| `tasks/distribute-ca-root.yml` | Implemented | Distribute the internal CA root to a host's trust store |
| `tasks/distribute-caddy-site.yml` | Implemented | Place a per-service Caddy site fragment (composable Caddy model) |
| `tasks/mint-internal-cert.yml` | Implemented | Mint a certificate from the internal step-ca |
| `tasks/issue-internal-leaf.yml` | Implemented | Issue ONE declared leaf from the internal CA with its key generated on the consumer; entered through `mint-internal-cert.yml` when `_mint_name` is set |
| `tasks/manage-cloudflare-record.yml` | Implemented | Create/update one Cloudflare DNS record |
| `tasks/registry-login.yml` | Implemented | Authenticate the container engine to a registry |
| `tasks/resolve-become-password.yml` | Implemented | Resolve the bootstrap sudo password from OpenBao before privileged tasks; leave sanitized status visible |
| `tasks/emit-step-result.yml` | Implemented | Record one workflow step result per include with `set_stats` (aggregated `step_results` list, last one also as `step_result`), so a run covering several steps records each; runs in check mode too. Refuses, recording nothing, when any of its inputs arrives as an extra var — an extra var outranks the include vars the executor computes, so `-e step_result_status=pass` would forge a failed step's result (it checks whether each input NAME is in `hostvars`, which holds extra vars but not include vars, so a templated extra var that renders differently per task cannot slip past; `workflow_id` stays an extra var by design). The seven input names (`step_result_*`, `_step_result_record`) are therefore RESERVED: pass them only as include vars, never as an inventory var (site-config included), `set_fact` or `register`, which `hostvars` also holds and the task refuses alike |
| `tasks/list-service-containers.yml` | Implemented | The containers one service's compose project created, by the compose `working_dir` label (project names are not stable per service): the monorepo deploy path, or `compose_working_dir` for a service still running from a legacy directory; listed as root when `podman_rootful: true`. A read |
| `tasks/netbox-api-headers.yml` | Implemented | NetBox API headers for a stored token: `Bearer` for a v2 `nbt_` token, `Token` for a legacy v1 one |
| `tasks/assert-local-discovery-scope.yml` | Implemented | Confine discovery to local-dev: the target allowlist is observed from the engine's networks, and no targets means discovery is disabled |
| `tasks/bao-merge-keys.yml` | Implemented | Merge keys into one OpenBao KV-v2 path with merge-patch (siblings preserved, create with CAS, write verified) |
| `tasks/push-loki-lines.yml` | Implemented | The one direct push to Loki's push API; the caller decides with the required `push_loki_must_succeed` whether a non-204 fails the run |
| `tasks/require-tmpfs-remote-tmp.yml` | Implemented | Prove a writable tmpfs for Ansible's remote temp with `raw` only, before any module is copied to a host whose root may be full |
| `tasks/agw-probe.yml` | Implemented | The one gateway probe path: a `uri` call to the gateway's published port, presenting a client leaf when listener TLS is on |
| `tasks/agw-probe-resolution.yml` | Implemented | Resolve the gateway server leaf's SAN on the probing host: the interim marked hosts line, or the internal zone once `agw_internal_dns_authoritative` is declared |
| `tasks/agw-legacy-key-check.yml` | Implemented | Shared-key retirement check: record the shared vLLM key's fingerprint once at `legacy_shared_key_sha256` and compute whether it has been rotated away; prints nothing |
| `tasks/assert-orchestrated.yml` | Implemented (unwired) | Critical Rule #1 as code: refuse deploys outside a Semaphore environment; bootstrap exemption requires `_bootstrap_play: true` + `--tags bootstrap`. Wiring blocked on marker verification (`plan/archive/development/LOCAL-DEV-DEPLOYMENT.md` §11) |

Planned tasks (not yet implemented):

| Task | Purpose |
|------|---------|
| `tasks/sparse-checkout.yml` | Sparse-clone monorepo for specific service paths |
| `tasks/setup-runtime-dir.yml` | Create ~/services/<name>/, symlinks to clone |
| `tasks/run-deploy.yml` | Execute deploy.sh from runtime dir (passes CLONE_DIR) |
| `tasks/verify-health.yml` | Health check a service endpoint with retry/backoff |
| `tasks/write-secret-metadata.yml` | Write KV v2 custom metadata after secret store |
| `tasks/rotate-credential.yml` | Generic Create-Verify-Retire rotation wrapper |
| `tasks/revoke-service-credentials.yml` | Revoke AppRole secret_id + delete Hydra clients |

## Adding a New Service

For the complete onboarding checklist (7 phases, all tiers), see `plan/architecture/02-service-onboarding.md`.

**Quick reference for the composable pattern (preferred for all new services):**

1. Create `platform/services/<name>/deployment/` with `deploy.sh` (container-lifecycle only), `compose.yml`, and `templates/*.j2`
2. Define `_secret_definitions` and `_env_templates` for the service
3. Create `platform/playbooks/deploy-<name>.yml` using composable tasks: `manage-secrets.yml` -> deploy.sh -> verify
4. Create `platform/playbooks/clean-deploy-<name>.yml` using `tasks/clean-service.yml`
5. Add host to site-config inventory with `service_name`, `monorepo_deploy_path`, `service_url`
6. Add Semaphore templates to `platform/semaphore/templates.yml`, run `setup-templates.yml`
7. Generate SSH key pair, store in OpenBao, run `distribute-ssh-keys.yml`, and confirm the
   pair reached site-config — `generate-service-ssh-key.yml` writes it there when passed
   `site_config_dir`, and the `Back Up Service SSH Key` template copies an existing pair out. A key
   that lives only in OpenBao cannot be used from a workstation, so the operator half of
   the two-path access proof cannot be performed and the host cannot safely be hardened
8. Optionally provision a dedicated AppRole via `tasks/manage-approle.yml`

**Legacy pattern (for services not yet migrated):**

1. Add the service to the inventory (private repo) under `agent_cloud` with `service_name` and `monorepo_deploy_path`
2. Create `platform/services/<service>/deployment/deploy.sh` (idempotent, sources `../../lib/common.sh`)
3. Create `deploy-<service>.yml` wrapper (import `deploy-service.yml` with `target_service: <service>_svc`)
4. Create `update-<service>.yml` wrapper (import `update-service.yml`)
5. Create Semaphore task templates pointing at the wrapper playbooks
6. Generate an SSH key pair, store in OpenBao at `secret/services/ssh/<service>`
7. Run `distribute-ssh-keys.yml` to deploy the key to the VM

Note: The legacy pattern is maintained for backward compatibility. All new services should use the composable pattern.

## Dependencies

Declared in `collections/requirements.yml` (auto-installed by Semaphore):
- `community.hashi_vault` — OpenBao/Vault lookups
- `ansible.posix` — `authorized_key` module
