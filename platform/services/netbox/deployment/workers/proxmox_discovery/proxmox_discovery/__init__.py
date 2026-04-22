"""Proxmox VE API → NetBox Diode worker for orb-agent.

Queries the Proxmox REST API for cluster nodes, QEMU VMs, and LXC containers,
then returns Diode entities for ingestion into NetBox.

Phase 2a: Cluster metadata (nodes, VMs, LXC with resource configs).
Phase 2b: Guest agent network interfaces and IPs (nodes, VMs, LXC).
Phase 2c-i: Primary IPv4 assignment on all devices/VMs.
Phase 2c-ii: Cluster modeling (Cluster + VirtualMachine entities, VMInterface for VMs/LXC).
Phase 2c-iv: Description sanitization (strips credential lines from VM/LXC descriptions).

Policy config expects:
  proxmox_url: Proxmox API URL (e.g. https://pve.example.com:8006)
  token_id: API token ID (e.g. user@pam!discovery)
  api_token: API token secret
  verify_ssl: Whether to verify TLS (default: false for self-signed)
  site_name: NetBox site (default: "Uhstray.io Datacenter")

Example agent.yaml policy:
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
"""

import re
import socket
import sys
from collections.abc import Iterable
from urllib.parse import urlparse

from proxmoxer import ProxmoxAPI

from netboxlabs.diode.sdk.ingester import (
    Cluster,
    ClusterType,
    Device,
    DeviceRole,
    DeviceType,
    Entity,
    Interface,
    IPAddress,
    Location,
    Manufacturer,
    Platform,
    Rack,
    Region,
    Site,
    Tenant,
    VirtualMachine,
    VMInterface,
)
from worker.backend import Backend as _Backend
from worker.models import Metadata, Policy

MANUFACTURER = "Proxmox"
PLATFORM = "Proxmox VE"
CLUSTER_TYPE = "Proxmox VE"

# Interface types to skip (loopback, internal Proxmox bridges, etc.)
_SKIP_IFACE_NAMES = {"lo"}
_SKIP_IFACE_PREFIXES = ("fwbr", "fwln", "fwpr", "tap", "veth")


