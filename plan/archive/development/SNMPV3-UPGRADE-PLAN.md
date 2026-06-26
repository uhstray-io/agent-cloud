# SNMPv3 Upgrade Plan

**Date:** 2026-04-21
**Status:** DEFERRED — security hardening only, not blocking functionality
**Priority:** LOW
**Effort:** Medium (credential setup + snmpd config on pfSense)
**Impact:** Medium (encrypted SNMP on the wire)

---

## Overview

Upgrade the existing SNMP discovery from SNMPv2c (plaintext community string) to SNMPv3 (SHA auth + AES encryption). Currently the `snmp_discovery` orb-agent backend does a subnet-wide scan of 192.168.1.0/24 on port 161 using an SNMPv2c community string. Any device responding to SNMP gets enriched with hostname, manufacturer, model, interfaces, and MACs.

## Current State

- **SNMPv2c** community string stored in OpenBao at `secret/services/netbox/snmp_community`
- **snmp_discovery** backend runs every 6 hours via orb-agent
- **Devices responding to SNMP:** primarily pfSense (Netgate 4200) and any managed network gear
- Proxmox nodes do NOT run snmpd by default — enriched via Proxmox API worker instead
- pfSense REST API provides richer data than SNMP — the SNMP scan is supplementary

## Why Deferred

1. Proxmox API (Phase 2) already provides most device data
2. pfSense REST API provides richer data than SNMP
3. The community string is already stored in OpenBao (not hardcoded)
4. The SNMP scan runs on an isolated management LAN

## Implementation (when prioritized)

### 1. Generate SNMPv3 credentials
- Create username, auth password (SHA), privacy password (AES)
- Store in OpenBao at `secret/services/discovery/snmp_v3`

### 2. Configure snmpd on pfSense
- Add SNMPv3 user via pfSense web UI or CLI
- Optionally disable SNMPv2c to enforce encryption

### 3. Update agent.yaml.j2
- Change SNMP policy from SNMPv2c to SNMPv3
- Reference vault paths for SNMPv3 credentials
- Update protocol_version, add auth/priv settings

### 4. Update Semaphore inventory
- Add SNMPv3-related template variables if needed

### 5. Test and validate
- Verify snmp_discovery still enriches devices
- Confirm SNMPv2c is disabled (if desired)
- Packet capture to verify encryption on the wire

## OpenBao Credential Layout

| Path | Contents |
|------|----------|
| `secret/services/discovery/snmp_v3` | username, auth_password, priv_password |
| `secret/services/netbox/snmp_community` | Legacy SNMPv2c community string (keep until migration complete) |
