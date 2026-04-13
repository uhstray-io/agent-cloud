"""Proxmox VE API → NetBox Diode worker for orb-agent.

Queries the Proxmox REST API for cluster nodes, QEMU VMs, and LXC containers,
then returns Diode entities for ingestion into NetBox.

Phase 2a: Cluster metadata only (nodes, VMs, LXC with resource configs).
Phase 2b (future): Guest agent network interfaces and IPs.

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

import sys
from collections.abc import Iterable
from urllib.parse import urlparse

from proxmoxer import ProxmoxAPI

from netboxlabs.diode.sdk.ingester import (
    Device,
    DeviceRole,
    DeviceType,
    Entity,
    Manufacturer,
    Platform,
    Site,
)
from worker.backend import Backend
from worker.models import Metadata, Policy

MANUFACTURER = "Proxmox"
PLATFORM = "Proxmox VE"


def _mb_to_gb(mb):
    """Convert MiB to GiB, rounded to 1 decimal."""
    return round(mb / 1024, 1)


def _bytes_to_gb(b):
    """Convert bytes to GiB, rounded to 1 decimal."""
    return round(b / (1024 ** 3), 1)


class ProxmoxDiscoveryBackend(Backend):
    """orb-agent worker backend for Proxmox VE API discovery.

    Note on dedup: All orb-agent backends share the common agent_name
    (netbox-discovery-agent). The app_name here maps to producer_app_name
    in Diode, which tracks provenance but doesn't affect entity identity.
    Dedup relies on entity uniqueness (device name + site). Validate after
    deployment that no duplicate Devices are created.
    """

    def setup(self) -> Metadata:
        return Metadata(
            name="proxmox-discovery",
            app_name="proxmox-discovery",
            app_version="1.0.0",
            description="Proxmox VE API → NetBox via Diode",
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

        try:
            prox = self._connect(url, token_id, api_token, verify_ssl)
            entities = self._discover(prox, site_name)
            print(f"[proxmox-discovery] Produced {len(entities)} entities from {url}")
            return entities
        except Exception as e:
            print(f"[proxmox-discovery] ERROR: {e}", file=sys.stderr)
            return []

    def _connect(self, url, token_id, api_token, verify_ssl):
        """Create authenticated Proxmox API connection.

        token_id format: 'user@realm!tokenname' (e.g. 'discovery@pve!netbox')
        The proxmoxer library needs user and token_name split apart.
        """
        # Parse URL for host and port
        # url might be https://192.168.1.10:8006 or https://pve.example.com
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or 8006

        # Split token_id: "user@realm!tokenname" → user="user@realm", token_name="tokenname"
        if "!" in token_id:
            user, token_name = token_id.split("!", 1)
        else:
            # Fallback: treat entire string as user, no token name
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

        nodes = prox.nodes.get()
        for node_data in nodes:
            node_name = node_data["node"]
            node_status = node_data.get("status", "unknown")

            # --- Node as Device (role: hypervisor) ---
            node_entities = self._build_node(node_data, prox, site_name)
            entities.extend(node_entities)

            if node_status != "online":
                print(
                    f"[proxmox-discovery] Skipping VMs/LXC on offline node {node_name}",
                    file=sys.stderr,
                )
                continue

            # --- QEMU VMs on this node ---
            try:
                vms = prox.nodes(node_name).qemu.get()
                for vm_data in vms:
                    vm_entities = self._build_vm(
                        vm_data, node_name, prox, site_name
                    )
                    entities.extend(vm_entities)
            except Exception as e:
                print(
                    f"[proxmox-discovery] WARNING: Failed to list VMs on {node_name}: {e}",
                    file=sys.stderr,
                )

            # --- LXC containers on this node ---
            try:
                containers = prox.nodes(node_name).lxc.get()
                for ct_data in containers:
                    ct_entities = self._build_lxc(
                        ct_data, node_name, prox, site_name
                    )
                    entities.extend(ct_entities)
            except Exception as e:
                print(
                    f"[proxmox-discovery] WARNING: Failed to list LXC on {node_name}: {e}",
                    file=sys.stderr,
                )

        return entities

    def _build_node(self, node_data, prox, site_name):
        """Build entities for a Proxmox cluster node."""
        entities = []
        node_name = node_data["node"]

        # Fetch detailed node status for hardware info
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
            print(
                f"[proxmox-discovery] WARNING: Failed to get status for node {node_name}: {e}",
                file=sys.stderr,
            )

        device = Device(
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
            comments=(
                f"Proxmox node. CPUs: {cpu_count}, RAM: {mem_gb} GiB. "
                f"Discovered via Proxmox API."
            ),
        )
        entities.append(Entity(device=device))

        return entities

    def _build_vm(self, vm_data, node_name, prox, site_name):
        """Build entities for a QEMU virtual machine."""
        entities = []
        vmid = vm_data["vmid"]
        vm_name = vm_data.get("name", f"vm-{vmid}")
        vm_status = vm_data.get("status", "unknown")

        # Map Proxmox status to NetBox status
        status_map = {
            "running": "active",
            "stopped": "offline",
            "paused": "offline",
        }
        nb_status = status_map.get(vm_status, "offline")

        # Get VM config for resource details
        cpu_count = vm_data.get("cpus", vm_data.get("maxcpu", 0))
        # List endpoint returns maxmem in bytes; config endpoint returns memory in MiB
        raw_mem = vm_data.get("maxmem", 0)
        mem_gb = _bytes_to_gb(raw_mem) if raw_mem > 1024 * 1024 else _mb_to_gb(raw_mem)

        try:
            config = prox.nodes(node_name).qemu(vmid).config.get()
            cpu_count = config.get("cores", cpu_count)
            sockets = config.get("sockets", 1)
            cpu_count = cpu_count * sockets
            if "memory" in config:
                mem_gb = _mb_to_gb(config["memory"])
        except Exception as e:
            print(f"[proxmox-discovery] DEBUG: Failed to get config for VM {vmid}: {e}", file=sys.stderr)

        device = Device(
            name=vm_name,
            device_type=DeviceType(
                model=f"QEMU VM ({cpu_count} vCPU, {mem_gb} GiB)",
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            site=Site(name=site_name),
            role=DeviceRole(name="server"),
            status=nb_status,
            comments=(
                f"VMID: {vmid}. Host: {node_name}. "
                f"vCPUs: {cpu_count}, RAM: {mem_gb} GiB. "
                f"Discovered via Proxmox API."
            ),
        )
        entities.append(Entity(device=device))

        return entities

    def _build_lxc(self, ct_data, node_name, prox, site_name):
        """Build entities for an LXC container."""
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

        try:
            config = prox.nodes(node_name).lxc(vmid).config.get()
            cpu_count = config.get("cores", cpu_count)
            if "memory" in config:
                mem_gb = _mb_to_gb(config["memory"])
        except Exception as e:
            print(f"[proxmox-discovery] DEBUG: Failed to get config for LXC {vmid}: {e}", file=sys.stderr)

        device = Device(
            name=ct_name,
            device_type=DeviceType(
                model=f"LXC Container ({cpu_count} vCPU, {mem_gb} GiB)",
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            site=Site(name=site_name),
            role=DeviceRole(name="container"),
            status=nb_status,
            comments=(
                f"VMID: {vmid}. Host: {node_name}. "
                f"vCPUs: {cpu_count}, RAM: {mem_gb} GiB. "
                f"Discovered via Proxmox API."
            ),
        )
        entities.append(Entity(device=device))

        return entities
