"""Proxmox VE API → NetBox Diode worker for orb-agent.

Queries the Proxmox REST API for cluster nodes, QEMU VMs, and LXC containers,
then returns Diode entities for ingestion into NetBox.

Phase 2a: Cluster metadata (nodes, VMs, LXC with resource configs).
Phase 2b: Guest agent network interfaces and IPs (nodes, VMs, LXC).

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

import socket
import sys
from collections.abc import Iterable
from urllib.parse import urlparse

from proxmoxer import ProxmoxAPI

from netboxlabs.diode.sdk.ingester import (
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
)
from worker.backend import Backend as _Backend
from worker.models import Metadata, Policy

MANUFACTURER = "Proxmox"
PLATFORM = "Proxmox VE"

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


class ProxmoxDiscoveryBackend(_Backend):
    """orb-agent worker backend for Proxmox VE API discovery."""

    def setup(self) -> Metadata:
        return Metadata(
            name="proxmox-discovery",
            app_name="proxmox-discovery",
            app_version="2.1.0",
            description="Proxmox VE API → NetBox via Diode (nodes, VMs, LXC + interfaces/IPs + seed data)",
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
        self._rack_name = getattr(config, "rack_name", "")
        self._tenant_name = getattr(config, "tenant_name", "")

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
        """Run full cluster discovery: nodes → VMs → LXC containers."""
        entities = []

        # Emit seed entities (region, location, rack, tenant) first
        entities.extend(self._build_seed_entities())

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

    def _site(self):
        """Site reference with optional Region nesting."""
        kwargs = {"name": self._site_name}
        if self._region_name:
            kwargs["region"] = Region(name=self._region_name)
        return Site(**kwargs)

    def _rack_or_none(self):
        """Rack reference with Location nesting, or None."""
        if not self._rack_name:
            return None
        kwargs = {"name": self._rack_name}
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
        """Emit standalone Region/Location/Rack/Tenant entities.

        These ensure the hierarchy objects exist in NetBox with descriptions
        before devices reference them.
        """
        entities = []
        try:
            if self._region_name:
                entities.append(Entity(region=Region(name=self._region_name)))
            if self._location_name:
                entities.append(Entity(location=Location(
                    name=self._location_name,
                    site=Site(name=self._site_name),
                )))
            if self._rack_name:
                rack_kwargs = {"name": self._rack_name}
                if self._location_name:
                    rack_kwargs["location"] = Location(
                        name=self._location_name,
                        site=Site(name=self._site_name),
                    )
                entities.append(Entity(rack=Rack(**rack_kwargs)))
            if self._tenant_name:
                entities.append(Entity(tenant=Tenant(name=self._tenant_name)))
        except Exception as e:
            print(f"[proxmox-discovery] WARNING: Failed to build seed entities: {e}", file=sys.stderr)
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

    # ── Interface + IP entity builder ─────────────────────────────

    def _build_iface_entities(self, iface_name, device_ref, hw_addr=None, ips=None):
        """Build Interface + IPAddress entities for one network interface.

        Args:
            iface_name: Interface name (e.g. eth0, vmbr0)
            device_ref: Device reference for linking
            hw_addr: MAC address (optional)
            ips: List of dicts with 'address' and 'prefix' keys (optional)

        Returns:
            List of Entity objects
        """
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
            # Skip link-local and loopback
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

        # Physical node — gets site (with region), rack, and tenant
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
            site=self._site(),
            role=DeviceRole(name="hypervisor"),
            status="active" if node_data.get("status") == "online" else "offline",
            description=node_desc if node_desc else None,
            comments=(
                f"Proxmox node. CPUs: {cpu_count}, RAM: {mem_gb} GiB. "
                f"Discovered via Proxmox API."
            ),
        )
        rack = self._rack_or_none()
        if rack:
            device_kwargs["rack"] = rack
        tenant = self._tenant_or_none()
        if tenant:
            device_kwargs["tenant"] = tenant
        device = Device(**device_kwargs)
        entities.append(Entity(device=device))

        # Phase 2b: Node network interfaces
        if node_data.get("status") == "online":
            device_ref = self._device_ref(node_name, site_name)
            try:
                network = prox.nodes(node_name).network.get()
                for net in network:
                    iface_name = net.get("iface", "")
                    if _should_skip_iface(iface_name):
                        continue

                    ips = []
                    # IPv4
                    if net.get("address") and net.get("netmask"):
                        prefix = self._netmask_to_prefix(net["netmask"])
                        if prefix:
                            ips.append({"address": net["address"], "prefix": prefix})
                    # CIDR format (some Proxmox versions)
                    if net.get("cidr"):
                        cidr_prefix = _prefix_len(net["cidr"])
                        addr = str(net["cidr"]).split("/")[0]
                        if cidr_prefix and addr:
                            ips.append({"address": addr, "prefix": cidr_prefix})
                    # IPv6
                    if net.get("address6") and net.get("netmask6"):
                        ips.append({"address": net["address6"], "prefix": _int(net["netmask6"])})

                    hw_addr = net.get("mac", None)
                    iface_entities = self._build_iface_entities(iface_name, device_ref, hw_addr, ips)
                    entities.extend(iface_entities)
            except Exception as e:
                print(f"[proxmox-discovery] WARNING: Failed to get network for node {node_name}: {e}", file=sys.stderr)

        return entities

    # ── VM builder ────────────────────────────────────────────────

    def _build_vm(self, vm_data, node_name, prox, site_name):
        """Build entities for a QEMU VM (device + guest agent interfaces + IPs)."""
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
        raw_mem = vm_data.get("maxmem", 0)
        mem_gb = _bytes_to_gb(raw_mem) if raw_mem > 1024 * 1024 else _mb_to_gb(raw_mem)

        vm_desc = ""
        try:
            config = prox.nodes(node_name).qemu(vmid).config.get()
            vm_desc = config.get("description", "")
            cpu_count = _int(config.get("cores", cpu_count), cpu_count)
            sockets = _int(config.get("sockets", 1), 1)
            cpu_count = cpu_count * sockets
            if "memory" in config:
                mem_gb = _mb_to_gb(config["memory"])
        except Exception as e:
            print(f"[proxmox-discovery] DEBUG: Failed to get config for VM {vmid}: {e}", file=sys.stderr)

        # Virtual machine — gets site (with region) and tenant, no rack
        device_kwargs = dict(
            name=vm_name,
            device_type=DeviceType(
                model=f"QEMU VM ({cpu_count} vCPU, {mem_gb} GiB)",
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            site=Site(name=site_name),
            role=DeviceRole(name="server"),
            status=nb_status,
            description=vm_desc if vm_desc else None,
            comments=(
                f"VMID: {vmid}. Host: {node_name}. "
                f"vCPUs: {cpu_count}, RAM: {mem_gb} GiB. "
                f"Discovered via Proxmox API."
            ),
        )
        tenant = self._tenant_or_none()
        if tenant:
            device_kwargs["tenant"] = tenant
        device = Device(**device_kwargs)
        entities.append(Entity(device=device))

        # Phase 2b: Guest agent network interfaces (only for running VMs)
        if vm_status == "running":
            device_ref = self._device_ref(vm_name, site_name)
            try:
                agent_ifaces = prox.nodes(node_name).qemu(vmid).agent("network-get-interfaces").get()
                # Response is {"result": [{...}, ...]} or a list directly
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
                        ip_type = ip_info.get("ip-address-type", "")
                        if addr and prefix:
                            ips.append({"address": addr, "prefix": prefix})

                    iface_entities = self._build_iface_entities(iface_name, device_ref, hw_addr, ips)
                    entities.extend(iface_entities)

            except Exception as e:
                err_str = str(e)
                # QEMU guest agent not running/installed — expected for some VMs
                if "QEMU guest agent is not running" in err_str or "500" in err_str:
                    pass  # Silent — many VMs don't have guest agent
                else:
                    print(f"[proxmox-discovery] DEBUG: No guest agent for VM {vm_name} ({vmid}): {e}", file=sys.stderr)

        return entities

    # ── LXC builder ───────────────────────────────────────────────

    def _build_lxc(self, ct_data, node_name, prox, site_name):
        """Build entities for an LXC container (device + interfaces + IPs)."""
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
        raw_mem = ct_data.get("maxmem", 0)
        mem_gb = _bytes_to_gb(raw_mem) if raw_mem > 1024 * 1024 else _mb_to_gb(raw_mem)

        ct_desc = ""
        try:
            config = prox.nodes(node_name).lxc(vmid).config.get()
            ct_desc = config.get("description", "")
            cpu_count = _int(config.get("cores", cpu_count), cpu_count)
            if "memory" in config:
                mem_gb = _mb_to_gb(config["memory"])
        except Exception as e:
            print(f"[proxmox-discovery] DEBUG: Failed to get config for LXC {vmid}: {e}", file=sys.stderr)

        # Container — gets site (with region) and tenant, no rack
        device_kwargs = dict(
            name=ct_name,
            device_type=DeviceType(
                model=f"LXC Container ({cpu_count} vCPU, {mem_gb} GiB)",
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            site=Site(name=site_name),
            role=DeviceRole(name="container"),
            status=nb_status,
            description=ct_desc if ct_desc else None,
            comments=(
                f"VMID: {vmid}. Host: {node_name}. "
                f"vCPUs: {cpu_count}, RAM: {mem_gb} GiB. "
                f"Discovered via Proxmox API."
            ),
        )
        tenant = self._tenant_or_none()
        if tenant:
            device_kwargs["tenant"] = tenant
        device = Device(**device_kwargs)
        entities.append(Entity(device=device))

        # Phase 2b: LXC network interfaces (only for running containers)
        if ct_status == "running":
            device_ref = self._device_ref(ct_name, site_name)
            try:
                lxc_ifaces = prox.nodes(node_name).lxc(vmid).interfaces.get()

                for lxc_iface in lxc_ifaces:
                    if not isinstance(lxc_iface, dict):
                        continue
                    iface_name = lxc_iface.get("name", "")
                    hw_addr = lxc_iface.get("hwaddr", None)

                    ips = []
                    # IPv4
                    if lxc_iface.get("inet"):
                        cidr = lxc_iface["inet"]
                        prefix = _prefix_len(cidr)
                        addr = str(cidr).split("/")[0]
                        if addr and prefix:
                            ips.append({"address": addr, "prefix": prefix})
                    # IPv6
                    if lxc_iface.get("inet6"):
                        cidr6 = lxc_iface["inet6"]
                        prefix6 = _prefix_len(cidr6)
                        addr6 = str(cidr6).split("/")[0]
                        if addr6 and prefix6:
                            ips.append({"address": addr6, "prefix": prefix6})

                    iface_entities = self._build_iface_entities(iface_name, device_ref, hw_addr, ips)
                    entities.extend(iface_entities)

            except Exception as e:
                print(f"[proxmox-discovery] DEBUG: Failed to get interfaces for LXC {ct_name} ({vmid}): {e}", file=sys.stderr)

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
