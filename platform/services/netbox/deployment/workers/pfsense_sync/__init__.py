"""pfSense REST API → NetBox Diode worker for orb-agent.

Queries the pfSense REST API (pfrest v2) for device, interface, IP address,
ARP, and gateway data, then returns Diode entities for ingestion.

Policy config expects:
  pfsense_host: hostname:port (or from vault)
  pfsense_api_key: API key (or from vault)
  site_name: NetBox site (default: "Uhstray.io Datacenter")
  device_role: NetBox role (default: "gateway-router")

Example agent.yaml policy:
  worker:
    pfsense_sync:
      config:
        package: pfsense_sync
        schedule: "*/15 * * * *"
        site_name: "Uhstray.io Datacenter"
        device_role: "gateway-router"
      scope:
        host: "${vault://secret/services/discovery/pfsense/host}"
        api_key: "${vault://secret/services/discovery/pfsense/api_key}"
"""

import sys
from collections.abc import Iterable

import requests
import urllib3

from netboxlabs.diode.sdk.ingester import (
    Device,
    DeviceRole,
    DeviceType,
    Entity,
    Interface,
    IPAddress,
    Manufacturer,
    Platform,
    Site,
)
from worker.backend import Backend
from worker.models import Metadata, Policy

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MANUFACTURER = "Netgate"
PLATFORM_BASE = "pfSense"


class PfSenseClient:
    """Minimal client for the pfSense REST API v2 (pfrest package)."""

    def __init__(self, host, api_key, verify_ssl=False):
        self.session = requests.Session()
        self.base_url = f"https://{host}/api/v2"
        self.session.headers["X-API-Key"] = api_key
        self.session.verify = verify_ssl

    def _get(self, path):
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        return body.get("data", body)

    def get_hostname(self):
        return self._get("/system/hostname")

    def get_version(self):
        return self._get("/system/version")

    def get_system_status(self):
        return self._get("/status/system")

    def get_interfaces(self):
        return self._get("/status/interfaces")

    def get_gateways(self):
        return self._get("/routing/gateways")

    def get_gateway_status(self):
        return self._get("/status/gateways")

    def get_arp_table(self):
        return self._get("/diagnostics/arp_table")


