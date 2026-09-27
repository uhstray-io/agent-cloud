# A production internal CA: step-ca on its own host

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Decisions by Joe, 2026-09-27: the
production internal CA is step-ca on its own small VM; Caddy re-encrypts to the
agentgateway listeners with mutual TLS; vLLM on the DGX Spark head serves HTTPS with a
certificate from this CA.

Companion changes: agent-cloud `inference-gateway-agentgateway` (the first consumer; its
task group 6 configures the gateway and Caddy side of the transport and depends on this
change) and the dgx-spark vLLM TLS change (the vLLM side, owned by the dgx-spark
repository and session, which receives a handoff from this one).

## Why

The inference path is encrypted only at the edge. Caddy terminates the public TLS
connection and then talks to the gateway, and the gateway talks to vLLM, in cleartext
over the LAN. The operator decided on 2026-09-27 that every hop on that path is to be
encrypted and that the gateway must know that its caller is Caddy. Both need
certificates for internal names that no public CA will issue, which means an internal CA
in production.

The platform already has one, but only in local-dev:

- `platform/services/step-ca/` is a complete composable service (compose file, overlay,
  `deploy.sh`, `env.j2`) with a stable root and intermediate auto-initialized into the
  `step-ca-data` volume, and `platform/playbooks/deploy-step-ca.yml` deploys it. It is
  declared only in `platform/inventory/local-dev.yml.example` (`step_ca_svc`, lines 53-64)
  and only local templates exist (`platform/semaphore/templates-local.yml` lines 25 and
  124); `platform/semaphore/templates.yml` has no step-ca template, and the production
  inventory in site-config has no `step_ca_svc` group and no step-ca entry in
  `proxmox/vm-specs.yml` (checked 2026-09-27 on the site-config branch
  `feat/agentgateway-host`).
- Issuance is same-host only. `platform/playbooks/tasks/mint-internal-cert.yml` lines 3-7
  say so directly: every step is an `exec` or `cp` against the local container, and
  "cross-host reuse ... would need the exec/cp steps delegated to the CA host — out of
  scope today". The same holds for the root-distribution task
  `platform/playbooks/tasks/distribute-ca-root.yml` (lines 11-12).
- The local task mints the key inside the CA container and copies it out
  (`mint-internal-cert.yml` lines 58-85). That is acceptable when CA and consumer share one
  laptop; across hosts it would move private keys through the CA host and the controller.
- The only leaf is a year-long wildcard re-minted on each Caddy deploy
  (`deploy-step-ca.yml` lines 66-103 raise the provisioner's maximum to `8760h`). Nothing
  renews a certificate on a schedule and nothing alerts on expiry.
- The platform's own architecture document still recommends a directive that Caddy
  2.11.4 deprecates: `plan/architecture/05-platform-infra.md` line 483 shows
  `tls_trusted_ca_certs`, and Caddy's v2.11.4 source logs "The 'tls_trusted_ca_certs'
  field is deprecated. Use the 'tls_trust_pool' field instead."
  (`modules/caddyhttp/reverseproxy/caddyfile.go` lines 1209-1210 at tag v2.11.4). The
  platform's Caddy image is 2.11.4 (`platform/services/caddy/deployment/compose.yml`
  line 12).

## What Changes

- **A dedicated CA host.** One small VM declared in site-config's `proxmox/vm-specs.yml`
  and a `step_ca_svc` group in the production inventory, brought up through the service
  deployment workflow's steps (lookup, address check, provision, cloud-init, SSH keys and
  their backup, hardening, firewall, persistence). No Caddy route, no public hostname, no
  Cloudflare record: the CA is not a web service.
- **step-ca deployed to production through Semaphore** with the existing playbook and
  service files, parameterized by inventory: the CA API bound to the host loopback only,
  the ACME provisioner off (nothing in production validates by ACME), a dedicated issuing
  provisioner separate from the bootstrap one, and production leaf lifetimes that are
  short rather than a year. Secrets stay where the service already keeps them: the key
  password in `secret/services/step-ca`, the encrypted keys in the volume.
- **Cross-host issuance, key stays on the consumer.** The issuance task generates the
  private key and a certificate request on the consumer host, signs the request inside
  the CA container on the CA host, and writes the certificate back. Only the request (to
  the CA) and the certificate (from it) cross the network; both are public. The same task
  serves local-dev, where the CA host and the consumer are the same machine, so there is
  one implementation, not a local one and a production one.