def _int(val, default=0):
    """Coerce Proxmox API values to int (API may return strings)."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _mb_to_gb(mb):
    """Convert MiB to GiB, rounded to 1 decimal."""
    return round(_int(mb) / 1024, 1)


def _bytes_to_gb(b):
    """Convert bytes to GiB, rounded to 1 decimal."""
    return round(_int(b) / (1024 ** 3), 1)


def _should_skip_iface(name):
    """Skip loopback, firewall bridges, tap devices, and veth pairs."""
    if not name:
        return True
    if name in _SKIP_IFACE_NAMES:
        return True
    if name.startswith(_SKIP_IFACE_PREFIXES):
        return True
    return False


def _iface_type(name):
    """Map interface name to NetBox interface type string."""
    if name.startswith("vmbr"):
        return "bridge"
    if name.startswith("bond"):
        return "lag"
    if name.startswith(("vlan", ".")):
        return "virtual"
    if name.startswith("wg"):
        return "virtual"
    if name.startswith("eth") or name.startswith("en"):
        return "other"
    return "other"


def _prefix_len(cidr):
    """Extract prefix length from CIDR string, return None if invalid."""
    if "/" in str(cidr):
        try:
            return int(str(cidr).split("/")[1])
        except (ValueError, IndexError):
            return None
    return None


def _reverse_dns(ip, timeout=0.5):
    """Attempt reverse DNS lookup. Returns hostname or empty string."""
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        hostname, _, _ = socket.gethostbyaddr(ip)
        return hostname
    except (socket.herror, socket.gaierror, socket.timeout, OSError):
        return ""
    finally:
        socket.setdefaulttimeout(old_timeout)


_SENSITIVE_LINE = re.compile(
    r"(?:\b|_)(password|passwd|secret|token|key)(?:\b|_)", re.IGNORECASE
)


def _sanitize_description(desc):
    """Strip lines containing credential keywords from Proxmox descriptions."""
    if not desc:
        return None
    lines = [ln for ln in desc.splitlines() if not _SENSITIVE_LINE.search(ln)]
    return "\n".join(lines).strip() or None


def _pick_primary_ipv4(ips):
    """Select the best management IPv4 from collected IP dicts.

    Skips loopback, link-local, and IPv6. Returns (address, prefix) or (None, None).
    """
    for ip in ips:
        addr = ip.get("address", "")
        prefix = ip.get("prefix")
        if not addr or not prefix:
            continue
        if ":" in addr:
            continue
        if addr.startswith("127.") or addr.startswith("169.254."):
            continue
        return addr, prefix
    return None, None


class ProxmoxDiscoveryBackend(_Backend):
    """orb-agent worker backend for Proxmox VE API discovery."""

    def setup(self) -> Metadata:
        return Metadata(
            name="proxmox-discovery",
            app_name="proxmox-discovery",
            app_version="3.0.0",
            description="Proxmox VE API → NetBox via Diode (nodes as Device, VMs/LXC as VirtualMachine, Cluster, primary_ip4)",
        )

    def run(self, policy_name: str, policy: Policy) -> Iterable[Entity]:
        scope = policy.scope
        config = policy.config

        # Read credentials from scope (resolved from vault by orb-agent)
        url = scope.get("url", "") if isinstance(scope, dict) else ""
        token_id = scope.get("token_id", "") if isinstance(scope, dict) else ""
        api_token = scope.get("api_token", "") if isinstance(scope, dict) else ""

        if not url or not token_id or not api_token:
            print(
                "[proxmox-discovery] ERROR: url, token_id, or api_token missing from scope",
                file=sys.stderr,
            )
            return []

        # Config overrides
        site_name = (
            config.site_name
            if hasattr(config, "site_name")
            else "Uhstray.io Datacenter"
        )
        verify_ssl = (
            config.verify_ssl if hasattr(config, "verify_ssl") else False
        )

        # Seed data — organizational hierarchy
        self._site_name = site_name
        self._region_name = getattr(config, "region_name", "")
        self._location_name = getattr(config, "location_name", "")
        self._tenant_name = getattr(config, "tenant_name", "")
        self._site_latitude = getattr(config, "site_latitude", "")
        self._site_longitude = getattr(config, "site_longitude", "")

        # Per-node rack assignments (dict of node_name → rack_name)
        raw_rack = getattr(config, "rack_assignments", None)
        if isinstance(raw_rack, dict):
            self._rack_assignments = raw_rack
        elif raw_rack is not None:
            try:
                self._rack_assignments = dict(raw_rack)
            except (TypeError, ValueError):
                self._rack_assignments = {}
        else:
            self._rack_assignments = {}
        # Fallback: single rack_name applies to all nodes
        self._default_rack = getattr(config, "rack_name", "")

        try:
            prox = self._connect(url, token_id, api_token, verify_ssl)
            entities = self._discover(prox, site_name)
            print(f"[proxmox-discovery] Produced {len(entities)} entities from {url}")
            return entities
        except Exception as e:
            print(f"[proxmox-discovery] ERROR: {e}", file=sys.stderr)
            return []

    def _connect(self, url, token_id, api_token, verify_ssl):
        """Create authenticated Proxmox API connection."""
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or 8006

        if "!" in token_id:
            user, token_name = token_id.split("!", 1)
        else:
            user = token_id
            token_name = "discovery"

        return ProxmoxAPI(
            host,
            port=port,
            user=user,
            token_name=token_name,
            token_value=api_token,
            verify_ssl=verify_ssl,
            timeout=30,
        )

    def _discover(self, prox, site_name):
        """Run full cluster discovery: cluster → nodes → VMs → LXC containers."""
        entities = []

        # Emit seed entities (region, location, rack, tenant) first
        entities.extend(self._build_seed_entities())

        # Query Proxmox cluster name and emit Cluster entity
        cluster_name = None
        try:
            cluster_status = prox.cluster.status.get()
            for entry in cluster_status:
                if isinstance(entry, dict) and entry.get("type") == "cluster":
                    cluster_name = entry.get("name")
                    break
        except Exception as e:
            print(f"[proxmox-discovery] WARNING: Failed to get cluster status: {e}", file=sys.stderr)

        if not cluster_name:
            cluster_name = "pve-cluster"

        self._cluster_name = cluster_name
        cluster_kwargs = {
            "name": cluster_name,
            "type": ClusterType(name=CLUSTER_TYPE),
            "status": "active",
            "scope_site": Site(name=site_name),
        }
        tenant = self._tenant_or_none()
        if tenant:
            cluster_kwargs["tenant"] = tenant
        entities.append(Entity(cluster=Cluster(**cluster_kwargs)))

        nodes = prox.nodes.get()
        for node_data in nodes:
            node_name = node_data["node"]
            node_status = node_data.get("status", "unknown")

            node_entities = self._build_node(node_data, prox, site_name)
            entities.extend(node_entities)

            if node_status != "online":
                print(
                    f"[proxmox-discovery] Skipping VMs/LXC on offline node {node_name}",
                    file=sys.stderr,
                )
                continue

            # QEMU VMs
            try:
                vms = prox.nodes(node_name).qemu.get()
                for vm_data in vms:
                    vm_entities = self._build_vm(vm_data, node_name, prox, site_name)
                    entities.extend(vm_entities)
            except Exception as e:
                print(f"[proxmox-discovery] WARNING: Failed to list VMs on {node_name}: {e}", file=sys.stderr)

            # LXC containers
            try:
                containers = prox.nodes(node_name).lxc.get()
                for ct_data in containers:
                    ct_entities = self._build_lxc(ct_data, node_name, prox, site_name)
                    entities.extend(ct_entities)
            except Exception as e:
                print(f"[proxmox-discovery] WARNING: Failed to list LXC on {node_name}: {e}", file=sys.stderr)

        return entities

    # ── Seed data helpers ────────────────────────────────────────

    def _rack_for_node(self, node_name):
        """Rack reference for a specific node, or None.

        Looks up rack from per-node rack_assignments dict, falls back
        to default_rack (from legacy rack_name config) if set.
        """
        rack_name = self._rack_assignments.get(node_name, self._default_rack)
        if not rack_name:
            return None
        kwargs = {"name": rack_name}
        if self._location_name:
            kwargs["location"] = Location(
                name=self._location_name,
                site=Site(name=self._site_name),
            )
        return Rack(**kwargs)

    def _tenant_or_none(self):
        """Tenant reference, or None."""
        if not self._tenant_name:
            return None
        return Tenant(name=self._tenant_name)

    def _build_seed_entities(self):
        """Emit standalone Region/Site/Location/Rack/Tenant entities.

        These ensure the hierarchy objects exist in NetBox before devices
        reference them. Each entity type is wrapped in its own try/except
        so a failure on one doesn't block the others.

        NOTE: Diode rejects Device entities that nest Region inside Site.
        The Site→Region link MUST be established via a standalone Site entity.
        """
        entities = []

        if self._region_name:
            try:
                entities.append(Entity(region=Region(name=self._region_name)))
            except Exception as e:
                print(f"[proxmox-discovery] WARNING: Failed to emit Region entity: {e}", file=sys.stderr)

        # Standalone Site entity — carries Region link and GPS coordinates.
        # Always emitted so lat/lon propagate even if they weren't set on initial creation.
        try:
            site_kwargs = {"name": self._site_name}
            if self._region_name:
                site_kwargs["region"] = Region(name=self._region_name)
            if self._site_latitude:
                try:
                    site_kwargs["latitude"] = float(self._site_latitude)
                except (ValueError, TypeError):
                    pass
            if self._site_longitude:
                try:
                    site_kwargs["longitude"] = float(self._site_longitude)
                except (ValueError, TypeError):
                    pass
            entities.append(Entity(site=Site(**site_kwargs)))
        except Exception as e:
            print(f"[proxmox-discovery] WARNING: Failed to emit Site entity: {e}", file=sys.stderr)

        if self._location_name:
            try:
                entities.append(Entity(location=Location(
                    name=self._location_name,
                    site=Site(name=self._site_name),
                )))
            except Exception as e:
                print(f"[proxmox-discovery] WARNING: Failed to emit Location entity: {e}", file=sys.stderr)

        # Emit Rack entities for each unique rack in rack_assignments
        rack_names = set(self._rack_assignments.values())
        if self._default_rack:
            rack_names.add(self._default_rack)
        for rack_name in sorted(rack_names):
            try:
                rack_kwargs = {"name": rack_name, "site": Site(name=self._site_name)}
                if self._location_name:
                    rack_kwargs["location"] = Location(
                        name=self._location_name,
                        site=Site(name=self._site_name),
                    )
                entities.append(Entity(rack=Rack(**rack_kwargs)))
            except Exception as e:
                print(f"[proxmox-discovery] WARNING: Failed to emit Rack entity '{rack_name}': {e}", file=sys.stderr)

        if self._tenant_name:
            try:
                entities.append(Entity(tenant=Tenant(name=self._tenant_name)))
            except Exception as e:
                print(f"[proxmox-discovery] WARNING: Failed to emit Tenant entity: {e}", file=sys.stderr)

        return entities

    # ── Device reference helper ───────────────────────────────────

    def _device_ref(self, name, site_name):
        """Minimal Device reference for linking interfaces/IPs."""
        return Device(
            name=name,
            device_type=DeviceType(
                model="Proxmox",
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            site=Site(name=site_name),
        )

    # ── Interface + IP entity builder (physical devices) ────────────

    def _build_iface_entities(self, iface_name, device_ref, hw_addr=None, ips=None):
        """Build Interface + IPAddress entities for a physical device interface."""
        entities = []

        if _should_skip_iface(iface_name):
            return entities

        iface = Interface(
            name=iface_name,
            device=device_ref,
            type=_iface_type(iface_name),
            enabled=True,
            primary_mac_address=hw_addr if hw_addr else None,
            description=f"Discovered via Proxmox API",
        )
        entities.append(Entity(interface=iface))

        for ip_info in (ips or []):
            addr = ip_info.get("address", "")
            prefix = ip_info.get("prefix")
            if not addr or not prefix:
                continue
            if addr.startswith("fe80:") or addr.startswith("127.") or addr == "::1":
                continue

            dns_name = _reverse_dns(addr)
            ip_kwargs = dict(
                address=f"{addr}/{prefix}",
                assigned_object_interface=Interface(
                    name=iface_name,
                    device=device_ref,
                ),
                status="active",
                description=f"{iface_name} on {device_ref.name}",
            )
            if dns_name:
                ip_kwargs["dns_name"] = dns_name
            ip_entity = IPAddress(**ip_kwargs)
            entities.append(Entity(ip_address=ip_entity))

        return entities

    # ── VMInterface + IP entity builder (virtual machines) ────────

    def _vm_ref(self, name, site_name):
        """Minimal VirtualMachine reference for linking VMInterfaces/IPs."""
        return VirtualMachine(
            name=name,
            cluster=Cluster(
                name=self._cluster_name,
                type=ClusterType(name=CLUSTER_TYPE),
            ),
            site=Site(name=site_name),
        )

    def _build_vm_iface_entities(self, iface_name, vm_ref, hw_addr=None, ips=None):
        """Build VMInterface + IPAddress entities for a virtual machine interface."""
        entities = []

        if _should_skip_iface(iface_name):
            return entities

        vm_iface = VMInterface(
            name=iface_name,
            virtual_machine=vm_ref,
            enabled=True,
            primary_mac_address=hw_addr if hw_addr else None,
            description=f"Discovered via Proxmox API",
        )
        entities.append(Entity(vm_interface=vm_iface))

        for ip_info in (ips or []):
            addr = ip_info.get("address", "")
            prefix = ip_info.get("prefix")
            if not addr or not prefix:
                continue
            if addr.startswith("fe80:") or addr.startswith("127.") or addr == "::1":
                continue

            dns_name = _reverse_dns(addr)
            ip_kwargs = dict(
                address=f"{addr}/{prefix}",
                assigned_object_vm_interface=VMInterface(
                    name=iface_name,
                    virtual_machine=vm_ref,
                ),
                status="active",
                description=f"{iface_name} on {vm_ref.name}",
            )
            if dns_name:
                ip_kwargs["dns_name"] = dns_name
            ip_entity = IPAddress(**ip_kwargs)
            entities.append(Entity(ip_address=ip_entity))

        return entities

    # ── Node builder ──────────────────────────────────────────────

    def _build_node(self, node_data, prox, site_name):
        """Build entities for a Proxmox cluster node (device + interfaces + IPs)."""
        entities = []
        node_name = node_data["node"]

        cpu_model = "Unknown CPU"
        cpu_count = node_data.get("maxcpu", 0)
        mem_gb = _bytes_to_gb(node_data.get("maxmem", 0))
        pve_version = ""

        try:
            status = prox.nodes(node_name).status.get()
            cpu_info = status.get("cpuinfo", {})
            cpu_model = cpu_info.get("model", "Unknown CPU")
            cpu_count = cpu_info.get("cpus", cpu_count)
            pve_version = status.get("pveversion", "")
        except Exception as e:
            print(f"[proxmox-discovery] WARNING: Failed to get status for node {node_name}: {e}", file=sys.stderr)

        node_desc = ""
        try:
            node_config = prox.nodes(node_name).config.get()
            node_desc = node_config.get("description", "")
        except Exception:
            pass

        # Collect network interfaces first to determine primary IPv4
        collected_ifaces = []
        all_ipv4s = []
        if node_data.get("status") == "online":
            try:
                network = prox.nodes(node_name).network.get()
                for net in network:
                    iface_name = net.get("iface", "")
                    if _should_skip_iface(iface_name):
                        continue

                    ips = []
                    seen = set()
                    if net.get("address") and net.get("netmask"):
                        prefix = self._netmask_to_prefix(net["netmask"])
                        if prefix:
                            seen.add((net["address"], prefix))
                            ips.append({"address": net["address"], "prefix": prefix})
                    if net.get("cidr"):
                        cidr_prefix = _prefix_len(net["cidr"])
                        addr = str(net["cidr"]).split("/")[0]
                        if cidr_prefix and addr and (addr, cidr_prefix) not in seen:
                            ips.append({"address": addr, "prefix": cidr_prefix})
                    if net.get("address6") and net.get("netmask6"):
                        ips.append({"address": net["address6"], "prefix": _int(net["netmask6"])})

                    hw_addr = net.get("mac", None)
                    collected_ifaces.append((iface_name, hw_addr, ips))
                    all_ipv4s.extend(ip for ip in ips if ":" not in ip["address"])
            except Exception as e:
                print(f"[proxmox-discovery] WARNING: Failed to get network for node {node_name}: {e}", file=sys.stderr)

        primary_addr, primary_prefix = _pick_primary_ipv4(all_ipv4s)

        # Physical node — gets rack and tenant (Region linked via standalone Site entity)
        device_kwargs = dict(
            name=node_name,
            device_type=DeviceType(
                model=cpu_model,
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            platform=Platform(
                name=f"Proxmox VE {pve_version}" if pve_version else PLATFORM,
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            site=Site(name=site_name),
            role=DeviceRole(name="hypervisor"),
            status="active" if node_data.get("status") == "online" else "offline",
            description=node_desc if node_desc else None,
            comments=(
                f"Proxmox node. CPUs: {cpu_count}, RAM: {mem_gb} GiB. "
                f"Discovered via Proxmox API."
            ),
        )
        rack = self._rack_for_node(node_name)
        if rack:
            device_kwargs["rack"] = rack
        tenant = self._tenant_or_none()
        if tenant:
            device_kwargs["tenant"] = tenant
        if primary_addr:
            device_kwargs["primary_ip4"] = IPAddress(
                address=f"{primary_addr}/{primary_prefix}"
            )
        device = Device(**device_kwargs)
        entities.append(Entity(device=device))

        # Build Interface + IP entities from collected data
        device_ref = self._device_ref(node_name, site_name)
        for iface_name, hw_addr, ips in collected_ifaces:
            iface_entities = self._build_iface_entities(iface_name, device_ref, hw_addr, ips)
            entities.extend(iface_entities)

        return entities

    # ── VM builder ────────────────────────────────────────────────

    def _build_vm(self, vm_data, node_name, prox, site_name):
        """Build entities for a QEMU VM (VirtualMachine + VMInterfaces + IPs)."""
        entities = []
        vmid = vm_data["vmid"]
        vm_name = vm_data.get("name", f"vm-{vmid}")
        vm_status = vm_data.get("status", "unknown")

        status_map = {
            "running": "active",
            "stopped": "offline",
            "paused": "offline",
        }
        nb_status = status_map.get(vm_status, "offline")

        cpu_count = vm_data.get("cpus", vm_data.get("maxcpu", 0))
        raw_mem = _int(vm_data.get("maxmem", 0))
        mem_mb = raw_mem // (1024 * 1024) if raw_mem > 1024 * 1024 else raw_mem
        disk_mb = 0

        vm_desc = ""
        vm_config = {}
        try:
            vm_config = prox.nodes(node_name).qemu(vmid).config.get()
            config = vm_config
            vm_desc = config.get("description", "")
            cpu_count = _int(config.get("cores", cpu_count), cpu_count)
            sockets = _int(config.get("sockets", 1), 1)
            cpu_count = cpu_count * sockets
            if "memory" in config:
                mem_mb = _int(config["memory"])
            for key, val in config.items():
                if key.startswith(("scsi", "virtio", "ide", "sata")) and isinstance(val, str) and "size=" in val:
                    try:
                        size_str = val.split("size=")[1].split(",")[0].strip()
                        if size_str.endswith("G"):
                            disk_mb += int(size_str[:-1]) * 1024
                        elif size_str.endswith("M"):
                            disk_mb += int(size_str[:-1])
                    except (ValueError, IndexError):
                        pass
        except Exception as e:
            print(f"[proxmox-discovery] DEBUG: Failed to get config for VM {vmid}: {e}", file=sys.stderr)

        # Collect guest agent interfaces to determine primary IPv4
        collected_ifaces = []
        all_ipv4s = []
        if vm_status == "running":
            try:
                agent_ifaces = prox.nodes(node_name).qemu(vmid).agent("network-get-interfaces").get()
                iface_list = agent_ifaces.get("result", agent_ifaces) if isinstance(agent_ifaces, dict) else agent_ifaces

                for agent_iface in iface_list:
                    if not isinstance(agent_iface, dict):
                        continue
                    iface_name = agent_iface.get("name", "")
                    hw_addr = agent_iface.get("hardware-address", None)

                    ips = []
                    for ip_info in agent_iface.get("ip-addresses", []):
                        addr = ip_info.get("ip-address", "")
                        prefix = _int(ip_info.get("prefix", 0))
                        if addr and prefix:
                            ips.append({"address": addr, "prefix": prefix})

                    collected_ifaces.append((iface_name, hw_addr, ips))
                    all_ipv4s.extend(ip for ip in ips if ":" not in ip["address"])

            except Exception as e:
                err_str = str(e)
                if "QEMU guest agent is not running" in err_str or "500" in err_str:
                    pass
                else:
                    print(f"[proxmox-discovery] DEBUG: No guest agent for VM {vm_name} ({vmid}): {e}", file=sys.stderr)

        # Fallback: parse ipconfig0 from cloud-init config when guest agent has no IPs
        if not all_ipv4s:
            try:
                for key in ("ipconfig0", "ipconfig1"):
                    ipconf = vm_config.get(key, "")
                    if isinstance(ipconf, str) and "ip=" in ipconf:
                        ip_part = ipconf.split("ip=")[1].split(",")[0].strip()
                        if "/" in ip_part:
                            addr, prefix_str = ip_part.split("/", 1)
                            prefix = _int(prefix_str)
                            if addr and prefix and not addr.startswith("127."):
                                all_ipv4s.append({"address": addr, "prefix": prefix})
                                collected_ifaces.append(("eth0", None, [{"address": addr, "prefix": prefix}]))
                                break
            except Exception:
                pass

        primary_addr, primary_prefix = _pick_primary_ipv4(all_ipv4s)
        vm_desc = _sanitize_description(vm_desc)

        vm_kwargs = dict(
            name=vm_name,
            cluster=Cluster(
                name=self._cluster_name,
                type=ClusterType(name=CLUSTER_TYPE),
            ),
            site=Site(name=site_name),
            role=DeviceRole(name="server"),
            status=nb_status,
            vcpus=float(cpu_count),
            memory=mem_mb,
            disk=disk_mb if disk_mb else None,
            description=vm_desc,
            comments=(
                f"VMID: {vmid}. Host: {node_name}. "
                f"Discovered via Proxmox API."
            ),
        )
        tenant = self._tenant_or_none()
        if tenant:
            vm_kwargs["tenant"] = tenant
        if primary_addr:
            vm_kwargs["primary_ip4"] = IPAddress(
                address=f"{primary_addr}/{primary_prefix}"
            )
        entities.append(Entity(virtual_machine=VirtualMachine(**vm_kwargs)))

        # Build VMInterface + IP entities from collected data
        vm_ref = self._vm_ref(vm_name, site_name)
        for iface_name, hw_addr, ips in collected_ifaces:
            iface_entities = self._build_vm_iface_entities(iface_name, vm_ref, hw_addr, ips)
            entities.extend(iface_entities)

        return entities

    # ── LXC builder ───────────────────────────────────────────────

    def _build_lxc(self, ct_data, node_name, prox, site_name):
        """Build entities for an LXC container (VirtualMachine + VMInterfaces + IPs)."""
        entities = []
        vmid = ct_data["vmid"]
        ct_name = ct_data.get("name", f"ct-{vmid}")
        ct_status = ct_data.get("status", "unknown")

        status_map = {
            "running": "active",
            "stopped": "offline",
        }
        nb_status = status_map.get(ct_status, "offline")

        cpu_count = ct_data.get("cpus", ct_data.get("maxcpu", 0))
        raw_mem = _int(ct_data.get("maxmem", 0))
        mem_mb = raw_mem // (1024 * 1024) if raw_mem > 1024 * 1024 else raw_mem
        disk_mb = 0

        ct_desc = ""
        try:
            config = prox.nodes(node_name).lxc(vmid).config.get()
            ct_desc = config.get("description", "")
            cpu_count = _int(config.get("cores", cpu_count), cpu_count)
            if "memory" in config:
                mem_mb = _int(config["memory"])
            rootfs = config.get("rootfs", "")
            if isinstance(rootfs, str) and "size=" in rootfs:
                try:
                    size_str = rootfs.split("size=")[1].split(",")[0].strip()
                    if size_str.endswith("G"):
                        disk_mb = int(size_str[:-1]) * 1024
                    elif size_str.endswith("M"):
                        disk_mb = int(size_str[:-1])
                except (ValueError, IndexError):
                    pass
        except Exception as e:
            print(f"[proxmox-discovery] DEBUG: Failed to get config for LXC {vmid}: {e}", file=sys.stderr)

        # Collect LXC interfaces to determine primary IPv4
        collected_ifaces = []
        all_ipv4s = []
        if ct_status == "running":
            try:
                lxc_ifaces = prox.nodes(node_name).lxc(vmid).interfaces.get()

                for lxc_iface in lxc_ifaces:
                    if not isinstance(lxc_iface, dict):
                        continue
                    iface_name = lxc_iface.get("name", "")
                    hw_addr = lxc_iface.get("hwaddr", None)

                    ips = []
                    if lxc_iface.get("inet"):
                        cidr = lxc_iface["inet"]
                        prefix = _prefix_len(cidr)
                        addr = str(cidr).split("/")[0]
                        if addr and prefix:
                            ips.append({"address": addr, "prefix": prefix})
                    if lxc_iface.get("inet6"):
                        cidr6 = lxc_iface["inet6"]
                        prefix6 = _prefix_len(cidr6)
                        addr6 = str(cidr6).split("/")[0]
                        if addr6 and prefix6:
                            ips.append({"address": addr6, "prefix": prefix6})

                    collected_ifaces.append((iface_name, hw_addr, ips))
                    all_ipv4s.extend(ip for ip in ips if ":" not in ip["address"])

            except Exception as e:
                print(f"[proxmox-discovery] DEBUG: Failed to get interfaces for LXC {ct_name} ({vmid}): {e}", file=sys.stderr)

        primary_addr, primary_prefix = _pick_primary_ipv4(all_ipv4s)
        ct_desc = _sanitize_description(ct_desc)

        vm_kwargs = dict(
            name=ct_name,
            cluster=Cluster(
                name=self._cluster_name,
                type=ClusterType(name=CLUSTER_TYPE),
            ),
            site=Site(name=site_name),
            role=DeviceRole(name="container"),
            status=nb_status,
            vcpus=float(cpu_count),
            memory=mem_mb,
            disk=disk_mb if disk_mb else None,
            description=ct_desc,
            comments=(
                f"VMID: {vmid}. Host: {node_name}. LXC container. "
                f"Discovered via Proxmox API."
            ),
        )
        tenant = self._tenant_or_none()
        if tenant:
            vm_kwargs["tenant"] = tenant
        if primary_addr:
            vm_kwargs["primary_ip4"] = IPAddress(
                address=f"{primary_addr}/{primary_prefix}"
            )
        entities.append(Entity(virtual_machine=VirtualMachine(**vm_kwargs)))

        # Build VMInterface + IP entities from collected data
        vm_ref = self._vm_ref(ct_name, site_name)
        for iface_name, hw_addr, ips in collected_ifaces:
            iface_entities = self._build_vm_iface_entities(iface_name, vm_ref, hw_addr, ips)
            entities.extend(iface_entities)

        return entities

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _netmask_to_prefix(netmask):
        """Convert dotted netmask to prefix length (e.g. 255.255.255.0 → 24)."""
        try:
            parts = [int(x) for x in str(netmask).split(".")]
            binary = "".join(f"{p:08b}" for p in parts)
            return binary.count("1")
        except (ValueError, AttributeError):
            return None
