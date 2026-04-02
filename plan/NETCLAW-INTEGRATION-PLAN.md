# NetClaw Integration Plan — Network Management & Automation

**Date:** 2026-03-29
**Status:** Proposed
**Context:** Workflow Agents project — Phase 1 enhancement

---

## 1. What Is NetClaw

NetClaw is an open-source, CCIE-level AI network engineering agent built on OpenClaw (the same agent harness that NemoClaw wraps). It provides 101+ skills and 46 MCP server backends for autonomous network monitoring, troubleshooting, configuration, and security auditing — all driven by natural language through Slack, WebEx, or web chat.

**Key capabilities relevant to this homelab:**

- **Device health monitoring** — CPU, memory, interfaces, NTP, logs — fleet-wide in parallel via pyATS
- **NetBox integration** — DCIM/IPAM source-of-truth reconciliation (read-write), topology discovery, IP drift detection
- **GitHub config-as-code** — commit config backups, create issues from findings, open PRs for changes
- **Packet capture analysis** — deep tshark analysis of pcap files uploaded via Slack
- **Network scanning** — nmap host discovery, port scanning, OS fingerprinting (scope-enforced)
- **Topology discovery** — CDP/LLDP, ARP, routing peers with reconciliation against NetBox
- **UML/diagram generation** — 27+ diagram types via Kroki (network topology, rack layouts, packet headers)
- **Grafana/Prometheus observability** — dashboards, PromQL, Loki logs, alerting, incidents
- **Live BGP/OSPF participation** — control-plane peering, route injection/withdrawal, RIB/LSDB queries
- **ContainerLab** — deploy containerized network labs (SR Linux, cEOS, FRR, etc.)
- **ITSM-gated change management** — ServiceNow CR gating for write operations
- **Immutable audit trail (GAIT)** — every agent action logged with reasoning and outcomes

**Critical relationship:** NetClaw runs on OpenClaw, and NemoClaw is NVIDIA's security wrapper around OpenClaw. They are sibling projects sharing the same agent harness. This means NetClaw's skills and MCP servers can potentially be loaded into the existing NemoClaw sandbox, or NetClaw can run as a separate OpenClaw instance alongside NemoClaw.

---

## 2. Why NetClaw for This Project

The homelab has specific network management gaps that NetClaw addresses directly:

| Current Gap | NetClaw Solution |
|---|---|
| No network device monitoring (pfSense, switches) | pyATS + health monitoring skills for fleet-wide parallel checks |
| NetBox deployed but not populated with live state | NetBox MCP reconciles live device state against DCIM/IPAM |
| Proxmox monitoring is API-only (no L2/L3 visibility) | Network scanning, topology discovery, ARP/CDP/LLDP mapping |
| No pcap analysis capability | Packet Buddy MCP for deep tshark analysis |
| No network topology visualization | Kroki UML diagrams (nwdiag, rackdiag) + draw.io topology maps |
| Config backup is manual | GitHub MCP for automated config-as-code backups |
| No network change management | ITSM gating + GAIT audit trail |
| No BGP/OSPF visibility (if applicable) | Protocol MCP for live control-plane participation |

---

## 3. Integration Architecture — Two Options

### Option A: Separate VM (Recommended)

Deploy NetClaw on its own VM as a standalone OpenClaw agent, communicating with the existing service layer through the same APIs NemoClaw uses.

