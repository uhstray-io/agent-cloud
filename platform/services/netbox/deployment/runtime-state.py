#!/usr/bin/env python3
"""Report safe NetBox Compose state and refuse image or volume drift."""

import json
import subprocess
import sys

SERVICES = ("postgres", "redis", "redis-cache", "netbox")
COMPOSE = ("docker", "compose", "--project-name", "netbox", "-f", "docker-compose.yml")


def read_json(*argv):
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"inspection failed: {' '.join(argv[:3])}")
    return json.loads(result.stdout)


def inspect_state(require_ready=False):
    # Compose config and container inspect include credentials. Keep both in memory;
    # return only image, named-volume, policy, and health metadata to Semaphore.
    config = read_json(*COMPOSE, "config", "--format", "json")
    if config.get("name") != "netbox":
        raise ValueError("Compose project must be netbox")

    report = {}
    for service in SERVICES:
        declared = config["services"][service]
        if declared.get("restart") != "always":
            raise ValueError(f"{service}: Compose restart policy is not always")
        image_ref = declared["image"]
        container = read_json("docker", "inspect", f"netbox-{service}-1")[0]
        local_image = read_json("docker", "image", "inspect", image_ref)[0]
        if container["Config"]["Image"] != image_ref or container["Image"] != local_image["Id"]:
            raise ValueError(f"{service}: local Compose image differs from existing container")

        expected_volumes = sorted(
            (
                config["volumes"][mount["source"]].get("name", f"netbox_{mount['source']}")
                if mount["source"] in config.get("volumes", {})
                else mount["source"],
                mount["target"],
            )
            for mount in declared.get("volumes", [])
            if mount["type"] == "volume"
        )
        actual_volumes = sorted(
            (mount["Name"], mount["Destination"])
            for mount in container["Mounts"]
            if mount["Type"] == "volume"
        )
        if actual_volumes != expected_volumes:
            raise ValueError(f"{service}: named volumes differ from Compose")

        state = container["State"]
        policy = container["HostConfig"]["RestartPolicy"]["Name"]
        health = state.get("Health", {}).get("Status", "none")
        if require_ready and (state["Status"] != "running" or policy != "always" or health not in ("healthy", "none")):
            raise ValueError(f"{service}: expected running, healthy container with restart policy always")
        report[service] = {
            "container_id": container["Id"],
            "state": state["Status"],
            "health": health,
            "restart_policy": policy,
            "image_ref": image_ref,
            "image_id": container["Image"],
            "volumes": actual_volumes,
        }
    return report


if __name__ == "__main__":
    try:
        print(json.dumps(inspect_state(require_ready="--require-ready" in sys.argv), sort_keys=True))
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"NetBox runtime preflight refused: {exc}", file=sys.stderr)
        sys.exit(1)
