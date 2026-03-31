#!/usr/bin/env python3
"""pfSense REST API → Diode SDK ingestion script.

Queries the pfSense REST API (pfrest v2) for device, interface, IP address,
ARP, and gateway data, then pushes it to NetBox via the Diode gRPC pipeline.

This supplements SNMP discovery with richer data that SNMP can't provide:
  - Device serial number, platform version, exact model
  - Interface descriptions and VLAN assignments
  - ARP table entries
  - Gateway/routing information

Prerequisites:
  1. Install the pfrest REST API package on the pfSense device:
       pkg-static add https://github.com/pfrest/pfSense-pkg-RESTAPI/releases/latest/download/pfSense-2.8.1-pkg-RESTAPI.pkg
  2. Create an API key in pfSense: System > REST API > API Keys
  3. Store the API key in secrets/pfsense_api_key.txt
  4. Install Python dependencies: pip install netboxlabs-diode-sdk requests

Usage:
  python3 lib/pfsense-sync.py [--host HOST[:PORT]] [--dry-run]

Environment variables (override defaults):
  PFSENSE_HOST           pfSense hostname/IP[:port] (required)
  PFSENSE_API_KEY        API key (overrides secrets/pfsense_api_key.txt)
  PFSENSE_VERIFY_SSL     Set to "true" to verify TLS (default: false)
  DIODE_TARGET           Diode gRPC target (default: grpc://localhost:8081/diode)
  DIODE_CLIENT_ID        OAuth2 client ID (overrides secrets/)
  DIODE_CLIENT_SECRET    OAuth2 client secret (overrides secrets/)
"""

import argparse
import json
import os
import sys
import urllib3
import yaml

# Suppress InsecureRequestWarning when verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Path setup ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
SECRETS_DIR = os.path.join(ROOT_DIR, "secrets")
ROLES_FILE = os.path.join(ROOT_DIR, "discovery", "roles.yaml")


def read_secret(name):
    """Read a secret from secrets/<name>.txt, return empty string if missing."""
    path = os.path.join(SECRETS_DIR, f"{name}.txt")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def load_valid_roles():
    """Load the canonical device role list from discovery/roles.yaml."""
    with open(ROLES_FILE) as f:
        data = yaml.safe_load(f)
    return data["device_roles"]


def validate_role(role):
    """Raise ValueError if *role* is not in discovery/roles.yaml."""
    valid = load_valid_roles()
    if role not in valid:
        raise ValueError(
            f"Device role '{role}' is not valid. "
            f"Allowed roles (from discovery/roles.yaml): {', '.join(valid)}"
        )


# ─── pfSense REST API client ────────────────────────────────────

class PfSenseClient:
    """Minimal client for the pfSense REST API v2 (pfrest package)."""

    def __init__(self, host, api_key, verify_ssl=False):
        import requests
        self.session = requests.Session()
        self.base_url = f"https://{host}/api/v2"
        self.session.headers["X-API-Key"] = api_key
        self.session.verify = verify_ssl

    def _get(self, path):
        """GET an API endpoint and return the JSON response data."""
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        # pfrest v2 wraps responses: {"code": 200, "status": "ok", "data": ...}
        return body.get("data", body)

    def get_hostname(self):
        return self._get("/system/hostname")

    def get_version(self):
        return self._get("/system/version")

    def get_system_status(self):
        return self._get("/status/system")

    def get_interfaces(self):
        """Get live interface stats (includes MAC, status, IP)."""
        return self._get("/status/interfaces")

    def get_gateways(self):
        """Get gateway configuration (may have 'dynamic' for DHCP gateways)."""
        return self._get("/routing/gateways")

    def get_gateway_status(self):
        """Get live gateway status (has resolved IPs for DHCP gateways)."""
        return self._get("/status/gateways")

    def get_arp_table(self):
        return self._get("/diagnostics/arp_table")


# ─── Entity builders ────────────────────────────────────────────

SITE_NAME = "Uhstray.io Datacenter"
MANUFACTURER = "Netgate"
DEVICE_ROLE = "gateway-router"
PLATFORM_BASE = "pfSense"


