#!/usr/bin/env python3
"""Report safe NetBox Compose state and refuse image or volume drift."""

import json
import subprocess
import sys
from pathlib import Path

SERVICES = ("postgres", "redis", "redis-cache", "netbox")
COMPOSE = ("docker", "compose", "--project-name", "netbox", "-f", "docker-compose.yml")


def read_json(*argv):
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"inspection failed: {' '.join(argv[:3])}")
    return json.loads(result.stdout)


def environment_map(values):
    pairs = [entry.split("=", 1) for entry in values]
    if any(len(pair) != 2 for pair in pairs) or len({pair[0] for pair in pairs}) != len(pairs):
        raise ValueError("container environment is ambiguous")
    return dict(pairs)


def bind_mounts(mounts, declared=False):
    result = []
    for mount in mounts:
        if mount["type" if declared else "Type"] != "bind":
            continue
        source = Path(mount["source" if declared else "Source"]).resolve(strict=True)
        if not (source.is_file() or source.is_dir()):
            raise ValueError("bind source is neither a file nor a directory")
        result.append((
            str(source),
            mount["target" if declared else "Destination"],
            not mount.get("read_only", False) if declared else mount["RW"],
            "directory" if source.is_dir() else "file",
        ))
    return sorted(result)


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

        try:
            expected_binds = bind_mounts(declared.get("volumes", []), declared=True)
            actual_binds = bind_mounts(container["Mounts"])
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"{service}: a declared or existing bind source is unavailable") from exc
        if actual_binds != expected_binds:
            raise ValueError(f"{service}: bind mounts differ from Compose")

        expected_env = {
            **environment_map(local_image["Config"].get("Env") or []),
            **declared.get("environment", {}),
        }
        actual_env = environment_map(container["Config"].get("Env") or [])
        if actual_env != expected_env:
            raise ValueError(f"{service}: container environment differs from Compose")

        state = container["State"]
        policy = container["HostConfig"]["RestartPolicy"]["Name"]
        if policy not in ("no", "always"):
            raise ValueError(f"{service}: unexpected restart policy")
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
