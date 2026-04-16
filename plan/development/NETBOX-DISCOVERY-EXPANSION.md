# NetBox Discovery Expansion Plan

**Date:** 2026-04-04 (revised 2026-04-05 after cross-team review)
**Status:** APPROVED — Reviewed by infrastructure, network, automation, and architecture specialists
**Context:** The orb-agent discovers IPs and SNMP devices. This plan expands discovery to cover Proxmox VMs, richer pfSense data, SNMPv3 security, and network topology.

---

## Current State

**Working:**
- **network_discovery** — SYN scan finds 32+ live IPs and open ports on 192.168.1.0/24
- **snmp_discovery** — Enriches SNMP-responsive devices (pfSense gateway: hostname, manufacturer, model, interfaces, MACs)
- **Orb Agent** — Running with dedicated AppRole, vault-integrated credentials, 4h token TTL

**Gaps:**
- Most devices don't respond to SNMP (Proxmox VMs, Docker hosts, workstations)
- No Proxmox VM/LXC discovery (40+ virtual machines invisible to NetBox)
- pfSense data limited to SNMP (missing serial, version, ARP table, gateway details)
- SNMP v2c community strings are plaintext on the wire
- No network topology mapping (LLDP/CDP)

---

## OS Detection — ENABLED (needs validation)

`os_detection: true` added to the subnet_scan scope in `agent.yaml.j2`. The CLAUDE.md previously stated this caused `exit status 1`, but that was documented against an older orb-agent version. The current v2.7.0 agent runs nmap internally with `--privileged` and `CAP_NET_RAW`, which should support `-O`. If scans fail after deployment, remove the flag — the rest of discovery continues working without it.

### ~~NAPALM/SSH device_discovery~~ — DROPPED
**Reason:** No viable NAPALM drivers exist for the target devices:
- **pfSense** runs FreeBSD — no NAPALM FreeBSD driver. The `ios` driver won't work. The pfSense REST API (Phase 1) provides better data anyway.
- **Proxmox nodes** — the NAPALM `linux` driver exists but only returns hostname/uptime. Cannot enumerate interfaces, IPs, or hardware. The Proxmox API worker (Phase 2) is the correct approach.
- **Network specialist verdict:** "No viable drivers exist. Use REST APIs instead."

---

## Revised Expansion Plan

### Prerequisite: Deduplication Strategy

**Before adding any new discovery backends**, define how NetBox handles the same entity from multiple sources.

**Problem:** If network_discovery, snmp_discovery, and the Proxmox worker all discover the same host, Diode may create duplicate Device objects or silently overwrite fields.

**Merge rules:**
| Entity | Authoritative Source | Fallback Source |
|--------|---------------------|-----------------|
| Proxmox VM device | Proxmox API worker | — |
| Proxmox VM interfaces/IPs | QEMU guest agent (via API) | network_discovery |
| Proxmox node device | Proxmox API worker | SNMP |
| pfSense device | pfSense REST API sync | SNMP |
| pfSense interfaces/IPs | pfSense REST API sync | SNMP |
| Network-only IPs (no device) | network_discovery | — |
| Unknown SNMP devices | snmp_discovery | — |

**Implementation:** Use Diode's `agent_name` field to distinguish sources. Each discovery source uses a unique agent name:
- `netbox-discovery-agent` (orb-agent network + SNMP) — configured in `agent.yaml.j2` common backend
- `proxmox-discovery-agent` (Proxmox worker) — will be set in worker `setup()` metadata
- `pfsense-sync-agent` (pfSense REST API) — set in `PfSenseSyncBackend.setup()` as app_name

**Status:** PARTIALLY IMPLEMENTED — agent_name conventions coded into `agent.yaml.j2` and pfSense worker. Not yet validated with multiple sources pushing overlapping data.

**Validation:** After Phase 2 deployment, verify no duplicate devices in NetBox DCIM.

---

