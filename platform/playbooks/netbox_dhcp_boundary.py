"""Refuse NetBox reservations that conflict with live pfSense DHCP configuration."""

import ipaddress
import json
import sys


def _range(start, end, prefix):
    first, last = ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)
    if first not in prefix or last not in prefix or first > last:
        raise ValueError("DHCP range is invalid for the requested prefix")
    return first, last


def check_boundary(request):
    prefix = ipaddress.IPv4Network(request["prefix"], strict=True)
    response = request["response"]
    if not isinstance(response, dict) or response.get("code") != 200:
        raise ValueError("pfSense did not return a successful DHCP response")
    server = response.get("data")
    if not isinstance(server, dict) or server.get("interface") != request["interface"]:
        raise ValueError("pfSense did not return the selected DHCP interface")
    if not isinstance(server.get("enable"), bool):
        raise ValueError("pfSense DHCP enabled state is unknown")
    if not isinstance(server.get("pool"), list) or not isinstance(server.get("staticmap"), list):
        raise ValueError("pfSense DHCP pools or static mappings are missing")

    # The primary range ties the selected router interface to this exact prefix.
    # An empty range leaves that relationship unproven, even when DHCP is disabled.
    ranges = [_range(server["range_from"], server["range_to"], prefix)]
    for pool in server["pool"]:
        if not isinstance(pool, dict):
            raise ValueError("pfSense returned an invalid additional DHCP pool")
        ranges.append(_range(pool["range_from"], pool["range_to"], prefix))

    static = set()
    for mapping in server["staticmap"]:
        if not isinstance(mapping, dict) or "ipaddr" not in mapping:
            raise ValueError("pfSense returned an invalid static mapping")
        if mapping["ipaddr"]:
            address = ipaddress.IPv4Address(mapping["ipaddr"])
            if address not in prefix:
                raise ValueError("pfSense static mapping is outside the requested prefix")
            static.add(address)

    assignments = request["assignments"]
    if not isinstance(assignments, list) or not assignments:
        raise ValueError("reservation requires named addresses")
    for number, item in enumerate(assignments, 1):
        if not isinstance(item, dict) or "address" not in item:
            raise ValueError(f"assignment {number} has no address")
        candidate = ipaddress.IPv4Interface(item["address"])
        if candidate.network != prefix or candidate.ip in (
            prefix.network_address,
            prefix.broadcast_address,
        ):
            raise ValueError(f"assignment {number} is outside the requested prefix")
        if any(first <= candidate.ip <= last for first, last in ranges):
            raise ValueError(f"assignment {number} is inside a DHCP range")
        if candidate.ip in static:
            raise ValueError(f"assignment {number} is a DHCP static mapping")

    return f"Live pfSense DHCP boundary excludes {len(assignments)} requested address(es)"


if __name__ == "__main__":
    try:
        print(check_boundary(json.load(sys.stdin)))
    except (KeyError, TypeError, ValueError) as error:
        print(f"REFUSED: {error}")
        sys.exit(1)
