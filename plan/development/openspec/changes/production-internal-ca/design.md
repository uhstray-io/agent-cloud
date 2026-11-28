# Design: a production internal CA

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

## Context

Verified 2026-09-27 by reading the files and sources named. Anything not verified is
marked as such.

**The service as it exists (agent-cloud).**

- `platform/services/step-ca/deployment/compose.yml`: image
  `docker.io/smallstep/step-ca:0.30.2` (line 21); first-run auto-init from
  `DOCKER_STEPCA_INIT_*` (lines 26-30), with the ACME switch falling back to `false`
  (`STEPCA_INIT_ACME`, line 30) and `env.j2` turning it on only in local_mode unless
  inventory declares it (until 2026-09-30 it was on by default); port published on
  `${STEPCA_BIND:-127.0.0.1}:${STEPCA_PORT:-9000}` (line 33); everything under
  `/home/step` in the `step-ca-data` volume (line 35); health check `step ca health`
  against the in-volume root (line 40).
- `templates/env.j2` renders the bind, port, name, DNS names, provisioner name and the
  ACME switch from inventory with defaults (lines 5-11) and `STEPCA_INIT_PASSWORD` from
  OpenBao (line 12). `context/architecture.md` lines 45-46: keys live encrypted in the
  volume and only the password is in OpenBao at `secret/services/step-ca`.
- `platform/playbooks/deploy-step-ca.yml`: targets `step_ca_svc` (line 14); one secret,
  `init_password`, random, 40 characters (line 26); Phase 2.5 raises the `admin`
  provisioner's maximum and default leaf lifetime to `stepca_x509_max_dur`, default
  `8760h`, because the JWK provisioner's default caps leaves at 24 hours (lines 66-96),
  then sends `SIGHUP` (line 99); Phase 3 checks health and reads the root's subject
  (lines 105-134). Its comment points at `plan/development/INTERNAL-CA-DEPLOYMENT.md`
  (line 10), which now lives at `plan/archive/development/INTERNAL-CA-DEPLOYMENT.md`.
- `platform/playbooks/clean-deploy-step-ca.yml` lines 4-8: destroying the volume deletes
  the root and intermediate and a fresh deploy creates a different root. It carries no
  confirmation guard today.
- `platform/playbooks/tasks/mint-internal-cert.yml`: same-host only (lines 3-7); signs
  with the `admin` provisioner, reading the password from the CA container's own
  environment (lines 59-63); mints a wildcard plus the apex plus derived nested wildcards
  (lines 62-66); the key is generated inside the CA container and copied to the consumer
  with `podman cp` (lines 79-85); SANs are checked against a hostname pattern before they
  reach the shell (lines 39-43).
- `platform/playbooks/tasks/distribute-ca-root.yml`: same-host only (lines 11-12); builds
  `certs/step-ca-bundle.crt` as root plus intermediate, mode 0644, because the served
  chain needs both (lines 41-56). Used by the ERPNext, Postiz and agentgateway
  deploys in local mode only.
- Local-dev inventory: `step_ca_svc` with `stepca_bind: 127.0.0.1`, port 9000
  (`platform/inventory/local-dev.yml.example` lines 53-64). Local templates only
  (`templates-local.yml` lines 25 and 124).
- `platform/playbooks/manage-caddy-sites.yml` lines 15-20: a single-file bind mount pins
  the original inode, so a rewritten file is invisible to the running container and
  `caddy reload` re-reads stale content; the play restarts the container instead and
  notes a directory mount could reload. Local Caddy mounts its certificates as a
  directory (`platform/services/caddy/deployment/compose.local.yml`, `./certs` mount);
  the base compose file has no certificate mount. Production Caddy is addressed through
  inventory (`caddy_compose_dir`, `caddy_caddyfile_path`, `caddy_container`); whether its
  running compose file is the monorepo's is **unverified** (task 5.1 checks).
- Caddy reload (source at tag v2.11.4, read 2026-09-28): `caddy reload` skips a config
  identical to the running one (`caddy.go:220-221`, "config is unchanged") unless given
  `--force` ("Force config reload, even if it is the same", `cmd/commands.go:206`). A
  reload provisions the reverse proxy's HTTP transport again, and provisioning loads the
  client certificate and key from their files
  (`modules/caddyhttp/reverseproxy/httptransport.go:413`, `694-705`, `tls.LoadX509KeyPair`).
  The platform already reloads Caddy in the container with `caddy reload --config
  /etc/caddy/Caddyfile` (`platform/playbooks/tasks/distribute-caddy-site.yml:177`).
- The local agentgateway overlay mounts the bundle as a single file
  (`platform/services/agentgateway/deployment/compose.local.yml`, the
  `./certs/step-ca-bundle.crt` mount), which is the pinned-inode shape the Caddy note
  warns about.
- The o11y stack alerts through Grafana-managed rules (`templates/alerts.yml.j2`) and has
  a Loki datasource (`config/grafana/provisioning/datasources/datasources.yml` line 18).
  The conformance collector already pushes one Loki line per result to
  `/loki/api/v1/push`, inline in its own play (`collect-service-conformance.yml` lines
  271-285), and runs on a schedule declared in `templates.yml` (`schedule: cron:`, lines
  862-863; the only other scheduled template is at lines 447-448). No shared Loki push
  task exists under `platform/playbooks/tasks/` (listed 2026-09-28). No certificate
  expiry signal exists anywhere in the stack (searched `platform/services/o11y`).
- `platform/playbooks/apply-firewall.yml` takes `firewall_ssh_cidrs`,
  `firewall_controller_cidr`, `firewall_detect_ports`, `firewall_upstream_source` and
  `firewall_allow_rules` (header, lines 22-57).
- The service deployment workflow's steps are listed in
  `platform/workflows/service-onboarding/registry.yml`: `lookup-inventory`,
  `validate-address`, `provision-vm`, `cloud-init`, `ssh-keys`, `ssh-key-backup`,
  `access-harden`, `service-assess`, `vm-rightsize`, `secrets-approle`,
  `instrument-host`, `service-deploy`, `service-validate`, `edge-route`, `fw-assess`,
  `fw-harden`, `instrument-service`, `systemd-enablement`, `access-assess`,
  `oidc-config`, `credential-backup`.
- `backup-credentials-to-site-config.yml` copies OpenBao fields into site-config on a new
  branch per run and prints only names (header, lines 1-50), through the shared clone and
  push tasks.

**site-config (checked 2026-09-27, branch `feat/agentgateway-host`).** No step-ca entry in
`proxmox/vm-specs.yml`; no `step_ca_svc` group in `inventory/production.yml`; no inventory
host for the DGX Spark nodes (the vLLM upstream appears only as a value on the gateway and
Caddy hosts). The VM template is two cores, 2 GB, 20G (`vm-specs.yml` lines 9-11).

`plan/ARCHITECTURE-REFERENCE.md`, which the root `CLAUDE.md` cites for the credential
backup policy, does not exist on that branch; the policy used here is the one encoded in
the backup playbook and in `plan/architecture/04-credentials-access.md` line 375.

