# Internal DNS naming: service, instance, host and management names per site

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Decisions by Joe, 2026-09-28: the site
label is part of every internal name from day one; a service name resolves to the load
balancer wherever one exists (agentgateway for inference, Caddy for HTTP), and to several
address records only where there is none; the decision is recorded both as a ratified
section of `plan/architecture/05-platform-infra.md` ("Internal DNS naming (decided
2026-09-28)") and as this change.

In every name below, `<zone>` is the internal zone and `<site>` is the site label. Both
are declared in site-config. This repository is public and never carries either value.

Companion changes: `production-internal-ca` (its leaf SANs move to these names; its
open question 4 asks for the production zone name, which stays a site-config value) and
`inference-gateway-agentgateway` (its group 7 needs internal records for the gateway's
SAN and a split-horizon answer for the identity provider).

## Why

Joe's request: "we'll want to have DNS names that simplify routing/access to those
resources. For example, dgx-spark can be dgx01.vllm-primary.<zone>. Think of a strong
naming strategy that allows us to horizontally scale resources through our internal DNS."
(The zone name in his message is replaced with `<zone>`.)

Today the internal zone is a wildcard plus a flat list of explicit records
(`platform/services/dns/deployment/templates/zone.local-dev.j2:15-18`), and hickory-dns
runs in local-dev only (`platform/services/dns/context/architecture.md:7`, `:45`). Nothing
says how a name is built, so each consumer invents its own: the production CA design uses
`agentgateway.<internal-zone>` and `vllm.<internal-zone>` as verification names that "need
no DNS records" (`plan/development/openspec/changes/production-internal-ca/design.md:186-189`),
and the NetBox reservation playbook writes whatever `dns_name` the caller passes
(`platform/playbooks/netbox-allocate-ip.yml:320`). Adding a second vLLM node, moving a
service to other hardware, or opening a second site would rename things that clients and
certificates depend on.

## What Changes

- A naming scheme with four families under one tree per site: service
  (`<service>.<site>.<zone>`), instance (`<class><NN>.<service>.<site>.<zone>`), host
  (`<hostname>.host.<site>.<zone>`) and management (`<hostname>.mgmt.<site>.<zone>`),
  with label rules, reserved labels and TTLs.
- A site-config inventory shape that declares every name: the site label and zone, the
  service list (with the load balancer that fronts each, if any), and per host its host
  name, management address and the instances it carries. Hosts this repository does not
  manage (GPU nodes, hypervisor nodes) are declared as entries in a
  variables list, `dns_records_only_hosts`, not as inventory hosts, so no play can
  target them.
- The DNS deploy renders service, instance, host, management and reverse (PTR) records
  from that declaration, refuses an invalid or reserved label and a CNAME that shares an
  owner with other data, and renders no wildcard in production.
- A read-only reconcile playbook that compares every declared host and management name
  and address with NetBox's IP-address records and fails on any disagreement.
- The internal CA's leaf SAN examples and the gateway's `tls.hostname` move to these
  names, through their own changes.
- The ratified section in `plan/architecture/05-platform-infra.md` (written with this
  change) and the DNS service's architecture notes.

## Capabilities

### New Capabilities
- `platform/internal-dns-naming`: how internal names are built, declared, rendered,
  validated and reconciled with IPAM, so that capacity scales out and roles move without
  any client or certificate changing the name it uses.

### Modified Capabilities
- None in the main specs. `production-internal-ca` and `inference-gateway-agentgateway`
  consume this capability through their own tasks.

## Impact

- agent-cloud files: `platform/services/dns/deployment/templates/zone.local-dev.j2`
  (renamed or joined by a production zone template), `named.toml.j2` (a reverse zone),
  `platform/playbooks/deploy-dns.yml` (render and validation), a new validation task and
  a new read-only reconcile playbook under `platform/playbooks/`,
  `platform/semaphore/templates.yml`, BATS tests under `platform/tests/`,
  `platform/services/dns/context/architecture.md`, `plan/architecture/05-platform-infra.md`,
  root `CLAUDE.md` workflow rows.
- site-config: the naming variables, `dns_instances` on each managed host, the
  `dns_records_only_hosts` list, and NetBox `dns_name` values rewritten to host names.
- NetBox: `dns_name` on the platform's IP-address records; no schema change.
  `netbox-allocate-ip.yml` sets `dns_name` only when it creates an address
  (`platform/playbooks/netbox-allocate-ip.yml:311-335`) and has no update path, so
  rewriting an existing value needs one (task 4.2).
- Live: production hickory-dns is not deployed. Its VM is provisioned, key-only over
  SSH and firewalled to its declared clients (2026-09-28/29), and `templates.yml` has no
  production DNS deploy template yet.
  This change defines what that deploy serves. Local-dev keeps its wildcard until its names are migrated.
- Out of scope, recorded: cross-site names under the bare `<service>.<zone>` form
  (reserved, not built); dynamic registration (the RFC 2136 challenge sub-zone of
  `platform/services/dns/context/architecture.md:45-46` stays its own work); which public
  names get a split-horizon answer (`inference-gateway-agentgateway` group 7). The
  mechanism that renders them, a declared list, is in scope since 2026-09-29 (design
  decision 15, task 2.5a).

## Rollback Plan

- The scheme lands as new records beside the existing ones, so rollback is removing the
  new declarations from site-config and re-running the DNS deploy template through
  Semaphore (`Deploy DNS (Local)`, `platform/semaphore/templates-local.yml:21`, locally;
  the production template arrives with the production DNS deploy); the zone returns to
  its previous content. No client moves to a new name until its own
  change switches it, and each of those switches back with one inventory value.
- The validation guard is part of the render. If it refuses a declaration that must go
  out, revert the guard commit on `dev` and redeploy; the guard holds no state.
- The reconcile playbook is read-only against NetBox and the zone; disabling its
  template in `templates.yml` and re-running `setup-templates.yml` removes it.
- NetBox `dns_name` rewrites go through the update path of task 4.2, which reports each
  address's value before it writes, so each old value can be written back the same way.
