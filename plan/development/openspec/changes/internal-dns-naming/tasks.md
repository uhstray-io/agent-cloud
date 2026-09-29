# Tasks: internal DNS naming

Every task is safe to re-run: detect the state it would produce first, and converge or
skip when it already holds. Live work goes through Semaphore templates, never a shell on a
host. Push, pull requests and merges happen only when Joe authorizes each one. `<zone>`
and `<site>` stay placeholders in this repository; their values live in site-config.

## 0. Branch and decisions
- [x] 0.1 Feature branch from `dev` (`docs/internal-dns-naming`) in its own worktree,
      carrying this change and the ratified section in
      `plan/architecture/05-platform-infra.md` ("Internal DNS naming (decided 2026-09-28)")
- [ ] 0.2 Confirm the open questions with Joe, or record that the design's defaults apply:
      LAN clients and the gateway's mutual TLS (1), digit-first hostnames (2), `ca` or
      `step-ca` (3), which prefixes get reverse zones (4)
- [ ] 0.3 Validation gate: `openspec validate internal-dns-naming --strict` passes and the
      answers are written into `design.md` as dated amendments; this phase proves no spec
      scenario on its own and gates phase 1

## 1. Inventory declaration (site-config)
- [ ] 1.1 Declare the all-hosts naming variables in the shape of design decision 9:
      `dns_zone`, `dns_site`, `dns_sites`, `dns_services` (with `front` where a load
      balancer fronts the service: `inference` → `gateway`), `dns_retired_instances`,
      `dns_reverse_zones`. Values chosen by Joe; none of them enters this repository
- [ ] 1.2 On each managed host, declare `dns_instances` (and `dns_hostname`,
      `dns_address` or `dns_mgmt_address` only where the defaults do not hold): `vm01` of
      `gateway`, `openbao`, `semaphore`, `netbox`, `authentik`, `dns`, `ca` and `caddy`
- [ ] 1.3 Declare the all-hosts list `dns_records_only_hosts` (design decision 9) for
      machines this repository does not manage: the GPU nodes (in the design's example,
      `<gpu-head>` as `dgx01` of `vllm-primary`, serving, and `<gpu-worker>` as `dgx02`,
      `serves: false`) and the hypervisor nodes (host
      address, and a management address only where it is distinct). They are list
      entries, never inventory hosts or a group, so no `hosts:` pattern (including `all`
      or a `target_service` survey value) can reach them
- [ ] 1.4 Confirm no GPU or hypervisor node is an inventory host in the Semaphore
      inventory; any that is gets moved into the list by this change
- [ ] 1.5 `platform/inventory/local-dev.yml.example` gains the same variables with
      placeholder values and a local site label, beside the existing `dns_records`
- [ ] 1.6 Declare the site's concrete record map in site-config: which hosts carry which
      instances of which services, which machines are records-only, and every host and
      management address (each reserved in NetBox first). agent-cloud is a template and
      carries only the scheme, the renderer and role-placeholder examples; no real host
      name, vmid or address from any site enters this repository
- [ ] 1.7 Validation gate: `ansible-inventory --list` against the Semaphore inventory
      shows `dns_records_only_hosts` as a variable and none of its hostnames among the
      inventory's hosts; this phase proves no spec scenario on its own and gates phase 2

## 2. Render and guard (agent-cloud)
- [ ] 2.1 A record builder that reads every inventory host and returns the full record set
      (service, instance, host, management, PTR, SRV) with per-record TTLs, as a small
      Python module under `platform/services/dns/deployment/lib/` with pytest, following
      the precedent of `platform/services/caddy/deployment/lib/caddyfile_sites.py`, added
      to `testpaths` in `pyproject.toml`