> **Updated 2026-09-29.** The CA VM is now declared in site-config (a vm-specs entry and a
> `step_ca_svc` group), provisioned, key-only over SSH and firewalled to SSH only
> (`firewall_allow_rules: []`, port detection off), all through Semaphore. The step-ca
> service is not deployed, and its service variables (`stepca_bind`, `stepca_init_acme`,
> `stepca_name`) are not declared yet.

**Consumers (upstream sources, fetched 2026-09-27).**

- Caddy v2.11.4, `modules/caddyhttp/reverseproxy/caddyfile.go`: `tls_trusted_ca_certs`
  logs a deprecation warning pointing at `tls_trust_pool` (lines 1209-1210) and cannot be
  combined with it (lines 1218-1219, 1353-1354). The reverse_proxy documentation gives
  `tls_trust_pool <module>`, `tls_client_auth <automate_name> | <cert_file> <key_file>`
  and `tls_server_name <server_name>`; the `file` trust-pool provider takes
  `file [<pem_file>...]`.
- agentgateway v1.5.0, `schema/config.md`: `gateways.*.tls.cert`, `.key`, and `.root`
  ("Path to a root CA certificate file used to validate client certificates (mTLS)");
  `llm.models[].tls.cert`, `.key`, `.root`, `.hostname`, `.insecure`. Source
  `crates/agentgateway/src/types/agent.rs` lines 564-579: when `root` is set the listener
  builds a `WebPkiClientVerifier` over those roots, so client authentication is required
  and **any** certificate chaining to the root is accepted. Listener certificate files are
  fetched through the resource manager (`types/local.rs` lines 5601-5617) and the state
  manager reloads on a resource change (`state_manager.rs` lines 186-191); whether a
  rename inside a mounted directory triggers that reload is **unverified** (task 6.3
  drills it). Whether the model-side `tls` files are watched the same way is
  **unverified**.
- vLLM `serve` documentation: `--ssl-keyfile`, `--ssl-certfile`, `--ssl-ca-certs`,
  `--ssl-cert-reqs`, and `--enable-ssl-refresh` ("Refresh SSL Context when SSL
  certificate files change", default off).
- step CLI documentation: `step ca sign <csr-file> <crt-file>` with `--provisioner`,
  `--provisioner-password-file`, `--not-after`, `--ca-url`, `--root`, `--force`;
  `step ca provisioner add` with `--type`, `--create`, `--password-file`,
  `--x509-template`, `--x509-default-dur`, `--x509-max-dur`, `--disable-renewal`,
  `--allow-renewal-after-expiry`; `step ca renew --daemon` renews by default once two
  thirds of the validity period has elapsed. A per-provisioner DNS-name allowlist flag was
  **not** found on the `provisioner add` page; how to express a name policy on the CA is
  **unverified** (task 4.4).
- **Unverified:** which extended key usages step-ca's default leaf template sets (task
  4.4 inspects an issued leaf before the profiles are fixed).

## Goals / Non-Goals

Goals: one production CA whose root outlives every redeploy; certificates for declared
internal names only, issued without moving any private key off its consumer; every
consumer trusts the same bundle; leaves are short-lived and renewed on a schedule, with
an alert before any lapse; the CA material is backed up and restorable to the same root;
the CA is reachable only through the orchestrator; local-dev keeps working through the
same tasks.