def build_entities(pfsense):
    """Query pfSense API and build Diode SDK entities."""
    from netboxlabs.diode.sdk.ingester import (
        Device, DeviceRole, DeviceType, Entity, Interface, IPAddress,
        Manufacturer, Platform, Site,
    )

    entities = []

    # ── Device info ──────────────────────────────────────────────
    hostname_data = pfsense.get_hostname()
    version_data = pfsense.get_version()
    status_data = pfsense.get_system_status()

    hostname = hostname_data.get("hostname", "pfsense")
    domain = hostname_data.get("domain", "")
    fqdn = f"{hostname}.{domain}" if domain else hostname

    version = version_data.get("version", "unknown")
    # Platform and serial come from /status/system, not /system/version
    device_type_model = status_data.get("platform", "Netgate 4200")
    serial = status_data.get("serial", "")
    platform_full = f"pfSense {version}" if version != "unknown" else "pfSense"

    device = Device(
        name=fqdn,
        device_type=DeviceType(
            model=device_type_model,
            manufacturer=Manufacturer(name=MANUFACTURER),
        ),
        platform=Platform(
            name=platform_full,
            manufacturer=Manufacturer(name=MANUFACTURER),
        ),
        site=Site(name=SITE_NAME),
        role=DeviceRole(name=DEVICE_ROLE),
        serial=serial,
        status="active",
        comments=f"Synced from pfSense REST API. Version: {version}",
    )
    entities.append(Entity(device=device))
    print(f"  Device: {fqdn} ({device_type_model}, {platform_full})")

    # ── Interfaces (from /status/interfaces — live stats with MAC) ──
    try:
        ifaces = pfsense.get_interfaces()
        if isinstance(ifaces, dict):
            # /status/interfaces returns a dict keyed by interface ID
            ifaces = list(ifaces.values())

        iface_count = 0
        for iface in ifaces:
            if not isinstance(iface, dict):
                continue

            # /status/interfaces fields: name, hwif, descr, macaddr, mtu, enable, ipaddr, subnet
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
                device=Device(
                    name=fqdn,
                    device_type=DeviceType(
                        model=device_type_model,
                        manufacturer=Manufacturer(name=MANUFACTURER),
                    ),
                    site=Site(name=SITE_NAME),
                    role=DeviceRole(name=DEVICE_ROLE),
                ),
                type="other",
                enabled=enabled,
                primary_mac_address=mac if mac else None,
                mtu=mtu,
                description=descr or f"pfSense interface {iface_name}",
            )
            entities.append(Entity(interface=iface_entity))
            iface_count += 1

            # ── IP addresses on this interface ───────────────────
            ipaddr = iface.get("ipaddr", "")
            subnet = iface.get("subnet", "")
            if ipaddr and subnet:
                ip_entity = IPAddress(
                    address=f"{ipaddr}/{subnet}",
                    assigned_object_interface=Interface(
                        name=iface_name,
                        device=Device(
                            name=fqdn,
                            device_type=DeviceType(
                                model=device_type_model,
                                manufacturer=Manufacturer(name=MANUFACTURER),
                            ),
                            site=Site(name=SITE_NAME),
                            role=DeviceRole(name=DEVICE_ROLE),
                        ),
                    ),
                    status="active",
                    description=f"{descr} ({iface_name})" if descr else f"pfSense {iface_name}",
                )
                entities.append(Entity(ip_address=ip_entity))

        print(f"  Interfaces: {iface_count}")
    except Exception as e:
        print(f"  WARNING: Failed to fetch interfaces: {e}", file=sys.stderr)

    # ── Gateways ─────────────────────────────────────────────────
    # Merge config (/routing/gateways) with live status (/status/gateways)
    # to resolve actual IPs for DHCP gateways that show "dynamic" in config.
    try:
        gateways = pfsense.get_gateways()
        if isinstance(gateways, dict):
            gateways = list(gateways.values())

        # Try to get live gateway status for resolved DHCP IPs
        gw_status_map = {}
        try:
            gw_status = pfsense.get_gateway_status()
            if isinstance(gw_status, dict):
                # Keyed by gateway name or interface
                gw_status_map = gw_status
            elif isinstance(gw_status, list):
                for s in gw_status:
                    if isinstance(s, dict) and s.get("name"):
                        gw_status_map[s["name"]] = s
        except Exception as e:
            print(f"    Note: /status/gateways unavailable ({e}) — DHCP IPs may be unresolved")

        gw_count = 0
        for gw in gateways:
            if not isinstance(gw, dict):
                continue
            gw_ip = gw.get("gateway", "")
            gw_name = gw.get("name", "")
            gw_iface = gw.get("interface", "")
            is_default = gw.get("defaultgw")

            # For DHCP gateways ("dynamic"), try to resolve from live status
            if not gw_ip or gw_ip == "dynamic":
                status = gw_status_map.get(gw_name, {})
                # Status endpoint may use: srcip, gateway, monitorip, or ipaddr
                resolved_ip = (
                    status.get("gateway")
                    or status.get("srcip")
                    or status.get("ipaddr")
                    or status.get("monitorip")
                    or ""
                )
                if resolved_ip and resolved_ip != "dynamic":
                    # Strip IPv6 zone IDs (%ifname) — NetBox doesn't accept them
                    if "%" in resolved_ip:
                        resolved_ip = resolved_ip.split("%")[0]
                    gw_ip = resolved_ip
                    print(f"    {gw_name}: resolved DHCP gateway → {gw_ip}")
                else:
                    print(f"    {gw_name}: DHCP gateway on {gw_iface} — could not resolve IP")
                    gw_count += 1
                    continue

            if "/" not in gw_ip:
                gw_ip = f"{gw_ip}/32"
            desc = f"Gateway: {gw_name}" if gw_name else "pfSense gateway"
            if not gw.get("gateway") or gw.get("gateway") == "dynamic":
                desc += " (DHCP)"
            gw_kwargs = dict(
                address=gw_ip,
                status="active",
                description=desc,
            )
            if is_default:
                gw_kwargs["role"] = "vip"
            ip_entity = IPAddress(**gw_kwargs)
            entities.append(Entity(ip_address=ip_entity))
            gw_count += 1
        print(f"  Gateways: {gw_count}")
    except Exception as e:
        print(f"  WARNING: Failed to fetch gateways: {e}", file=sys.stderr)

    # ── ARP table ────────────────────────────────────────────────
    try:
        arp_entries = pfsense.get_arp_table()
        if isinstance(arp_entries, dict):
            arp_entries = list(arp_entries.values())

        arp_count = 0
        for entry in arp_entries:
            if not isinstance(entry, dict):
                continue
            # /diagnostics/arp_table fields: ip_address, mac_address, interface, hostname
            ip = entry.get("ip_address", "")
            if ip and "/" not in ip:
                ip = f"{ip}/32"
            if ip:
                mac = entry.get("mac_address", "")
                iface = entry.get("interface", "")
                arp_host = entry.get("hostname", "")
                desc_parts = [f"ARP: {mac}" if mac else "ARP"]
                if iface:
                    desc_parts.append(f"on {iface}")
                if arp_host:
                    desc_parts.append(f"({arp_host})")
                ip_entity = IPAddress(
                    address=ip,
                    status="active",
                    description=" ".join(desc_parts),
                )
                entities.append(Entity(ip_address=ip_entity))
                arp_count += 1
        print(f"  ARP entries: {arp_count}")
    except Exception as e:
        print(f"  WARNING: Failed to fetch ARP table: {e}", file=sys.stderr)

    return entities


