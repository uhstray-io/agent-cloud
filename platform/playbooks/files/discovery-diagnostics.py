"""Bounded read-only NetBox incident evidence; run only through check-discovery.yml."""

import json
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone

LIMIT = 16 * 1024 * 1024
CONFIG_LIMIT = 256 * 1024
DB_QUERY = """
import json
from django.db import connection, transaction
from django.db.models import Q
from dcim.models import Device, Site, Region, Location, Rack
from ipam.models import IPAddress
from tenancy.models import Tenant
from virtualization.models import VirtualMachine, Cluster
with transaction.atomic():
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = '15s'")
        cursor.execute("SHOW transaction_read_only")
        if cursor.fetchone()[0] != 'on':
            raise RuntimeError("read_only_transaction_required")
    result = {model.__name__: model.objects.count() for model in
              (Device, Site, Region, Location, Rack, IPAddress, Tenant, VirtualMachine, Cluster)}
    result['sites_missing_gps'] = Site.objects.filter(Q(latitude__isnull=True) | Q(longitude__isnull=True)).count()
    transaction.set_rollback(True)
print(json.dumps(result, sort_keys=True))
"""
COUNTS = (
    "Device",
    "Site",
    "Region",
    "Location",
    "Rack",
    "IPAddress",
    "Tenant",
    "VirtualMachine",
    "Cluster",
    "sites_missing_gps",
)
SOURCES = ("network_discovery", "snmp_discovery", "pfsense_sync", "proxmox_discovery")
SIGNALS = {
    "authentication_rejected": (
        r"\b(401|403|unauthenticated|unauthorized|forbidden|permission denied|authentication failed)\b"
    ),
    "transport_error": r"connection refused|connection reset|no route to host|timed? out|timeout|TLS handshake",
    "credential_missing": r"missing .*credential|credential.*not found|secret.*not found",
    "partial_collection": r"skipping .*offline|partial collection|pagination.*failed",
    "submission_message": r"Policy .*Successfully ingested \d+ entities",
    "error": r"\berror\b|\bexception\b|\bfailed\b",
    "success_message": r"\bsuccess(?:ful|fully)?\b|\bcompleted\b",
}


class ReadFailure(Exception):
    """Never include raw command output in exceptions."""


