#!/usr/bin/env python3
"""Build platform/tests/test_durations.json from pytest junit XML (plan 21 phase 2, D3).

Usage:
    python3 scripts/test-durations.py REPORT.xml [MORE.xml ...] --out platform/tests/test_durations.json

Build the input only from a green CI run's report, produced with ``-o junit_family=xunit1`` so each
testcase carries ``file`` and maps back to a node ID. A run stopped by ``-x`` is partial: its
durations file would say nothing about the tests it never reached.

Each testcase's ``time`` is pytest's total for that test (setup, call and teardown summed, the
``junit_duration_report`` default). A testcase carrying a ``<skipped>`` child is kept with
``"skipped": true``, never dropped, so a test that CI skips is not mistaken for a missing one.
Testcases sharing a node ID are summed; such a node is skipped only when every one of them was.

Stdlib only. Output is sorted JSON with one entry per line. The reader is the stdlib XML parser on
purpose: its input is the CI runner's own report, never untrusted XML (the two lines that import and
parse it carry the bandit B405 and B314 suppressions for that reason).
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET  # nosec B405
from pathlib import Path


def node_id(case: ET.Element) -> str:
    """Map one xunit1 testcase to a pytest node ID: ``file::name`` or ``file::Class::name``."""
    file_attr = case.get("file")
    name = case.get("name")
    if not file_attr or not name:
        raise ValueError("testcase without file/name attributes: build the report with -o junit_family=xunit1")
    classname = case.get("classname", "")
    last = classname.rsplit(".", 1)[-1]
    if last != Path(file_attr).stem and last[:1].isupper():
        return f"{file_attr}::{last}::{name}"
    return f"{file_attr}::{name}"


def read_report(path: Path) -> list[tuple[str, float, bool]]:
    root = ET.parse(path).getroot()  # nosec B314
    rows = []
    for case in root.iter("testcase"):
        skipped = case.find("skipped") is not None
        rows.append((node_id(case), float(case.get("time", "0")), skipped))
    return rows


def build(reports: list[Path]) -> dict[str, dict]:
    times: dict[str, float] = {}
    skips: dict[str, bool] = {}
    for report in reports:
        for nodeid, seconds, skipped in read_report(report):
            times[nodeid] = times.get(nodeid, 0.0) + seconds
            skips[nodeid] = skips.get(nodeid, True) and skipped
    if not times:
        raise ValueError("no testcases found in the report(s): refusing to write an empty durations file")
    return {nodeid: {"time": times[nodeid], "skipped": skips[nodeid]} for nodeid in sorted(times)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the per-test durations file from junit XML.")
    parser.add_argument("reports", nargs="+", type=Path, help="junit XML report(s) from a green CI run")
    parser.add_argument("--out", required=True, type=Path, help="durations JSON to write")
    args = parser.parse_args(argv)
    try:
        durations = build(args.reports)
    except (ValueError, ET.ParseError) as exc:
        print(f"test-durations: {exc}", file=sys.stderr)
        return 1
    lines = [json.dumps(nodeid) + ": " + json.dumps(durations[nodeid], sort_keys=True) for nodeid in durations]
    args.out.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    print(f"test-durations: {len(durations)} node IDs -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