# ─── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync pfSense data to NetBox via Diode")
    parser.add_argument("--host", default=None, help="pfSense hostname/IP[:port] (env: PFSENSE_HOST)")
    parser.add_argument("--dry-run", action="store_true", help="Print entities without ingesting")
    args = parser.parse_args()

    # Resolve configuration — PFSENSE_HOST must be set via env or --host flag
    host = args.host or os.environ.get("PFSENSE_HOST")
    if not host:
        parser.error("pfSense host required: set PFSENSE_HOST env var or use --host")
    api_key = os.environ.get("PFSENSE_API_KEY") or read_secret("pfsense_api_key")
    verify_ssl = os.environ.get("PFSENSE_VERIFY_SSL", "false").lower() == "true"
    diode_target = os.environ.get("DIODE_TARGET", "grpc://localhost:8081/diode")

    if not api_key:
        print("ERROR: No pfSense API key found.", file=sys.stderr)
        print("  Set PFSENSE_API_KEY env var or create secrets/pfsense_api_key.txt", file=sys.stderr)
        sys.exit(1)

    # Set Diode credentials from secrets if not in environment
    if not os.environ.get("DIODE_CLIENT_ID"):
        client_id = read_secret("orb_agent_client_id")
        if client_id:
            os.environ["DIODE_CLIENT_ID"] = client_id
    if not os.environ.get("DIODE_CLIENT_SECRET"):
        client_secret = read_secret("orb_agent_client_secret")
        if client_secret:
            os.environ["DIODE_CLIENT_SECRET"] = client_secret

    validate_role(DEVICE_ROLE)

    print(f"==> pfSense Sync: {host} → {diode_target}", flush=True)

    # Query pfSense
    import requests as req_lib
    pfsense = PfSenseClient(host, api_key, verify_ssl)
    try:
        entities = build_entities(pfsense)
    except req_lib.exceptions.ConnectTimeout:
        print(f"ERROR: Connection to {host} timed out. Is pfSense reachable?", file=sys.stderr)
        sys.exit(1)
    except req_lib.exceptions.ConnectionError as e:
        print(f"ERROR: Cannot connect to {host}: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"==> Total entities: {len(entities)}")

    if args.dry_run:
        print("==> Dry run — skipping ingestion")
        for i, e in enumerate(entities):
            print(f"  [{i}] {e}")
        return

    # Ingest via Diode SDK
    from netboxlabs.diode.sdk import DiodeClient

    with DiodeClient(
        target=diode_target,
        app_name="pfsense-sync",
        app_version="1.0.0",
    ) as client:
        response = client.ingest(entities=entities)
        if response.errors:
            print(f"WARNING: Ingestion returned {len(response.errors)} error(s):", file=sys.stderr)
            for err in response.errors:
                print(f"  {err}", file=sys.stderr)
        else:
            print(f"==> Successfully ingested {len(entities)} entities")


if __name__ == "__main__":
    main()
