#!/usr/bin/env python3
"""Return bounded receiver-host container log-source metadata and content-class counts only."""

import json
import posixpath
import re
import subprocess
import sys

DRIVERS = {"journald", "k8s-file", "json-file", "none", "other", "unknown"}
JOURNAL_WINDOW = "-15min"
O11Y_PREFIX = "o11y-"
# shortcut: only the pinned Alloy journal destinations count; extend when config adopts another target.
JOURNAL_DESTINATIONS = {"/var/log/journal", "/run/log/journal"}
# Mounts from other host paths have unknown provenance and cannot establish a journal source.
JOURNAL_SOURCES = {"/var/log/journal", "/run/log/journal"}
# Receiver containers whose journald content is classified. The journal collector is deliberately absent.
CONTENT_CONTAINERS = ("node-exporter", "prometheus", "loki", "alloy", "tempo", "pyroscope", "grafana")
# Newest journald entries per receiver container that the content scan reads (journalctl -n). A window that
# reaches the cap is reported capped and refuses the collector gate, so the cap must exceed one receiver's
# 15-minute volume. At 200, grafana and pyroscope reached it on 2026-11-07.
CONTENT_CAP = 5000
# Fixed content classes. Only counts leave this process; the matched text never does.
CONTENT_PATTERNS = {
    # The lookbehind anchors each match at the start of a local-part run. Without it the engine retries
    # from every position inside a long run of letters or base64, which is quadratic (about 1.7 s per 48 KB line).
    "email": re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # Digit and dot guards, not \b: \b matches after a dot, so "999.1.1.1" and "1.1.1.1.1" would count.
    # The trailing guard lets a full stop end a sentence; only a following digit or ".digit" rules a match out.
    "ipv4": re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?!\d|\.\d)"),
    "credential": re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # Keyword anywhere, not after a word boundary, so "db_password=" and "accessToken=" match. An optional quote
    # may sit before the separator (JSON keys) and after it (JSON values). Linear: the value run is one scan.
    "secret_pair": re.compile(
        r"""(?i)(?:token|password|passwd|secret|api[_-]?key|authorization)["']?\s*[=:]\s*["']?[^\s,;&"']{4,}"""
    ),
    # A JWT: three base64url segments, the first beginning eyJ. The lookbehind starts a match only at a run
    # boundary, so a long run of "eyJ" cannot restart the scan at each position.
    "jwt": re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "session": re.compile(r"(?i)(?:\bcookie:|\bset-cookie:|_session=)"),
}
# url_query and dsn_userinfo are not patterns: each is a per-token scan, so no regex can restart at each scheme.
CONTENT_CLASSES = (*CONTENT_PATTERNS, "url_query", "dsn_userinfo")
URL_SCHEME = re.compile(r"https?://", re.IGNORECASE | re.ASCII)
URL_QUERY_TAIL = re.compile(r"\?[^#]")
# A bare IPv4 address is reported but does not block; the other classes do.
BLOCKING_CLASSES = (
    "email",
    "credential",
    "secret_pair",
    "url_query",
    "dsn_userinfo",
    "jwt",
    "private_key",
    "session",
)


def run(argv, timeout=8):
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def decode_json(value):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def driver(value):
    if not isinstance(value, str) or not value:
        return "unknown"
    normalized = value.lower()
    return normalized if normalized in DRIVERS - {"unknown", "other"} else "other"


def inspect(name):
    rc, output = run(["podman", "inspect", "--type", "container", "--format", "json", name])
    data = decode_json(output) if rc == 0 else None
    return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None


def log_driver(container):
    host = container.get("HostConfig")
    config = host.get("LogConfig") if isinstance(host, dict) else None
    value = config.get("Type") if isinstance(config, dict) else None
    return driver(value)


def default_driver():
    rc, output = run(["podman", "info", "--format", "json"])
    data = decode_json(output) if rc == 0 else None
    host = data.get("host") if isinstance(data, dict) else None
    return driver(host.get("logDriver") if isinstance(host, dict) else None)


def running_containers():
    rc, output = run(["podman", "ps", "--filter", "status=running", "--format", "json"])
    data = decode_json(output) if rc == 0 else None
    if not isinstance(data, list):
        return None
    names = []
    for item in data:
        if not isinstance(item, dict):
            return None
        values = item.get("Names")
        if isinstance(values, str):
            values = [values]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value for value in values)
        ):
            return None
        names.extend(value for value in values if value.startswith(O11Y_PREFIX))
    return sorted(set(names))