- [ ] 2.2 The guard of design decision 8 runs in that builder and `deploy-dns.yml` calls it
      before its first template task, so a refusal writes no file: label pattern, 253
      character limit, reserved service labels (`host`, `mgmt`, `ns`, `_*`, every
      `dns_sites` entry), names outside `<site>.<zone>`, duplicates, a CNAME owner with
      other data, retired ordinals, a wildcard when `local_mode` is false. Each refusal
      names the offending label or record and prints no other inventory value
- [ ] 2.3 `zone.local-dev.j2` (or a production sibling rendered from the same record set,
      never a fork of the logic) writes the record set with per-record TTLs; the SOA
      minimum is asserted at or below the smallest service TTL (design decision 7)
- [ ] 2.4 `named.toml.j2` renders one Primary zone per `dns_reverse_zones` entry and a
      reverse-zone template renders its PTR records; `deploy-dns.yml` renders one file
      per reverse zone
- [ ] 2.5 pytest for every refusal of 2.2 and for the record set of the applied map
      (design "Applied to what exists today" in `05-platform-infra.md`), with a mutation
      check per guard: remove the guard, watch its test fail, restore it (the mutation
      rule of `CONTRIBUTING.md` "Writing BATS Tests", applied to pytest);
      it also proves a hostname that is both an inventory host and a
      `dns_records_only_hosts` entry is refused, and a list entry produces the same host,
      instance and PTR records a managed host would
- [ ] 2.6 Validation gate: the pytest suite proves scenarios "A reserved label is refused
      at render", "An invalid label is refused", "A retired ordinal is refused", "A
      load-balanced service points at the load balancer", "A non-serving member is named
      but not pooled", "Moving the front door is one record", "A role move changes one
      CNAME" (these two by diffing the rendered zone before and after the change) and
      "Records-only machines are never inventory hosts"

## 3. Live behaviour in local-dev
- [ ] 3.1 Through `Deploy DNS (Local)` with a local declaration, measure the pinned
      hickory-dns 0.26.0 answers left unverified in design.md: a CNAME answer carries the
      in-zone target's A record in the same response; every A record of a multi-member
      name is returned; whether their order rotates between queries; per-record TTLs are
      honoured; a reverse zone loads beside the forward zone; a zone-apex wildcard does
      not answer below an existing intermediate name. Record each result, with the query
      and its output, as a dated amendment to design.md; a failed expectation gets its
      fallback decided there before phase 6
- [ ] 3.2 `deploy-dns.yml`'s verify phase asserts one service, one instance, one host and
      one PTR answer from the declaration, and, when not in local mode, NXDOMAIN for an
      undeclared name under `<site>.<zone>`; the existing wildcard probe runs only in
      local mode
- [ ] 3.3 Read the pinned agentgateway version's source or documentation for how a model
      backend whose name has several A records is resolved and whether a failed address
      is taken out of use; record the finding with its citation in design.md. Until it
      is proven, a second serving `vllm-primary` member is listed to the gateway by
      instance name
- [ ] 3.4 Validation gate: against local hickory-dns, scenario "Scale-out adds an instance
      without a client change" (declare a second serving member, redeploy, query the
      service name) and scenario "A reverse lookup names the host" pass; scenario "A
      production wildcard is refused" passes through the render and through an NXDOMAIN
      query in a test run with `local_mode` false

## 4. NetBox agreement
- [ ] 4.1 A read-only reconcile playbook (proposed name `verify-dns-ipam.yml`) that
      compares every declared host and management name and address with NetBox's
      IP-address records and fails naming the address and both values on any
      disagreement. NetBox headers come from `tasks/netbox-api-headers.yml`; the token read
      is its own `no_log` task, the comparison loops over the clean declaration and
      indexes into the reads (`docs/MISTAKES.md` 4.6); identical under `--check`
- [ ] 4.2 An update path for `dns_name` on an address that already exists, in
      `netbox-allocate-ip.yml` (today it writes `dns_name` only on create,
      `platform/playbooks/netbox-allocate-ip.yml:311-335`): explicit opt-in, reports the
      current value before writing, read back after