Non-Goals: ACME issuance in production; browser or workstation trust of the production
root; certificates for public names (Caddy and Let's Encrypt keep those); an OpenBao PKI
engine; moving vLLM configuration into this repository.

## Decisions

1. **step-ca on its own VM (Joe, 2026-09-27).** The CA's intermediate key signs every
   internal identity, so the host that holds it runs nothing else, is firewalled to SSH
   from the controller, and can be rebuilt from backup without touching any other
   service. Sized from the VM template (two cores, 2 GB, 20G) unless `vm-rightsize` says
   otherwise. Alternative rejected: co-locate on the Caddy host, because the front door is
   the most exposed machine on the platform and a compromise there would hand over the
   signing key. Alternative rejected: co-locate on the OpenBao host, because the secrets
   store's host should not gain a second, unrelated container and its failure domain.
   Alternative rejected: an OpenBao PKI engine, recorded in the archived plan as the
   strongest alternative; step-ca already exists, is proven in local-dev, and the
   consumers need only files.

2. **The CA API stays on the host loopback; issuance goes through SSH.** Production keeps
   `stepca_bind: 127.0.0.1` (the service's default) and publishes nothing else. The
   issuance task reaches the CA the way the local task already does, by running `step`
   inside the container, only now on the CA host through the Semaphore runner's SSH
   connection. No consumer ever contacts the CA, so the CA needs no inbound rule except
   SSH. Alternative rejected: publish port 9000 to the consumers so each runs
   `step ca renew --daemon` itself, because that puts the step CLI on every consumer,
   opens the signing service to the LAN, and spreads renewal state across hosts that
   the orchestrator does not observe. It stays open for dgx-spark if that side prefers
   it (open question 1).

3. **The key is generated on the consumer; only the request travels.** The evolved
   `mint-internal-cert.yml` runs in three places: on the consumer it generates a P-256
   key and a certificate request with `openssl` (asserted present, never installed
   silently), keeping the key 0600 in the consumer's certificate directory; on the CA
   host (`delegate_to`) it places the request in the container and runs `step ca sign`
   with the issuing provisioner; back on the consumer it writes the certificate. The
   request and certificate are public, so no step needs `no_log` except the one that
   supplies the provisioner password inside the CA container, which already reads it from
   the container's environment. Alternative rejected: keep the local shape and copy the
   minted key through the controller, because a private key in transit through the
   runner and in the CA's `/tmp` is exactly what TLS on the LAN is meant to prevent.
   Alternative rejected: a second task for production, because one codebase means one
   issuance path; local-dev passes `_mint_ca_host` equal to the consumer and nothing
   else changes for the Caddy wildcard.

4. **Leaves are declared in inventory, with profiles.** One list in site-config declares
   every production leaf: a name, the consumer host, the destination directory, the DNS
   names, a profile (`server` or `client`), and a reload action. The issuance task refuses
   any name that is not in the declaration for that consumer and any name outside the
   internal zone. SANs are DNS names in the internal zone (for example
   `agentgateway.<internal-zone>`, `vllm.<internal-zone>`, `caddy.<internal-zone>`); the
   names are verification names, set explicitly with Caddy's `tls_server_name` and the
   gateway's model `tls.hostname`, so they need no DNS records and no IP SANs. The first
   declared set is:

   | Leaf | Consumer | Profile | Used by |
   |---|---|---|---|
   | `caddy` client | Caddy host | client | `tls_client_auth` towards both gateway listeners |
   | `agw-verifier` client | gateway host | client | the one gateway probe path (`inference-gateway-agentgateway` task 6.1a): the gateway deploy's keyed and keyless probes, the personal-key 401 gates and the access-record verify of `agentgateway-observability` (its requirement "Every gateway request produces an access record in Loki"), all sent from the gateway host to its published port |
   | `bench` client | benchmark VM | client | `inference-benchmarking` runs whose target is the gateway listener (sent directly, not through Caddy) or vLLM directly, which requires a client leaf once mutual TLS is on |
   | Gateway server | gateway host | server | `gateways.default.tls` and `gateways.ui.tls` |
   | vLLM server | DGX Spark head (handoff) | server | `--ssl-certfile`/`--ssl-keyfile` |
   | `agw-upstream` client | gateway host | client | the model's `tls.cert`/`tls.key` towards vLLM, which requires a client certificate (`--ssl-cert-reqs`, `--ssl-ca-certs` = the internal root; open question 3, decided 2026-09-28) |

   One gateway leaf covers both listeners: they are the same process on the same host,
   so a second key buys nothing. The `agw-verifier` leaf lives on the gateway host
   because the gateway probe path runs `uri` calls from that host to the gateway's
   published port (`platform/playbooks/deploy-agentgateway.yml:341-352`, the "Keyed
   probes" tasks today); its key is generated there and its CSR goes through the same
   issuance flow as every other leaf. The `bench` leaf keeps the benchmark A/B
   meaningful: the direct-gateway target stays one hop, so the measured difference is the
   gateway's own, not Caddy's. The three client leaves are the names the gateway's
   allowlist may carry (the gateway's rule, decision 5's pointer); a client leaf
   declared for any other purpose, such as the gateway's OTLP client leaf towards Alloy
   (`agentgateway-observability`, requirement "The telemetry push hop is mutually
   authenticated and encrypted"), is not on it. The gateway's own client
   certificate towards vLLM is not in the first set; it is added only if dgx-spark turns
   on `--ssl-cert-reqs` (open question 3).

5. **Profiles are enforced by extended key usage.** The gateway accepts any certificate
   that chains to its `root` (context above), so a server leaf that also carried client
   authentication could be presented to the gateway as if it were Caddy. Server leaves
   therefore carry server authentication only and client leaves client authentication
   only, via a dedicated issuing provisioner per profile or an x509 template per profile,
   whichever task 4.4 finds step-ca supports cleanly. The issuing provisioners are
   separate from the bootstrap `admin` provisioner, have their own passwords in
   `secret/services/step-ca`, and carry a maximum lifetime equal to the production leaf
   lifetime. Alternative rejected: reuse `admin` for production issuance, because its
   password is the key password and its lifetime was raised to a year for the local
   wildcard.

   Profiles alone do not identify the caller, because every client-profile leaf chains
   to the same root. Which client leaves the gateway admits is the gateway's rule, owned
   by `inference-gateway-agentgateway` (its design decision 12 and task 6.1, with the
   v1.5.0 source evidence). This change supplies only what that rule reads: the declared
   client-profile leaves in decision 4, by name and SAN.

6. **Thirty-day leaves, renewed daily inside the last third.** Default production leaf
   lifetime `720h`, inventory-parameterized. A scheduled template runs daily; a leaf is
   re-issued when less than a third of its lifetime remains (the same threshold step's
   own renewal daemon uses), which gives ten daily attempts before expiry. Each run is
   idempotent: a leaf outside its window is left alone and reported. Re-issuance creates a
   new key each time. The local wildcard keeps its year-long lifetime and its
   re-mint-on-deploy behaviour. Alternative rejected: a year-long production leaf, because
   the renewal path would run once a year and nobody would notice it had rotted.
   Alternative rejected: hours-long leaves, because an orchestrator outage of a day would
   then take the inference path down.

7. **Certificates live in a mounted directory and are swapped atomically.** Consumers
   mount their certificate directory read-only, never individual files, so a replacement
   is visible inside the container (the pinned-inode lesson in `manage-caddy-sites.yml`).
   Each issuance writes into a fresh subdirectory named for the new serial and then
   replaces a `current` symlink in one rename, so a reader never sees a new certificate
   paired with an old key; the previous subdirectory is kept until the next successful
   run for rollback. Consumer configuration references `current/`. The symlink is
   relative (`current` names the serial directory beside it), so it resolves to the same
   files inside a container that mounts the parent directory. Whether each long-running
   consumer follows the swap without its reload action is drilled per consumer (task
   6.3); the reload action runs regardless (decision 8). The local
   agentgateway overlay's single-file bundle mount becomes a directory mount as part of
   this.

8. **Renew per host, reload once, then prove what is in use.** When any declared leaf on
   a consumer host enters its renewal window, the run re-issues every declared leaf on
   that host, so each host sees one swap and one reload action per run, not one per leaf.
   The proof depends on how the leaf's user reads its files:
   - **A server leaf** (gateway, vLLM) is checked on the consumer's serving listener: the
     served certificate's serial must equal the one just issued. The gateway's reload
     action is none if the drill in task 6.3 shows its file watch sees the swap, otherwise
     a restart; vLLM's is `--enable-ssl-refresh` on the dgx-spark side.
   - **A client leaf whose user opens the files on each call** (`agw-verifier`, whose
     user is the `uri` module in the gateway probe path, and `bench`, whose tool reads
     `cert_path`/`key_path` at run start) needs no reload action. It is proven by one
     request through the gateway probe path (`inference-gateway-agentgateway` task 6.1a)
     from the leaf's host, presenting the new files: a completed request proves the new
     leaf chains to the root and passes the gateway's allowlist.
   - **A client leaf held by a long-running process** (`caddy`) has the reload action
     `caddy reload --force` inside the Caddy container. `--force` is required, because
     the Caddyfile is unchanged by a renewal and a plain reload is skipped (Context). A
     forced reload provisions the transport again and loads the files at `current/`, and
     it fails if they do not load (Context, `httptransport.go:694-705`). The run then
     requires the serial in `current/` on the Caddy host to equal the one just issued and
     sends one request through Caddy's inference route, which must complete. Together
     these prove the process loaded the new leaf. This holds only for a directory mount;
     if task 5.1 finds production Caddy mounts files, the reload action is the container
     restart `manage-caddy-sites.yml` already uses (its header, lines 14-20).
   A mismatch or a failed request fails the run. No proof reads a Loki record, so this
   change has no ordering against the gateway's access-record export. Alternative
   rejected: compare a client-certificate digest from the gateway's access record in
   Loki, because it makes certificate renewal depend on a telemetry pipeline, adds an
   access-log field whose hashed encoding was unverified, and proves nothing the forced
   reload and the serial check do not already establish.

9. **Root distribution reads from the CA host.** `distribute-ca-root.yml` gains the same
   `delegate_to` shape: it reads root and intermediate from the CA container on the CA
   host, builds the bundle in memory (both are public), and writes it 0644 on each
   consumer. The CA remains the only source; no copy of the production root is committed
   to this public repository. The site-config backup holds a copy for recovery.

10. **Expiry alerting through Loki and Grafana, and one rule for every silent scheduled
    job.** This change extracts the conformance collector's inline Loki push
    (`collect-service-conformance.yml:271-285`) into a shared
    `platform/playbooks/tasks/push-loki-lines.yml`, and the collector adopts it in the
    same change, so the push exists once. The renewal run pushes through it one line per
    leaf and one for the intermediate, labelled with the service and leaf name and
    carrying the expiry time and the remaining seconds. Grafana-managed rules join
    `alerts.yml.j2`: any leaf with fewer than seven days remaining and the intermediate
    with fewer than ninety. The silence check is not specific to renewal: every scheduled
    job pushes one result line per run under a bounded `job` label, and one rule, "Scheduled
    job silent", fires for any job in a declared list (job name and the longest silence
    it may keep) whose newest line is older than that silence. The renewal job declares
    thirty-six hours; `inference-personal-keys` declares its hourly reconcile in the same
    list. Alternative rejected: a
    blackbox exporter probing each port, because it would be the first exporter of its
    kind on the o11y host and still would not see the vLLM port if dgx-spark's firewall
    narrows it to the gateway.

11. **Backup is the volume's key material, on a new branch per run.** A new
    `backup-step-ca-to-site-config.yml` reads the CA's certificates, encrypted keys and
    configuration out of the container on the CA host and writes them into a site-config
    clone through the shared clone and push tasks, printing only file names. The key
    password travels through the existing credential backup
    (`credential_service=step-ca`). Both land in the same private repository, which is
    the posture site-config already has for SSH private keys and service credentials; the
    keys at least stay encrypted at rest there. A restore drill in local-dev (restore into
    a fresh volume, compare the root fingerprint) is the proof. Alternative rejected:
    snapshot the VM only, because a snapshot lives on the same hypervisor cluster the CA
    would be recovered onto and is not reviewable.

12. **The destructive reset is gated.** `clean-deploy-step-ca.yml` refuses unless the
    launch names the CA host in a confirmation variable, following `destroy-vm.yml`'s
    `confirm_destroy` pattern; local-dev keeps working by passing the confirmation from
    its make target. Production gets no clean-deploy template in `templates.yml` until an
    operator asks for one.

13. **Documentation and the recorded exception.** `05-platform-infra.md` line 483 moves to
    `tls_trust_pool file <bundle>` and gains a mutual-TLS row (`tls_client_auth <cert>
    <key>` with `tls_server_name`); its default policy ("All platform services run plain
    HTTP on the internal network") gains a dated exception for the inference path that
    names this change. The stale plan pointers in `compose.yml`, `deploy-step-ca.yml` and
    `mint-internal-cert.yml` and in `05-platform-infra.md` line 201 are corrected to the
    archived plan's location.

## Risks / Trade-offs

- [A server leaf is accepted as a client at the gateway] → decision 5 separates the
  profiles; task 4.4 proves a server leaf is refused as a client before any consumer
  config lands. If step-ca cannot separate them, a separate intermediate for client
  leaves is the fallback, recorded as an amendment.
- [The running process never picks up a renewed certificate] → decision 8 compares the
  served serial for server leaves and forces Caddy's reload before checking its
  `current/` serial; the expiry alert catches a miss days before it bites.
- [The orchestrator is down for longer than the renewal window] → ten daily attempts
  and a seven-day alert threshold leave about three weeks between the first missed run
  and expiry.
- [The CA host is lost] → restore from the site-config backup onto a new VM keeps the
  root; consumers need no change. Without a usable backup, the gated reset mints a new
  root and every consumer redeploys.
- [site-config holds the encrypted keys and their password] → equal to the repository's
  existing posture for other private keys; an offline root would reduce it (open question
  2).
- [The provisioner password is visible to anyone with container access on the CA host] →
  the host runs only the CA and accepts SSH only from the controller and operator ranges.
- [Production Caddy's compose file is not the monorepo's] → task 5.1 establishes which
  file is live before adding the certificate directory mount; the mount lands as code in
  whichever file that is.

## Migration Plan

1. Allocate the CA VM in site-config and bring it up through the workflow steps up to
   hardened SSH.
2. Deploy step-ca through Semaphore with production parameters; confirm a second run keeps
   the root; apply the firewall; back up the CA material and run the restore drill in
   local-dev.
3. Land cross-host issuance and root distribution, proven first in local-dev (where the
   Caddy wildcard must be unchanged) and then against the production CA with a throwaway
   leaf on the gateway host.
4. Issue the Caddy client and gateway server leaves; the companion change then switches
   the gateway listeners and the Caddy transport. Hand the vLLM request and bundle to
   dgx-spark. Nothing here waits on the gateway's access records reaching Loki.
5. Enable the renewal schedule and the alert rules; run the renewal and alert drills.
6. Correct the documentation, write the recorded exception, archive, and retain the
   outcome into bank `agent-cloud-750a33b9`.

## Decisions recorded 2026-09-28

Joe answered the four open questions on 2026-09-28:

1. **dgx-spark certificate channel: signed through a template.** dgx-spark generates the
   vLLM server key and CSR on its head node; a Semaphore template here signs the CSR and
   returns the certificate and bundle through the channel agreed with the dgx-spark
   session. The key never leaves the node and the CA port stays closed to it.
2. **Root key: online for the first rollout.** Backed up to site-config (decision on
   backup); taking the root offline after backup is a later hardening change.
3. **Mutual TLS towards vLLM: yes.** dgx-spark sets `--ssl-cert-reqs` and
   `--ssl-ca-certs` (the internal root), and the gateway presents a new `agw-upstream`
   client leaf through the model's `tls.cert`/`tls.key` (`llm.models[].tls.cert`/`.key`,
   agentgateway v1.5.0 `schema/config.md:70130-70131`; field at `types/local.rs:820-822`). Limit, accepted: vLLM's TLS layer checks only that the
   client leaf chains to the root, not which leaf it is; unverified: whether vLLM or its
   server offers a SAN check. The vLLM API key stays the caller check; mutual TLS narrows
   callers to holders of an internal-CA client leaf.
4. **Internal zone: a name under the reserved `.internal` suffix, chosen by Joe and declared
   in site-config** (the name itself stays out of this public repo). ICANN Board Resolution
   2024.07.29.06 reserves
   `.INTERNAL` from delegation in the DNS root zone permanently for private use
   (<https://www.icann.org/en/board-activities-and-meetings/materials/approved-resolutions-special-meeting-of-the-icann-board-29-07-2024-en>),
   so a SAN in it can never name a public host. Declared in site-config.

## Amendment 2026-09-29: leaf names follow the internal naming scheme

`internal-dns-naming` decision 14 (certificates follow the names) replaces decision 4's
example SANs. Each leaf carries its instance name and its service name under
`<site>.<zone>`: the vLLM server leaf `dgx01.vllm-primary.<site>.<zone>` and
`vllm-primary.<site>.<zone>`; the gateway leaf `vm01.gateway`, `gateway` and `inference`
under `<site>.<zone>` (a load balancer's leaf carries every service name that points at
it); the Caddy leaf `vm01.caddy` and `caddy`. The gateway's model `tls.hostname` is
`vllm-primary.<site>.<zone>`, and Caddy's `tls_server_name` towards the gateway is
`gateway.<site>.<zone>`. These names get DNS records in the internal zone, so decision 4's
"they need no DNS records" no longer holds. Task 4.2's guard refuses any SAN outside
`<site>.<zone>`.

## Decisions recorded 2026-09-29

1. **The CA's name and DNS names are site values.** The production `stepca_name` and
   `stepca_dns_names` are declared in site-config only, never in this repository. The CA's
   own certificate carries its naming-scheme name, `ca.<site>.<zone>`, so a later
   network-reachable CA needs no reset.
2. **Two issuing provisioners** (task 4.4): `issuer-server` and `issuer-client`, one JWK
   provisioner per leaf profile, each with its own password in `secret/services/step-ca`.
3. **A production clean-deploy template.** Unlike the plan's "no production clean
   template", `Clean Deploy step-ca` exists in production, guarded by `confirm_ca_reset`
   naming the host, so the scenario "Reset without confirmation is refused" is proven there.

## Recorded risk 2026-09-29: the key password sits beside the keys

Read from the step-ca 0.30.2 image's `/entrypoint.sh`: on first boot it writes
`DOCKER_STEPCA_INIT_PASSWORD` to `/home/step/secrets/password` (mode 0644) and to
`provisioner_password`, and prints the latter to the container log ("Your CA administrative
password is: ..."). That password also encrypts the root and intermediate keys, which live in
the same volume. So the volume, and the container log of the first boot, hold what an attacker
needs to use the root key: encryption at rest protects the key only from someone who has
the volume but not those files.

Accepted for the first rollout: the CA host is SSH-only, key-only, and holds nothing else.
Mitigations for a later change: a separate root key password through
`DOCKER_STEPCA_INIT_KEY_PASSWORD_FILE`, which the entrypoint supports; removing the plaintext
file after first boot; and rotating the first-boot container log.

## Findings 2026-09-30: task 4.4, profiles and a CA-side name policy

Measured on a throwaway step-ca 0.30.2 container (no published port, dummy password,
removed afterwards):

1. **A JWK provisioner without a template issues both key usages.** A leaf signed by a
   freshly added JWK provisioner carries Extended Key Usage "Server Authentication, Client
   Authentication". The production issuers were added this way on 2026-09-29, so until
   they carry templates a server leaf is also a valid client certificate: exactly what
   decision 5 forbids. Nothing has been issued from them yet.
2. **An x509 template per provisioner enforces the profile.** `step ca provisioner update
   <name> --x509-template <file>` followed by a SIGHUP made the next leaf carry
   "Server Authentication" only. step-ca stores the template's text inline in `ca.json`
   (`options.x509.template`), so the file is needed only for the update. Decision 5's
   mechanism is therefore a template per issuing provisioner, not a provisioner per
   template.
3. **A provisioner-level name policy is not available without the Admin API.** The CLI
   route (`step ca policy provisioner x509 allow dns`) needs admin credentials against a
   CA with remote management enabled, which this CA does not have, and a provisioner
   `policy` block written into `ca.json` was ignored even after a restart.
4. **An authority-level name policy is enforced, with sharp edges.** `authority.policy.
   x509.allow.dns` in `ca.json`, applied by SIGHUP, refused a name outside it. But a
   wildcard entry matches exactly one label (`*.dc1.<zone>` refused
   `vm01.gateway.dc1.<zone>`), wildcard leaves are refused too, and an entry step-ca
   cannot parse (`.dc1.<zone>`, `*.*.dc1.<zone>`) makes the reload fail with "error
   reloading ca: cannot parse permitted domain constraint" while the CA keeps serving
   its previous configuration, and then prevents the CA from starting on its next
   restart.

Decided by Joe 2026-09-30, as recommended: render the authority policy from the exact
SANs of the declared leaves (decision 4's list), never a pattern, so the CA itself
refuses any undeclared name even if task 4.2's guard were bypassed. The deploy would
refuse an entry that is not a plain DNS name before writing `ca.json`, and would read the
CA's log after the reload and fail on a reload error, because the running CA does not
fail on one. Local-dev keeps no policy: its admin wildcard would be refused. The leaves are
declared once, as the all-hosts site-config list `internal_leaves` (decision 4's fields:
`name`, `host`, `dir`, `profile`, `sans`, `reload`), and every SAN must sit under
`<dns_site>.<dns_zone>` (the `internal-dns-naming` variables). While none is declared the CA
carries no policy, and nothing is issued, because the issuance task refuses an undeclared
leaf (task 4.2).

**Corrected 2026-10-01 (review of site-config#57).** "No policy" was not "nothing is
issued": the issuance task's refusal covers only this repository's path, and anyone holding
an issuer password could sign any name. Measured on a throwaway step-ca 0.30.2: an absent
`authority.policy` and an empty `allow.dns` list both issue in-zone names, out-of-zone
names and IP addresses, after a SIGHUP and after a restart. An allow list holding only
`no-leaf-declared.invalid` (the RFC 2606 reserved TLD, which the deploy's own guard would
refuse as a declared SAN) refused all three and survived both. So with no leaf declared
the production CA now carries that one-name policy, and Phase 3 refuses a CA without it.
Local-dev still carries none.

## Decisions recorded 2026-11-21: certificates-only consumers on the DGX Spark nodes

AC-1022 and gateway task 6.3. This supersedes decision 1 of 2026-09-28 and open question 1's answer:
dgx-spark's ADR-0017 (accepted 2026-10-16) made each node a certificates-only consumer of this
repository's issuance, so the key and request are made on the node by `Issue Internal Leaf (Dev)`
like every other leaf, and no signing template is built (task 2.4a). No leaf is issued by this
record.

The four leaves:

| Leaf | Host | Profile | SANs | Reload | Used by |
|---|---|---|---|---|---|
| `vllm-server` | the head node | server | `vllm-primary.<site>.<zone>`, `dgx01.vllm-primary.<site>.<zone>` | none (`--enable-ssl-refresh`) | vLLM's API listener |
| `vllm-lifecycle` | the head node | client | `lifecycle.vllm-primary.<site>.<zone>` | none | dgx-spark's lifecycle coordinator, Phase B only |
| `image-server` | the second node | server | `image-primary.<site>.<zone>`, `dgx02.image-primary.<site>.<zone>` | none (`--enable-ssl-refresh`) | the image server's listener |
| `agw-upstream` | gateway host | client | `upstream.gateway.<site>.<zone>` | none until Phase B, then restart unless 5.4a's drill says none | the gateway's model `tls.cert`/`tls.key` towards vLLM |

The first three SANs were agreed with dgx-spark on 2026-10-26 (gateway task 6.3 note) and by the
operator on 2026-11-20 (image server). `upstream.gateway` follows the shape of the gateway's
existing client leaf (`verifier.gateway`); vLLM checks only that a client leaf chains to the root
(decision 3 of 2026-09-28), so the name serves the CA's name policy and operators. It is a new name,
open to review.

1. **(a) One machine holds its instance name under two services.** Decided here, as a rule the
   naming scheme does not yet state: the ordinal names the machine within its class, so the same
   ordinal appears under every service the machine is a member of. The second node is `dgx02.vllm-primary`
   (`serves: false`, naming decision 6) and `dgx02.image-primary` (serving). Both are declared on
   the second node's inventory host entry (`dns_instances`, a list per host in naming decision 9; decision 3
   below), so no shape changes; retiring the machine later takes one `dns_retired_instances` entry
   per service (the list is keyed by service and instance). The naming change records the rule through a follow-up. The image-server leaf
   carries only the `image-primary` names. Alternatives rejected: *a new ordinal for the image
   instance* (`dgx03.image-primary`), because the ordinal would no longer identify the machine, and
   naming decision 5 exists so that a name carries one machine's history; *one second-node leaf carrying
   `dgx02.vllm-primary` too*, because the second node serves no vLLM API (naming decision 6), and a client
   verifying a `vllm-primary` name could then be answered by the image port.

2. **(b) The renewal proof per leaf.** Decision 8 proves each leaf in use after its host's reload.
   The renewal header (`renew-internal-certs.yml:70-72`) adds that a gateway leaf whose use cannot
   be proven is left alone, unrenewed; these classes take a different rule. Here a leaf whose
   listener or caller does not use it yet is still renewed with its host, and checked only on its
   files (`issue-internal-leaf.yml -e leaf_action=inspect`), which shows the files are valid, not
   that anything loaded them. Which leaves are in use is not read from the nodes: it is chosen by
   switches the operator declares in site-config, described below. Once a switch says a leaf is in
   use, the proof named in that leaf's bullet below applies, and each bullet says what it reads
   and what it does not: for `vllm-server` and `image-server`, the serial the listener serves; for
   `vllm-lifecycle` and `agw-upstream`, that vLLM accepts the new files, not that the process
   holding the leaf loaded them.
   - Three states this decision does not read are an open build decision (AC-1041): whether a
     listener really serves what its switch declares; where agent-cloud reads
     `vllm_tls_require_client`, the name dgx-spark gives its switch for requiring client
     certificates (its Phase B); and which `agw-upstream` leaf the gateway process presents after
     a restart. agent-cloud does not read `vllm_tls_require_client` today; below, "with
     `vllm_tls_require_client` on" means the state agent-cloud will read once AC-1041 settles where.
   - `vllm-server` and `image-server` get one row each in `vars/renew-listener-servers.yml`, the
     class OPA, the launch gate and skynet already use: the leaf's own host connects to its listener
     and the served serial must equal the one issued. The rows differ from the existing ones in
     three ways, each a field of the row, so the existing rows keep their behaviour. Their leaf
     directory is `<certificates directory>/<leaf>` under the consumer account, not
     `<deploy>/certs/<leaf>`. Their switch (`vllm_tls`, `image_server_tls`) is an inventory value
     the operator declares in site-config, off by default, meaning "the listener serves TLS now";
     dgx-spark controls the listener, and nothing reads the node to set or check the switch. While
     the switch is off, the leaf is renewed with its host and checked on its files only: the run
     does not see what the listener serves, so a listener that serves TLS while its switch is still
     off is never compared with `current` (AC-1041). Once the switch is on, the served serial is
     read and compared. And, with `vllm_tls_require_client` on, the `vllm-server` proof presents
     the host's own `vllm-lifecycle` leaf, where today's listener proof presents no client
     certificate (`renew-internal-certs.yml:22-26`). Reload stays `none`: agent-cloud's account has
     no right to restart a dgx-spark service. With the switch on, the served-serial proof in the run
     that renewed the leaf fails that run if `--enable-ssl-refresh` does not follow the swap, which
     the rotation drill (dgx-spark task 3.4) tests before anything relies on it; dgx-spark owns the
     fallback (a restart or a fixed-name copy). The existing rows that have a switch (OPA, skynet)
     keep refusing a leaf whose switch is off; the launch gate's row has none
     (`platform/playbooks/vars/renew-listener-servers.yml:42`).
   - `vllm-lifecycle` gets a node client class: with `vllm_tls_require_client` off, nothing presents
     it, so it is renewed with its host and checked on its files only. With it on, the leaf's host
     completes a handshake with its own vLLM listener presenting the new files, and the same
     connection without a client certificate must be refused, or the handshake proved nothing about
     the leaf. That shows vLLM accepts the new files, not that dgx-spark's coordinator holds them:
     whether the coordinator re-reads the files on each connection is dgx-spark's to establish.
   - `agw-upstream` gets a gateway upstream class. With `vllm_tls_require_client` off, vLLM asks for
     no client certificate, so the gateway never sends the leaf and an old copy held in memory costs
     nothing: reload `none`, checked on its files only. Its directory is under the gateway
     deploy's `certs/`, which the deploy's change check hashes
     (`platform/services/agentgateway/deployment/deploy.sh:22`, `:102-125`), so a real run of Phase B's gateway
     deploy recreates the gateway when those files differ from the ones it started with (a dry run
     skips that check, so whether Phase B's launch may skip the real run is part of AC-1041). From then on its reload is the gateway
     container's restart, run once per host per decision 8, after the swap in the same run. The
     proof reads the gateway's readiness after the restart and makes a handshake from the gateway
     host to vLLM presenting the new files and verifying `vllm-primary.<site>.<zone>`, which shows
     vLLM accepts them. It does not read which leaf the restarted process presents, and nothing yet
     shows that the gateway loads its model-side files when it starts (task 5.4a's drill covers a
     reload on change): how the build proves that is an open build decision (AC-1041). Reload `none` replaces the restart only when the drill in task
     5.4a shows agentgateway reloads model-side TLS files and the renewal also checks the leaf the
     gateway process itself presents to vLLM; a handshake from another client on the host does not
     show which leaf the process holds. Accepted cost: from
     Phase B, unless the gateway server leaf already restarts the gateway or the drill shows a
     reload, each renewal of the gateway host (about every 20 days) restarts it and drops open
     streams. Alternatives rejected: *the gateway's rule, "not in use, left alone", while a listener
     does not serve TLS*, because dgx-spark's launcher refuses to start TLS without the files (declared by
     dgx-spark, not checked by this repository), so
     the leaf must exist and stay current for an unknown time before anything serves it; *proving
     `vllm-server` from the gateway host with `agw-upstream`* (dgx-spark's proposal), because it
     ties a head-node renewal to a second host and a second leaf, where the listener class proves from
     the leaf's own host; *reload `restart` from agent-cloud*, because it would need a privilege on
     dgx-spark's machines that the boundary withholds.

3. **(c) How issuance, renewal and the bundle reach the nodes.** This amends naming decision 9
   (the operator, 2026-11-21). Decision 9 keeps machines this repository does not manage, GPU nodes
   among them, out of the inventory as records-only entries, so that no play can target them. A
   machine reached only through its certificates-only account may now be an inventory host: its DNS
   records move to that host entry, and the protection is the account's key boundary, not absence
   from the inventory. The render's refusal of a hostname declared both ways needs a build change,
   and the naming change records the amendment through its own item. Each node is an inventory host
   of its own group in site-config, under its own inventory name (the head node's existing inventory name is the drill
   host's), with `ansible_user` the certificates-only account (`vllm-tls`, the same account and key on
   both nodes) and `service_conformance_tracked: false`. A host var names the OpenBao key it
   connects with (`secret/services/ssh/vllm-tls`); `issue-internal-leaf.yml`,
   `renew-internal-certs.yml` and the bundle step connect to such a host with that key through one
   shared task, as the fault drill does: `tasks/materialise-ssh-key.yml`, then
   `ansible_ssh_private_key_file` for those hosts only, then `tasks/remove-ssh-key.yml` in `always:`. The key is set per
   host, never in play or block `vars:`, and the signing step delegated to the CA host is meant to
   connect with the CA host's own credentials; nothing shows that yet, and the build's test of how
   Ansible resolves a delegated task's connection variables is an open build decision (AC-1042). Every play runs `become: false` there already. Any
   other play that targets these groups connects with Semaphore's own key, which the account does
   not admit, so it fails to connect: that is the boundary dgx-spark built, declared by dgx-spark (the
   account admits only its own key) and not checked by this repository. The bundle is placed by
   the issuance itself: for a certificates-only host, `Issue Internal Leaf (Dev)` and the renewal
   write `step-ca-bundle.crt` into the certificates directory through `tasks/distribute-ca-root.yml`
   (`_ca_bundle_dest`) in the same run. Alternatives rejected: *Semaphore's inventory key admitted
   on the account*, because dgx-spark admits only the account's own key; *a separate bundle
   placement template*, because these hosts have no deploy that places it, and a separate launch is
   one more step to forget after a root change; *the signing template of task 2.4a*, superseded
   above.

4. **(d) Where clients find the bundle.** On each node, `step-ca-bundle.crt` in the account's
   certificates directory, beside the leaf directories (where the renewal proof already reads it,
   `renew-internal-certs.yml:73-76`). dgx-spark points `--ssl-ca-certs` and its lifecycle opener at
   it. agent-cloud's own clients keep the bundles their deploys place: the gateway
   `<deploy>/certs/step-ca-bundle.crt`, Caddy `<caddy_certs_dir>/step-ca-bundle.crt`, o11y its own.
   Alternative rejected: *a copy of the root committed to this public repository*, which decision
   9 forbids; the site-config copy decision 9 keeps is a recovery backup, not a client's bundle.

5. **(e) Name resolution while production internal DNS is not deployed.** No client needs a DNS
   record: each one dials the node's address from inventory and sets the name it verifies
   explicitly, which the scheme already relies on (the gateway's model `tls.hostname`, Caddy's
   `tls_server_name`, Prometheus `tls_config.server_name`, the prover's `server_hostname`). The
   image server's declared client is the gateway (plan 27's image path). When production internal
   DNS deploys, `image-primary` and `dgx02.image-primary` are declared on the second node's inventory host
   entry (decision 3's amendment of naming decision 9). Alternatives rejected: *a managed hosts-file line on each
   agent-cloud client*, rejected for the same reason in naming decision 15's 2026-09-29 amendment;
   *waiting for production internal DNS*, which would hold the build on a deploy nothing has
   scheduled. A hosts-file line on a node for dgx-spark's own local callers (as `tls-access.yml`
   writes on the head node) is dgx-spark's choice.

6. **(f) Order against dgx-spark's tasks.** Three constraints set the order; the build's first pull
   request carries the sequence. The leaves exist before dgx-spark's TLS switch, because its
   launchers refuse to start without the files (declared by dgx-spark, not checked by this
   repository). The inference-path callers (the gateway's
   `https://` upstream, Caddy's `/health` transport, Rollback Inference Route's `direct` mode
   transport, the o11y scrape) change in dgx-spark's Phase A window (its task 3.3), because a
   plain-HTTP caller fails the moment vLLM serves TLS only. Phase B (dgx-spark 4.1 to 4.3) follows
   only after every caller holds its leaf.

7. **(g) Every production launch the build needs.** Each is lane C: the operator approves that exact
   run, dry run included, in the acting session's chat, and manual launches follow ledger 3.20 and
   3.22. `Deploy step-ca (Dev)` renders the CA's name policy with every new name before any issuance
   (ledger 10.20). Every site-config change is followed by the operator-side inventory sync
   (`scripts/semaphore-sync-inventory.py`) before the run that reads it. The numbered launch list
   goes with the build's first pull request.

## Decisions recorded 2026-11-27: the leaf build's open decisions and launch list

AC-1041. This settles the three states that decision (b) of 2026-11-21 left open (named there
"open build decision (AC-1041)"), and completes decision (g) of 2026-11-21 with the numbered launch
list. AC-1042, the credentials the delegated signing step connects with (decision (c) of
2026-11-21), stays open: task 10.1's test settles it. No leaf is issued by this record.

1. **(h) Switch versus the listener.** The two new listener rows (`vllm-server`, `image-server`)
   declare the address the probe connects to as `<prefix>_bind` and `<prefix>_port` in site-config,
   with no default port. That is a fourth difference from the existing rows, which carry
   `default_port` (`platform/playbooks/vars/renew-listener-servers.yml:16`). With the switch off,
   after the host's renewal the leaf's host makes one TLS connection to that declared address. A
   completed TLS handshake fails that host's renewal and names the switch to turn on. A refused or
   closed connection, or an answer that is not TLS, passes, and the leaf is checked on its files
   only (`issue-internal-leaf.yml -e leaf_action=inspect`: the key matches the certificate, the key
   file is `0600`, and the certificate verifies against its profile's root). The check reads whether
   that one declared address answers TLS at that moment. It does not read any other port, or what
   the listener serves after the run. With the switch on, the proof is the served serial, read at
   the declared address and compared, unchanged from decision (b).

2. **(i) Where agent-cloud reads `vllm_tls_require_client`.** From its own site-config copy: an
   inventory value on the head node's host, off by default, which the operator declares to match
   dgx-spark's switch. In every run with `vllm_tls` on, a probe of `vllm-server`'s listener holds
   that copy honest. With the copy off, one request with no client certificate must be answered
   over TLS, or the run fails naming the mismatch. With the copy on, the same request must be
   refused, and the refusal is read the way the o11y client leaf proof reads one: an authentication
   alert in the handshake or on the first read after it (phase `AUTH_ALERT_READ`,
   `platform/playbooks/renew-internal-certs.yml:435-505`). The probe reads how the listener at the
   declared address answers a request without a client certificate; it does not read dgx-spark's
   configuration. `vllm_tls_require_client` on with `vllm_tls` off is refused at admission, by
   reading the two declared values, because no listener requires client certificates without TLS;
   like every admission refusal, it refuses the run before anything is issued. Alternative
   rejected: *reading dgx-spark's switch from the node*, because the `vllm-tls` account is
   certificates-only and this repository has no read of dgx-spark's configuration (declared by
   dgx-spark, not checked here).

3. **(j) The leaf the gateway process presents.** Three parts.
   - Launch 10's `Deploy agentgateway (Dev)` is a real run whatever its dry run shows. A dry run
     skips `deploy.sh`. A real run compares the running container's `io.agent-cloud.inputs-sha256`
     label with the digest of the current inputs, `./certs` included, and recreates the container
     on a difference (`platform/services/agentgateway/deployment/deploy.sh:84`, `:102-125`,
     `:182-201`). Its `deploy-result` line reads that comparison: the container was created from
     the current files. This resolves the CodeRabbit thread on #1218 (review of 0970d2cf).
   - From Phase B the renewal's reload for `agw-upstream` is the container restart
     (`<engine> restart agentgateway`, `platform/playbooks/renew-internal-certs.yml:735-739`), run
     once per host. A restart keeps the label written at create, because the label rides the
     overlay of `compose up -d --force-recreate` only (`deploy.sh:182-201`), so the label is not
     the proof after a renewal. The proof reads three things: the gateway's readiness after the
     restart; the engine's recorded start time of the container, which must be later than the swap
     of `current`; and a handshake from the gateway host presenting the new files and verifying
     `vllm-primary.<site>.<zone>`, which shows vLLM accepts them. It does not read the serial the
     process presents.
   - What a started gateway presents is established once, before launch 10, by task 5.4a's drill,
     extended. Against a local upstream that requires a client certificate and records the serial
     presented, the drill records: (1) after a start, the gateway presents the leaf `current` held
     at start; (2) after a swap and a restart, it presents the new one; (3) whether it reloads
     model-side files on change without a restart; (4) that the engine's start time changes on a
     restart. Launch 10 waits on that record. If (1) or (2) fails, Phase B does not proceed and the
     decision returns to this design. Reload `none` replaces the restart only as decision (b) of
     2026-11-21 says.

4. **(k) Every production launch.** One numbered list, 0 to 10. Each launch is lane C; manual
   launches follow ledger 3.20 and 3.22; `Deploy step-ca (Dev)` renders the name policy before any
   issuance (ledger 10.20). Release scope: launches 0 to 6 and the reply to dgx-spark ship in
   milestone v0.18.0 (tasks 10.5 and 10.6); launches 7 to 10 wait on dgx-spark serving TLS. The
   switch values `vllm_tls`, `image_server_tls` and `vllm_tls_require_client` are declared by the
   operator in the site-config inventory; nothing in this list reads them from the nodes.
   0. The operator-side inventory sync (`scripts/semaphore-sync-inventory.py`, preview, then
      `--apply`) after every site-config change, before the run that reads it. The changes: task
      10.4's declaration (before launch 1); each node's switch (launch 7); the Phase A inventory
      values (before launch 9); task 10.9's declaration (before launch 10's step-ca run);
      `vllm_tls_require_client` (launch 10).
   1. `Deploy step-ca (Dev)`: dry run, then real run. It renders the name policy with
      six new names (the five node SANs and `upstream.gateway.<site>.<zone>`) before any
      issuance; the run's name count rises by six.
   2. `Issue Internal Leaf (Dev)` on the head node's group, `leaf_name=vllm-server`: dry run, real
      run, then `leaf_action=inspect`.
   3. The same for `vllm-lifecycle` on the head node's group.
   4. The same for `image-server` on the second node's group.
   5. The same for `agw-upstream` on the gateway host's group.
   6. `Renew Internal Certs (Dev)` in check mode: every new leaf is classified and none is refused.
      The next scheduled run is the first real renewal.
   7. After dgx-spark serves TLS on a node: one site-config change turns that node's switch on
      (`vllm_tls` or `image_server_tls`), launch 0's sync, then `Renew Internal Certs (Dev)` with
      `--tags verify` reads the served serial at the declared address.
   8. The rotation drill with dgx-spark (its task 3.4): `Renew Internal Certs (Dev)` with
      `renew_threshold=1`.
   9. In dgx-spark's Phase A window (its task 3.3), after the Phase A inventory values and launch
      0's sync: `Deploy agentgateway (Dev)`, `Manage Caddy Sites (Dev)` and `Deploy o11y (Dev)`,
      each dry run first; `Rollback Inference Route (Dev)` `gateway-config` as a dry run only, once
      the read of route task 6.2's run 5370 is made.
   10. Phase B, in order:
       (a) task 10.9: the scrape's client leaf is named with its renewal proof class (open until
       then, task 10.9), declared, launch 0's sync, `Deploy step-ca (Dev)` dry run then real run,
       and `Issue Internal Leaf (Dev)` for it on the o11y host's group (dry run, real run,
       `leaf_action=inspect`);
       (b) task 5.4a's extended drill recorded (decision (j));
       (c) before dgx-spark's mutual-TLS restart (its task 4.3): `Deploy agentgateway (Dev)` as a
       real run whatever its dry run shows, `Manage Caddy Sites (Dev)` with the `caddy` leaf on the
       `/health` and `direct` transports, and `Deploy o11y (Dev)` with the scrape's client leaf,
       each dry run first;
       (d) after dgx-spark's 4.3: the `vllm_tls_require_client` change, launch 0's sync,
       `Renew Internal Certs (Dev)` with `--tags verify`, and gateway task 6.4's remaining legs.

## Open Questions

All four questions below were answered on 2026-09-28; see "Decisions recorded
2026-09-28". They are kept for the record.

1. **How dgx-spark receives its certificate.** The DGX Spark nodes are not in this
   repository's inventory, and the boundary keeps them dgx-spark's. Options: (a)
   dgx-spark generates the key and request on the head node and passes the request to a
   Semaphore template here that signs it and returns the certificate and bundle through a
   channel both sides can read; (b) the head node is added to this inventory for issuance
   only; (c) dgx-spark runs `step ca renew` itself, which needs the CA port opened to the
   head node (decision 2's rejected alternative). Default if unanswered: (a), with the
   channel agreed with the dgx-spark session.
2. **Offline root.** Local-dev keeps the root key in the volume. Production could remove
   the root key from the online CA after backup, leaving only the intermediate online.
   Default if unanswered: keep it online for the first rollout and record the choice.
3. **Mutual TLS towards vLLM.** Whether dgx-spark sets `--ssl-cert-reqs` so vLLM also
   requires the gateway's client certificate. Default: no; server TLS only, the vLLM API
   key stays the caller check, and the gateway's client leaf is added if that changes.
4. **The internal zone name for production SANs.** Local-dev uses `agent-cloud.test`;
   production needs its own internal-only zone name, declared in site-config. Default: a
   reserved or clearly internal suffix chosen by Joe, never a public zone.