```
┌────────────────────────────────────────────────────────────────┐
│ Existing Workflow Agents Stack                                 │
│                                                                │
│  ┌──────────┐  ┌──────────┐  ┌──────┐  ┌───────────┐           │
│  │ OpenBao  │  │  NocoDB  │  │  n8n │  │ Semaphore │           │
│  │ .164     │  │  .161    │  │ .118 │  │  .117     │           │
│  └──────────┘  └──────────┘  └──────┘  └───────────┘           │
│       ↑              ↑           ↑          ↑                  │
│       │              │           │          │                  │
│  ┌────┴──────────────┴───────────┴──────────┴────────┐         │
│  │                    OpenBao AppRole                │         │
│  └────┬──────────────┬───────────────────────────────┘         │
│       │              │                                         │
│  ┌────┴────┐    ┌────┴────┐    ┌───────────┐  ┌──────────┐     │
│  │NemoClaw │    │NetClaw  │    │  NetBox   │  │ Proxmox  │     │
│  │  .163   │    │  NEW VM │    │   .116    │  │   .52    │     │
│  │(Docker) │    │(Docker) │    │           │  │          │     │
│  └─────────┘    └─────────┘    └───────────┘  └──────────┘     │
└────────────────────────────────────────────────────────────────┘
```

**Pros:** Independent failure domain, dedicated resources for MCP servers (some are memory-hungry), can run different OpenClaw versions, separate network policy (NetClaw needs broader network access than NemoClaw for device polling).

**Cons:** Additional VM to manage, duplicated OpenClaw runtime.

### Option B: Skills Injection into NemoClaw

Load NetClaw's skills and selected MCP servers into the existing NemoClaw sandbox by adding them to the agent-cloud network policy and copying skill files.

**Pros:** No additional VM, single agent manages both workflow automation and network engineering.

**Cons:** NemoClaw's sandbox policy would need significantly broader network access (every managed device), mixing concerns (workflow automation + network engineering), harder to troubleshoot, skill conflicts possible.

### Recommendation: Option A

NetClaw needs direct network access to managed devices (pfSense at .1/.2, physical servers, switches). This is fundamentally different from NemoClaw's API-only access pattern. A separate VM with its own network policy keeps the security model clean. NemoClaw stays scoped to service APIs; NetClaw gets scoped to network infrastructure.

---

## 4. VM Provisioning

### New VM Specification

| Attribute | Value | Rationale |
|---|---|---|
| VM Name | `netclaw` | Matches naming convention |
| VMID | 265 | In the 200-299 provisioning range |
| IP | {{ netclaw_host }} | Next available after openbao (.164) |
| Cores | 4 | MCP servers run in parallel; pyATS is CPU-intensive |
| Memory | 8192 MB | Multiple MCP servers + Python environments + tshark |
| Disk | 60 GB | pcap storage, config backups, ContainerLab images |
| Runtime | Docker | OpenClaw/OpenShell requires Docker (same as NemoClaw) |
| Proxmox Node | alphacentauri | Primary VM host |

### Provisioning via Existing Playbooks

```bash
# Clone template → configure → start (uses existing provision-vm.yml)
ansible-playbook -i semaphore/inventory/local.yml \
  semaphore/playbooks/provision-vm.yml \
  -e target_service=netclaw

# Or via Semaphore task template: "Provision NetClaw VM"
```

Add to `proxmox/vm-specs.yml`:

```yaml
netclaw:
  vmid: 265
  name: netclaw
  cores: 4
  memory: 8192
  disk: 60G
  ip: {{ netclaw_host }}
  node: alphacentauri
  runtime: docker
```

---

## 5. Deployment Architecture

### Directory Structure

```
deployments/agent-cloud/
├── vms/
│   └── netclaw-network/               # Distinct from vms/nemoclaw/
│       ├── deploy.sh                   # Install NetClaw + configure integrations
│       ├── compose.yml                 # Supporting services (if any)
│       └── config/
│           ├── testbed.yaml            # pyATS device inventory
│           ├── netclaw.env             # Platform credentials (gitignored)
│           ├── USER.md                 # NetClaw identity (operator name, timezone)
│           └── network-policy.yaml     # OpenShell network policy
```

### deploy.sh Pattern (5-Step)

Following the established pattern from other deploy scripts:

