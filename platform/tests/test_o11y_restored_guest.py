"""Failure-focused fixtures for rootless restored telemetry data proof."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "playbooks" / "files"))
import inspect_o11y_restored_guest as guest  # noqa: E402


def fixture_inspection(monkeypatch, tmp_path):
    uid = os.getuid()
    user = "svc_o11y"
    root = tmp_path / "service"
    root.mkdir()
    monkeypatch.setattr(guest.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=uid, pw_dir=str(root)))
    mounts = {}
    for service, (volume, _container, _destination, _port) in guest.VOLUMES.items():
        path = root / volume
        path.mkdir()
        mounts[service] = str(path)
    grafana_db = Path(mounts["grafana"]) / "grafana.db"
    with sqlite3.connect(grafana_db) as db:
        db.execute("CREATE TABLE migration_log (id INTEGER)")
        db.execute("INSERT INTO migration_log VALUES (1)")
        db.execute("CREATE TABLE dashboard (id INTEGER)")
        db.execute("INSERT INTO dashboard VALUES (1)")

    def json_command(argv, _user):
        if argv[:3] == ["podman", "volume", "ls"]:
            return [
                {
                    "Name": f"o11y_{volume}",
                    "Labels": {"com.docker.compose.project": "o11y", "com.docker.compose.volume": volume},
                }
                for volume, _container, _destination, _port in guest.VOLUMES.values()
            ]
        if argv[:3] == ["podman", "volume", "inspect"]:
            physical_name = argv[3]
            service = next(name for name, item in guest.VOLUMES.items() if f"o11y_{item[0]}" == physical_name)
            volume = guest.VOLUMES[service][0]
            return [
                {
                    "Name": physical_name,
                    "Mountpoint": mounts[service],
                    "Labels": {"com.docker.compose.project": "o11y", "com.docker.compose.volume": volume},
                }
            ]
        if argv[:2] == ["podman", "inspect"]:
            container = argv[2]
            service = next(name for name, item in guest.VOLUMES.items() if item[1] == container)
            _volume, _container, destination, _port = guest.VOLUMES[service]
            network = "o11y_o11y"
            return [
                {
                    "Name": container,
                    "State": {"Running": True},
                    "Mounts": [{"Destination": destination, "Source": mounts[service], "RW": True}],
                    "NetworkSettings": {"Networks": {network: {"IPAddress": f"10.88.1.{len(mounts)}"}}},
                }
            ]
        if argv[:3] == ["podman", "network", "inspect"]:
            name = argv[3]
            return [
                {"name": name, "labels": {"com.docker.compose.project": "o11y", "com.docker.compose.network": "o11y"}}
            ]
        raise AssertionError(argv)

    monkeypatch.setattr(guest, "_json_command", json_command)

    def digest(name):
        return hashlib.sha256(name.encode()).hexdigest()

    monkeypatch.setattr(guest, "_probe_count", lambda name, _address, _epoch: (4, digest(name)))
    expectations = {name: {"expectation": "present", "evidence_sha256": digest(name)} for name in guest.BACKENDS}
    payload = {
        "schema_version": 1,
        "service_user": user,
        "artifact_creation_time_epoch": 1_800_000_000,
        "backend_expectations": expectations,
    }
    return payload, mounts


def test_all_five_rootless_volumes_and_historical_backends_are_verified(monkeypatch, tmp_path):
    payload, _mounts = fixture_inspection(monkeypatch, tmp_path)
    result = guest.inspect(payload)
    assert result["status"] == "verified"
    assert result["volume_count"] == 5
    assert len(result["volume_bindings_sha256"]) == 64
    assert len(result["container_bindings_sha256"]) == 64
    assert result["backend_count"] == 4
    assert result["grafana_integrity"] == "ok"
    assert all(item["historical_records"] == 4 for item in result["backends"].values())


def test_quiet_backend_requires_explicit_historical_expectation(monkeypatch, tmp_path):
    payload, _mounts = fixture_inspection(monkeypatch, tmp_path)
    payload["backend_expectations"]["pyroscope"]["expectation"] = "quiet"
    monkeypatch.setattr(
        guest,
        "_probe_count",
        lambda name, _address, _epoch: (0 if name == "pyroscope" else 4, hashlib.sha256(name.encode()).hexdigest()),
    )
    assert guest.inspect(payload)["backends"]["pyroscope"]["historical_records"] == 0
    payload["backend_expectations"]["pyroscope"]["evidence_sha256"] = None
    with pytest.raises(guest.Refusal, match="historical_expectation_invalid"):
        guest.inspect(payload)


def test_live_sqlite_wal_without_existing_shared_memory_fails_without_creating_it(monkeypatch, tmp_path):
    payload, mounts = fixture_inspection(monkeypatch, tmp_path)
    wal = Path(mounts["grafana"]) / "grafana.db-wal"
    shm = Path(mounts["grafana"]) / "grafana.db-shm"
    wal.write_bytes(b"bounded fixture")
    with pytest.raises(guest.Refusal, match="grafana_integrity_invalid"):
        guest.inspect(payload)
    assert not shm.exists()


def test_rootless_podman_command_uses_only_the_owned_user_runtime(monkeypatch):
    captured = {}
    monkeypatch.setattr(guest.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=321, pw_dir="/home/<user>"))
    monkeypatch.setattr(guest.os, "lstat", lambda _path: SimpleNamespace(st_mode=0o40700, st_uid=321))

    def run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"ok")

    monkeypatch.setattr(guest.subprocess, "run", run)
    assert guest._run(["podman", "info"], user="svc") == b"ok"
    assert captured["command"] == ["runuser", "-u", "svc", "--", "podman", "info"]
    assert captured["env"] == {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/home/<user>",
        "LANG": "C",
        "LC_ALL": "C",
        "XDG_RUNTIME_DIR": "/run/user/321",
    }
    assert captured["stderr"] == guest.subprocess.DEVNULL


@pytest.mark.parametrize("value", ["1e999", "NaN", "-1", "9999999999999999999"])
def test_historical_counter_rejects_invalid_or_unbounded_numeric_results(value):
    with pytest.raises(guest.Refusal, match="backend_response_invalid"):
        guest._instant_result_count([{"value": [1, value]}])


def test_historical_counter_does_not_treat_malformed_rows_as_quiet():
    assert guest._instant_result_count([]) == 0
    malformed_values = (None, [{}], [{"value": "bad"}], [{"value": [1]}], [{"value": [1, "0"]}, {"value": [2, "0"]}])
    for malformed in malformed_values:
        with pytest.raises(guest.Refusal, match="backend_response_invalid"):
            guest._instant_result_count(malformed)


def test_backend_json_refuses_duplicate_keys_and_nonstandard_numbers():
    for body in (b'{"status":"success","status":"error"}', b'{"value":NaN}'):
        with pytest.raises(guest.Refusal, match="backend_response_invalid"):
            guest._json_bytes(body)


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("missing_volume", "volume_state_invalid"),
        ("wrong_owner", "volume_owner_invalid"),
        ("symlink", "volume_state_invalid"),
        ("mount_mismatch", "volume_mount_mismatch"),
        ("corrupt_db", "grafana_integrity_invalid"),
    ],
)
def test_missing_or_untrusted_filesystem_evidence_fails_closed(monkeypatch, tmp_path, fault, reason):
    payload, mounts = fixture_inspection(monkeypatch, tmp_path)
    original = guest._json_command

    if fault == "missing_volume":
        monkeypatch.setattr(
            guest,
            "_json_command",
            lambda argv, user: (
                [
                    item
                    for item in original(argv, user)
                    if item["Labels"].get("com.docker.compose.volume") != "tempo-data"
                ]
                if argv[:3] == ["podman", "volume", "ls"]
                else original(argv, user)
            ),
        )
    elif fault == "wrong_owner":
        original_safe = guest._safe_path
        monkeypatch.setattr(
            guest,
            "_safe_path",
            lambda path, **kwargs: (
                (_ for _ in ()).throw(guest.Refusal("volume_owner_invalid"))
                if path == mounts["loki"]
                else original_safe(path, **kwargs)
            ),
        )
    elif fault == "symlink":
        path = Path(mounts["loki"])
        child = path.parent / "link"
        child.symlink_to(path, target_is_directory=True)
        monkeypatch.setattr(
            guest,
            "_json_command",
            lambda argv, user: (
                [
                    {
                        "Name": "o11y_loki-data",
                        "Mountpoint": str(child),
                        "Labels": {"com.docker.compose.project": "o11y", "com.docker.compose.volume": "loki-data"},
                    }
                ]
                if argv[:3] == ["podman", "volume", "inspect"] and argv[3] == "o11y_loki-data"
                else original(argv, user)
            ),
        )
    elif fault == "mount_mismatch":

        def mismatched(argv, user):
            result = original(argv, user)
            if argv[:2] == ["podman", "inspect"] and argv[2] == "o11y-grafana":
                result[0]["Mounts"][0]["Source"] = "/different"
            return result

        monkeypatch.setattr(guest, "_json_command", mismatched)
    else:
        Path(mounts["grafana"], "grafana.db").write_bytes(b"not sqlite")

    with pytest.raises(guest.Refusal, match=reason):
        guest.inspect(payload)
