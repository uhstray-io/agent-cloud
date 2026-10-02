# Tasks: a production internal CA

Every task is safe to re-run: detect the state it would produce first, and converge or
skip when it already holds. Live work goes through Semaphore templates, never a shell on a
host. Push, pull requests and merges happen only when Joe authorizes each one.

## 0. Branch and decisions
- [ ] 0.1 Feature branch from `dev` (`feat/production-internal-ca`) in its own worktree
      2026-10-02: PARTIAL — no branch of that name exists on origin; the change landed
      through one feature branch per step, each based on `dev` and merged by pull request
      (#349, #352, #354, #359, #363, #367-#373, #376); remaining: amend this task to that
      practice, or record why the single branch was not used
- [x] 0.2 Confirm the open questions with Joe, or record that the design's defaults apply:
      the dgx-spark handoff channel (1), offline root (2), mutual TLS towards vLLM (3), the
      production internal zone name (4). Answered 2026-09-28: signed through a template,
      root online, mutual TLS towards vLLM, a zone under `.internal` declared in site-config (design "Decisions recorded
      2026-09-28")
- [x] 0.3 Validation gate: `openspec validate production-internal-ca` passes and the
      answers are written into `design.md` as dated amendments; this phase proves no spec
      scenario on its own and gates phase 1 (valid with `--strict` 2026-09-29)

## 1. CA host
- [ ] 1.1 site-config: declare the VM in `proxmox/vm-specs.yml` (template sizing unless
      `vm-rightsize` says otherwise; next free vmid checked against the live cluster and
      the ledger, not assumed) and a `step_ca_svc` group in `inventory/production.yml`
      with `service_name: step-ca`, `monorepo_deploy_path:
      platform/services/step-ca/deployment`, `container_engine: podman`,
      `stepca_bind: 127.0.0.1`, `stepca_init_acme: "false"`, a production `stepca_name`,
      and the firewall variables of task 3.1. Sync the Semaphore inventory record
      2026-10-02: PARTIAL — site-config#40 declared the VM in `proxmox/vm-specs.yml` and
      `inventory/production.yml` at the template sizing (two cores, 2 GB, 20G), vmid 221 as
      the first free id in the live cluster's listing (Semaphore task 1732); #49 added the
      first-boot settings (loopback bind, ACME off, production `stepca_name`) and #42 the
      firewall variables. Semaphore's inventory carries them: Deploy step-ca (Dev) 1989 and
      1990 passed the production assertions that refuse a run without them. Remaining: the
      vmid check against the ledger is not recorded
- [x] 1.2 Workflow steps `lookup-inventory` and `validate-address` (the address reserved
      in NetBox before provisioning, or the reason it could not be recorded as the
      agentgateway change did). 2026-09-28: address reserved in NetBox (Semaphore task
      1734, reserve mode) and the NetBox VM records created: Lookup Service Inventory
      1741/1742 and Validate Address Free 1749/1750, for the DNS and CA hosts
- [x] 1.3 Workflow steps `provision-vm`, `cloud-init`, `ssh-keys` (Generate Service SSH
      Key, Distribute SSH Keys), `ssh-key-backup` (Back Up Service SSH Key),
      `access-harden` (Verify Host Access, then Harden SSH). 2026-09-28/29, for the DNS
      and CA hosts as pairs: provision 1753/1755, key generate 1763/1764, backup
      1767/1768, Verify Host Access 1769/1770, distribute 1807/1808, Harden SSH 1811/1812
      (targets read back from Semaphore 2026-09-29)
- [x] 1.4 Validation gate: key-only SSH to the CA host works from the controller and from a
      workstation and password authentication is refused (2026-09-29: Harden SSH 1812 on the CA
      host passed its password-rejection probe; Joe's workstation key-only login confirmed); this is the precondition for
      scenario "Deploy converges and keeps the root"

## 2. Deploy step-ca to production
- [x] 2.1 `deploy-step-ca.yml`: create the issuing provisioners idempotently (read the
      provisioner list first; add only what is missing), one per profile or one with
      per-profile templates as task 4.4 decides; their passwords are new `random` entries
      in `_secret_definitions` (`secret/services/step-ca`), so manage-secrets generates
      them once and reuses them; maximum and default lifetime from a new
      `stepca_leaf_dur` (default `720h`). Keep the `admin` provisioner's local behaviour
      unchanged; make Phase 2.5's lifetime raise report `changed` only when the value
      actually differs. Done 2026-09-29: two JWK provisioners (`issuer-server`,
      `issuer-client`; decision 4.4 of 2026-09-29), planned from the provisioner list and
      added or updated only on a difference; the CLI sequence was checked against a
      throwaway step-ca 0.30.2 container (add of an existing name fails, so the list
      decides). Tests: `platform/tests/test_step_ca_deploy.py`
- [x] 2.2 Production parameters flow through the existing `env.j2` variables (bind,
      name, DNS names, ACME switch); add a parameter only where one is missing. Confirm the
      ACME provisioner is absent after first boot with `stepca_init_acme: "false"`.
      2026-09-29: Phase 1 refuses a production run without `stepca_name`, `stepca_dns_names`,
      `stepca_init_acme: "false"` and a loopback bind; Phase 3 asserts no ACME provisioner and
      the loopback publish (the image's `entrypoint.sh:80` enables ACME only for the literal
      `"true"`). Confirmed after first boot 2026-09-29: runs 1989 and 1990 passed the
      no-ACME assertion on the production CA
- [x] 2.3 `clean-deploy-step-ca.yml`: refuse unless `-e confirm_ca_reset=<inventory
      hostname>` names the target, following `destroy-vm.yml`'s `confirm_destroy`
      assertion; update `templates-local.yml`'s `Clean Deploy step-ca (Local)` and any
      make target that calls the playbook to pass it. Done 2026-09-29 (no make target calls
      it); the refusal is tested with no, a wrong and the right confirmation
- [x] 2.4 `platform/semaphore/templates.yml`: a `Deploy step-ca` template (production
      inventory, `main`) and its generated `(Dev)` variant; no production clean-deploy
      template; run `setup-templates.yml`. Amended 2026-09-29 (Joe): a production
      `Clean Deploy step-ca` template is added too, guarded by a required `confirm_ca_reset`
      survey with no default, so the reset refusal is proven in production. Both are declared
      as dev-bound `(Dev)` templates (`repository: agent-cloud dev`) with no main-bound twin
      until promotion, because main's playbooks lack the guards (review of #349). Published
      2026-09-29 through the scoped Dev publisher: `Deploy step-ca (Dev)` (publisher tasks
      1966 dry run, 1967) and `Clean Deploy step-ca (Dev)` (1968 dry run, 1969), bindings read
      back
- [ ] 2.4a `platform/semaphore/templates.yml`: a signing template for dgx-spark's vLLM
      request (decision 1 of 2026-09-28), with a Dev variant; it refuses a SAN that is not
      in the declared vLLM leaf and returns the certificate and bundle through the channel
      agreed with the dgx-spark session
- [x] 2.5 BATS: the provisioner step is idempotent in shape (reads before it adds), the
      reset refuses without confirmation, production inventory values render the loopback
      bind and ACME off. Done 2026-09-29 as pytest (`test_step_ca_deploy.py`, 17 cases,
      mutation-checked): the lifted tasks run against a stub host
- [x] 2.6 Run `Deploy step-ca (Dev)` twice; record the root fingerprint after each run.
      2026-09-29: dry run 1970 clean. Run 1971 created the root, raised the admin lifetime
      and failed at the issuer add (docs/MISTAKES.md 10.17, occurrence 2; fixed in #352).
      Run 1989 added both issuers, applied the pending reload and passed every production
      assertion; run 1990 changed nothing (no add, no reload). The root fingerprint printed
      by 1989 and 1990 is identical; its value is kept in site-config, not here. The three
      secrets were backed up to site-config (task 1992)
- [x] 2.7 Validation gate: scenarios "Deploy converges and keeps the root" and "Reset
      without confirmation is refused". 2026-09-29: the first by runs 1989/1990 (same
      fingerprint, second run a no-op); the second by `Clean Deploy step-ca (Dev)` dry run
      1993 with a wrong hostname, which refused at its first task and never reached the
      destroy play, so the root is unchanged, plus `test_step_ca_deploy.py`

## 3. Firewall
- [x] 3.1 site-config: `firewall_ssh_cidrs` and `firewall_controller_cidr` for the CA
      host, no `firewall_allow_rules`, and `firewall_detect_ports: false` (the API publishes
      on loopback only). Declared 2026-09-28 (site-config #42)
      2026-10-02: done — site-config#42 (merged 2026-09-29) gives the CA host
      `firewall_detect_ports: false`, `firewall_controller_cidr` and an empty
      `firewall_allow_rules`; `firewall_ssh_cidrs` is inherited from the `agent_cloud` group
      variables, as on every host. Applied by Apply Firewall (Dev) 1814, 1817, 1819, 2069 and
      2071
- [ ] 3.2 Workflow steps `fw-assess` (Snapshot Firewall) and `fw-harden` (Apply Firewall)
      2026-10-02: PARTIAL — `fw-harden` ran on the CA host: Apply Firewall (Dev) 1814, 1817
      and 1819 (2026-09-29), 2069 and 2071 (2026-09-30); remaining: `fw-assess`, because
      Snapshot Firewall has not run on the CA host
- [ ] 3.3 Workflow step `systemd-enablement` (Verify Service Persistence) and a reboot of the
      CA host through the supported path, then `service-validate`
      2026-10-02: PARTIAL — `systemd-enablement` ran on the CA host: Verify Service
      Persistence (Dev) 2073; remaining: the reboot and `service-validate`. The supported
      reboot path (Reboot Host (Dev), Probe Reachability (Dev)) is being built on branch
      `feat/reboot-and-reach-probe` and is not on `dev`
- [ ] 3.4 Validation gate: scenarios "A LAN host cannot reach the CA API" and "Only the
      declared sources reach SSH", checked from a LAN host that is not the controller
      2026-10-02: not started — the firewall these scenarios test is applied (task 3.2), but
      no check from a LAN host other than the controller is recorded

## 4. Cross-host issuance and root distribution
- [ ] 4.1 Evolve `tasks/mint-internal-cert.yml`: new optional `_mint_ca_host` (default:
      the play host), `_mint_name`, `_mint_profile`, `_mint_sans`; key and request
      generated on the consumer with `openssl` (asserted present); request copied to the
      CA host and signed there with `step ca sign` via `delegate_to`; certificate written
      back on the consumer into a new serial-named subdirectory and a `current` symlink
      swapped in one rename; the previous subdirectory kept until the next success. The
      existing wildcard interface keeps working for `deploy-caddy.yml`. 2026-09-30: built as
      `tasks/issue-internal-leaf.yml`, entered through `mint-internal-cert.yml` when `_mint_name`
      is set (the wildcard path unchanged otherwise). `_mint_profile` and `_mint_sans` were not
      added: both come from the leaf's declaration, so a caller cannot ask for more than it
      declares. A leaf with a current certificate is kept unless `_mint_reissue` (renewal is
      group 6). Proven end to end on a throwaway step-ca 0.30.2: client-only key usage, the
      declared SANs, leaf plus intermediate, `openssl verify -purpose sslclient` OK and
      `sslserver` refused, no key file left in the CA container. Open: 4.6, 4.7
      2026-10-02: PARTIAL — the cross-host path is proven in production: task 4.7 (issue
      2166, inspect 2249) and the edge leaves of task 5.2 (Issue Internal Leaf (Dev) 2431,
      2433, 2435), each key made on its consumer and nothing left in the CA container;
      remaining: the wildcard path through `deploy-caddy.yml` itself (task 4.6 proved it at
      task level only), and a dated design amendment for dropping `_mint_profile` and
      `_mint_sans`
- [x] 4.2 Declared-name guard: the task refuses any SAN not in the consumer's declared leaf
      (site-config list, decision 4) and any name outside `<site>.<zone>` (amendment
      2026-09-29), before
      anything reaches the CA; the existing hostname-character assertion stays. 2026-09-30:
      the leaf is looked up by name in `internal_leaves` and must be declared once, for this
      host, with a server or client profile, an absolute directory, and plain SANs under
      `<dns_site>.<dns_zone>`; the same rule as the CA's name policy
      2026-10-02: done — on `dev`, `tasks/issue-internal-leaf.yml` asserts the declaration
      before its first step that reaches the CA, and the SANs come only from the declaration,
      so a caller cannot add one; the wildcard path keeps its hostname-character assertion
      (`mint-internal-cert.yml`, "Refuse an extra SAN outside hostname characters").
      `test_issue_internal_leaf.py` covers 13 refused declarations, each with nothing sent
      to the CA
- [x] 4.3 Evolve `tasks/distribute-ca-root.yml`: optional `_ca_host`; root and intermediate
      read from the CA container on that host, bundle written 0644 on the consumer into
      the mounted certificate directory. 2026-09-30: one path for every case, the local
      single host included: the root and intermediate are read with `exec cat` on `_ca_host`
      (default the play host, through `delegate_to`, also under `--check`), refused unless two
      certificates come back, and written by `copy` (0644, changed only when the bundle
      differs; it was changed on every run before) to `<dir>/certs/step-ca-bundle.crt` or an
      exact `_ca_bundle_dest`. `platform/tests/test_distribute_ca_root.py`; proven against a
      throwaway step-ca 0.30.2: the bundle's root matches the CA's fingerprint and the
      intermediate chains to it
      2026-10-02: done — on `dev` (#367); one correction to the note above: since b5aa6184
      the bundle is rewritten in place (`cat >`, then `chmod 0644`), not by `copy`, and still
      only when it differs. Its production consumers are the gateway (#376) and Caddy (#377,
      open), recorded under task 5.2
- [ ] 4.4 Profiles: issue one server and one client test leaf in local-dev, inspect their
      extended key usage, and settle decision 5's mechanism (separate provisioners or
      x509 templates) and whether the CA can also enforce a name policy; record the result
      in `design.md`. 2026-09-30: measured on a throwaway step-ca 0.30.2 and recorded
      (design, "Findings 2026-09-30"): a template per issuer, now set and planned by
      `deploy-step-ca.yml` and asserted on the running CA in Phase 3; a CA-side name policy
      at authority level from the exact declared SANs (decided by Joe 2026-09-30), written,
      reload-checked and asserted by `deploy-step-ca.yml`. Open: the
      local-dev server and client test leaves, which need task 4.1's issuance path
      2026-10-02: PARTIAL — in production, Issue Internal Leaf (Dev) issued a server leaf
      (`agw-server`, 2431) and two client leaves (`agw-verifier` 2433, `caddy` 2435), and the
      inspections 2432, 2434 and 2436 found the extended key usage of each profile; remaining:
      the local-dev server and client test leaves this task names
- [ ] 4.5 BATS: no task step reads, copies or templates the consumer's key onto the CA host
      or the controller; the name guard is scoped to the issuance task; the symlink swap
      is a single rename; each assertion mutated once to watch it go red. 2026-09-30: as
      pytest, `platform/tests/test_issue_internal_leaf.py`, against a stub engine that keeps
      what the CA host receives (password line and request, never a key)
      2026-10-02: PARTIAL — on `dev`, the tests prove the CA host receives no key (the stub
      keeps what crossed: password line and request) and that exactly one step is delegated;
      remaining: an assertion that no step fetches the key to the controller, one that the
      name guard lives only in the issuance task, one that the `current` swap is a single
      rename, and a record of each assertion's mutation (#363 states this only for the
      change as a whole)
- [ ] 4.6 Local-dev regression: `Deploy Caddy (Local)` and `make local-bootstrap` still serve
      the wildcard, and every local consumer of the bundle still verifies the IdP. 2026-09-30, task level (decided
      with Joe): from `dev` at a8644078, against the live local step-ca, the reworked
      `mint-internal-cert.yml` wildcard path minted `*.agent-cloud.test`, the apex and
      `*.inference.agent-cloud.test`, which verify against the bundle the reworked
      `distribute-ca-root.yml` wrote; that bundle is byte-identical to the one postiz's local
      deploy holds, and TLS to the local IdP verifies with it. The full run through local
      Semaphore waits for the next local-dev refresh from `dev`, because local Semaphore runs
      the main checkout, which another session holds on an older branch
- [x] 4.7 Production proof with a throwaway leaf declared on the gateway host: issue it
      through a Semaphore run, check where its key exists, then remove the declaration and
      the files. Done 2026-09-30 to 2026-10-01, every step through `Issue Internal Leaf (Dev)` or
      `Deploy step-ca (Dev)`, with a dry run before each real one:
      - **Declared:** site-config#56 declared the `probe` client leaf. Task 2150 installed the
        CA name policy holding exactly its one SAN.
      - **Issued:** task 2166. The task output holds no key and no issuer password.
      - **Inspected:** tasks 2248 (dry run) and 2249 (real). Same serial as task 2166. The
        certificate verifies as a client and not as a server. The key is mode 0600 on the
        gateway host and matches the certificate. The issuance left nothing in the CA
        container, so the key exists only on the consumer.
      - **Files removed:** task 2251. Dry run 2252 then reported nothing left to remove.
      - **Declaration removed:** site-config#57. Its review found that an empty
        `internal_leaves` removed the CA's name policy, and step-ca 0.30.2 then issues any name
        (MISTAKES 10.20). Fixed in #371: with no leaf declared, the CA allows only
        `no-leaf-declared.invalid`.
      - **Policy closed:** task 2268 applied it; the reload was checked, Phase 3 asserted it,
        and the root fingerprint is unchanged. Dry run 2269 plans no further change.
- [ ] 4.8 Validation gate: scenarios "The private key never leaves the consumer", "An
      undeclared name is refused", "Local-dev issuance is unchanged in effect", "A server
      leaf is refused as a client" and "Bundles match across consumers"

## 5. Consumers
- [x] 5.1 Establish which compose file production Caddy runs from (`caddy_compose_dir` in
      inventory versus the monorepo's `compose.yml`) and add a read-only certificate
      directory mount to that file as code; redeploy Caddy through Semaphore
      2026-10-02: done — `Mount Caddy Certs (Dev)` (#373): its dry run 2414 read the running
      container's compose labels and established that production Caddy runs from the
      `compose.yml` of its flat compose project in `caddy_compose_dir`, not the monorepo's
      file; run 2415 added the read-only `/etc/caddy/certs` directory mount to that file and
      recreated Caddy through Semaphore; run 2416 changed nothing
- [ ] 5.2 Declare and issue the gateway server leaf and the three allowlisted client
      leaves of design decision 4 (`caddy` on the Caddy host, `agw-verifier` on the
      gateway host, `bench` on the benchmark VM once that host exists), each key generated
      on its own host through the same CSR flow; distribute the bundle to each
      consumer. The gateway's certificate directory is mounted as a directory in
      production and in the local overlay (replacing the single-file bundle mount), and
      its key is readable by the container's non-root user
      2026-10-02: PARTIAL — site-config#58 declared `agw-server` (server) and the
      `agw-verifier` and `caddy` client leaves; Deploy step-ca (Dev) 2427 set the CA's name
      policy to their six SANs (closed with none declared since 2268, #371). Issue Internal
      Leaf (Dev) issued each with its key made on its own host, and the inspections found the
      profile's key usage, the key 0600 and matching, and nothing left in the CA container:
      `agw-server` 2431/2432, `agw-verifier` 2433/2434, `caddy` 2435/2436. #376 (merged)
      mounts the gateway's whole `./certs` directory in the production TLS overlay and the
      local overlay, and makes the 0600 key readable by the image's non-root user (rootless
      `userns keep-id`; local-dev's `user:`). Remaining: `bench` (its VM does not exist), the
      bundle on the gateway (distributed by the deploy once `agw_listener_tls` is on; not
      run yet) and on Caddy (#377, open)
- [ ] 5.3 Hand the issued files to the companion change and nothing more: its task 6.1
      owns the gateway listeners' TLS, the client allowlist rule and its render guard, and
      its task 6.2 owns Caddy's transport. Here, confirm each consumer's leaf and key sit
      at `current/` in the directory its container mounts, with the key readable by the
      container's user, and record each leaf's reload action in the site-config
      declaration: none for `agw-verifier` and `bench` (their users open the files per
      call), `caddy reload --force` for `caddy` if task 5.1 found a directory mount and the
      container restart otherwise, and the gateway server leaf's action from task 6.3
      2026-10-02: PARTIAL — site-config#58 records each declared leaf's reload action: none
      for `agw-verifier`, `caddy reload --force` for `caddy` (task 5.1 found a directory
      mount), and restart for `agw-server` until task 6.3 measures the file watch; the
      `caddy` leaf directory sits under the directory run 2415 mounted. Remaining: confirm on
      each running container that `current/` is visible and its key readable by the
      container's user (the gateway mounts its directory only once listener TLS is
      deployed), and `bench`
- [ ] 5.4 dgx-spark handoff, per open question 1's answer: the vLLM server leaf and the
      bundle delivered through the agreed channel, with the SAN the gateway's model
      `tls.hostname` will use and the flags dgx-spark owns (`--ssl-certfile`,
      `--ssl-keyfile`, `--enable-ssl-refresh`, and for mutual TLS `--ssl-cert-reqs` with
      `--ssl-ca-certs` set to the internal root); nothing on the nodes is changed from here
- [ ] 5.4a The `agw-upstream` client leaf on the gateway host (decision 3 of 2026-09-28),
      issued like `agw-verifier`; rendered into the model's `tls.cert`/`tls.key`.
      unverified: whether agentgateway v1.5.0 reloads model-side TLS files on change;
      record it from the local drill and choose reload or restart in the renewal action
- [ ] 5.5 Validation gate: scenario "A client leaf authenticates"; the gateway-side
      scenarios (no client certificate refused, another client leaf refused, allowlisted
      verifier served, undeclared entry refused at render) are proven by the companion's
      task 6.4

## 6. Renewal and expiry alerting
- [ ] 6.0 Extract the conformance collector's inline Loki push
      (`collect-service-conformance.yml:271-285`) into `platform/playbooks/tasks/push-loki-lines.yml`,
      whose inputs are the Loki URL and the streams, and whose caller decides whether a
      failed push fails the run (the collector keeps its `failed_when: false`); switch the
      collector to it in the same change; BATS asserts the collector includes the task and
      carries no inline push. The renewal run, the personal-key reconcile and the benchmark
      results all push through it
- [ ] 6.1 `renew-internal-certs.yml`: for every declared leaf, read the current
      certificate's expiry on the consumer; when any leaf on a consumer host has less than
      a third of its lifetime left, re-issue every declared leaf on that host through task
      4.1, then run that host's reload action once (design decision 8). Prove each renewed
      leaf in use: a server leaf by connecting to the consumer's serving listener and
      failing on a serial mismatch; `agw-verifier` and `bench` by one request through the
      gateway probe path (`inference-gateway-agentgateway` task 6.1a) from the leaf's host
      with the new files, which must complete; `caddy` by `caddy reload --force` in the
      Caddy container succeeding, the serial in `current/` on the Caddy host equalling the
      issued one, and one request through Caddy's inference route completing. Caddy's own
      listening port is never checked for its client leaf. Push through
      `tasks/push-loki-lines.yml` one line per leaf and one for the intermediate, plus the
      run's scheduled-job result line (task 6.4). Emit the step result
- [ ] 6.2 `templates.yml`: `Renew Internal Certs` with a daily `schedule:` declared as
      code; run `setup-templates.yml`
- [ ] 6.3 Rotation drill in production: temporarily set the renewal threshold so every leaf
      is inside its window, run the template, and confirm the gateway's serving listeners
      present the new server serial (recording whether the gateway's file watch picked up
      the swap with no restart, which fixes its reload action), each client leaf passes its
      proof in 6.1, and no request fails on the public path during the run (a paced
      request loop through the public hostname)
- [ ] 6.4 o11y `alerts.yml.j2`: leaf under seven days, intermediate under ninety, and the
      shared "Scheduled job silent" rule (design decision 10) over a declared list of
      scheduled jobs and the longest silence each may keep; this change declares
      `renew-internal-certs` at thirty-six hours. Each listed job pushes one result line per
      run under the bounded `job` label through `tasks/push-loki-lines.yml`. Deploy o11y
      through Semaphore
- [ ] 6.5 Alert drill: a canary leaf declared with a lifetime under seven days fires the
      expiry alert; pausing the schedule past the window fires the silent-job alert (or the
      rule's `for` window shortened for the drill and restored); both reach the contact
      point, then the canary is removed
- [ ] 6.6 Validation gate: scenarios "A server leaf inside its window is renewed and
      served", "Caddy's client leaf is renewed and loaded", "A per-call client leaf is
      renewed and proven through the probe path", "A fresh leaf is left alone", "A leaf
      near expiry raises an alert" and "A silent renewal job raises an alert"

## 7. Backup and restore
- [ ] 7.1 `backup-step-ca-to-site-config.yml`: read the CA's certificates, encrypted keys
      and configuration out of the container on the CA host and write them into a
      site-config clone under `secrets/step-ca/` through `tasks/site-config-clone.yml` and
      `tasks/site-config-push.yml`, a new branch per run, file names only in the output;
      the key-bearing steps `no_log`, nothing else
- [ ] 7.2 `templates.yml`: `Back Up step-ca to site-config`; run it, then run `Back Up
      Credentials to site-config` with `credential_service=step-ca` for the passwords
- [ ] 7.3 Restore drill in local-dev: restore the backed-up material into a fresh volume,
      start the CA, compare the root fingerprint, then return local-dev to its own CA
- [ ] 7.4 Validation gate: scenarios "Restore keeps the root" and "Backup output carries no
      key material"

## 8. Rollback path
- [ ] 8.1 Confirm each consumer's internal-TLS settings are inventory values with the plain
      transport as the default (Caddy blocks, gateway listeners, the gateway's model entry)
- [ ] 8.2 In local-dev, remove the values for Caddy, redeploy, check the public-path
      equivalent, then restore them
- [ ] 8.3 Validation gate: scenario "Caddy returns to a plain upstream"

## 9. Documentation and archive
- [ ] 9.1 `plan/architecture/05-platform-infra.md`: line 483 to `tls_trust_pool file
      <bundle>`; a mutual-TLS row with `tls_client_auth <cert> <key>` and
      `tls_server_name`; a dated exception to the plain-HTTP default policy for the
      inference path, naming this change; line 201's plan pointer corrected
- [ ] 9.2 Stale pointers to `plan/development/INTERNAL-CA-DEPLOYMENT.md` in
      `platform/services/step-ca/deployment/compose.yml`, `deploy-step-ca.yml` and
      `tasks/mint-internal-cert.yml` point at `plan/archive/development/`; the task header
      no longer says cross-host is out of scope. 2026-09-29: the three pointers now name
      the archived plan and the change, and the task header no longer claims production
      uses ACME; its cross-host sentence changes with task 4.1
- [ ] 9.3 `platform/services/step-ca/context/architecture.md`: a production section (own
      host, loopback API, consumer-side keys, declared leaves, renewal, backup, reset
      guard); root `CLAUDE.md`: workflow rows for the new templates and the widened
      `secret/services/step-ca` row. 2026-09-29: the production section records what is
      deployed and names the pending task groups; the root `CLAUDE.md` rows landed with
      #349. Renewal and backup are added to the section when groups 6 and 7 land
- [ ] 9.4 Validation gate: scenario "No deprecated directive remains"; `openspec validate
      production-internal-ca` passes; on archive, retain the outcome (worked / dead end /
      corrected) into bank `agent-cloud-750a33b9`
