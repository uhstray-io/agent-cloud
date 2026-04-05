# NetBox Discovery Expansion Plan

**Date:** 2026-04-04
**Status:** PROPOSED
**Context:** The orb-agent is running with network_discovery (SYN scan) and snmp_discovery, discovering IP addresses and SNMP-enabled devices (pfSense). This plan expands discovery to get richer data from all discovered hosts.

---

## Current State

**What we have:**
- **network_discovery** — SYN scan finds live IPs and open ports. Pushes IP Address entities to NetBox. No device details.
- **snmp_discovery** — Enriches SNMP-responsive devices (pfSense gateway found). Gets hostname, manufacturer, model, interfaces, MACs.
- **32 IPs discovered**, 1 device (pfSense) with full SNMP data.

**What's missing:**
- Most devices don't respond to SNMP (Proxmox VMs, Docker hosts, workstations)
- No OS detection on discovered IPs
- No service version detection
- No SSH-based device interrogation
- No configuration backup
- No interface/VLAN discovery for non-SNMP devices

---

## Orb-Agent Backend Capabilities

| Backend | Protocol | Discovers | Best For |
|---------|----------|-----------|----------|
| **network_discovery** | nmap (TCP/UDP/ICMP) | IPs, open ports, OS (with `-O`) | Finding what's on the network |
| **snmp_discovery** | SNMP v2c/v3 | Device details, interfaces, MACs, IPs, manufacturer | Network equipment, NAS, printers |
| **device_discovery** | SSH via NAPALM | OS, hardware, interfaces, IPs, VLANs, configs | Managed network devices (switches, routers, firewalls) |
| **worker** | Custom Python | Anything the Diode SDK supports | Custom integrations (Proxmox API, Docker API, etc.) |

---

## Expansion Plan

### Phase 1: Enable OS Detection on Network Scans

**What:** Add `os_detection: true` to the subnet_scan policy. The orb-agent runs privileged with `CAP_NET_RAW`, so nmap `-O` should work.

**Why:** OS detection fingerprints each discovered IP with its operating system (Linux, Windows, FreeBSD, etc.) which NetBox can store as Platform data.

**Config change in `agent.yaml.j2`:**
```yaml
network_discovery:
  subnet_scan:
    scope:
      os_detection: true   # nmap -O (requires privileged)
```

**Risk:** The network_discovery CLAUDE.md says `os_detection` causes `exit status 1`. Need to test on the production VM — may be a version-specific issue.

**Validation:** After scan, check NetBox IPAM > IP Addresses for OS fingerprint data.

**Security:** OS detection sends crafted packets — acceptable on own LAN, not for external scanning.

### Phase 2: Enable device_discovery (NAPALM/SSH)

**What:** Add the `device_discovery` backend to collect rich device data via SSH from known infrastructure.

**Why:** NAPALM connects to network devices and retrieves:
- Exact OS version and hardware model
- Interface configurations (speed, duplex, description, VLANs)
- IP addresses assigned to interfaces
- Running/startup configuration (backup)
- Platform and manufacturer details

**Targets:** Devices that support SSH + NAPALM drivers:
- pfSense (driver: generic SSH or custom)
- Proxmox nodes (driver: linux)
- Any managed switches/routers

**Config addition to `agent.yaml.j2`:**
```yaml
backends:
  device_discovery:
  # ... existing backends ...

policies:
  device_discovery:
    proxmox_nodes:
      config:
        schedule: "0 */12 * * *"
        defaults:
          site: "Uhstray.io Datacenter"
          role: hypervisor
          if_type: "1000base-t"
      scope:
        - driver: linux
          hostname: {{ proxmox_node_ips | join('\n        - driver: linux\n          hostname: ') }}
          username: "${vault://secret/services/proxmox/ssh_user}"
          password: "${vault://secret/services/proxmox/ssh_pass}"

    pfsense:
      config:
        schedule: "0 */12 * * *"
        capture_running_config: true
        defaults:
          site: "Uhstray.io Datacenter"
          role: gateway-router
      scope:
        - driver: ios   # or generic
          hostname: 192.168.1.1
          username: "${vault://secret/services/pfsense/ssh_user}"
          password: "${vault://secret/services/pfsense/ssh_pass}"
```

**Prerequisites:**
- SSH credentials for target devices stored in OpenBao
- NAPALM drivers available in the orb-agent container
- SSH key or password auth configured on target devices

**Validation:**
- NetBox DCIM > Devices shows Proxmox nodes with correct OS, interfaces, IPs
- Config backups visible if `capture_running_config: true`

**Security:**
- SSH credentials stored in OpenBao, fetched via `${vault://...}`
- Read-only access — NAPALM `get_*` methods don't modify devices
- Separate AppRole could scope access to only device credentials

### Phase 3: Custom Worker for Proxmox API Discovery

**What:** Build a custom worker backend using the Diode Python SDK that queries the Proxmox API to discover VMs, containers, and their resources.

**Why:** Proxmox VMs don't respond to SNMP or have SSH keys configured for NAPALM. The Proxmox API provides rich data:
- All VMs and LXC containers with resource allocations
- Network interfaces and IP addresses
- Storage configuration
- Cluster node status and health

