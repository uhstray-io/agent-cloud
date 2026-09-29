# Design: internal DNS naming

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

`<zone>` is the internal zone and `<site>` is the site label; both are declared in
site-config and never written in this repository.

## Context

**The DNS service as it exists (agent-cloud).** hickory-dns is authoritative for one zone
and forwards everything else (`platform/services/dns/context/architecture.md:10-15`). The
zone file is a SOA, an NS, an apex A, a wildcard A and a loop over a flat `dns_records`
list of `name`/`type`/`value` entries
(`platform/services/dns/deployment/templates/zone.local-dev.j2:5-18`); one `$TTL` covers
every record and the same value is the SOA minimum (`:5`, `:11`). `named.toml.j2` declares
one Primary zone and the forward store (`platform/services/dns/deployment/templates/named.toml.j2:10-31`).
`deploy-dns.yml` renders the config, the zone and the `.env` on the `dns_svc` hosts
(`platform/playbooks/deploy-dns.yml:15-53`), and its verify asserts only that a wildcard
probe resolves and that forwarding answers (`:100-127`). The pinned image is
`docker.io/hickorydns/hickory-dns:0.26.0` (`platform/services/dns/deployment/compose.yml:18`).
hickory-dns runs in local-dev only; production is planned
(`platform/services/dns/context/architecture.md:7`, `:45`), and the production DNS VM is
being onboarded (`platform/semaphore/templates.yml:1503-1504`).

**Authorities.** NetBox is the IPAM authority
(`plan/development/03-guardrails-governance.md:1217`), hickory-dns is the authority for
internal names, rendered from inventory and consuming NetBox-allocated addresses (`:1227`),
and the chain NetBox → hickory-dns → step-ca runs one way only (`:1301-1303`, D8).
`netbox-allocate-ip.yml` writes a caller-supplied `dns_name` when it creates an address
(`platform/playbooks/netbox-allocate-ip.yml:311-335`) and never updates an existing one.
NetBox's own `dns_name` validator accepts upper case, underscores and a `*` label
(`netbox/ipam/validators.py:87-91` on the netbox-community/netbox default branch, read
2026-09-28 through `gh api`), so NetBox does not enforce the label rules below. The
repository's NetBox image is a local build tag (`netbox:latest-plugins`,
`platform/services/netbox/deployment/docker-compose.yml:7`), so the exact NetBox version in
use is not established here.

**Consumers.** The production CA design uses verification names such as
`agentgateway.<internal-zone>` and `vllm.<internal-zone>` that "need no DNS records"
(`plan/development/openspec/changes/production-internal-ca/design.md:182-190`, decision 4)
and leaves the production zone name open (`:396-399`, open question 4). The gateway change
waits on internal records for its server leaf's SAN and a split-horizon answer for the
identity provider
(`plan/development/openspec/changes/inference-gateway-agentgateway/tasks.md:349-363`,
group 7). The DGX Spark nodes are not in this repository's inventory
(`plan/development/openspec/changes/production-internal-ca/design.md:382-384`); spark-1
serves the OpenAI-compatible API and spark-2 is its Ray worker (`AGENTS.md:460`).

**Standards read for this design (fetched 2026-09-28 from rfc-editor.org).**
RFC 1035 §2.3.1: a label starts with a letter, ends with a letter or digit, and has only
letters, digits and hyphens inside; §2.3.4: labels are at most 63 octets and names at most
255. RFC 1034 §3.6.2: "If a CNAME RR is present at a node, no other data should be
present". RFC 2782: the SRV owner is `_Service._Proto.Name`, and the target "MUST NOT be
an alias". RFC 2308 §5: a negative answer's TTL is the minimum of the SOA MINIMUM field and
the SOA's TTL. RFC 4592 §2.2.1: a wildcard does not answer for a name when that name or a
name between it and the wildcard exists. RFC 9525 §6.3: a certificate wildcard is the
whole left-most label and matches exactly one label.

**What hickory-dns 0.26.0 is known to do.** Its zone-file parser builds CNAME, PTR and SRV
records (`crates/proto/src/rr/record_data.rs:1069`, `:1084`, `:1087` at tag `v0.26.0` of
hickory-dns/hickory-dns, read through `gh api` 2026-09-28). Parsing is established; how
the authority answers them is not. Unverified until task 3.1 runs against the pinned
image: whether a CNAME answer carries the in-zone target's A records in the same
response, whether every A record of a multi-member name is returned, whether their order
rotates between queries, whether per-record TTLs are honoured, whether a second Primary
zone for `in-addr.arpa` loads beside the forward zone, and whether a wildcard stops at an
existing intermediate name as RFC 4592 requires.

## Goals / Non-Goals

