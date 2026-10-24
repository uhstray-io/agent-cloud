"""Failure-focused fixtures for rootless restored telemetry data proof."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "playbooks" / "files"))
import inspect_o11y_restored_guest as guest  # noqa: E402


def fixture_inspection(monkeypatch, tmp_path):
    uid = os.getuid()
    user = "svc_o11y"
    root = tmp_path / "service"
    root.mkdir()
    monkeypatch.setattr(guest.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=uid, pw_dir=str(root)))
    monkeypatch.setattr(guest, "_service_owner_policy", lambda _name, _account: ((), "a" * 64))
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
                    "State": {"Running": True, "Pid": 1000 + list(guest.VOLUMES).index(service)},
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


def test_subuid_parser_binds_named_and_unambiguous_numeric_account_entries():
    passwd_entries = [("root", 0), ("svc_o11y", 1000), ("alice", 1001)]
    named_ranges, named_digest = guest.parse_subuid(
        "svc_o11y:200000:65536\nalice:300000:65536\n",
        service_user="svc_o11y",
        service_uid=1000,
        passwd_entries=passwd_entries,
    )
    numeric_ranges, numeric_digest = guest.parse_subuid(
        "1000:200000:65536\nalice:300000:65536\n",
        service_user="svc_o11y",
        service_uid=1000,
        passwd_entries=passwd_entries,
    )
    assert named_ranges == numeric_ranges == ((200000, 265536),)
    assert named_digest == numeric_digest
    assert guest._owner_allowed(200001, 1000, named_ranges)
    assert not guest._owner_allowed(999999999, 1000, named_ranges)
    assert not guest._owner_allowed(300001, 1000, named_ranges)


@pytest.mark.parametrize(
    "content,passwd_entries",
    [
        ("svc_o11y:0:1\n", [("root", 0), ("svc_o11y", 1000), ("alice", 1001)]),
        ("svc_o11y:1000:1\n", [("root", 0), ("svc_o11y", 1000), ("alice", 1001)]),
        ("alice:1001:1\n", [("root", 0), ("svc_o11y", 1000), ("alice", 1001)]),
        ("svc_o11y:300000:10\nalice:300005:10\n", [("root", 0), ("svc_o11y", 1000), ("alice", 1001)]),
        ("svc_o11y:4294967295:2\n", [("root", 0), ("svc_o11y", 1000), ("alice", 1001)]),
        ("9999:300000:10\n", [("root", 0), ("svc_o11y", 1000), ("alice", 1001)]),
        ("1000:300000:10\n", [("root", 0), ("svc_o11y", 1000), ("alias", 1000)]),
    ],
)
def test_subuid_parser_rejects_uid_collisions_foreign_aliases_and_overlaps(content, passwd_entries):
    with pytest.raises(guest.Refusal, match="volume_owner_invalid"):
        guest.parse_subuid(
            content,
            service_user="svc_o11y",
            service_uid=1000,
            passwd_entries=passwd_entries,
        )


def test_subuid_parser_rejects_malformed_unbounded_or_duplicate_mapping_inputs():
    rows = [("root", 0), ("svc_o11y", 1000), ("alice", 1001)]
    for content in (
        "svc_o11y:200000:65536 trailing\n",
        "svc_o11y:200000:+65536\n",
        "svc_o11y:200000:65536\nalice:265535:1\n",
        "svc_o11y:200000:65536\nsvc_o11y:200000:65536\n",
    ):
        with pytest.raises(guest.Refusal, match="volume_owner_invalid"):
            guest.parse_subuid(content, service_user="svc_o11y", service_uid=1000, passwd_entries=rows)
    with pytest.raises(guest.Refusal, match="volume_owner_invalid"):
        guest.parse_subuid("svc_o11y:200000:1\n", service_user="svc_o11y", service_uid=1000, passwd_entries=rows * 1000)


@pytest.mark.parametrize(
    "metadata",
    [
        SimpleNamespace(st_mode=stat.S_IFREG | 0o666, st_uid=0, st_size=1),
        SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=123, st_size=1),
        SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_size=1),
        SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_size=guest.MAX_SUBUID_BYTES + 1),
    ],
)
def test_subuid_file_reader_rejects_unsafe_file_metadata(monkeypatch, tmp_path, metadata):
    monkeypatch.setattr(guest.os, "lstat", lambda _path: metadata)
    with pytest.raises(guest.Refusal, match="volume_owner_invalid"):
        guest._read_subuid(tmp_path / "subuid")


def test_rootless_subuid_owned_volume_and_database_are_verified_and_fingerprinted(monkeypatch, tmp_path):
    payload, mounts = fixture_inspection(monkeypatch, tmp_path)
    baseline = guest.inspect(payload)
    mapped_uid = 200123
    mapping_digest = hashlib.sha256(b"svc_o11y:200000:65536").hexdigest()
    monkeypatch.setattr(guest, "_service_owner_policy", lambda _name, _account: (((200000, 265536),), mapping_digest))
    original_lstat = guest.os.lstat

    def mapped_lstat(path):
        actual = original_lstat(path)
        if str(path) in set(mounts.values()) or str(path) == str(Path(mounts["grafana"]) / "grafana.db"):
            return SimpleNamespace(
                st_mode=actual.st_mode,
                st_uid=mapped_uid,
                st_size=actual.st_size,
                st_nlink=actual.st_nlink,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino,
            )
        return actual

    monkeypatch.setattr(guest.os, "lstat", mapped_lstat)
    result = guest.inspect(payload)
    assert result["status"] == "verified"
    assert set(result) == {
        "schema_version",
        "status",
        "reason",
        "volume_count",
        "volume_bindings_sha256",
        "container_bindings_sha256",
        "backend_count",
        "grafana_integrity",
        "grafana_migrations",
        "grafana_database_sha256",
        "backends",
        "receipt_fingerprint",
    }
    assert "owner_uid" not in result and "owner_mapping_sha256" not in result
    assert result["volume_bindings_sha256"] != baseline["volume_bindings_sha256"]
    assert result["grafana_database_sha256"] != baseline["grafana_database_sha256"]
    monkeypatch.setattr(
        guest,
        "_service_owner_policy",
        lambda _name, _account: (((200000, 265536),), hashlib.sha256(b"changed account mapping").hexdigest()),
    )
    changed_mapping = guest.inspect(payload)
    assert changed_mapping["volume_bindings_sha256"] != result["volume_bindings_sha256"]
    assert changed_mapping["grafana_database_sha256"] != result["grafana_database_sha256"]


def test_rootless_mapped_uid_is_rejected_outside_declared_subuid_range(monkeypatch, tmp_path):
    payload, mounts = fixture_inspection(monkeypatch, tmp_path)
    original_lstat = guest.os.lstat

    def foreign_lstat(path):
        actual = original_lstat(path)
        if str(path) == mounts["loki"]:
            return SimpleNamespace(
                st_mode=actual.st_mode,
                st_uid=300000,
                st_size=actual.st_size,
                st_nlink=actual.st_nlink,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino,
            )
        return actual

    monkeypatch.setattr(guest.os, "lstat", foreign_lstat)
    with pytest.raises(guest.Refusal, match="volume_owner_invalid"):
        guest.inspect(payload)


def test_subuid_owned_leaf_does_not_relax_parent_path_ownership(monkeypatch, tmp_path):
    root = tmp_path / "service"
    parent = root / "foreign-parent"
    leaf = parent / "volume"
    leaf.mkdir(parents=True)
    original_lstat = guest.os.lstat

    def foreign_parent_lstat(path):
        actual = original_lstat(path)
        if str(path) == str(parent):
            return SimpleNamespace(st_mode=actual.st_mode, st_uid=300000)
        return actual

    monkeypatch.setattr(guest.os, "lstat", foreign_parent_lstat)
    with pytest.raises(guest.Refusal, match="volume_state_invalid"):
        guest._safe_path(str(leaf), root=root, owner_uid=1000, subuid_ranges=((200000, 265536),))


def test_subuid_owned_leaf_cannot_escape_account_root(monkeypatch, tmp_path):
    root = tmp_path / "service"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(guest.Refusal, match="volume_state_invalid"):
        guest._safe_path(
            str(root / ".." / "outside"),
            root=root,
            owner_uid=os.getuid(),
            subuid_ranges=((200000, 265536),),
        )


def test_subuid_mapping_does_not_allow_a_symlinked_grafana_database(monkeypatch, tmp_path):
    _payload, mounts = fixture_inspection(monkeypatch, tmp_path)
    grafana = Path(mounts["grafana"])
    database = grafana / "grafana.db"
    target = grafana / "grafana.real"
    database.rename(target)
    database.symlink_to(target.name)
    with pytest.raises(guest.Refusal, match="grafana_integrity_invalid"):
        guest._sqlite_integrity(
            grafana,
            owner_uid=os.getuid(),
            subuid_ranges=((200000, 265536),),
            owner_mapping_sha256="b" * 64,
        )


def test_sqlite_database_hardlink_is_still_rejected(monkeypatch, tmp_path):
    _payload, mounts = fixture_inspection(monkeypatch, tmp_path)
    grafana = Path(mounts["grafana"])
    (grafana / "grafana.db-copy").hardlink_to(grafana / "grafana.db")
    with pytest.raises(guest.Refusal, match="grafana_integrity_invalid"):
        guest._sqlite_integrity(
            grafana,
            owner_uid=os.getuid(),
            subuid_ranges=((200000, 265536),),
            owner_mapping_sha256="b" * 64,
        )


def test_sqlite_accepts_mapped_owner_on_live_wal_and_shared_memory(monkeypatch, tmp_path):
    _payload, mounts = fixture_inspection(monkeypatch, tmp_path)
    grafana = Path(mounts["grafana"])
    database = grafana / "grafana.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("INSERT INTO migration_log VALUES (2)")
    connection.commit()
    wal = grafana / "grafana.db-wal"
    shm = grafana / "grafana.db-shm"
    assert wal.exists() and shm.exists()

    original_lstat = guest.os.lstat
    mapped_uid = 200123

    def mapped_lstat(path):
        actual = original_lstat(path)
        if str(path) in {str(database), str(wal), str(shm)}:
            return SimpleNamespace(
                st_mode=actual.st_mode,
                st_uid=mapped_uid,
                st_size=actual.st_size,
                st_nlink=actual.st_nlink,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino,
            )
        return actual

    monkeypatch.setattr(guest.os, "lstat", mapped_lstat)
    count, digest = guest._sqlite_integrity(
        grafana,
        owner_uid=os.getuid(),
        subuid_ranges=((200000, 265536),),
        owner_mapping_sha256="c" * 64,
    )
    connection.close()
    assert count > 0
    assert len(digest) == 64


@pytest.mark.parametrize("foreign_file", ["grafana.db", "grafana.db-wal", "grafana.db-shm"])
def test_sqlite_files_reject_unmapped_uid_owners(monkeypatch, tmp_path, foreign_file):
    payload, mounts = fixture_inspection(monkeypatch, tmp_path)
    del payload
    grafana = Path(mounts["grafana"])
    if foreign_file != "grafana.db":
        (grafana / "grafana.db-wal").write_bytes(b"bounded wal fixture")
        (grafana / "grafana.db-shm").write_bytes(b"bounded shm fixture")
    foreign_path = grafana / foreign_file
    original_lstat = guest.os.lstat

    def foreign_file_lstat(path):
        actual = original_lstat(path)
        if str(path) == str(foreign_path):
            return SimpleNamespace(
                st_mode=actual.st_mode,
                st_uid=300000,
                st_size=actual.st_size,
                st_nlink=actual.st_nlink,
                st_dev=actual.st_dev,
                st_ino=actual.st_ino,
            )
        return actual

    monkeypatch.setattr(guest.os, "lstat", foreign_file_lstat)
    with pytest.raises(guest.Refusal, match="grafana_integrity_invalid"):
        guest._sqlite_integrity(
            grafana,
            owner_uid=os.getuid(),
            subuid_ranges=((200000, 265536),),
            owner_mapping_sha256="b" * 64,
        )


def test_historical_queries_use_only_loopback_inside_the_exact_container_namespace(monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=b'{"status":"success"}')

    monkeypatch.setattr(guest.subprocess, "run", run)
    assert guest._namespace_http(4321, "http://127.0.0.1:3200/api/search") == b'{"status":"success"}'
    assert captured["command"][0:2] == ["/usr/bin/nsenter", "--net=/proc/4321/ns/net"]
    assert captured["command"][-3:] == ["http://127.0.0.1:3200/api/search", "8", str(guest.MAX_HTTP_BYTES)]
    assert captured["env"] == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}


def test_loki_historical_counter_uses_api_evaluation_time_not_logql_at_modifier(monkeypatch):
    captured = {}

    def get_json(_pid, url):
        captured.update(url=url)
        return {"status": "success", "data": {"result": []}}

    monkeypatch.setattr(guest, "_get_json", get_json)
    count, _digest = guest._probe_count("loki", 4321, 1_700_000_000)

    query = parse_qs(urlsplit(captured["url"]).query)
    assert count == 0
    assert query["time"] == ["1700000000"]
    assert "[24h]" in query["query"][0]
    assert "@" not in query["query"][0]


def test_pyroscope_historical_read_uses_its_container_network_namespace_loopback(monkeypatch):
    captured = {}

    def read(pid, url, **kwargs):
        captured.update(pid=pid, url=url, kwargs=kwargs)
        return b"o11y.cpu 3\n"

    monkeypatch.setattr(guest, "_namespace_http", read)
    count, digest = guest._probe_count("pyroscope", 4321, 1_700_000_000)
    assert count == 3
    assert digest == hashlib.sha256(b"o11y.cpu 3\n").hexdigest()
    assert captured["pid"] == 4321
    assert captured["url"].startswith("http://127.0.0.1:4040/pyroscope/render?")


def test_container_inspection_requires_a_real_running_process_pid(monkeypatch, tmp_path):
    payload, _mounts = fixture_inspection(monkeypatch, tmp_path)
    original = guest._json_command

    def missing_pid(argv, user):
        result = original(argv, user)
        if argv[:2] == ["podman", "inspect"]:
            result[0]["State"].pop("Pid")
        return result

    monkeypatch.setattr(guest, "_json_command", missing_pid)
    with pytest.raises(guest.Refusal, match="container_state_invalid"):
        guest.inspect(payload)


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
