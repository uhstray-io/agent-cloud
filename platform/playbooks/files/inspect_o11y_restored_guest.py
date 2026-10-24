#!/usr/bin/env python3
"""Read-only, target-local verification of restored rootless o11y storage.

Called only through QGA with fixed argv and bounded JSON expectations. It emits
fixed categories, counts, and hashes; backend labels, samples, logs, traces,
profiles, file paths, and credentials never leave this process.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import pwd
import re
import sqlite3
import stat
import subprocess
import sys
import urllib.parse
import urllib.request
from bisect import bisect_left
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_INPUT_BYTES = 64 * 1024
MAX_COMMAND_BYTES = 4 * 1024 * 1024
MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_SUBUID_BYTES = 256 * 1024
MAX_SUBUID_ROWS = 4096
MAX_UID = 2**32 - 1
WINDOW_SECONDS = 86_400
VOLUMES = {
    "prometheus": ("prometheus-data", "o11y-prometheus", "/prometheus", 9090),
    "loki": ("loki-data", "o11y-loki", "/loki", 3100),
    "tempo": ("tempo-data", "o11y-tempo", "/var/tempo", 3200),
    "pyroscope": ("pyroscope-data", "o11y-pyroscope", "/data", 4040),
    "grafana": ("grafana-data", "o11y-grafana", "/var/lib/grafana", 3000),
}
BACKENDS = {"prometheus", "loki", "tempo", "pyroscope"}
USERNAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\$?\Z")
SAFE_REASONS = {
    "invalid_input",
    "podman_unavailable",
    "volume_state_invalid",
    "container_state_invalid",
    "volume_mount_mismatch",
    "volume_owner_invalid",
    "backend_unavailable",
    "backend_response_invalid",
    "historical_data_missing",
    "historical_expectation_invalid",
    "grafana_integrity_invalid",
}


class Refusal(ValueError):
    def __init__(self, reason: str):
        self.reason = reason if reason in SAFE_REASONS else "invalid_input"
        super().__init__(self.reason)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refusal(reason)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_subuid(path: Path = Path("/etc/subuid")) -> bytes:
    """Read a bounded, root-owned subuid file without following its final symlink."""
    try:
        before = os.lstat(path)
        _require(
            stat.S_ISREG(before.st_mode)
            and not stat.S_ISLNK(before.st_mode)
            and before.st_uid == 0
            and before.st_mode & 0o022 == 0
            and before.st_size <= MAX_SUBUID_BYTES,
            "volume_owner_invalid",
        )
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            _require(
                stat.S_ISREG(opened.st_mode)
                and opened.st_uid == 0
                and opened.st_mode & 0o022 == 0
                and opened.st_size <= MAX_SUBUID_BYTES
                and (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino),
                "volume_owner_invalid",
            )
            content = stream.read(MAX_SUBUID_BYTES + 1)
        _require(len(content) <= MAX_SUBUID_BYTES, "volume_owner_invalid")
        return content
    except (OSError, TypeError, ValueError):
        raise Refusal("volume_owner_invalid") from None


def parse_subuid(
    content: bytes | str,
    *,
    service_user: str,
    service_uid: int,
    passwd_entries: list[tuple[str, int]],
) -> tuple[tuple[tuple[int, int], ...], str]:
    """Resolve only this account's validated subordinate ranges and fingerprint the map.

    Returned ranges are half-open. This pure parser takes NSS/passwd rows explicitly so
    account binding and hostile allocation cases can be tested without changing host IDs.
    """
    try:
        _require(isinstance(content, (bytes, str)), "volume_owner_invalid")
        raw = content if isinstance(content, bytes) else content.encode("ascii")
        _require(len(raw) <= MAX_SUBUID_BYTES, "volume_owner_invalid")
        text = raw.decode("ascii")
        _require("\x00" not in text, "volume_owner_invalid")
        _require(
            isinstance(service_user, str)
            and USERNAME.fullmatch(service_user)
            and type(service_uid) is int
            and 0 <= service_uid <= MAX_UID
            and isinstance(passwd_entries, list),
            "volume_owner_invalid",
        )
        rows = [(name, uid) for name, uid in passwd_entries]
        _require(
            len(rows) <= MAX_SUBUID_ROWS * 16
            and all(
                isinstance(name, str)
                and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,254}\$?", name))
                and type(uid) is int
                and 0 <= uid <= MAX_UID
                for name, uid in rows
            ),
            "volume_owner_invalid",
        )
        names: dict[str, list[int]] = {}
        uids: dict[int, list[str]] = {}
        for name, uid in rows:
            names.setdefault(name, []).append(uid)
            uids.setdefault(uid, []).append(name)
        passwd_uids = sorted(uids)
        _require(names.get(service_user) == [service_uid], "volume_owner_invalid")

        allocations: list[tuple[int, int, str, str]] = []
        own_ranges: list[tuple[int, int]] = []
        raw_lines = text.splitlines()
        _require(len(raw_lines) <= MAX_SUBUID_ROWS, "volume_owner_invalid")
        for line in raw_lines:
            if not line or line.startswith("#"):
                continue
            _require(line == line.strip() and line.count(":") == 2, "volume_owner_invalid")
            selector, start_text, count_text = line.split(":")
            _require(
                bool(selector)
                and bool(re.fullmatch(r"[0-9]+", start_text))
                and bool(re.fullmatch(r"[0-9]+", count_text))
                and len(start_text) <= 10
                and len(count_text) <= 10,
                "volume_owner_invalid",
            )
            start, count = int(start_text), int(count_text)
            _require(
                0 < start <= MAX_UID and 0 < count <= MAX_UID and start + count <= MAX_UID + 1,
                "volume_owner_invalid",
            )

            if re.fullmatch(r"[0-9]+", selector):
                _require(len(selector) <= 10, "volume_owner_invalid")
                selector_uid = int(selector)
                _require(
                    selector_uid <= MAX_UID and len(uids.get(selector_uid, [])) == 1,
                    "volume_owner_invalid",
                )
                resolved_name = uids[selector_uid][0]
                principal = f"account:{resolved_name}:{selector_uid}"
            else:
                _require(
                    bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,254}\$?", selector)),
                    "volume_owner_invalid",
                )
                matches = names.get(selector, [])
                _require(len(matches) <= 1, "volume_owner_invalid")
                principal = f"account:{selector}:{matches[0]}" if matches else f"unresolved:{selector}"

            end = start + count
            first_uid = bisect_left(passwd_uids, start)
            _require(first_uid == len(passwd_uids) or passwd_uids[first_uid] >= end, "volume_owner_invalid")
            allocations.append((start, end, principal, selector))
            if principal == f"account:{service_user}:{service_uid}":
                own_ranges.append((start, end))

        allocations.sort(key=lambda item: (item[0], item[1], item[2]))
        for previous, current in zip(allocations, allocations[1:], strict=False):
            _require(previous[1] <= current[0], "volume_owner_invalid")

        canonical = [
            {"principal": principal, "start": start, "end": end} for start, end, principal, _selector in allocations
        ]
        fingerprint = _sha(
            json.dumps(
                {
                    "service_user": service_user,
                    "service_uid": service_uid,
                    "passwd_uid_bindings": sorted({(name, uid) for name, uid in rows}),
                    "allocations": canonical,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        return tuple(sorted(own_ranges)), fingerprint
    except (UnicodeError, TypeError, ValueError, OverflowError):
        raise Refusal("volume_owner_invalid") from None


def _service_owner_policy(service_user: str, account: object) -> tuple[tuple[tuple[int, int], ...], str]:
    try:
        passwd_records = pwd.getpwall()
        _require(len(passwd_records) <= MAX_SUBUID_ROWS * 16, "volume_owner_invalid")
        passwd_entries = [(entry.pw_name, entry.pw_uid) for entry in passwd_records]
    except (OSError, AttributeError):
        raise Refusal("volume_owner_invalid") from None
    return parse_subuid(
        _read_subuid(),
        service_user=service_user,
        service_uid=account.pw_uid,
        passwd_entries=passwd_entries,
    )


def _owner_allowed(uid: int, account_uid: int, subuid_ranges: tuple[tuple[int, int], ...]) -> bool:
    return uid == account_uid or any(start <= uid < end for start, end in subuid_ranges)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        _require(key not in result, "backend_response_invalid")
        result[key] = value
    return result


def _json_bytes(body: bytes) -> object:
    try:
        return json.loads(
            body,
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid constant")),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
        raise Refusal("backend_response_invalid") from None


def _numeric_count(value: object) -> int:
    _require(isinstance(value, str) and len(value) <= 64, "backend_response_invalid")
    try:
        count = Decimal(value)
    except InvalidOperation:
        raise Refusal("backend_response_invalid") from None
    _require(count.is_finite() and count >= 0 and count <= Decimal("1e15"), "backend_response_invalid")
    return int(count)


def _instant_result_count(results: object) -> int:
    _require(isinstance(results, list) and len(results) <= 1, "backend_response_invalid")
    if not results:
        return 0
    _require(isinstance(results[0], dict), "backend_response_invalid")
    value = results[0].get("value")
    _require(isinstance(value, list) and len(value) == 2, "backend_response_invalid")
    return _numeric_count(value[1])


def _run(argv: list[str], *, user: str | None = None, timeout: int = 20) -> bytes:
    command = ["runuser", "-u", user, "--", *argv] if user else argv
    home = "/root"
    runtime = None
    if user:
        try:
            account = pwd.getpwnam(user)
            home = account.pw_dir
            runtime = Path(f"/run/user/{account.pw_uid}")
            runtime_stat = os.lstat(runtime)
        except (KeyError, OSError):
            raise Refusal("podman_unavailable") from None
        _require(
            stat_is_dir(runtime_stat.st_mode)
            and runtime_stat.st_uid == account.pw_uid
            and runtime_stat.st_mode & 0o077 == 0,
            "podman_unavailable",
        )
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            env={
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "HOME": home,
                "LANG": "C",
                "LC_ALL": "C",
                **({"XDG_RUNTIME_DIR": str(runtime)} if runtime else {}),
            },
        )
    except (OSError, subprocess.TimeoutExpired, KeyError):
        raise Refusal("podman_unavailable") from None
    _require(result.returncode == 0 and len(result.stdout) <= MAX_COMMAND_BYTES, "podman_unavailable")
    return result.stdout


def stat_is_dir(mode: int) -> bool:
    return (mode & 0o170000) == 0o040000


def _json_command(argv: list[str], user: str) -> object:
    try:
        return json.loads(_run(argv, user=user))
    except (json.JSONDecodeError, UnicodeError):
        raise Refusal("podman_unavailable") from None


def _safe_path(
    path: object,
    *,
    root: Path,
    owner_uid: int,
    subuid_ranges: tuple[tuple[int, int], ...],
) -> tuple[Path, int]:
    _require(isinstance(path, str) and path.startswith("/"), "volume_state_invalid")
    candidate = Path(path)
    try:
        relative = candidate.relative_to(root)
        _require(".." not in relative.parts, "volume_state_invalid")
        root_stat = os.lstat(root)
        _require(
            stat.S_ISDIR(root_stat.st_mode)
            and not stat.S_ISLNK(root_stat.st_mode)
            and root_stat.st_uid in {0, owner_uid},
            "volume_state_invalid",
        )
        current = root
        leaf_stat = None
        for part in relative.parts:
            current = current / part
            current_stat = os.lstat(current)
            _require(
                not stat.S_ISLNK(current_stat.st_mode)
                and (current == candidate or current_stat.st_uid in {0, owner_uid}),
                "volume_state_invalid",
            )
            if current == candidate:
                leaf_stat = current_stat
    except (OSError, ValueError):
        raise Refusal("volume_state_invalid") from None
    _require(
        leaf_stat is not None
        and stat.S_ISDIR(leaf_stat.st_mode)
        and _owner_allowed(leaf_stat.st_uid, owner_uid, subuid_ranges),
        "volume_owner_invalid",
    )
    return candidate, leaf_stat.st_uid


def _namespace_http(pid: int, url: str, *, timeout: int = 8, max_bytes: int = MAX_HTTP_BYTES) -> bytes:
    _require(type(pid) is int and pid > 1, "backend_unavailable")
    _require(isinstance(url, str) and url.startswith("http://127.0.0.1:"), "backend_unavailable")
    script = (
        "import sys,urllib.request;"
        "r=urllib.request.urlopen(urllib.request.Request(sys.argv[1],headers={'Accept':'application/json'},method='GET'),timeout=int(sys.argv[2]));"
        "d=r.read(int(sys.argv[3])+1);sys.stdout.buffer.write(d)"
    )
    try:
        result = subprocess.run(
            ["/usr/bin/nsenter", f"--net=/proc/{pid}/ns/net", "--", "/usr/bin/python3", "-c", script,
             url, str(timeout), str(max_bytes)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout + 2,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired, TimeoutError):
        raise Refusal("backend_unavailable") from None
    _require(result.returncode == 0, "backend_unavailable")
    body = result.stdout
    _require(len(body) <= max_bytes, "backend_response_invalid")
    return body


def _get_json(pid: int, url: str) -> object:
    return _json_bytes(_namespace_http(pid, url))


def _probe_count(name: str, pid: int, epoch: int) -> tuple[int, str]:
    end = epoch
    start = max(1, end - WINDOW_SECONDS)
    if name == "prometheus":
        query = f'sum(count_over_time({{__name__!=""}}[24h] @ {end}))'
        data = _get_json(pid, f"http://127.0.0.1:9090/api/v1/query?{urllib.parse.urlencode({'query': query})}")
        _require(
            isinstance(data, dict) and data.get("status") == "success" and isinstance(data.get("data"), dict),
            "backend_response_invalid",
        )
        results = data["data"].get("result")
        count = _instant_result_count(results)
        digest = _sha(json.dumps({"status": data["status"], "count": count}, sort_keys=True).encode())
        return count, digest
    if name == "loki":
        query = ' sum(count_over_time({job=~".+"}[24h])) '
        data = _get_json(
            pid,
            f"http://127.0.0.1:3100/loki/api/v1/query?{urllib.parse.urlencode({'query': query, 'time': str(end)})}"
        )
        _require(
            isinstance(data, dict) and data.get("status") == "success" and isinstance(data.get("data"), dict),
            "backend_response_invalid",
        )
        results = data["data"].get("result")
        count = _instant_result_count(results)
        summary = {"status": data["status"], "count": count}
        return count, _sha(json.dumps(summary, sort_keys=True).encode())
    if name == "tempo":
        parameters = {"start": str(start), "end": str(end), "limit": "10"}
        query_string = urllib.parse.urlencode(parameters)
        data = _get_json(pid, f"http://127.0.0.1:3200/api/search?{query_string}")
        traces = data.get("traces") if isinstance(data, dict) else None
        _require(isinstance(traces, list) and len(traces) <= 10, "backend_response_invalid")
        summary = {
            "count": len(traces),
            "ids": sorted(str(item.get("traceID", "")) for item in traces if isinstance(item, dict)),
        }
        _require(
            len(summary["ids"]) == len(traces)
            and all(re.fullmatch(r"[0-9a-f]{16,64}", item) for item in summary["ids"]),
            "backend_response_invalid",
        )
        return len(traces), _sha(json.dumps(summary, sort_keys=True).encode())
    if name == "pyroscope":
        query = 'process_cpu:cpu:nanoseconds:cpu:nanoseconds{service_name="o11y"}'
        parameters = {
            "query": query,
            "from": str(start),
            "until": str(end),
            "format": "folded",
        }
        url = f"http://127.0.0.1:4040/pyroscope/render?{urllib.parse.urlencode(parameters)}"
        body = _namespace_http(pid, url, max_bytes=MAX_HTTP_BYTES)
        _require(len(body) <= MAX_HTTP_BYTES, "backend_response_invalid")
        lines = [line for line in body.splitlines() if line.strip()]
        count = 0
        _require(len(lines) <= 100_000, "backend_response_invalid")
        for line in lines:
            number = line.rsplit(b" ", 1)[-1]
            _require(len(number) <= 20 and re.fullmatch(rb"[0-9]+", number) is not None, "backend_response_invalid")
            try:
                count += int(number)
            except ValueError:
                raise Refusal("backend_response_invalid") from None
            _require(count <= 10**15, "backend_response_invalid")
        return count, _sha(body)
    raise Refusal("invalid_input")


def _sqlite_integrity(
    path: Path,
    *,
    owner_uid: int,
    subuid_ranges: tuple[tuple[int, int], ...],
    owner_mapping_sha256: str,
) -> tuple[int, str]:
    db_path = path / "grafana.db"
    try:
        st = os.lstat(db_path)
        _require(
            stat.S_ISREG(st.st_mode)
            and not stat.S_ISLNK(st.st_mode)
            and st.st_size >= 4096
            and _owner_allowed(st.st_uid, owner_uid, subuid_ranges)
            and st.st_nlink == 1,
            "grafana_integrity_invalid",
        )
        accepted_file_owners = {st.st_uid}
        wal_path = path / "grafana.db-wal"
        shm_path = path / "grafana.db-shm"
        if os.path.lexists(wal_path):
            wal_stat = os.lstat(wal_path)
            shm_stat = os.lstat(shm_path) if os.path.lexists(shm_path) else None
            _require(
                stat.S_ISREG(wal_stat.st_mode)
                and not stat.S_ISLNK(wal_stat.st_mode)
                and _owner_allowed(wal_stat.st_uid, owner_uid, subuid_ranges)
                and shm_stat is not None
                and stat.S_ISREG(shm_stat.st_mode)
                and not stat.S_ISLNK(shm_stat.st_mode)
                and _owner_allowed(shm_stat.st_uid, owner_uid, subuid_ranges),
                "grafana_integrity_invalid",
            )
            accepted_file_owners.update((wal_stat.st_uid, shm_stat.st_uid))
        # Grafana may still append to this database while the restored stack is
        # running. Use SQLite's normal WAL-aware read-only transaction rather
        # than immutable mode, which can silently ignore a live WAL.
        uri = f"file:{urllib.parse.quote(str(db_path))}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=3) as db:
            db.execute("PRAGMA query_only=ON")
            result = db.execute("PRAGMA integrity_check").fetchone()
            _require(result == ("ok",), "grafana_integrity_invalid")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            _require({"migration_log", "dashboard"} <= tables, "grafana_integrity_invalid")
            migrations = db.execute("SELECT COUNT(*) FROM migration_log").fetchone()[0]
            dashboards = db.execute("SELECT COUNT(*) FROM dashboard").fetchone()[0]
            _require(
                type(migrations) is int and migrations > 0 and type(dashboards) is int and dashboards > 0,
                "grafana_integrity_invalid",
            )
        integrity_evidence = {
            "size": st.st_size,
            "migrations": migrations,
            "dashboards": dashboards,
            "result": "ok",
            "accepted_file_owner_uids": sorted(accepted_file_owners),
            "owner_mapping_sha256": owner_mapping_sha256,
        }
        return migrations, _sha(json.dumps(integrity_evidence, sort_keys=True, separators=(",", ":")).encode())
    except (OSError, sqlite3.Error, TypeError):
        raise Refusal("grafana_integrity_invalid") from None


def inspect(payload: object) -> dict:
    _require(
        isinstance(payload, dict)
        and set(payload) == {"schema_version", "service_user", "artifact_creation_time_epoch", "backend_expectations"},
        "invalid_input",
    )
    _require(payload["schema_version"] == 1 and type(payload["schema_version"]) is int, "invalid_input")
    user = payload["service_user"]
    _require(isinstance(user, str) and USERNAME.fullmatch(user), "invalid_input")
    epoch = payload["artifact_creation_time_epoch"]
    _require(type(epoch) is int and epoch > 1_600_000_000, "invalid_input")
    expected = payload["backend_expectations"]
    _require(isinstance(expected, dict) and set(expected) == BACKENDS, "historical_expectation_invalid")
    normalized = {}
    for name, item in expected.items():
        _require(
            isinstance(item, dict) and set(item) == {"expectation", "evidence_sha256"}, "historical_expectation_invalid"
        )
        _require(item["expectation"] in {"present", "quiet"}, "historical_expectation_invalid")
        evidence = item["evidence_sha256"]
        _require(
            isinstance(evidence, str) and re.fullmatch(r"[0-9a-f]{64}", evidence), "historical_expectation_invalid"
        )
        normalized[name] = item
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        raise Refusal("volume_owner_invalid") from None
    subuid_ranges, owner_mapping_sha256 = _service_owner_policy(user, account)
    root = Path(account.pw_dir)
    _require(root.is_absolute() and root != Path("/"), "invalid_input")
    volumes = {}
    containers = {}
    container_pids = {}
    volume_evidence = []
    container_evidence = []
    for service, (volume, container, destination, _port) in VOLUMES.items():
        listed_volumes = _json_command(["podman", "volume", "ls", "--format", "json"], user)
        _require(isinstance(listed_volumes, list) and len(listed_volumes) <= 128, "volume_state_invalid")
        matches = [
            item
            for item in listed_volumes
            if isinstance(item, dict)
            and isinstance(item.get("Labels"), dict)
            and item["Labels"].get("com.docker.compose.project") == "o11y"
            and item["Labels"].get("com.docker.compose.volume") == volume
        ]
        _require(len(matches) == 1 and isinstance(matches[0].get("Name"), str), "volume_state_invalid")
        physical_name = matches[0]["Name"]
        raw_volume = _json_command(["podman", "volume", "inspect", physical_name], user)
        _require(
            isinstance(raw_volume, list) and len(raw_volume) == 1 and isinstance(raw_volume[0], dict),
            "volume_state_invalid",
        )
        vol = raw_volume[0]
        _require(vol.get("Name") == physical_name and isinstance(vol.get("Mountpoint"), str), "volume_state_invalid")
        labels = vol.get("Labels") or {}
        _require(
            isinstance(labels, dict)
            and labels.get("com.docker.compose.project") == "o11y"
            and labels.get("com.docker.compose.volume") == volume,
            "volume_state_invalid",
        )
        mountpoint, mount_owner_uid = _safe_path(
            vol["Mountpoint"],
            root=root,
            owner_uid=account.pw_uid,
            subuid_ranges=subuid_ranges,
        )
        volumes[service] = mountpoint
        volume_evidence.append(
            {
                "service": service,
                "logical_volume": volume,
                "volume_name_sha256": _sha(physical_name.encode()),
                "mountpoint_sha256": _sha(os.fsencode(mountpoint)),
                "owner_uid": mount_owner_uid,
                "owner_mapping_sha256": owner_mapping_sha256,
            }
        )
        raw_container = _json_command(["podman", "inspect", container], user)
        _require(
            isinstance(raw_container, list) and len(raw_container) == 1 and isinstance(raw_container[0], dict),
            "container_state_invalid",
        )
        inspected = raw_container[0]
        container_pid = inspected.get("State", {}).get("Pid")
        _require(
            inspected.get("Name", "").lstrip("/") == container
            and inspected.get("State", {}).get("Running") is True
            and type(container_pid) is int
            and container_pid > 1,
            "container_state_invalid",
        )
        mounts = inspected.get("Mounts")
        _require(isinstance(mounts, list), "volume_mount_mismatch")
        matches = [mount for mount in mounts if isinstance(mount, dict) and mount.get("Destination") == destination]
        _require(
            len(matches) == 1 and matches[0].get("Source") == str(mountpoint) and matches[0].get("RW") is True,
            "volume_mount_mismatch",
        )
        networks = inspected.get("NetworkSettings", {}).get("Networks")
        _require(isinstance(networks, dict) and len(networks) == 1, "container_state_invalid")
        network_name, network = next(iter(networks.items()))
        _require(
            isinstance(network, dict)
            and network_name.rsplit("_", 1)[-1] == "o11y"
            and isinstance(network.get("IPAddress"), str),
            "container_state_invalid",
        )
        raw_network = _json_command(["podman", "network", "inspect", network_name], user)
        _require(
            isinstance(raw_network, list)
            and len(raw_network) == 1
            and isinstance(raw_network[0], dict)
            and raw_network[0].get("name") == network_name
            and (raw_network[0].get("labels") or {}).get("com.docker.compose.project") == "o11y"
            and (raw_network[0].get("labels") or {}).get("com.docker.compose.network") == "o11y",
            "container_state_invalid",
        )
        address = network.get("IPAddress")
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise Refusal("container_state_invalid") from None
        _require(
            ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local, "container_state_invalid"
        )
        containers[service] = str(ip)
        container_pids[service] = container_pid
        container_evidence.append(
            {
                "service": service,
                "container": container,
                "network_sha256": _sha(network_name.encode()),
                "address_sha256": _sha(address.encode()),
                "mount_sha256": _sha(str(mountpoint).encode()),
            }
        )
    database_count, database_fingerprint = _sqlite_integrity(
        volumes["grafana"],
        owner_uid=account.pw_uid,
        subuid_ranges=subuid_ranges,
        owner_mapping_sha256=owner_mapping_sha256,
    )
    backends = {}
    for name in sorted(BACKENDS):
        count, proof = _probe_count(name, container_pids[name], epoch)
        mode = normalized[name]["expectation"]
        _require((count > 0 if mode == "present" else count == 0), "historical_data_missing")
        backends[name] = {
            "expectation": mode,
            "historical_records": count,
            "evidence_sha256": normalized[name]["evidence_sha256"],
            "readback_sha256": proof,
        }
    body = {
        "schema_version": 1,
        "status": "verified",
        "reason": "restored_historical_data_verified",
        "volume_count": len(volumes),
        "volume_bindings_sha256": _sha(
            json.dumps(sorted(volume_evidence, key=lambda item: item["service"]), sort_keys=True, separators=(",", ":"))
            .encode()
        ),
        "container_bindings_sha256": _sha(
            json.dumps(
                sorted(container_evidence, key=lambda item: item["service"]), sort_keys=True, separators=(",", ":")
            ).encode()
        ),
        "backend_count": len(backends),
        "grafana_integrity": "ok",
        "grafana_migrations": database_count,
        "grafana_database_sha256": database_fingerprint,
        "backends": backends,
    }
    body["receipt_fingerprint"] = _sha(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    return body


def _main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        _require(len(raw) <= MAX_INPUT_BYTES, "invalid_input")
        payload = json.loads(raw)
        result = inspect(payload)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Refusal as exc:
        print(json.dumps({"schema_version": 1, "status": "unavailable", "reason": exc.reason}, separators=(",", ":")))
        return 2
    except (json.JSONDecodeError, UnicodeError, AttributeError, KeyError, TypeError, ValueError):
        print('{"schema_version":1,"status":"unavailable","reason":"invalid_input"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