**Implementation:**
```yaml
backends:
  worker:

policies:
  worker:
    proxmox_discovery:
      config:
        package: nbl_proxmox_discovery  # custom Python package
        schedule: "0 */6 * * *"
        proxmox_url: "${vault://secret/services/proxmox/url}"
        proxmox_token_id: "${vault://secret/services/proxmox/token_id}"
        proxmox_token: "${vault://secret/services/proxmox/api_token}"
      scope:
        cluster: all
```

**Custom package (`nbl_proxmox_discovery`):**
- Uses `proxmoxer` Python library to query the PVE REST API
- Maps VMs to NetBox Device objects (name, role, site, platform)
- Maps VM network interfaces to NetBox Interface objects
- Maps VM IPs to NetBox IP Address objects
- Maps Proxmox nodes to NetBox Device objects (role: hypervisor)
- Pushes all entities via the Diode SDK

**Prerequisites:**
- Proxmox API token in OpenBao (already have this)
- Custom Python package built and mounted into the orb-agent container
- Diode SDK dependency

**Validation:**
- NetBox DCIM > Devices shows all Proxmox VMs
- NetBox Virtualization shows VM instances with correct resources

**Security:**
- Proxmox API token is read-only (PVEAuditor role recommended)
- Credentials from OpenBao vault, never in config files

### Phase 4: pfSense REST API Sync (Independent Workflow)

**What:** The existing `pfsense-sync.py` script already queries the pfSense REST API and pushes to Diode. Make it a scheduled Semaphore workflow running every 15 minutes.

**Why:** Supplements SNMP with richer data: exact serial number, platform version, interface descriptions, ARP table, gateway routing info.

**Implementation:** `run-pfsense-sync.yml` playbook (already planned, see project memory)

**Validation:** pfSense device in NetBox has serial, platform version, all interfaces with descriptions, ARP entries.

### Phase 5: SNMP v3 and Additional SNMP Targets

**What:** Upgrade SNMP from v2c to v3 for better security on capable devices. Add SNMP configuration to Proxmox nodes (install snmpd).

**Why:** v2c community strings are plaintext on the wire. v3 provides auth + encryption. Adding snmpd to Proxmox nodes enables hardware discovery.

**Config change:**
```yaml
authentication:
  protocol_version: SNMPv3
  security_level: authPriv
  username: "${vault://secret/services/snmp/username}"
  auth_protocol: SHA
  auth_password: "${vault://secret/services/snmp/auth_password}"
  priv_protocol: AES
  priv_password: "${vault://secret/services/snmp/priv_password}"
```

**Prerequisites:**
- Install and configure snmpd on Proxmox nodes
- Create SNMPv3 user credentials, store in OpenBao
- Update agent.yaml.j2 with v3 config

---

## Implementation Priority

| Phase | Effort | Impact | Priority |
|-------|--------|--------|----------|
| 1. OS Detection | Low (config change) | Medium (OS fingerprints for all IPs) | **Do first** |
| 2. device_discovery (NAPALM) | Medium (SSH creds + config) | High (full device data for managed devices) | **Do second** |
| 3. Proxmox worker | High (custom Python) | High (discovers all VMs/containers) | **Do third** |
| 4. pfSense sync | Low (playbook exists) | Medium (richer pfSense data) | **Do in parallel** |
| 5. SNMPv3 | Medium (snmpd install + creds) | Medium (security + more SNMP targets) | **Do later** |

---

## Validation Criteria

### Phase 1 (OS Detection)
- [ ] `os_detection: true` in agent.yaml doesn't cause exit status 1
- [ ] NetBox IP Addresses show OS fingerprint data after scan
- [ ] No scan failures in orb-agent logs

### Phase 2 (device_discovery)
- [ ] NAPALM connects to at least one device via SSH
- [ ] NetBox shows device with correct OS version, interfaces, IPs
- [ ] SSH credentials fetched from OpenBao (not in config)

### Phase 3 (Proxmox worker)
- [ ] Worker package runs inside orb-agent container
- [ ] All Proxmox VMs appear as NetBox devices
- [ ] VM interfaces and IPs correctly mapped

### Phase 4 (pfSense sync)
- [ ] `run-pfsense-sync.yml` runs on 15-min schedule via Semaphore
- [ ] pfSense device in NetBox has serial, version, ARP, gateways

### Phase 5 (SNMPv3)
- [ ] SNMPv3 credentials in OpenBao
- [ ] Proxmox nodes respond to SNMPv3 queries
- [ ] Agent uses v3 auth (no plaintext community strings)

---

## Security Considerations

- **OS detection** sends crafted TCP/IP packets — only run on own LAN
- **device_discovery SSH credentials** must be in OpenBao with scoped AppRole, never in config
- **Proxmox API token** should use PVEAuditor role (read-only)
- **SNMP community strings** are plaintext — upgrade to v3 for production
- **Config backups** via NAPALM may contain credentials — store in private repo or encrypted
- **Worker package** runs inside privileged container — review code for security before deployment
