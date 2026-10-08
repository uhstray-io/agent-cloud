"""The NetBox preflight must refuse unintended runtime drift without leaking secrets."""

import runpy
from pathlib import Path

import pytest


def test_runtime_preflight_refuses_image_volume_bind_and_environment_drift(tmp_path):
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
            "Config": {
                "Image": f"example/{service}:stable",
                "Env": ["PATH=/usr/bin", "DATABASE_PASSWORD=must-not-appear"],
            },
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
    config_file = tmp_path / "plugins.py"
    config_file.write_text("# synthetic\n")
    config_dir = tmp_path / "configuration"
    config_dir.mkdir()
    for source, target in ((config_file, "/etc/netbox/plugins.py"), (config_dir, "/etc/netbox/config")):
        config["services"]["netbox"]["volumes"].append({
            "type": "bind", "source": str(source), "target": target, "read_only": True,
        })
        containers["netbox"]["Mounts"].append({
            "Type": "bind", "Source": str(source), "Destination": target, "RW": False,
        })
    commands = []

    def fake_read_json(*argv):
        commands.append(argv)
        if "compose" in argv:
            return config
        if argv[:2] == ("docker", "image"):
            return [{"Id": local_ids[argv[-1].split("/")[-1].split(":")[0]],
                     "Config": {"Env": ["PATH=/usr/bin"]}}]
        return [containers[argv[-1].removeprefix("netbox-").removesuffix("-1")]]

    inspect_state.__globals__["read_json"] = fake_read_json
    report = inspect_state(require_ready=True)
    assert set(report) == set(services)
    assert all(item["planned_action"] == "noop" for item in report.values())
    assert "must-not-appear" not in str(report)
    assert all("up" not in argv and "pull" not in argv for argv in commands)

    local_ids["postgres"] = "sha256:changed"
    with pytest.raises(ValueError, match="postgres: local Compose image differs"):
        inspect_state()

    local_ids["postgres"] = "sha256:postgres"
    containers["postgres"]["Mounts"][0]["Name"] = "replacement-volume"
    with pytest.raises(ValueError, match="postgres: named volumes differ"):
        inspect_state()
    containers["postgres"]["Mounts"][0]["Name"] = "netbox_netbox-postgres"

    config_file.unlink()
    with pytest.raises(ValueError, match="netbox: a declared or existing bind source is unavailable"):
        inspect_state()
    config_file.write_text("# synthetic\n")

    containers["netbox"]["Mounts"][-1]["Destination"] = "/wrong"
    with pytest.raises(ValueError, match="netbox: bind mounts differ"):
        inspect_state()
    containers["netbox"]["Mounts"][-1]["Destination"] = "/etc/netbox/config"

    containers["postgres"]["Config"]["Env"][1] = "DATABASE_PASSWORD=changed"
    with pytest.raises(ValueError, match="postgres: container environment differs") as error:
        inspect_state()
    assert "changed" not in str(error.value)
    containers["postgres"]["Config"]["Env"][1] = "DATABASE_PASSWORD=must-not-appear"

    containers["postgres"]["HostConfig"]["RestartPolicy"]["Name"] = "no"
    assert inspect_state()["postgres"]["planned_action"] == "recreate"
    with pytest.raises(ValueError, match="postgres: expected running, healthy container"):
        inspect_state(require_ready=True)
    containers["postgres"]["HostConfig"]["RestartPolicy"]["Name"] = "always"
    containers["postgres"]["State"]["Status"] = "exited"
    assert inspect_state()["postgres"]["planned_action"] == "start"
    containers["postgres"]["State"]["Status"] = "running"
    containers["postgres"]["State"]["Health"]["Status"] = "unhealthy"
    with pytest.raises(ValueError, match="postgres: unsupported runtime state"):
        inspect_state()
    containers["postgres"]["State"]["Health"]["Status"] = "healthy"
    containers["postgres"]["HostConfig"]["RestartPolicy"]["Name"] = "unless-stopped"
    with pytest.raises(ValueError, match="postgres: unexpected restart policy"):
        inspect_state()