1. **Generate secrets** — Create `netclaw.env` with credentials pulled from OpenBao (NetBox token, GitHub PAT, Discord bot token, Proxmox token)
2. **Install NetClaw** — Clone repo, run `install.sh`, configure OpenClaw with Anthropic API key
3. **Configure integrations** — Generate `testbed.yaml` from NetBox device inventory, set up Slack/Discord channels, configure NetBox MCP connection
4. **Store credentials in OpenBao** — Create `netclaw` AppRole (read-only for all service secrets + NetBox write access)
5. **Validate** — Health check OpenClaw gateway, verify MCP server connectivity, test device reachability

### OpenBao Integration

New AppRole for NetClaw:

```hcl
# netclaw-readwrite.hcl
# NetClaw needs read access to all service secrets (like NemoClaw)
# Plus write access to NetBox for source-of-truth reconciliation

path "secret/data/services/*" {
  capabilities = ["read"]
}

path "secret/metadata/services/*" {
  capabilities = ["list", "read"]
}

path "secret/data/services/netbox" {
  capabilities = ["create", "update", "read", "patch"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
```

New secret path for NetClaw-specific credentials:

```
secret/services/netclaw
  ├── anthropic_api_key    # Claude API key for OpenClaw
  ├── gateway_token        # OpenClaw gateway auth token
  └── slack_bot_token      # Dedicated Slack bot (separate from NemoClaw's Discord bot)
```

---

## 6. Network Policy

NetClaw requires significantly broader network access than NemoClaw because it needs to reach managed infrastructure directly.

### OpenShell Network Policy

```yaml
preset:
  name: netclaw-network
  description: "NetClaw — network device access + agent-cloud service layer"

network_policies:
  netclaw-network:
    name: netclaw-network
    endpoints:
      # ── Managed Infrastructure ──────────────────────────────
      # pfSense firewalls (SSH + web UI for config backup)
      - host: {{ pfsense_host }}
        port: 443
        access: full
      - host: {{ pfsense2_host }}
        port: 443
        access: full

      # Physical servers (SSH for pyATS, SNMP for monitoring)
      # Expand this list based on testbed.yaml
      - host: {{ lan_subnet }}
        port: 22
        access: full
      - host: {{ lan_subnet }}
        port: 161
        access: full    # SNMP

      # ── Workflow Agents Service Layer ───────────────────────
      # NetBox (DCIM/IPAM source of truth — read-write)
      - host: {{ netbox_host }}
        port: 8000
        access: full

      # NocoDB (shared data layer — task_log, monitored_resources)
      - host: {{ nocodb_host }}
        port: 8181
        access: full

      # n8n (workflow triggering — alert webhooks)
      - host: {{ n8n_host }}
        port: 5678
        access: full

      # OpenBao (credential retrieval)
      - host: {{ openbao_host }}
        port: 8200
        access: full

      # ── External APIs ──────────────────────────────────────
      # Anthropic API (Claude inference)
      - host: api.anthropic.com
        port: 443
        access: full

      # GitHub (config-as-code, issues)
      - host: api.github.com
        port: 443
        access: full
      - host: github.com
        port: 443
        access: full

      # Discord (alerting)
      - host: discord.com
        port: 443
        access: full
      - host: gateway.discord.gg
        port: 443
        access: full

      # Slack (primary NetClaw interface)
      - host: slack.com
        port: 443
        access: full
      - host: wss-primary.slack.com
        port: 443
        access: full
      - host: files.slack.com
        port: 443
        access: full

    binaries:
      - { path: /usr/local/bin/node }
      - { path: /usr/local/bin/openclaw }
      - { path: /usr/bin/curl }
      - { path: /usr/bin/python3 }
      - { path: /usr/bin/jq }
      - { path: /usr/bin/tshark }
      - { path: /usr/bin/nmap }
```

---

## 7. Integration Points with Existing Services

### 7.1 NetBox (Bidirectional)

NetClaw's NetBox MCP server provides read-write access to NetBox's DCIM/IPAM. This is the highest-value integration.

