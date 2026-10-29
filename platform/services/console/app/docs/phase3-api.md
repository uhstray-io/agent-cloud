# Dispatch Console: Phase 3 API (as built)

Author: Joseph A. Wisneski IV
Scope: server side of plan 17 task 6.3 (decisions D22-D25). Read-only NetBox and Proxmox
inventory. This file is the contract the UI worker mirrors. Field names and nesting are
exactly what the handlers in `src/server/inventory.rs` serialize.

## Who may call what

| Route | Who | Answer |
|---|---|---|
| `GET /v1/tenants/{t}/inventory` | members of tenant `t` (a tenant `groups` entry matches, or `platform-admins`) | the tenant's sites, joined to NetBox and Proxmox |
| `GET /v1/tenants/{t}/services` | same | the tenant's VMs that carry workflow fields |
| `GET /v1/tenants/{t}/services/{name}` | same | one service and its VM |
| `GET /v1/network` | `platform-admins` only | every prefix NetBox holds, global ones included |
| `GET /v1/flows` | `platform-admins` only | the data-flow catalogue |

- No session: `401 {"error":"unauthenticated"}`.
- Tenant routes, tenant the caller may not see, or tenant unknown: `404 {"error":"unknown_tenant"}`. The two
  answers are byte-identical, so a route cannot be used to learn that a tenant exists.
- Admin-only routes, caller not in `platform-admins`: `403 {"error":"forbidden"}`.
- Service name not in the tenant: `404 {"error":"unknown_service"}`.
- Unknown `/v1/*` paths: `404 {"error":"not_found"}` with a session.

Prefixes live only on the admin route. A prefix without a site names no tenant, so it cannot
sit in a tenant-scoped answer (D16, D25). The tenant inventory carries topology only.

## Common envelope

Every inventory, services and network answer starts with these fields:

```json
{
  "source":     {"netbox": "ok", "proxmox": "unconfigured"},
  "error":      {"netbox": null, "proxmox": null},
  "stale":      false,
  "updated_at": {"netbox": "2026-10-26T12:01:00Z", "proxmox": null}
}
```

- `source.<authority>` is `ok`, `unconfigured` or `error`. `ok` with `updated_at: null` means
  configured but no read has finished yet.
- `error.<authority>` is a fixed text such as `NetBox answered HTTP 500`, or `null`. It never
  holds a URL, a response body or a token.
- `updated_at.<authority>` is the time of the last successful read, or `null`. It is kept
  across a failed read.
- `stale` is `true` when any configured authority's latest read failed. The data served is then
  the last good read.

The envelope is per authority, not single, so the UI can say which source is down.

## `GET /v1/tenants/{t}/inventory`

```json
{
  "source": {...}, "error": {...}, "stale": false, "updated_at": {...},
  "sites": [
    {
      "id": "dc1", "name": "DC 1", "org": "ops",
      "netbox_site": "dc-1", "mapped": true,
      "clusters": [
        {
          "name": "pve-prod",
          "nodes": [
            {"name": "pve-01", "status": "online", "cpu": 0.12, "maxcpu": 16,
             "mem": 8589934592, "maxmem": 68719476736, "uptime": 123456}
          ],
          "vms": [
            {
              "name": "tududi", "vmid": 101,
              "status": "active", "live_status": "running",
              "vcpus": 2.0, "memory_mb": 4096, "disk_gb": 40,
              "primary_ip": "198.51.100.5/24", "tags": ["tududi-sync"],
              "workflow": {"steps": {"step-a": "pass", "step-b": "fail"},
                           "failed": ["step-b"], "last_run": "2026-10-26T12:00:00Z"},
              "rag": "red"
            }
          ]
        }
      ]
    },
    {"id": "dc2", "name": "DC 2", "org": "ops", "netbox_site": null, "mapped": false, "clusters": []}
  ]
}
```

- `sites` are the tenant's sites in scope-file order, flattened across orgs. `org` is the owning
  org id.
- `mapped` is `false` when the site declares no `netbox_site`, or NetBox holds no site with that
  slug, or NetBox is unconfigured or has no data. An unmapped site has `clusters: []`.
- `nodes` are the Proxmox nodes that host this cluster's matched VMs, sorted by name. A node with
  no matched VM of the cluster is not listed. Node units are Proxmox's: `cpu` is a fraction of
  `maxcpu`, `mem`/`maxmem` are bytes, `uptime` is seconds.
- VM `status` is the NetBox status. `live_status` is the Proxmox status, or `null` when no unique
  guest of that name exists (Proxmox unconfigured, or two guests share the name).
- `vmid` is the Proxmox id, or `null` when there is no live match.
- `primary_ip` is the one primary address NetBox holds, prefix length included, or `null`.
  Other addresses are never returned.
- `workflow` is `null` for a VM with none of the three `ac_*` fields. Otherwise `steps` maps each
  step id to its status (`pass`, `fail`, `skip`, or `unknown` for a non-string value), `failed`
  is the `ac_failed_steps` list, and `last_run` is `ac_last_run` or `null`.
- No secret custom field, no token and no other custom field is ever read or returned.

## `GET /v1/tenants/{t}/services`

