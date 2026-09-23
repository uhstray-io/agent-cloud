"""The NetBox preflight must refuse a changed local image or data volume."""

import runpy
from pathlib import Path

import pytest


def test_runtime_preflight_refuses_image_and_volume_drift():
    script = Path(__file__).parents[1] / "services/netbox/deployment/runtime-state.py"
    namespace = runpy.run_path(str(script), run_name="test_runtime_state")
    inspect_state = namespace["inspect_state"]
    services = namespace["SERVICES"]
    mounts = {
        "postgres": [("netbox-postgres", "/var/lib/postgresql")],
        "redis": [("netbox-redis-data", "/data")],
        "redis-cache": [("netbox-redis-cache-data", "/data")],
        "netbox": [("netbox-media-files", "/media")],
    }
    config = {
        "name": "netbox",
        "services": {
            service: {
                "image": f"example/{service}:stable",
                "restart": "always",
                "environment": {"DATABASE_PASSWORD": "must-not-appear"},
                "volumes": [
                    {"type": "volume", "source": source, "target": target}
                    for source, target in mounts[service]
                ],
            }
            for service in services
        },
        "volumes": {source: {"name": f"netbox_{source}"} for pairs in mounts.values() for source, _ in pairs},
    }
    containers = {
        service: {
            "Id": f"container-{service}",
            "Config": {"Image": f"example/{service}:stable", "Env": ["PASSWORD=must-not-appear"]},
            "Image": f"sha256:{service}",
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "HostConfig": {"RestartPolicy": {"Name": "always"}},
            "Mounts": [
                {"Type": "volume", "Name": f"netbox_{source}", "Destination": target}
                for source, target in mounts[service]
            ],
        }
        for service in services
    }
    local_ids = {service: f"sha256:{service}" for service in services}
    commands = []

    def fake_read_json(*argv):
        commands.append(argv)
        if "compose" in argv:
            return config
        if argv[:2] == ("docker", "image"):
            return [{"Id": local_ids[argv[-1].split("/")[-1].split(":")[0]]}]
        return [containers[argv[-1].removeprefix("netbox-").removesuffix("-1")]]

    inspect_state.__globals__["read_json"] = fake_read_json
    report = inspect_state(require_ready=True)
    assert set(report) == set(services)
    assert "must-not-appear" not in str(report)
    assert all("up" not in argv and "pull" not in argv for argv in commands)

    local_ids["postgres"] = "sha256:changed"
    with pytest.raises(ValueError, match="postgres: local Compose image differs"):
        inspect_state()

    local_ids["postgres"] = "sha256:postgres"
    containers["postgres"]["Mounts"][0]["Name"] = "replacement-volume"
    with pytest.raises(ValueError, match="postgres: named volumes differ"):
        inspect_state()