**Workflow:**
1. NetClaw discovers topology via CDP/LLDP/ARP on homelab devices
2. Reconciles against NetBox: flags undocumented devices, missing cables, IP drift
3. Updates NetBox with current live state (interfaces, IPs, connections)
4. Generates topology diagrams from NetBox data via Kroki

**Configuration:** NetClaw reads the NetBox URL and token from OpenBao at `secret/services/netbox`. The existing NetBox deployment at {{ netbox_host }} requires no changes — NetClaw uses the standard NetBox REST API.

### 7.2 NocoDB (Write — Task Logging)

NetClaw writes to the same `task_log` and `monitored_resources` tables NemoClaw uses, creating a unified audit trail.

**New table: `network_health`**

| Column | Type | Purpose |
|---|---|---|
| id | Auto | Primary key |
| timestamp | DateTime | Check time |
| device | Text | Device hostname |
| device_ip | Text | Management IP |
| check_type | Text | cpu/memory/interface/bgp/ospf |
| status | Text | ok/warning/critical |
| value | Number | Metric value (CPU %, memory %, etc.) |
| threshold | Number | Alert threshold |
| details | JSON | Full check output |
| source | Text | "netclaw" (distinguishes from NemoClaw entries) |

### 7.3 n8n (Webhook Triggers)

NetClaw findings trigger n8n workflows for alerting and remediation:

- **Network health alert** → n8n webhook → Discord `#network-alerts` channel
- **Config drift detected** → n8n workflow → GitHub issue + Discord notification
- **NetBox reconciliation diff** → n8n workflow → NocoDB entry + daily digest
- **Device unreachable** → n8n webhook → Discord `#agent-alerts` (existing channel)

### 7.4 Discord / Slack

NetClaw's primary interface is Slack (built-in OpenClaw channel), while NemoClaw uses Discord. This natural separation avoids confusion:

| Agent | Primary Channel | Alert Channel |
|---|---|---|
| NemoClaw | Discord `#agent-activity` | Discord `#agent-alerts` |
| NetClaw | Slack `#netclaw-general` | Slack `#netclaw-alerts` / Discord `#network-alerts` |

NetClaw can also post to Discord via its API access for cross-agent visibility.

### 7.5 Semaphore (Playbook Trigger)

NetClaw can trigger Semaphore playbooks for network-related infrastructure tasks:

- **Device config backup** → Semaphore runs Ansible playbook against device inventory
- **Firmware upgrade** → Semaphore orchestrates rolling upgrade across nodes
- **Network troubleshooting** → Semaphore runs diagnostic playbooks

### 7.6 Proxmox (VM Network Monitoring)

NetClaw extends NemoClaw's Proxmox monitoring with L2/L3 network visibility:

- VM network interface health (packet errors, drops)
- VLAN/bridge configuration audit
- Network path tracing between VMs
- Bandwidth utilization monitoring

---

## 8. Device Inventory (testbed.yaml)

The pyATS testbed defines which devices NetClaw can manage. Initial inventory based on the existing lab:

```yaml
# config/testbed.yaml — NetClaw device inventory
# Populated from config/inventory.yml + NetBox

testbed:
  name: {{ ansible_user }}-homelab

devices:
  pfsense01:
    os: linux            # pfSense is FreeBSD but pyATS treats it as generic
    type: firewall
    connections:
      defaults:
        class: unicon.Unicon
      ssh:
        protocol: ssh
        ip: {{ pfsense_host }}
        port: 22

  pfsense02:
    os: linux
    type: firewall
    connections:
      defaults:
        class: unicon.Unicon
      ssh:
        protocol: ssh
        ip: {{ pfsense2_host }}
        port: 22

  # Physical servers — SSH access for system monitoring
  alphacentauri:
    os: linux
    type: server
    connections:
      ssh:
        protocol: ssh
        ip: {{ proxmox_node_ip }}
        port: 22

  # Add more servers as needed from inventory.yml
  # NetClaw can also auto-discover via nmap + ARP
```