### Phase 1: pfSense REST API Sync (Orb-Agent Worker)

**Priority:** DO FIRST — existing code, validates Diode pipeline, config-only
**Effort:** Low
**Impact:** Medium (richer pfSense data: serial, version, ARP, gateways, interface descriptions)
**Status:** COMPLETE (2026-04-05) — implemented as orb-agent worker package

**What:** ~~Schedule `lib/pfsense-sync.py` as an independent Semaphore workflow.~~ Implemented as an orb-agent worker backend package that runs inside the existing orb-agent container on a cron schedule.

**Architecture pivot:** The original plan called for a standalone Semaphore playbook + systemd timer. Instead, the pfSense sync was implemented as a self-contained orb-agent worker package. This is cleaner: vault credentials resolve natively via the agent's secrets manager, no new containers or timers needed, and the worker runs within the existing agent lifecycle.

**Implementation (actual):**
1. Created `workers/pfsense_sync/__init__.py` — `PfSenseSyncBackend(Backend)` with full pfSense REST API client (device info, interfaces, IPs, gateways, ARP)
2. Created `workers/pfsense_sync/pyproject.toml` — declares `requests`, `pyyaml` dependencies
3. Created `workers/workers.txt` — declares `/opt/orb/workers/pfsense_sync`
4. Added worker policy to `agent.yaml.j2` — `*/15 * * * *` schedule, vault credential refs
5. `deploy-orb-agent.yml` mounts `workers/` into container at `/opt/orb/workers`

**Credential paths:**
- `secret/services/discovery/pfsense/host` (pfSense hostname:port)
- `secret/services/discovery/pfsense/api_key` (REST API key)
- `secret/services/netbox/orb_agent_client_id` + `orb_agent_client_secret` (Diode auth, shared by all backends)

**Validation:**
- [ ] orb-agent starts with worker backend loaded (check logs for `pfsense-sync` registration)
- [ ] pfSense device in NetBox has: serial number, platform version, all interfaces with descriptions, ARP entries, gateway routes
- [ ] No duplicate pfSense device created (verify agent_name distinction works with SNMP-discovered pfSense)
- [ ] Worker runs on schedule (verify 15-min cycle in agent logs)

**Security:** pfSense API key stored in OpenBao, resolved at runtime by orb-agent vault integration. HTTPS with TLS verification disabled (self-signed cert on pfSense — acceptable for LAN).

---

### Phase 2: Proxmox API Discovery Worker

**Priority:** SECOND — highest impact, discovers all VMs/LXC invisible to current scanning
**Effort:** High (custom Python package)
**Impact:** High (40+ VMs and containers appear in NetBox)

**What:** Build a custom orb-agent worker backend using the Diode Python SDK that queries the Proxmox REST API.

**Split into two sub-phases (per infrastructure review):**

#### Phase 2a: Cluster Metadata (PVEAuditor sufficient)

Queries the Proxmox API for:
- **Nodes:** name, status, CPU/memory/storage totals → NetBox Device (role: hypervisor)
- **VMs (QEMU):** name, VMID, status, CPU/memory/disk config → NetBox Device (role: server) or Virtual Machine
- **LXC containers:** name, VMID, status, resource config → NetBox Device (role: container)
- **Storage pools:** name, type, capacity → NetBox inventory data

**Does NOT include:** guest network interfaces or IP addresses (requires guest agent or SSH).

**Implementation:**
```yaml
backends:
  worker:

policies:
  worker:
    proxmox_discovery:
      config:
        package: proxmox_discovery
        schedule: "0 */6 * * *"
        site_name: "Uhstray.io Datacenter"
      scope:
        url: "${vault://secret/services/discovery/proxmox_api/url}"
        token_id: "${vault://secret/services/discovery/proxmox_api/token_id}"
        api_token: "${vault://secret/services/discovery/proxmox_api/api_token}"
```

