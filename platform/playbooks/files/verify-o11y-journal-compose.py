#!/usr/bin/env python3
"""Verify the provider-resolved journal volume declaration without exposing config."""

import json
import subprocess
import sys

import yaml

VOLUME_KEY = "journal-collector-state-v3"
PROJECT = "o11y-journal"
VOLUME_NAME = "o11y_journal-collector-state-v3"
NETWORK_KEY = "o11y"
NETWORK_NAME = "o11y_o11y"
DATA_PATH = "/alloy-state"
MAX_CONFIG_BYTES = 1_048_576


def verify_config(config):
    if not isinstance(config, dict):
        return False
    services = config.get("services")
    service = services.get("journal-collector") if isinstance(services, dict) else None
    volumes = service.get("volumes") if isinstance(service, dict) else None
    definitions = config.get("volumes")
    networks = config.get("networks")
    network = networks.get(NETWORK_KEY) if isinstance(networks, dict) else None
    network_refs = service.get("networks") if isinstance(service, dict) else None
    volume_definition = definitions.get(VOLUME_KEY) if isinstance(definitions, dict) else None
    matches = [
        mount for mount in volumes if isinstance(mount, dict) and mount.get("target") == DATA_PATH
    ] if isinstance(volumes, list) else []
    return (
        config.get("name") == PROJECT
        and len(matches) == 1
        and matches[0].get("type") == "volume"
        and matches[0].get("source") == VOLUME_KEY
        and matches[0].get("volume") == {"nocopy": True}
        and isinstance(definitions, dict)
        and isinstance(volume_definition, dict)
        and volume_definition.get("name") == VOLUME_NAME
        and volume_definition.get("external") is True
        and isinstance(network, dict)
        and network.get("name") == NETWORK_NAME
        and network.get("external") is True
        and (
            isinstance(network_refs, list) and NETWORK_KEY in network_refs
            or isinstance(network_refs, dict) and NETWORK_KEY in network_refs
        )
    )


def main():
    result = {"status": "unavailable", "reason": "provider_config_unavailable"}
    try:
        completed = subprocess.run(
            ["podman-compose", "-p", PROJECT, "-f", "compose.journal.yml", "config"],
            cwd=sys.argv[1],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if completed.returncode == 0 and len(completed.stdout) <= MAX_CONFIG_BYTES:
            parsed = yaml.safe_load(completed.stdout)
            if verify_config(parsed):
                result = {"status": "verified", "reason": "provider_config_exact"}
            else:
                result = {"status": "refused", "reason": "provider_config_mismatch"}
    except (OSError, UnicodeError, subprocess.TimeoutExpired, yaml.YAMLError, IndexError):
        pass
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