**Auto-discovery:** NetClaw's nmap MCP server can scan the {{ lan_subnet }} subnet to discover hosts, then reconcile against NetBox and generate/update the testbed.

---

## 9. Selective MCP Server Deployment

NetClaw ships with 46 MCP integrations. Most target enterprise Cisco/Juniper/Arista gear. For this homelab, deploy only the relevant subset:

### Deploy (Relevant to Homelab)

| MCP Server | Purpose | Why |
|---|---|---|
| **NetBox** | DCIM/IPAM source of truth | Already deployed at .116 |
| **GitHub** | Config-as-code, issues | Already integrated in agent-cloud |
| **Packet Buddy** | pcap analysis via tshark | Useful for troubleshooting |
| **nmap** | Network scanning + discovery | Homelab device discovery |
| **UML/Kroki** | Diagram generation | Network topology visualization |
| **Protocol MCP** | BGP/OSPF (if lab routers exist) | FRR testbed for learning |
| **ContainerLab** | Containerized network labs | Lab simulation on NemoClaw VM |
| **Prometheus** | Direct PromQL queries | If Prometheus is deployed |
| **Grafana** | Dashboard/alerting | If Grafana is deployed |

### Skip (Not Relevant)

Cisco ACI, Cisco ISE, Cisco NSO, Cisco CML, Cisco Meraki, Cisco SD-WAN, Cisco FMC, Cisco ThousandEyes, Cisco RADKit, Arista CVP, F5 BIG-IP, Catalyst Center, Juniper JunOS, ServiceNow, Microsoft Graph, Nautobot, Infrahub, Itential, Kubeshark, AWS, GCP, Batfish, Palo Alto, FortiManager, Infoblox, SuzieQ.

### Configuration in install.sh

NetClaw's `install.sh` clones all MCP servers. After install, disable unused ones by not providing their credentials in `setup.sh`. NetClaw gracefully skips MCP servers without configured credentials.

---

## 10. Implementation Phases

### Phase A: Foundation (Week 1)

1. **Provision VM** — Clone template → netclaw VM at {{ netclaw_host }}
2. **Install NetClaw** — Clone repo, run `install.sh`, configure Anthropic API key
3. **Create OpenBao AppRole** — `netclaw` role with `netclaw-readwrite` policy
4. **Store Anthropic API key** — In OpenBao at `secret/services/netclaw`
5. **Configure basic integrations** — GitHub (reuse existing PAT), Discord
6. **Verify basic operation** — `openclaw gateway` + `openclaw chat` works

**Acceptance:** NetClaw responds to chat queries, can access GitHub, posts to Discord.

**Validation:** `openclaw chat "show version"` returns response. `bao kv get secret/services/netclaw` returns Anthropic key. SSH to VM works with netclaw service key.
**Smoke test:** Chat query asking for cluster summary. Verify Discord post in #network-alerts channel.
**Security:** Anthropic API key in OpenBao only, not in env files. Docker (not Podman) required for OpenShell sandbox. Network policy restricts to {{ lan_subnet }} — no external device access.

### Phase B: NetBox Integration (Week 2)

1. **Configure NetBox MCP** — Point to existing NetBox at .116, token from OpenBao
2. **Initial device inventory** — Run nmap discovery against {{ lan_subnet }}
3. **Populate NetBox** — Import discovered devices, interfaces, IPs
4. **Set up reconciliation** — Schedule periodic NetBox ↔ live state comparison
5. **Generate topology diagrams** — From NetBox data via Kroki

**Acceptance:** NetBox has accurate representation of homelab devices. Topology diagram generated.

**Validation:** NetBox UI shows discovered devices with correct IPs/interfaces. Topology diagram renders without errors. Reconciliation diff reports zero unexpected discrepancies.
**Smoke test:** Run nmap scan → verify devices appear in NetBox. Compare NetBox device count to expected count. Generate topology SVG.
**Security:** NetBox API token from OpenBao, scoped to read/write DCIM/IPAM. nmap restricted to {{ lan_subnet }} CIDR. No scanning outside defined ranges.