def journal_entry_count(output, expected_name):
    count = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        entry = decode_json(line)
        if not isinstance(entry, dict):
            return None
        names = entry.get("CONTAINER_NAME")
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            return None
        count += expected_name in names
    return count


def journal_status(names, drivers):
    if any(drivers.get(name, "unknown") == "unknown" for name in names):
        return "unverified", 0
    journal_names = [name for name in names if drivers.get(name) == "journald"]
    if not journal_names:
        return "unsupported", 0
    count = 0
    for name in journal_names:
        rc, output = run(
            [
                "journalctl",
                "--user",
                "--no-pager",
                "--quiet",
                "--output=json",
                "--output-fields=CONTAINER_NAME",
                f"--since={JOURNAL_WINDOW}",
                "-n",
                "100",
                f"CONTAINER_NAME={name}",
            ],
            timeout=12,
        )
        if rc is None:
            return "unverified", 0
        if rc != 0:
            return "unreadable", 0
        entry_count = journal_entry_count(output, name)
        if entry_count is None:
            return "unverified", 0
        count += entry_count
    return ("readable" if count else "no_entries"), count


def message_text(value):
    if isinstance(value, str):
        return value
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 255 for item in value)
    ):
        return bytes(value).decode("utf-8", errors="replace")
    return None


def count_url_query(text):
    """Count whitespace-separated tokens that hold a URL scheme with a query marker after it.

    The marker is a "?" followed by a character that is not "#", anywhere after the token's first scheme.
    A token counts at most once, so one long line of schemes costs one linear pass per token.
    """
    hits = 0
    for token in text.split():
        scheme = URL_SCHEME.search(token)
        if scheme and URL_QUERY_TAIL.search(token, scheme.end()):
            hits += 1
    return hits


def count_dsn_userinfo(text):
    """Count whitespace-separated tokens whose URL authority carries a password: user:secret@host.

    The authority is the text after the first "://" up to the next "/". It counts when a ":" sits before its
    "@". One pass per token, so the cost is linear in the line.
    """
    hits = 0
    for token in text.split():
        scheme = token.find("://")
        if scheme == -1:
            continue
        authority = token[scheme + 3 :].split("/", 1)[0]
        at = authority.find("@")
        if at != -1 and ":" in authority[:at]:
            hits += 1
    return hits


def content_scan(output, expected_name):
    """Count one container's entries, content-class hits, and entries whose text could not be classified.

    Returns None when the journal output is malformed.
    """
    entries = 0
    unscanned = 0
    classes = dict.fromkeys(CONTENT_CLASSES, 0)
    for line in output.splitlines():
        if not line.strip():
            continue
        entry = decode_json(line)
        if not isinstance(entry, dict):
            return None
        names = entry.get("CONTAINER_NAME")
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            return None
        if expected_name not in names:
            continue
        entries += 1
        text = message_text(entry.get("MESSAGE"))
        if text is None:
            # journalctl encodes a field longer than 4096 bytes as null unless --all is passed. An entry
            # whose text is not classified is a gap, never a clear result.
            unscanned += 1
            continue
        for label, pattern in CONTENT_PATTERNS.items():
            classes[label] += sum(1 for _ in pattern.finditer(text))
        classes["url_query"] += count_url_query(text)
        classes["dsn_userinfo"] += count_dsn_userinfo(text)
    return {"entries": entries, "unscanned": unscanned, "classes": classes}


def unverified_content():
    return {
        "status": "unverified",
        "entries": None,
        "capped": None,
        "unscanned": None,
        "blocking_hits": None,
        "classes": None,
    }