def read_command(argv, *, timeout=45):
    """Bound BOTH streams; kill the reader on overflow/timeout, never its container."""
    chunks = []
    size = 0
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                deadline = time.monotonic() + timeout
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise ReadFailure("command_timeout")
                    for key, _ in selector.select(0.1):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        size += len(data)
                        if size > LIMIT:
                            raise ReadFailure("output_limit_exceeded")
                        chunks.append((key.data, data))
                if process.wait(timeout=max(0.1, deadline - time.monotonic())) != 0:
                    raise ReadFailure("command_failed")
        except BaseException:
            process.kill()
            process.wait()
            raise
    return b"".join(data for stream, data in chunks if stream == "stdout"), b"".join(
        data for stream, data in chunks if stream == "stderr"
    )


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid_timestamp")
    # Preserve the recorded zone and fractional precision; never infer a local zone.
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)", value):
        raise ValueError("invalid_timestamp")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def log_summary(raw):
    coverage = []
    signals = {}
    unparsed = 0
    lines = raw.decode("utf-8", errors="replace").splitlines()
    for line in lines:
        parts = line.split(" ", 1)
        try:
            recorded = timestamp(parts[0])
        except ValueError:
            unparsed += 1
            continue
        coverage.append(recorded)
        message = parts[1] if len(parts) == 2 else ""
        source = next((name for name in SOURCES if name in message.lower().replace("-", "_")), "unattributed")
        for reason, pattern in SIGNALS.items():
            if re.search(pattern, message, re.IGNORECASE):
                key = (source, reason)
                record = signals.setdefault(
                    key, {"source": source, "signal": reason, "count": 0, "first": recorded, "last": recorded}
                )
                record["count"] += 1
                record["first"] = min(
                    record["first"], recorded, key=lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))
                )
                record["last"] = max(
                    record["last"], recorded, key=lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))
                )
    ordered = sorted(coverage, key=lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")))
    return {
        "line_count": len(lines),
        "unparsed_lines": unparsed,
        "first_retained_in_window": ordered[0] if ordered else None,
        "last_retained_in_window": ordered[-1] if ordered else None,
        "signals": list(signals.values()),
        "historical_completeness": "unverified",
        "interpretation": "text_signals_only_not_collection_or_reconciliation_proof",
    }


def config_summary(raw):
    try:
        import yaml
    except ImportError as error:
        raise ReadFailure("config_yaml_unavailable") from error

    data = yaml.safe_load(raw)
    orb = data["orb"]
    policies = orb["policies"]
    rows = {}
    for backend, policy, source in (
        ("network_discovery", "subnet_scan", "network_discovery"),
        ("snmp_discovery", "snmp_scan", "snmp_discovery"),
        ("worker", "pfsense_sync", "pfsense_sync"),
        ("worker", "proxmox_discovery", "proxmox_discovery"),
    ):
        entry = policies.get(backend, {}).get(policy)
        row = {"declared": entry is not None}
        if entry is not None:
            config = entry.get("config", {})
            schedule = config.get("schedule")
            # Only cron syntax and numeric bounds may cross this credential boundary.
            row["schedule"] = (
                schedule
                if isinstance(schedule, str) and re.fullmatch(r"[0-9*/ ,\-]{5,80}", schedule)
                else "unrecognized"
            )
            row["timeout_raw"] = config.get("timeout") if type(config.get("timeout")) is int else None
            row["timeout_unit"] = "unverified"
        rows[source] = row
    return {
        "sources": rows,
        "loaded_by_running_agent": "unverified",
        "vault_manager_declared": orb.get("secrets_manager", {}).get("active") == "vault",
    }


def collect(options):
    runtime = options["runtime"]
    if runtime != "docker":
        raise ReadFailure("docker_target_required")
    since, until = timestamp(options["since"]), timestamp(options["until"])
    if datetime.fromisoformat(since.replace("Z", "+00:00")) >= datetime.fromisoformat(until.replace("Z", "+00:00")):
        raise ValueError("invalid_window")
    report = {
        "observed_at": datetime.now(timezone.utc).isoformat(),  # noqa: UP017 -- support target Python 3.10
        "window": {"since": since, "until": until},
        "status": "baseline_only",
        "pipeline_status": "unknown",
        "components": {},
    }
    for component in ("orb-agent", "netbox", "diode-ingester", "diode-reconciler", "diode-auth"):
        container = "netbox-orb-agent" if component == "orb-agent" else f"netbox-{component}-1"
        row = {}
        report["components"][component] = row
        try:
            raw, _ = read_command([runtime, "inspect", container])
            (metadata,) = json.loads(raw)
            state = metadata["State"]["Status"]
            row["state"] = (
                state if state in ("running", "exited", "created", "restarting", "paused", "dead") else "unknown"
            )
            digest = metadata["Image"]
            row["image_id"] = (
                digest if isinstance(digest, str) and re.fullmatch(r"(sha256:)?[a-f0-9]{64}", digest) else "unverified"
            )
            row["created_at"] = timestamp(metadata["Created"])
        except Exception as error:
            row["metadata_error"] = str(error) if isinstance(error, ReadFailure) else "metadata_read_failed"
        if component == "orb-agent" and "metadata_error" not in row:
            try:
                # The actual mount, not an assumed checkout's agent.yaml. Never echo its path/content.
                mounts = [m for m in metadata["Mounts"] if m["Destination"] == "/opt/orb/agent.yaml"]
                (mount,) = mounts
                # Nonblocking open plus fstat refuses pipes/devices without hanging.
                with os.fdopen(os.open(mount["Source"], os.O_RDONLY | os.O_NONBLOCK), "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise ReadFailure("config_regular_file_required")
                    config = handle.read(CONFIG_LIMIT + 1)
                if len(config) > CONFIG_LIMIT:
                    raise ReadFailure("config_limit_exceeded")
                row["config"] = config_summary(config)
            except Exception as error:
                row["config_error"] = str(error) if isinstance(error, ReadFailure) else "config_read_failed"
        try:
            out, err = read_command([runtime, "logs", "--timestamps", "--since", since, "--until", until, container])
            row["logs"] = log_summary(out + (b"\n" if out and err else b"") + err)
        except Exception as error:
            row["logs_error"] = str(error) if isinstance(error, ReadFailure) else "log_read_failed"
    try:
        # Request a read-only startup connection; the fixed query independently
        # requires a read-only transaction and rolls it back before returning counts.
        out, _ = read_command(
            [
                runtime,
                "exec",
                "--env",
                "PGOPTIONS=-c default_transaction_read_only=on",
                "netbox-netbox-1",
                "/opt/netbox/netbox/manage.py",
                "shell",
                "--interface",
                "python",
                "-c",
                DB_QUERY,
            ]
        )
        counts = json.loads(out.decode().strip().splitlines()[-1])
        if set(counts) != set(COUNTS) or any(type(v) is not int or v < 0 for v in counts.values()):
            raise ValueError("invalid_counts")
        report["database_counts"] = counts
    except Exception:
        report["database_error"] = "read_only_query_failed"
    report["baseline_complete"] = "database_error" not in report and all(
        "metadata_error" not in row and "config_error" not in row and "logs_error" not in row
        for row in report["components"].values()
    )
    # This initial collector cannot prove source/run correlation. Even a complete
    # baseline exits nonzero so Semaphore cannot present it as recovered discovery.
    return report


def main():
    try:
        options = json.loads(sys.stdin.read(4097))
        result = collect(options)
    except Exception as error:
        result = {
            "status": "blocked",
            "reason": str(error) if isinstance(error, ReadFailure) else "invalid_input_or_collector_failure",
        }
    print(json.dumps(result, sort_keys=True))
    return 3


if __name__ == "__main__":
    sys.exit(main())