### Phase C: Monitoring & Alerting (Week 3)

1. **Build testbed.yaml** — From NetBox device inventory
2. **Configure health monitoring** — pyATS health checks for reachable devices
3. **Wire alerting** — NetClaw findings → n8n webhook → Discord `#network-alerts`
4. **NocoDB integration** — Write health data to `network_health` table
5. **n8n scheduled workflow** — Trigger health checks every 15 minutes

**Acceptance:** Health checks run on schedule, alerts fire on failures, data in NocoDB.

**Validation:** `network_health` table in NocoDB has rows with timestamps < 15 minutes old. Discord #network-alerts has at least one test alert. pyATS testbed.yaml matches NetBox device count.
**Smoke test:** Disable a test device's SSH → verify health check fails → verify Discord alert fires within 15 minutes.
**Security:** Device SSH credentials in OpenBao at `secret/services/netclaw`. SNMP community strings in OpenBao. pyATS connects read-only (show commands only). No config modification without ITSM gate.

### Phase D: Advanced Features (Week 4+)

1. **Config backup automation** — Git-backed config backups via GitHub MCP
2. **ContainerLab** — Deploy FRR testbed for BGP/OSPF experimentation
3. **Packet capture** — Upload pcaps via Slack for tshark analysis
4. **Slack integration** — Set up Slack workspace for primary NetClaw interaction
5. **Cross-agent coordination** — NemoClaw creates tasks, NetClaw executes network operations

**Acceptance:** Config backups in GitHub, ContainerLab runs, pcap analysis works.

**Validation:** Config backup commit appears in GitHub repo. ContainerLab topology starts successfully. Pcap upload + tshark analysis returns parsed output.
**Smoke test:** Trigger manual config backup → verify Git commit. Deploy FRR lab → verify BGP session establishes. Upload test pcap → verify analysis output.
**Security:** Config backups in a PRIVATE GitHub repo (contain device configs with potential credentials). Git-backed audit trail (GAIT) is append-only. ContainerLab runs in isolated network namespace. Cross-agent tasks validated: NocoDB task queue checks `source` field to prevent injection.

---

## 11. Cross-Agent Coordination (Phase 3 Enhancement)

NetClaw and NemoClaw can coordinate via NocoDB as the shared task queue:

```
Claude Cowork  ──creates task──→  NocoDB task_queue
                                       │
                            ┌──────────┴──────────┐
                            ▼                      ▼
                       NemoClaw                 NetClaw
                  (workflow tasks)        (network tasks)
                            │                      │
                            └──────────┬──────────┘
                                       ▼
                                  NocoDB results
                                       │
                                       ▼
                              Discord / Slack alerts
```

**Task routing:** A `task_type` field in the NocoDB task queue determines routing:
- `workflow:*` → NemoClaw (GitHub, n8n, Semaphore operations)
- `network:*` → NetClaw (device health, config, topology, pcap)
- `infra:*` → Either (Proxmox monitoring goes to NemoClaw, network path tracing to NetClaw)

---

## 12. Security Considerations

| Concern | Mitigation |
|---|---|
| NetClaw has broad network access | OpenShell policy restricts to {{ lan_subnet }} only; no internet device access |
| Device credentials (SSH keys, SNMP strings) | Stored in OpenBao, injected via AppRole; never in config files |
| Config changes to production devices | Read-only by default; writes require ITSM gate or explicit `LAB_MODE=true` |
| Anthropic API key exposure | Stored in OpenBao at `secret/services/netclaw`, not in env files |
| nmap scanning scope | CIDR scope enforcement built into NetClaw's nmap MCP |
| GAIT audit trail integrity | Append-only log in Git; tamper-evident by design |
| Cross-agent task injection | NocoDB task queue validates `source` field; only registered agents can create tasks |