**Custom package (`proxmox_discovery`):**
- Uses `proxmoxer` Python library
- Proxmox API credentials from OpenBao vault: `${vault://secret/services/discovery/proxmox_api}`
- Maps nodes → Device (role: hypervisor, site: Uhstray.io Datacenter)
- Maps VMs → Device (role: server) with resource annotations
- Maps LXC → Device (role: container) with resource annotations
- Pushes via Diode SDK with `app_name: proxmox-discovery` (shares common `agent_name` with other backends)

**Credential path (new):**
- `secret/services/discovery/proxmox_api` — contains `url`, `token_id`, `api_token`
- Copy from existing `secret/services/proxmox` or restructure

#### Phase 2b: Guest Network Details (QEMU Guest Agent)

**After 2a is validated**, add guest IP/interface discovery:
- Uses Proxmox API endpoint `/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces`
- Requires QEMU Guest Agent installed in each VM (already installed via cloud-init on agent-cloud VMs)
- Maps guest interfaces → NetBox Interface objects attached to the VM Device
- Maps guest IPs → NetBox IP Address objects

**Limitation:** VMs without QEMU guest agent won't report IPs. Fall back to network_discovery for those.

**Validation:**
- [ ] Phase 2a: All Proxmox nodes appear as Devices (role: hypervisor)
- [ ] Phase 2a: All VMs appear as Devices with correct VMID, CPU, memory
- [ ] Phase 2a: All LXC containers appear as Devices (role: container)
- [ ] Phase 2b: VMs with guest agent show interfaces and IPs in NetBox
- [ ] No duplicate devices between Proxmox worker and network/SNMP discovery

**Security:**
- Proxmox API token with PVEAuditor role (read-only). Phase 2b guest agent queries may need PVEVMUser for agent access.
- Credentials in OpenBao under `secret/services/discovery/proxmox_api`
- Dedicated AppRole via `tasks/manage-approle.yml`

---

### Phase 3: SNMPv3 Upgrade

**Priority:** THIRD — security hardening for existing SNMP infrastructure
**Effort:** Medium (credential setup + snmpd config on devices)
**Impact:** Medium (encrypted SNMP, enables SNMP on more devices)

**What:** Upgrade from SNMPv2c (plaintext community string) to SNMPv3 (auth + encryption).

**Implementation:**
1. Create SNMPv3 user on pfSense (System > SNMP > v3 Users)
2. Store SNMPv3 credentials in OpenBao at `secret/services/discovery/snmp_v3`
3. Update `agent.yaml.j2` SNMP policies:
```yaml
authentication:
  protocol_version: SNMPv3
  security_level: authPriv
  username: "${vault://secret/services/discovery/snmp_v3/username}"
  auth_protocol: SHA
  auth_password: "${vault://secret/services/discovery/snmp_v3/auth_password}"
  priv_protocol: AES
  priv_password: "${vault://secret/services/discovery/snmp_v3/priv_password}"
```

**Optional:** Install snmpd on Proxmox nodes for hardware-level discovery. However, the infrastructure review noted this is low ROI since the Proxmox API (Phase 2) already provides most metrics. **Defer snmpd installation unless Phase 2 leaves gaps.**

**Validation:**
- [ ] SNMPv3 credentials in OpenBao
- [ ] pfSense responds to SNMPv3 queries (test with `snmpwalk -v3`)
- [ ] Agent uses v3 auth (verify in orb-agent logs: no plaintext community)
- [ ] Existing SNMP discovery data quality maintained

**Security:** SNMPv3 with SHA auth + AES encryption. No plaintext community strings on the wire.

---

### Phase 4: LLDP Topology Discovery (Optional)

**Priority:** FOURTH — nice-to-have, fills topology gap
**Effort:** Low-Medium
**Impact:** Medium (reveals physical network connections)

**What:** Query LLDP neighbor data from pfSense to map physical network topology.