**Goals.** Names that clients never change when capacity is added, a member is replaced
or a service moves to other hardware; one tree per site so a second site adds names
without renaming any; every name declared as code, rendered, validated at render, and
reconciled against IPAM.

**Non-Goals.** Cross-site names (the form is reserved, nothing is built); dynamic
registration (the RFC 2136 challenge sub-zone stays separate,
`platform/services/dns/context/architecture.md:45-46`); health checking in DNS; LAN
answers for public hostnames (the gateway change's group 7); choosing the production zone
name or site label (site-config values, chosen by Joe).

## Decisions

1. **Every internal name carries the site label (Joe, 2026-09-28).** Names live under
   `<site>.<zone>`. A second site is a second subtree, and nothing in the first one is
   renamed. Alternative rejected: *no site label* (`<service>.<zone>`). It is shorter
   today, and on the day a second site arrives every client, certificate and dashboard
   that uses the name must change at once, while both sites are live. The label costs one
   label now; its absence costs a platform-wide rename later.

2. **Four families, hierarchical.**

   | Family | Shape | Record | TTL |
   |---|---|---|---|
   | Service | `<service>.<site>.<zone>` | CNAME to the front's service name, or one A per serving member | 30–60 s |
   | Instance | `<class><NN>.<service>.<site>.<zone>` | CNAME to the member's host name | 60 s |
   | Host | `<hostname>.host.<site>.<zone>` | A and PTR | 3600 s |
   | Management | `<hostname>.mgmt.<site>.<zone>` | A and PTR when the management address is distinct; otherwise CNAME to the host name | 3600 s |

   The address of a machine is written once, in its host record; every other name reaches
   it through a CNAME or is rendered from the same declaration. Alternative rejected:
   *flat names* (`dgx01-vllm-primary.<site>.<zone>`). A flat name cannot be delegated to
   another server per service, cannot hold a per-service subtree for SRV records, and
   buries the service/instance relationship in a string that every tool has to parse
   its own way.

3. **A service resolves to its load balancer where one exists; otherwise to the A records
   of its serving members (Joe, 2026-09-28).** A service declared with a `front` renders
   as a CNAME to the front's service name: `inference` → `gateway` (agentgateway), and an
   HTTP service → `caddy` once Caddy carries a route for that internal name. A service
   without a front renders one A record per serving member, taken from each member's host
   record. Moving the front door is one record. Alternative rejected: *always several A
   records, even behind a load balancer.* DNS carries no health: a failed member stays in
   the answer until its record is removed, and resolvers keep the stale answer for the
   TTL. Taking a failed member out is the load balancer's job, and the name belongs to
   it (whether agentgateway does so for its upstreams is checked in task 3.3).
   Alternative rejected: *the service's A record holds the load balancer's address
   directly.* Moving the load balancer would then mean editing every service it fronts;
   the CNAME keeps it to the one record the load balancer owns.

4. **Service names say what the service does; hardware appears only in the instance
   class.** Service names are a function plus a variant (`vllm-primary`, `vllm-embed`,
   `gateway`) or the platform product clients ask for by name (`openbao`, `semaphore`,
   `netbox`, `authentik`). The instance class is the kind of machine (`dgx`, `vm`).
   Alternative rejected: *hardware in service names* (`dgx-vllm`). Moving the model to
   other hardware would rename the service and every client, certificate and gateway
   entry that uses it, which is the rename this scheme exists to prevent.

5. **Instance ordinals are two digits and never reused.** `dgx01`, `dgx02`; a retired
   ordinal is listed in site-config (`dns_retired_instances`) and the render refuses to
   declare it again. A replacement machine gets the next ordinal. Certificates, log lines,
   dashboards and resolver caches that still carry a retired name then can never reach a
   different machine. Alternative rejected: *reuse the ordinal for the replacement.* It
   reads tidier, and it silently attaches the old machine's history, alerts and cached
   answers to a new one.

6. **A member that does not serve keeps its instance name and is left out of the pool.**
   spark-2 is `dgx02.vllm-primary.<site>.<zone>` with `serves: false`: its instance CNAME
   is rendered, and it is excluded from the `vllm-primary` A set and from every
   load-balancer backend list. The instance name records which deployment the machine
   belongs to (the model is split across both nodes; losing spark-2 takes
   `vllm-primary` down), which is what an operator looking for it needs. Alternatives
   rejected: *a separate service* (`vllm-primary-worker` or `ray`), because a service
   name promises clients something to connect to and there is nothing; *host name
   only*, because it drops the deployment membership that the name exists to show.

7. **Health lives in the load balancer; DNS TTLs stay short where membership changes.**
   Service names carry 30–60 s, instance names 60 s, host and management names 3600 s.
   The SOA minimum stays at or below the service TTL, because a client that asked for a
   name before it existed caches the NXDOMAIN for that long (RFC 2308 §5), which would
   delay a scale-out. A name answered by several A records has no health checks, and the
   render says so in a zone-file comment on each such name.

8. **Label rules and reserved labels are enforced at render.** Every label matches
   `^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$` (RFC 1035 §2.3.1 and §2.3.4, lower case only, so
   two spellings of one name never both appear), and the full name is at most 253
   characters. Underscores appear only in SRV owner labels. Reserved and refused as
   service names: `host`, `mgmt`, `ns` (the apex NS target,
   `platform/services/dns/deployment/templates/zone.local-dev.j2:12-13`), any label
   starting with `_`, and every declared site label (`dns_sites`). The bare
   `<service>.<zone>` form is reserved for future cross-site names and the render refuses
   any declaration outside `<site>.<zone>` except the SOA, NS and apex records. The
   render also refuses a CNAME owner that carries any other record (RFC 1034 §3.6.2) and a
   name declared twice. The guard runs before any file is written, so a refused
   declaration leaves the running zone untouched. NetBox cannot be the guard, because its
   validator accepts what these rules forbid (context).

9. **Records are declared in site-config inventory, in this shape.** The render reads
   every host in the inventory, not only the `dns_svc` group.

   ```yaml
   # site-config, all-hosts variables
   dns_zone: <zone>
   dns_site: <site>
   dns_sites: [<site>]              # every site label in use; all reserved
   dns_services:                    # one entry per service name
     - {name: inference, front: gateway}
     - {name: gateway}
     - {name: vllm-primary}
     - {name: openbao}
   dns_retired_instances: []        # e.g. [{service: vllm-primary, instance: dgx02}]
   dns_reverse_zones: [<prefix>]    # the IPv4 prefixes that get PTR records

   # site-config, per managed host (any *_svc group)
   dns_hostname: <hostname>         # default: inventory_hostname
   dns_address: <address>           # default: ansible_host
   dns_mgmt_address: <address>      # optional; a distinct management address
   dns_instances:
     - {service: gateway, instance: vm01}          # serves: true unless stated

   # site-config, a records-only group for hosts this repository does not manage
   dns_records_only:
     hosts:
       <spark-1 hostname>:
         dns_address: <address>
         dns_instances: [{service: vllm-primary, instance: dgx01}]
       <spark-2 hostname>:
         dns_address: <address>
         dns_instances: [{service: vllm-primary, instance: dgx02, serves: false}]
       <proxmox node>:
         dns_address: <address>
         dns_mgmt_address: <address>
   ```

   Records-only hosts carry no `ansible_host` and must never be a play's target. No play
   in `platform/playbooks/` uses `hosts: all` today (grep of every `hosts:` line,
   2026-09-28); plays name a group, `localhost`, or a `target_service` survey value
   (`hosts: "{{ target_service }}"` appears in 11 plays and two more default it). A survey
   value could name the records-only group, so the shared target-group preflight refuses
   it (task 1.4), and a BATS test fails on any play that names it or `all` (task 3.4).
   The existing flat `dns_records` list stays accepted during migration and passes the
   same guard. Alternative rejected: *a single central record list* in the DNS host's
   variables. It separates a host's name from the host's declaration, so moving a
   service between hosts would mean editing two places that nothing ties together.

10. **NetBox records the host name, and a read-only reconcile proves the two agree.** Each
    host address in NetBox carries `dns_name` = `<hostname>.host.<site>.<zone>`, and each
    distinct management address `<hostname>.mgmt.<site>.<zone>`. Service and instance
    names are not IP-address properties (one address serves many names) and are not
    written to NetBox. A new playbook compares every declared host and management name and
    address with NetBox's IP-address records and fails, naming the address, on any
    missing address, different address or different `dns_name`. It writes to neither
    side. Direction stays NetBox → DNS (D8): an address is reserved in NetBox first,
    through `netbox-allocate-ip.yml`, and the inventory carries the same value. The
    reservations made on 2026-09-28 used bare labels as `dns_name` (reported by the
    session that made them; not re-read here); those are rewritten to host names through
    an update path added in task 4.2. Alternative rejected: *generate the zone from
    NetBox.* It is the stronger single-authority story, and it makes every DNS render
    depend on a live NetBox read and the NetBox token on the DNS path; the reconcile gets
    the agreement guarantee without that runtime dependency. It stays open as a later
    change once NetBox's own availability is proven.

11. **Production renders no wildcard.** A mistyped name answers NXDOMAIN instead of
    resolving to a default target. Under RFC 4592 §2.2.1, a zone-apex wildcard would not
    answer inside `<site>.<zone>` once any name exists there anyway, so keeping it would
    only make behaviour differ by subtree. Local-dev keeps its wildcard
    (`zone.local-dev.j2:15`) until its names move to the scheme; the render refuses a
    wildcard when `local_mode` is false.

12. **PTR records exist for host and distinct management addresses only.** One PTR per
    address, naming the host (or management) name. Service and instance names never get
    a PTR, because an address with several PTRs has no single answer. Each prefix in
    `dns_reverse_zones` becomes a Primary zone in `named.toml`.

13. **SRV records, when a client needs one, target host names.** Owner
    `_<svc>._<proto>.<service>.<site>.<zone>`; targets are host names, because an SRV
    target "MUST NOT be an alias" (RFC 2782) and instance names are CNAMEs. No client
    needs SRV today; the render supports it so the owner-label exception in decision 8
    has a use.

14. **Certificates follow the names.** A member's server leaf carries its instance name
    and its service name as SANs (`dgx01.vllm-primary.<site>.<zone>` and
    `vllm-primary.<site>.<zone>`). A load balancer's leaf also carries every service name
    that CNAMEs to it, because a client verifies the name it asked for, not the CNAME
    target: the gateway leaf carries `vm01.gateway`, `gateway` and `inference`. The
    gateway's model `tls.hostname` is the upstream's service name
    (`vllm-primary.<site>.<zone>`) and Caddy's `tls_server_name` towards the gateway is
    `gateway.<site>.<zone>`. A certificate wildcard matches one label (RFC 9525 §6.3), so
    the local one-label wildcard (`plan/architecture/05-platform-infra.md:201`) covers no
    name under a site label. The production CA's decision 4 examples move to this scheme
    through a task on that change (task 5.1 here proposes it); this change does not edit
    it.

15. **Public names stay in the public zone.** `auth.uhstray.io` and the other
    Cloudflare-fronted names are not rendered into the internal zone, and the render
    refuses a name outside `<zone>`. A LAN answer for a public name is split-horizon work
    owned by `inference-gateway-agentgateway` tasks 7.1–7.3.

## Risks / Trade-offs

- **hickory-dns answer behaviour is unverified** (context). If CNAME answers do not carry
  the in-zone target, clients pay a second query; if multi-A order never rotates, every
  client picks the same first member. Task 2.1 measures both before any client depends
  on them; the fallback for the second is a load balancer in front of the pool.
- **A pool of A records keeps a dead member.** Accepted where no load balancer exists;
  the 30–60 s TTL bounds the cache, and removing the record is one inventory edit and a
  redeploy.
- **agentgateway's handling of a multi-A upstream is unverified.** Whether the gateway
  spreads requests across every A answer for `vllm-primary` and stops using a failed one
  is not established. Until task 3.3 proves it, a second serving member is listed to the
  gateway by instance name.
- **Longer names.** `dgx01.vllm-primary.<site>.<zone>` is long to type. Clients use the
  service name; instance names are for operators and certificates.
- **Two declarations of one address** (NetBox and inventory) can drift. The reconcile
  playbook is the control; it runs on a schedule and in the DNS deploy's verify.

## Migration Plan

1. Guard and render land with local-dev declarations only; the local wildcard stays.
2. Production DNS deploys with the scheme from its first run (no wildcard, host and
   management names, the records-only group).
3. NetBox `dns_name` values are rewritten to host names; the reconcile passes.
4. Consumers move one at a time through their own changes: the CA's leaf SANs, the
   gateway's `tls.hostname`, Caddy's `tls_server_name`, then clients to service names.
5. Local-dev names migrate and the local wildcard is removed, in a separate change.

## Open Questions

1. **LAN clients and the gateway's mutual TLS.** `inference.<site>.<zone>` CNAMEs to the
   gateway, whose listeners refuse a caller without a client certificate once the gateway
   change's group 6 lands
   (`plan/development/openspec/changes/inference-gateway-agentgateway/tasks.md:336-337`, task 6.4).
   A LAN client holding only an API key cannot use the internal name. Options: point
   `inference` at Caddy instead, issue client leaves to LAN clients, or keep LAN clients
   on the public name. Default if unanswered: the gateway, per Joe's decision, with LAN
   clients on the public name until he decides.
2. **Hostnames that start with a digit.** Decision 8 follows RFC 1035's letter-first rule.
   Whether any live hostname starts with a digit is not checked here (the hostnames are
   in site-config). Default: the guard refuses, and such a host is renamed or given a
   `dns_hostname`.
3. **`ca` or `step-ca`.** The service name for the internal CA is `ca` in Joe's list,
   while the 2026-09-28 reservation used `step-ca` (as reported). Default: `ca`, the
   function clients ask for.
4. **Which prefixes get reverse zones.** Default: every NetBox prefix that holds a
   declared host or management address, listed in `dns_reverse_zones`.
