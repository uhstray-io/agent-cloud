"""pfSense REST API → NetBox Diode worker for orb-agent.

Queries the pfSense REST API (pfrest v2) for device, interface, IP address,
and gateway data, then returns Diode entities for ingestion.

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

import ipaddress
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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MANUFACTURER = "Netgate"
PLATFORM_BASE = "pfSense"


def _is_valid_ip(addr_str):
    """Check if an IP address string is usable (not unspecified, loopback, or link-local)."""
    try:
        ip = ipaddress.ip_address(addr_str)
        return not (ip.is_unspecified or ip.is_loopback or ip.is_link_local)
    except (ValueError, TypeError):
        return False


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

class PfSenseSyncBackend(_Backend):
    """orb-agent worker backend for pfSense REST API sync."""

    def setup(self) -> Metadata:
        return Metadata(
            name="pfsense-sync",
            app_name="pfsense-sync",
            app_version="1.3.0",
            description="pfSense REST API → NetBox via Diode (+ seed data + primary_ip4)",
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

        # Seed data — organizational hierarchy
        self._site_name = site_name
        self._region_name = getattr(config, "region_name", "")
        self._location_name = getattr(config, "location_name", "")
        self._rack_name = getattr(config, "rack_name", "")
        self._tenant_name = getattr(config, "tenant_name", "")
        self._site_latitude = getattr(config, "site_latitude", "")
        self._site_longitude = getattr(config, "site_longitude", "")

        try:
            pfsense = PfSenseClient(host, api_key)
            entities = self._build_entities(pfsense, site_name, device_role)
            print(f"[pfsense-sync] Produced {len(entities)} entities from {host}")
            return entities
        except Exception as e:
            print(f"[pfsense-sync] ERROR: {e}", file=sys.stderr)
            return []

    # ── Seed data helpers ────────────────────────────────────────

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
        """Emit standalone Region/Site/Location/Rack/Tenant entities.

        NOTE: Diode rejects Device entities that nest Region inside Site.
        The Site→Region link MUST be established via a standalone Site entity.
        """
        entities = []

        if self._region_name:
            try:
                entities.append(Entity(region=Region(name=self._region_name)))
            except Exception as e:
                print(f"[pfsense-sync] WARNING: Failed to emit Region entity: {e}", file=sys.stderr)

        # Standalone Site entity — carries Region link and GPS coordinates
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
            print(f"[pfsense-sync] WARNING: Failed to emit Site entity: {e}", file=sys.stderr)

        if self._location_name:
            try:
                entities.append(Entity(location=Location(
                    name=self._location_name,
                    site=Site(name=self._site_name),
                )))
            except Exception as e:
                print(f"[pfsense-sync] WARNING: Failed to emit Location entity: {e}", file=sys.stderr)

        if self._rack_name:
            try:
                rack_kwargs = {"name": self._rack_name, "site": Site(name=self._site_name)}
                if self._location_name:
                    rack_kwargs["location"] = Location(
                        name=self._location_name,
                        site=Site(name=self._site_name),
                    )
                entities.append(Entity(rack=Rack(**rack_kwargs)))
            except Exception as e:
                print(f"[pfsense-sync] WARNING: Failed to emit Rack entity: {e}", file=sys.stderr)

        if self._tenant_name:
            try:
                entities.append(Entity(tenant=Tenant(name=self._tenant_name)))
            except Exception as e:
                print(f"[pfsense-sync] WARNING: Failed to emit Tenant entity: {e}", file=sys.stderr)

        return entities

    def _build_entities(self, pfsense, site_name, device_role):
        entities = []

        # Emit seed entities (region, location, rack, tenant) first
        entities.extend(self._build_seed_entities())

        # Device info
        hostname_data = pfsense.get_hostname()
        version_data = pfsense.get_version()
        status_data = pfsense.get_system_status()

        hostname = hostname_data.get("hostname", "pfsense")
        domain = hostname_data.get("domain", "")
        fqdn = f"{hostname}.{domain}" if domain else hostname
        device_name = fqdn

        version = version_data.get("version", "unknown")
        device_type_model = status_data.get("platform", "Netgate 4200")
        serial = status_data.get("serial", "")
        platform_full = f"pfSense {version}" if version != "unknown" else "pfSense"

        # Fetch interfaces first to find LAN IP for primary_ip4
        ifaces = []
        primary_ip = None
        try:
            raw_ifaces = pfsense.get_interfaces()
            if isinstance(raw_ifaces, dict):
                ifaces = list(raw_ifaces.values())
            else:
                ifaces = raw_ifaces if isinstance(raw_ifaces, list) else []

            for iface in ifaces:
                if not isinstance(iface, dict):
                    continue
                if iface.get("descr", "").upper() == "LAN":
                    ipaddr = iface.get("ipaddr", "")
                    subnet = iface.get("subnet", "")
                    if ipaddr and subnet and _is_valid_ip(ipaddr):
                        primary_ip = f"{ipaddr}/{subnet}"
                    break
        except Exception as e:
            print(f"[pfsense-sync] WARNING: Failed to fetch interfaces: {e}", file=sys.stderr)

        device_ref = Device(
            name=device_name,
            device_type=DeviceType(
                model=device_type_model,
                manufacturer=Manufacturer(name=MANUFACTURER),
            ),
            site=Site(name=site_name),
            role=DeviceRole(name=device_role),
        )

        # Physical gateway — gets rack and tenant (Region linked via standalone Site entity)
        device_kwargs = dict(
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
        rack = self._rack_or_none()
        if rack:
            device_kwargs["rack"] = rack
        tenant = self._tenant_or_none()
        if tenant:
            device_kwargs["tenant"] = tenant
        if primary_ip:
            device_kwargs["primary_ip4"] = IPAddress(address=primary_ip)
        device = Device(**device_kwargs)
        entities.append(Entity(device=device))

        # Build Interface + IP entities from already-fetched data
        try:
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

                ipaddr = iface.get("ipaddr", "")
                subnet = iface.get("subnet", "")
                if ipaddr and subnet and _is_valid_ip(ipaddr):
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
            print(f"[pfsense-sync] WARNING: Failed to build interface entities: {e}", file=sys.stderr)

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
                is_default = gw.get("defaultgw")

                # Resolve "dynamic" gateways from live status
                if gw_ip in ("dynamic", "") and gw_name in gw_status_map:
                    gw_ip = gw_status_map[gw_name].get("srcip", "")

                if gw_ip and gw_ip not in ("dynamic", ""):
                    ip_kwargs = dict(
                        address=f"{gw_ip}/32",
                        status="active",
                        description=f"Gateway: {gw_name} ({gw.get('interface', '')})",
                    )
                    if is_default:
                        ip_kwargs["role"] = "vip"
                    ip_entity = IPAddress(**ip_kwargs)
                    entities.append(Entity(ip_address=ip_entity))

        except Exception as e:
            print(f"[pfsense-sync] WARNING: Failed to fetch gateways: {e}", file=sys.stderr)

        # ARP table entity creation REMOVED (Phase A cleanup).
        # ARP entries created /32 IPs that conflicted with interface IPs
        # discovered with proper prefix lengths, causing duplicates in NetBox.

        return entities