**Implementation options:**
1. Add to `pfsense-sync.py` — query pfSense LLDP data via REST API or SSH (`lldpctl -f json`)
2. Add LLDP parsing to the Proxmox worker for nodes with `lldpd` installed
3. Map LLDP neighbors → NetBox Cable objects connecting Interface pairs

**Validation:**
- [ ] At least one cable connection visible in NetBox between pfSense and a switch/node
- [ ] LLDP neighbor data includes remote port, remote system name

**Security:** Read-only queries. No new credentials needed (reuses pfSense API key).

---

## OpenBao Credential Organization

**Per architecture review**, consolidate discovery credentials under a dedicated path:

| Path | Contents | Used By |
|------|----------|---------|
| `secret/services/discovery/proxmox_api` | url, token_id, api_token | Proxmox worker |
| `secret/services/discovery/snmp_v3` | username, auth_password, priv_password | SNMP policies |
| `secret/services/discovery/pfsense` | api_key, host | pfSense sync |
| `secret/services/netbox` | All NetBox secrets (existing) | orb-agent, deploy |
| `secret/services/approles/orb-agent` | role_id, secret_id | orb-agent vault auth |

**AppRole policy update:** The `orb-agent` AppRole needs read access to `secret/services/discovery/*` in addition to `secret/services/netbox`.

---

## Agent Template Architecture

**Per automation review**, the `agent.yaml.j2` template should remain a single file but use Jinja2 conditionals to enable/disable backends based on inventory variables:

```jinja2
{% if enable_proxmox_worker | default(false) %}
    worker:
{% endif %}
```

This keeps the template in one place while allowing per-environment customization via the site-config inventory. A full template split is deferred until the template exceeds 200 lines.

---

## Implementation Priority

| Phase | Effort | Impact | Depends On | Status |
|-------|--------|--------|------------|--------|
| Prerequisite: Dedup strategy | Low (documentation) | Critical | — | PARTIAL — agent_name coded, not validated |
| 1. pfSense REST API sync | Low (worker package) | Medium | Existing code | COMPLETE (2026-04-05) — orb-agent worker |
| 2a. Proxmox API metadata | High (custom Python) | High | Dedup strategy | COMPLETE (2026-04-16) — 31 entities |
| 2b. Proxmox guest IPs | Medium (API extension) | High | Phase 2a working | TODO |
| 3. SNMPv3 upgrade | Medium (cred setup) | Medium | Phase 1 validated | TODO |
| 4. LLDP topology | Low-Medium | Medium | Phase 1 or 2 working | OPTIONAL |

---

## Dropped Phases (Rationale)

| Phase | Why Dropped | Alternative |
|-------|-------------|-------------|
| NAPALM device_discovery | No FreeBSD driver for pfSense, Linux driver useless for Proxmox | pfSense REST API + Proxmox API worker cover both use cases better |

**Note:** OS Detection was initially dropped by the review team based on older CLAUDE.md documentation. It has been re-enabled as a config flag (`os_detection: true`) for validation against orb-agent v2.7.0. If it causes `exit status 1`, it will be removed. The rest of the scan continues without it.

---

## Cross-Team Review Summary

| Reviewer | Key Findings |
|----------|-------------|
| **Infrastructure** | NAPALM Linux driver useless for Proxmox. Proxmox API can't see guest IPs without QEMU agent. Split Phase 3 into API + guest agent. Skip SNMPv3 on Proxmox nodes. |
| **Network** | OS detection broken (exit status 1). No NAPALM driver for pfSense (FreeBSD). Increase network timeout to 20min. Add IPMI/NFS/SMB ports. |
| **Automation** | Start with pfSense sync (working code). OpenBao credential paths need definition. agent.yaml.j2 needs modularity. Semaphore may not support cron scheduling. |
| **Architecture** | Define dedup rules before adding backends. Consolidate creds under discovery/*. Consider LLDP for topology. Proxmox worker complements NemoClaw Phase 1. |