- [ ] 4.3 Rewrite the platform's existing `dns_name` values (including the 2026-09-28
      reservations) to `<hostname>.host.<site>.<zone>` through 4.2, and reserve any
      declared address NetBox does not hold yet through the existing reserve path
- [ ] 4.4 Semaphore template for the reconcile in `platform/semaphore/templates.yml` with a
      `(Dev)` variant and a schedule declared as code; run `setup-templates.yml`
- [ ] 4.5 BATS for the reconcile's refusal paths and for the no-loop-over-registered-read
      rule (`platform/tests/test_no_request_in_loop_items.py` already covers the pattern;
      confirm it scans the new playbook)
- [ ] 4.6 Validation gate: with one `dns_name` deliberately differing in the local NetBox,
      the reconcile fails naming it and changes nothing, proving scenario "A NetBox and
      inventory mismatch fails"; after the value is corrected through 4.2 it passes,
      proving scenario "Agreement passes"

## 5. Consumers
- [ ] 5.1 Propose to `production-internal-ca`, as a dated amendment on that change (not an
      edit from here), that decision 4's SAN examples
      (`plan/development/openspec/changes/production-internal-ca/design.md:186-187`) and
      its declared leaf list use this scheme: each member leaf carries its instance and
      service names, the gateway leaf also carries `inference.<site>.<zone>`, and the
      names get DNS records instead of being verification-only; its open question 4
      (`:396-399`) keeps the zone name a site-config value
- [ ] 5.2 Propose to `inference-gateway-agentgateway` that task 7.2's records are the
      gateway's instance and service names, that task 6.2's `tls_server_name` is
      `gateway.<site>.<zone>`, and that task 6.3's model `tls.hostname` is
      `vllm-primary.<site>.<zone>`; task 7.1's split-horizon answer for the public
      identity-provider name stays that change's work
- [ ] 5.3 Hand the vLLM leaf's SANs (`dgx01.vllm-primary` and `vllm-primary`) to the
      dgx-spark session through the channel agreed in `production-internal-ca` open
      question 1
- [ ] 5.4 Validation gate: the declared leaf list in site-config carries the SANs of
      scenario "Leaf SANs follow the names" and the CA's issuance task accepts them (the
      CA change's own gate issues them)

## 6. Production
- [ ] 6.1 The production DNS deploy uses the scheme from its first run: the production
      declaration from phase 1, no wildcard, the reverse zones. If the production DNS
      deploy template does not exist yet in `platform/semaphore/templates.yml`, it is
      added there (with its `(Dev)` variant) by the change that deploys production DNS,
      and this task waits on it
- [ ] 6.2 Run the production DNS deploy through Semaphore, then the reconcile
- [ ] 6.3 Validation gate: against production hickory-dns, scenarios "A load-balanced
      service points at the load balancer", "A non-serving member is named but not
      pooled" and "A reverse lookup names the host" pass by query, an undeclared name
      answers NXDOMAIN, and scenario "Agreement passes" holds for the production NetBox

## 7. Documentation and archive
- [ ] 7.1 `platform/services/dns/context/architecture.md` "Conventions" gains the naming
      rules and a pointer to the ratified section; root `CLAUDE.md` gains the reconcile
      playbook's workflow row; `plan/architecture/05-platform-infra.md` gains a dated note
      under its section with the phase 3 measurements (the section's text is not edited)
- [ ] 7.2 Record the local-dev migration (local names onto the scheme, then the local
      wildcard removed) as its own follow-up change
- [ ] 7.3 Archive this change and retain the outcome into bank `agent-cloud-750a33b9`,
      labelled worked / dead end / corrected, with the root cause of anything that failed
- [ ] 7.4 Validation gate: `openspec validate internal-dns-naming --strict` passes before
      archive and every scenario above has been proved by the gate that names it; this
      phase proves no spec scenario on its own
