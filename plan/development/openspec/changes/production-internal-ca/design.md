# Design: a production internal CA

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

## Context

Verified 2026-09-27 by reading the files and sources named. Anything not verified is
marked as such.

**The service as it exists (agent-cloud).**

- `platform/services/step-ca/deployment/compose.yml`: image
  `docker.io/smallstep/step-ca:0.30.2` (line 17); first-run auto-init from
  `DOCKER_STEPCA_INIT_*` (lines 21-26), with the ACME provisioner on by default
  (`STEPCA_INIT_ACME` default `true`, line 26); port published on
  `${STEPCA_BIND:-127.0.0.1}:${STEPCA_PORT:-9000}` (line 29); everything under
  `/home/step` in the `step-ca-data` volume (line 31); health check `step ca health`
  against the in-volume root (line 36).
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
  chain needs both (lines 41-56). Used by the ERPNext, Postiz, tududi and agentgateway
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