- **Declared leaves.** Every production leaf is declared in inventory with its consumer
  host, profile (server or client), names and reload behaviour. The first set: Caddy's
  client certificate for the gateway, the gateway's server certificate for its API and UI
  listeners, and vLLM's server certificate on the head node (issued for dgx-spark through
  a handoff).
- **Root distribution to every consumer.** The public trust bundle (root plus
  intermediate, as `distribute-ca-root.yml` already builds it) is read from the CA host
  and placed on each consumer: Caddy's trust-pool file, the gateway's container mount,
  and the dgx-spark handoff.
- **Scheduled renewal and expiry alerting.** A Semaphore template on a schedule declared
  as code renews every declared leaf that is inside its renewal window, confirms the
  running process now presents the new certificate, and writes one line per leaf to Loki;
  Grafana alerts when any leaf is close to expiry or when the renewal job goes silent.
- **Backup of CA material.** A playbook copies the CA's encrypted keys, certificates and
  configuration from the volume into site-config on a new branch per run, following the
  existing credential-backup channel; the key password travels separately through the
  existing credential backup. A restore drill in local-dev proves the root survives.
- **Firewall.** The CA host accepts SSH from the controller and the declared operator
  ranges and nothing else; the CA's own port is never published beyond the loopback.
- **Documentation.** The deprecated Caddy directive in `05-platform-infra.md` is replaced
  with `tls_trust_pool file` and a mutual-TLS row is added; stale pointers to the archived
  internal-CA plan are corrected; the service's architecture note gains a production
  section; the "plain HTTP on the internal network" default policy gains a recorded
  exception for the inference path.

## Capabilities

### New Capabilities
- `platform/internal-ca`: a production internal certificate authority that issues,
  distributes, renews, monitors and backs up certificates for internal names, reached
  only through the orchestrator.

### Modified Capabilities
- None in the main specs. The companion change `inference-gateway-agentgateway` consumes
  this capability for its transport-security tasks.

## Impact

- agent-cloud files: `platform/playbooks/tasks/mint-internal-cert.yml` (cross-host,
  consumer-side key), `platform/playbooks/tasks/distribute-ca-root.yml` (cross-host read),
  `platform/playbooks/deploy-step-ca.yml` (production parameters, issuing provisioner),
  `platform/playbooks/clean-deploy-step-ca.yml` (confirmation guard), new
  `renew-internal-certs.yml` and `backup-step-ca-to-site-config.yml`,
  `platform/semaphore/templates.yml`, `platform/services/step-ca/deployment/templates/env.j2`
  (new parameters only if the existing ones do not suffice),
  `platform/services/step-ca/context/architecture.md`, o11y `templates/alerts.yml.j2`,
  BATS for the tasks, `plan/architecture/05-platform-infra.md`, root `CLAUDE.md` rows.
- site-config: a `vm-specs.yml` entry, the `step_ca_svc` group with firewall variables,
  the declared leaf list, and the backup branches the playbooks push.
- OpenBao: `secret/services/step-ca` gains the issuing provisioner's password beside the
  existing `init_password`. No key material enters OpenBao.
- Live: one new VM; one Caddy change (a read-only certificate directory mount and the
  transport block, applied by the companion change); one gateway redeploy; a handoff to
  dgx-spark; one new scheduled template.
- Out of scope, recorded: ACME issuance in production (no consumer needs it; the archived
  plan's dns-01 path stays future work); an offline root (open question 2); trusting the
  production root on workstations or browsers (no browser reaches an internal name on
  this path).

## Rollback Plan

- Consumers first. Each consumer's internal TLS is gated by inventory: Caddy's inference
  blocks return to a plain `http://` upstream, the gateway's listeners and its model
  entry return to plain configuration, and a redeploy of each through Semaphore restores
  the cleartext LAN path that runs today. The dgx-spark side reverts on its own schedule;
  the gateway accepts either while its model entry is switched back.
- Renewal: disable the schedule in `templates.yml` and re-run `setup-templates.yml`;
  issued leaves remain valid until they expire, which bounds the window the consumer
  rollback must fit in (the leaf lifetime, design decision 6).
- The CA: once no consumer depends on it, remove the VM as code with `destroy-vm.yml`
  (a Semaphore template that refuses unless the live VM matches its declaration and the
  launch names the vmid). The site-config backup keeps the root, so a later
  redeploy restores the same trust anchor instead of minting a new one.
- A bad root (compromise or loss): the gated clean deploy mints a new root; every
  consumer then re-runs its deploy to pick up the new bundle and a new leaf. This is a
  deliberate reset, never an automatic step.
