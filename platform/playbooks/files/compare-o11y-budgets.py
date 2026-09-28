#!/usr/bin/env python3
"""Compare declared and live retention values without relying on a collection."""

import json
import re
import sys
from decimal import Decimal


SECONDS = {
    "ms": Decimal("0.001"),
    "s": Decimal(1),
    "m": Decimal(60),
    "h": Decimal(3600),
    "d": Decimal(86400),
    "w": Decimal(604800),
    "y": Decimal(31536000),
}
DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|[smhdwy])")
SIZE = re.compile(r"([0-9]+)(B|KB|MB|GB|TB|PB|EB)")


def seconds(value):
    value = str(value).replace(" ", "")
    parts = list(DURATION.finditer(value))
    if not parts or "".join(part.group() for part in parts) != value:
        raise ValueError("invalid duration")
    return sum(Decimal(part.group(1)) * SECONDS[part.group(2)] for part in parts)


def size_bytes(value):
    value = str(value).upper()
    if value == "0":
        return 0
    part = SIZE.fullmatch(value)
    if not part:
        raise ValueError("invalid size")
    units = ("B", "KB", "MB", "GB", "TB", "PB", "EB")
    return int(part.group(1)) * 1024 ** units.index(part.group(2))


def compare(payload):
    for name, parser in (("prom_time", seconds), ("loki_time", seconds),
                         ("tempo_time", seconds), ("prom_size", size_bytes)):
        if parser(payload["declared"][name]) != parser(payload["live"][name]):
            raise ValueError(f"{name} differs from the declared budget")


if __name__ == "__main__":
    try:
        compare(json.load(sys.stdin))
    except (KeyError, ValueError, TypeError) as exc:
        sys.exit(f"Budget readback mismatch: {exc}")
    print("verified")