def content_survey(names, drivers):
    report = {}
    for short in CONTENT_CONTAINERS:
        name = O11Y_PREFIX + short
        # Only journald content is readable here; a receiver that is not running or not journald is not read.
        if name not in names or drivers.get(name) != "journald":
            report[short] = unverified_content()
            continue
        rc, output = run(
            [
                "journalctl",
                "--user",
                "--no-pager",
                "--quiet",
                "--output=json",
                # Without --all, journalctl encodes any field over 4096 bytes as null.
                "--all",
                "--output-fields=CONTAINER_NAME,MESSAGE",
                f"--since={JOURNAL_WINDOW}",
                "-n",
                str(CONTENT_CAP),
                f"CONTAINER_NAME={name}",
            ],
            # Wait long enough for a full CONTENT_CAP-entry read; the metadata read keeps its own bound.
            timeout=45,
        )
        scanned = content_scan(output, name) if rc == 0 else None
        if scanned is None:
            report[short] = unverified_content()
            continue
        classes = scanned["classes"]
        blocking = sum(classes[label] for label in BLOCKING_CLASSES)
        unscanned = scanned["unscanned"]
        # blocked wins: a hit is actionable even when some entries could not be read.
        # An empty read proves nothing about the receiver: no entries in the window is "no_entries", never "clear".
        if blocking:
            status = "blocked"
        elif unscanned:
            status = "unverified"
        elif scanned["entries"] == 0:
            status = "no_entries"
        else:
            status = "clear"
        report[short] = {
            "status": status,
            "entries": scanned["entries"],
            "capped": scanned["entries"] >= CONTENT_CAP,
            "unscanned": unscanned,
            "blocking_hits": blocking,
            "classes": classes,
        }
    return report


def content_status(report):
    statuses = {item["status"] for item in report.values()}
    if "blocked" in statuses:
        return "blocked"
    if "unverified" in statuses:
        return "unverified"
    # A receiver with no entries in the window was read but not evidenced: the set is incomplete, not clear.
    if "no_entries" in statuses:
        return "incomplete"
    return "clear"


def alloy_source():
    container = inspect("o11y-alloy")
    if container is None:
        return None
    state = container.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        return None
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        return None
    uncertain = False
    readable_source = False
    seen_journal_mounts = {}
    for mount in mounts:
        if not isinstance(mount, dict):
            return None
        destination = mount.get("Destination")
        if (
            not isinstance(destination, str)
            or not destination.startswith("/")
            or posixpath.normpath(destination) != destination
        ):
            return None
        if destination not in JOURNAL_DESTINATIONS:
            if any(
                posixpath.commonpath((destination, journal_path)) == destination
                for journal_path in JOURNAL_DESTINATIONS
            ):
                uncertain = True
            continue
        writable = mount.get("RW")
        signature = (writable, mount.get("Source"))
        if destination in seen_journal_mounts and seen_journal_mounts[destination] != signature:
            uncertain = True
        else:
            seen_journal_mounts[destination] = signature
        if writable is True:
            continue
        if writable is not False:
            uncertain = True
            continue
        source = mount.get("Source")
        if not isinstance(source, str) or source not in JOURNAL_SOURCES:
            uncertain = True
            continue
        rc, output = run(
            [
                "podman",
                "exec",
                "o11y-alloy",
                "sh",
                "-c",
                f'if [ ! -d {destination} ]; then printf missing; '
                f'elif [ ! -r {destination} ]; then printf unreadable; '
                "else printf readable; fi",
            ]
        )
        if rc is None or rc != 0:
            uncertain = True
            continue
        probe = output.strip()
        if probe == "readable":
            readable_source = True
            continue
        if probe not in {"missing", "unreadable"}:
            uncertain = True
    if uncertain:
        return None
    return readable_source


def survey():
    default = default_driver()
    names = running_containers()
    if names is None:
        return {"status": "unavailable", "reason": "container_metadata_unavailable"}
    if not names:
        return {"status": "unavailable", "reason": "no_running_o11y_containers"}
    counts = dict.fromkeys(sorted(DRIVERS), 0)
    drivers = {}
    for name in names:
        container = inspect(name)
        if container is None:
            return {"status": "unavailable", "reason": "container_metadata_unavailable"}
        state = container.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            return {"status": "unavailable", "reason": "container_metadata_unavailable"}
        value = log_driver(container)
        drivers[name] = value
        counts[value] += 1
    journal, journal_count = journal_status(names, drivers)
    alloy_mounted = alloy_source()
    if alloy_mounted is None:
        return {"status": "unavailable", "reason": "alloy_source_unverified"}
    content = content_survey(names, drivers)
    return {
        "status": "observed",
        "rootless_default_log_driver": default,
        "running_o11y_container_count": len(names),
        "running_log_driver_counts": counts,
        "journald_metadata_read": journal,
        "journald_metadata_entry_count": journal_count,
        "alloy_read_only_journal_source_mounted": alloy_mounted,
        "content_survey": content,
        "content_survey_status": content_status(content),
    }


if __name__ == "__main__":
    try:
        print(json.dumps(survey(), sort_keys=True))
    except Exception:
        print(json.dumps({"status": "unavailable", "reason": "survey_failed"}, sort_keys=True))
        sys.exit(1)