---

## 13. Resource Estimates

| Resource | Estimate | Notes |
|---|---|---|
| VM cost | 4 cores, 8GB RAM, 60GB disk | Comparable to NemoClaw VM |
| Anthropic API | ~$5-20/month | Depends on monitoring frequency and chat volume |
| Network bandwidth | Minimal | SSH/SNMP polling is lightweight |
| Storage | ~5-10 GB for pcaps + config backups | Prune old pcaps on schedule |

---

## 14. Updated Service Inventory

| VM Name | IP | Services | Port | Runtime |
|---|---|---|---|---|
| `openbao` | `{{ openbao_host }}` | OpenBao | 8200 | Podman |
| `nocodb` | `{{ nocodb_host }}` | NocoDB + Postgres | 8181 | Podman |
| `n8n` | `{{ n8n_host }}` | n8n + Worker + Postgres + Redis | 5678 | Podman |
| `semaphore` | `{{ semaphore_host }}` | Semaphore + Postgres | 3000 | Podman |
| `nemoclaw` | `{{ nemoclaw_host }}` | NemoClaw + OpenShell | — | Docker |
| `netclaw` | `{{ netclaw_host }}` | **NetClaw + OpenClaw + MCP servers** | **18789** | **Docker** |
| `netbox` | `{{ netbox_host }}` | NetBox + Diode Pipeline | 8000 | Podman |

---

## 15. Files to Create/Modify

### New Files

| File | Purpose |
|---|---|
| `vms/netclaw-network/deploy.sh` | 5-step deploy script following established pattern |
| `vms/netclaw-network/config/testbed.yaml` | pyATS device inventory |
| `vms/netclaw-network/config/network-policy.yaml` | OpenShell network policy |
| `vms/netclaw-network/config/USER.md` | NetClaw operator identity |
| `vms/openbao/config/policies/netclaw-readwrite.hcl` | OpenBao policy |
| `proxmox/vm-specs.yml` | Add netclaw entry |
| `semaphore/inventory/production.yml` | Add netclaw host |

### Modified Files

| File | Change |
|---|---|
| `vms/openbao/deploy.sh` | Add `netclaw` AppRole in Step 6 |
| `orchestrate.sh` | Add `netclaw-network` to SERVICES array (after nemoclaw) |
| `nemoclaw/agent-cloud.yaml` | No change — NemoClaw doesn't need to reach NetClaw directly |
| `IMPLEMENTATION_PLAN.md` | Add NetClaw to Phase 1 / Phase 3 sections |
| `CLAUDE.md` | Add NetClaw to Production IPs, container names |
| `README.md` | Add NetClaw to topology table |

---

## 16. Open Questions

1. **Anthropic API key:** Does the project have an existing Anthropic API key for OpenClaw, or does one need to be created? NetClaw uses Claude as its inference provider.

2. **Slack workspace:** Is there an existing Slack workspace for the homelab? NetClaw's primary interface is Slack (WebSocket-based, first-party OpenClaw channel). Discord works but requires a community plugin.

3. **pfSense SSH access:** Do the pfSense firewalls have SSH enabled? NetClaw needs SSH for device interaction. If not, the REST API (port 443) is an alternative but requires a custom MCP server.

4. **Switch inventory:** Are there managed switches in the homelab? The inventory shows only servers and VMs. Managed switches would be the highest-value pyATS targets.

5. **Prometheus/Grafana:** Are Prometheus and Grafana deployed in the homelab? If yes, NetClaw's Grafana MCP (75+ tools) and Prometheus MCP (6 tools) add significant observability value.

6. **Budget for Anthropic API:** NetClaw makes Claude API calls for every interaction and scheduled task. Estimated $5-20/month depending on polling frequency. Is this acceptable?

7. **ContainerLab interest:** The FRR testbed + BGP daemon is a powerful learning tool but requires Docker-in-Docker or privileged containers. Should this be scoped for Phase D or deferred?