```json
{ "source": {...}, "error": {...}, "stale": false, "updated_at": {...},
  "services": [
    {"name": "tududi", "site": "dc1", "cluster": "pve-prod", "live_status": "running",
     "rag": "red", "failed_steps": ["step-b"], "last_run": "2026-10-26T12:00:00Z", "tags": ["tududi-sync"]}
  ] }
```

Rows are the tenant's VMs with `workflow` set, in site and cluster order, then NetBox list order.
`site` is the scope site id. `failed_steps` is the sorted union of `ac_failed_steps` and every step
whose status is `fail`.

## `GET /v1/tenants/{t}/services/{name}`

```json
{ "source": {...}, "error": {...}, "stale": false, "updated_at": {...},
  "service": { ...same row as the list... },
  "vm": { ...same VM object as in the inventory... } }
```

When two VMs of one tenant share a name, the first in list order is returned.

## `GET /v1/network` (admin-only)

```json
{ "source": {...}, "error": {...}, "stale": false, "updated_at": {...},
  "prefixes": [
    {"prefix": "10.20.0.0/24", "vlan": 20, "description": "management", "site": "dc-1"},
    {"prefix": "192.0.2.0/24", "vlan": null, "description": "documentation", "site": null}
  ] }
```

- `vlan` is the VLAN id (`vid`), or `null`.
- `site` is the NetBox site slug the prefix is scoped to, or `null` for a global prefix. A prefix
  is attributed to a site when that site's own prefix list names it.
- No interfaces and no IP-address list: prefixes only (D24, coordinator decision for v1).

## `GET /v1/flows` (admin-only)

```json
{"source": "ok", "flows": [{"kind": "oidc", "from": "console", "to": "authentik", "label": "sign-in"}]}
```

`source` is `unconfigured` with `flows: []` when `CONSOLE_FLOWS_FILE` is unset. Each entry is names
only. An entry with any other key, such as an address, fails startup.

## RAG (one rule, `inventory::rag`)

First match wins:

| Colour | Condition |
|---|---|
| `red` | a workflow step failed (`failed` list or a `fail` status), or live `stopped` while NetBox says `active` |
| `amber` | the answer is stale; NetBox says `planned` or `staged`; a step is pending or unknown (any status other than `pass`, `skip`, `fail`); or live state is neither `running` nor (above) stopped |
| `grey` | no live state (Proxmox unconfigured or never read, or no unique name match) |
| `green` | live `running`, and nothing above applies |

A skipped step counts as settled. `stopped` while NetBox says `planned` is `amber`, not `red`.

## Refresh and caching

- One background task reads each configured authority every 60 s. The first read starts at boot.
- Requests read the cache only. The join is computed from the cache and the scope, so a request
  costs no upstream call.
- A read that fails keeps the last good data and sets `stale`. A repeated identical failure is
  logged once, not on each refresh.
- A list that reports a next page fails that read (`NetBox list has more than one page`). The
  console never shows a partial list as complete.
- The NetBox read is all-or-nothing per refresh: any failed request fails the whole NetBox read.

## Configuration

| Variables | Rule |
|---|---|
| `CONSOLE_NETBOX_URL`, `CONSOLE_NETBOX_TOKEN` | all-or-none; a v2 token (`nbt_` prefix) is sent as `Bearer`, a legacy token as `Token` |
| `CONSOLE_PROXMOX_URL`, `CONSOLE_PROXMOX_TOKEN_ID`, `CONSOLE_PROXMOX_TOKEN` | all-or-none; sent as `Authorization: PVEAPIToken=<id>=<secret>` |
| `CONSOLE_PROXMOX_CA_FILE` | optional PEM bundle added as roots for the Proxmox client only; counts toward the group, so alone it names `CONSOLE_PROXMOX_URL` as missing |
| `CONSOLE_FLOWS_FILE` | optional JSON array of flow entries |

- Cleartext `http://` is accepted only for `localhost`, an IPv4 loopback or RFC1918 address, or an
  IPv6 loopback address. Any other `http://` URL fails startup, naming the variable. `https://` is
  always accepted.
- Certificate verification is never turned off. Without `CONSOLE_PROXMOX_CA_FILE` the Proxmox
  client trusts the roots bundled with reqwest's `rustls-tls` feature (the Mozilla set via
  `webpki-roots`, not the operating system store). A self-signed Proxmox certificate then fails
  the read and shows as `source.proxmox: "error"`.
- The NetBox client shares the OIDC client, so `CONSOLE_EXTRA_CA_FILE` roots apply to it too.
- Tokens are never printed, returned or put in an error text.

## Not verified

- The NetBox 4.5 prefix filter `?site_id=` is used as the brief gives it. It is unverified against
  a live NetBox: no NetBox server and no NetBox source are in this repository.
- The NetBox VM fields `vcpus`, `memory`, `disk`, `primary_ip`, `tags` and the `custom_fields` shape
  are taken from the brief. `custom_fields` on a VM is confirmed by
  `platform/playbooks/collect-service-conformance.yml`; the other names are unverified live.
- The Proxmox `cluster/resources` field names are taken from the brief and the repository's test
  fixtures. Node `status` values are as Proxmox reports them; no live response was read.