class PfSenseSyncBackend(Backend):
    """orb-agent worker backend for pfSense REST API sync."""

    def setup(self) -> Metadata:
        return Metadata(
            name="pfsense-sync",
            app_name="pfsense-sync",
            app_version="1.0.0",
            description="pfSense REST API → NetBox via Diode",
        )

    def run(self, policy_name: str, policy: Policy) -> Iterable[Entity]:
        config = policy.config
        scope = policy.scope

        # Read credentials from scope (resolved from vault by orb-agent)
        host = scope.get("host", "") if isinstance(scope, dict) else ""
        api_key = scope.get("api_key", "") if isinstance(scope, dict) else ""

        if not host or not api_key:
            print(f"[pfsense-sync] ERROR: host or api_key missing from scope", file=sys.stderr)
            return []

        # Config overrides
        site_name = config.site_name if hasattr(config, "site_name") else "Uhstray.io Datacenter"
        device_role = config.device_role if hasattr(config, "device_role") else "gateway-router"

        try:
            pfsense = PfSenseClient(host, api_key)
            entities = self._build_entities(pfsense, site_name, device_role)
            print(f"[pfsense-sync] Produced {len(entities)} entities from {host}")
            return entities
        except Exception as e:
            print(f"[pfsense-sync] ERROR: {e}", file=sys.stderr)
            return []

    def _build_entities(self, pfsense, site_name, device_role):
        entities = []

        # Device info
        hostname_data = pfsense.get_hostname()
        version_data = pfsense.get_version()
        status_data = pfsense.get_system_status()

        hostname = hostname_data.get("hostname", "pfsense")
        domain = hostname_data.get("domain", "")
        fqdn = f"{hostname}.{domain}" if domain else hostname

        # Use short hostname as device name to match SNMP sysName.
        # This ensures Diode merges SNMP-discovered and worker-discovered
        # entities for the same device instead of creating duplicates.
        device_name = hostname

        version = version_data.get("version", "unknown")
        device_type_model = status_data.get("platform", "Netgate 4200")
        serial = status_data.get("serial", "")
        platform_full = f"pfSense {version}" if version != "unknown" else "pfSense"

        device_ref = Device(
            name=device_name,
            device_type=DeviceType(
                model=device_type_model,
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            site=Site(name=site_name),
            role=DeviceRole(name=device_role),
        )

        device = Device(
            name=device_name,
            device_type=DeviceType(
                model=device_type_model,
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            platform=Platform(
                name=platform_full,
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            site=Site(name=site_name),
            role=DeviceRole(name=device_role),
            serial=serial,
            status="active",
            comments=f"FQDN: {fqdn}. Synced from pfSense REST API. Version: {version}",
        )
        entities.append(Entity(device=device))

        # Interfaces
        try:
            ifaces = pfsense.get_interfaces()
            if isinstance(ifaces, dict):
                ifaces = list(ifaces.values())

            for iface in ifaces:
                if not isinstance(iface, dict):
                    continue

                iface_name = iface.get("hwif", iface.get("name", ""))
                if not iface_name:
                    continue

                descr = iface.get("descr", "")
                mac = iface.get("macaddr", "")
                enabled = bool(iface.get("enable"))
                mtu = None
                if iface.get("mtu"):
                    try:
                        mtu = int(iface["mtu"])
                    except (ValueError, TypeError):
                        pass

                iface_entity = Interface(
                    name=iface_name,
                    device=device_ref,
                    type="other",
                    enabled=enabled,
                    primary_mac_address=mac if mac else None,
                    mtu=mtu,
                    description=descr or f"pfSense interface {iface_name}",
                )
                entities.append(Entity(interface=iface_entity))

                # IP addresses on this interface
                ipaddr = iface.get("ipaddr", "")
                subnet = iface.get("subnet", "")
                if ipaddr and subnet:
                    ip_entity = IPAddress(
                        address=f"{ipaddr}/{subnet}",
                        assigned_object_interface=Interface(
                            name=iface_name,
                            device=device_ref,
                        ),
                        status="active",
                        description=f"{descr} ({iface_name})" if descr else f"pfSense {iface_name}",
                    )
                    entities.append(Entity(ip_address=ip_entity))

        except Exception as e:
            print(f"[pfsense-sync] WARNING: Failed to fetch interfaces: {e}", file=sys.stderr)

        # Gateways as IP addresses
        try:
            gateways = pfsense.get_gateways()
            if isinstance(gateways, dict):
                gateways = list(gateways.values())

            gw_status_map = {}
            try:
                gw_status = pfsense.get_gateway_status()
                if isinstance(gw_status, dict):
                    for gw_name, gw_data in gw_status.items():
                        if isinstance(gw_data, dict) and gw_data.get("srcip"):
                            gw_status_map[gw_name] = gw_data
            except Exception:
                pass

            for gw in gateways:
                if not isinstance(gw, dict):
                    continue
                gw_ip = gw.get("gateway", "")
                gw_name = gw.get("name", "")

                # Resolve "dynamic" gateways from live status
                if gw_ip in ("dynamic", "") and gw_name in gw_status_map:
                    gw_ip = gw_status_map[gw_name].get("srcip", "")

                if gw_ip and gw_ip not in ("dynamic", ""):
                    ip_entity = IPAddress(
                        address=f"{gw_ip}/32",
                        status="active",
                        description=f"Gateway: {gw_name} ({gw.get('interface', '')})",
                    )
                    entities.append(Entity(ip_address=ip_entity))

        except Exception as e:
            print(f"[pfsense-sync] WARNING: Failed to fetch gateways: {e}", file=sys.stderr)

        # ARP table as IP addresses
        try:
            arp_entries = pfsense.get_arp_table()
            if isinstance(arp_entries, dict):
                arp_entries = list(arp_entries.values())
            if isinstance(arp_entries, list):
                for entry in arp_entries:
                    if not isinstance(entry, dict):
                        continue
                    ip = entry.get("ip", "")
                    if ip:
                        ip_entity = IPAddress(
                            address=f"{ip}/32",
                            status="active",
                            description=f"ARP: {entry.get('mac', '')} on {entry.get('interface', '')}",
                        )
                        entities.append(Entity(ip_address=ip_entity))
        except Exception as e:
            print(f"[pfsense-sync] WARNING: Failed to fetch ARP table: {e}", file=sys.stderr)

        return entities
