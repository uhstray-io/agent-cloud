#!/usr/bin/env python3
"""Refuse discovery targets outside the networks local-dev is allowed to scan.

Local NetBox discovery may only scan local-dev (change service-deployment-workflow,
platform/netbox-local, "Discovery is confined to local-dev targets"). The allowed networks
are read from the live container engine by the caller; this checks containment with the
standard library, because the orchestrator does not install ansible.utils (ipaddr).

usage: discovery_scope.py ALLOWED_JSON TARGETS_JSON
  ALLOWED_JSON  JSON list of CIDRs the engine reports (e.g. podman network subnets)
  TARGETS_JSON  JSON list of declared targets (CIDRs or single addresses)
Exit 0 and print {"allowed": [...], "targets": [...]} when every target is inside some
allowed network; exit 1 naming each target outside them; exit 2 on bad input.
"""

import ipaddress
import json
import sys


def outside(allowed: list[str], targets: list[str]) -> list[str]:
    nets = [ipaddress.ip_network(a, strict=False) for a in allowed]
    bad = []
    for target in targets:
        net = ipaddress.ip_network(target, strict=False)
        if not any(net.version == a.version and net.subnet_of(a) for a in nets):
            bad.append(target)
    return bad


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        allowed, targets = json.loads(argv[1]), json.loads(argv[2])
        bad = outside(allowed, targets)
    except (ValueError, TypeError) as exc:
        print(f"discovery_scope: bad input: {exc}", file=sys.stderr)
        return 2
    if bad:
        print(f"discovery targets outside every local-dev network {allowed}: {bad}", file=sys.stderr)
        return 1
    print(json.dumps({"allowed": allowed, "targets": targets}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
