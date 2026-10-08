#!/usr/bin/env python3
"""Grow an LVM-backed ext4 root filesystem into the free space of its virtual disk, online.

    grow-root-lvm.py plan    report the chain and the steps a grow would take (writes nothing)
    grow-root-lvm.py apply   take them: growpart -> pvresize -> lvextend -r (online resize2fs),
                             or resize2fs alone when an earlier run grew the LV but not its
                             filesystem

Run as root. Prints one JSON object with sizes and step names only: no device paths, LV
paths or VG names (the o11y diagnostics' convention). Refuses, before any change, anything
but the one shape this is written for: `/` is ext4 on one LV, in a VG of exactly one PV,
which is one partition of one disk. Every step is idempotent: growpart's NOCHANGE, a PV
already the size of its partition and a VG with no free extents are each "nothing to do".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from typing import Any


class Refused(Exception):
    """A fixed, safe-to-display reason the grow will not proceed."""


def run(argv: list[str], ok: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode not in ok:
        raise Refused(f"{argv[0]} failed (rc {r.returncode})")
    return r


def _lvm_report(argv: list[str], key: str) -> list[dict[str, Any]]:
    data = json.loads(run(argv).stdout)
    return [row for rep in data.get("report", []) for row in rep.get(key, [])]


def collect() -> dict[str, Any]:
    units = ["--readonly", "--reportformat", "json", "--units", "b", "--nosuffix"]
    return {
        "root": json.loads(run(["findmnt", "--json", "--output", "SOURCE,FSTYPE,MAJ:MIN", "--target", "/"]).stdout),
        "lsblk": json.loads(run(["lsblk", "--json", "--bytes", "--paths", "--output",
                                 "NAME,TYPE,SIZE,MAJ:MIN"]).stdout),
        "pvs": _lvm_report(["pvs", *units, "-o", "pv_name,vg_name,pv_size,dev_size"], "pv"),
        "vgs": _lvm_report(["vgs", *units, "-o", "vg_name,vg_free,pv_count"], "vg"),
        "lvs": _lvm_report(["lvs", *units, "-o", "lv_path,vg_name,lv_size"], "lv"),
    }


def chain(state: dict[str, Any]) -> dict[str, Any]:
    """The root's disk -> partition -> PV -> VG -> LV, or Refused."""
    fs = (state["root"].get("filesystems") or [{}])[0]
    if fs.get("fstype") != "ext4":
        raise Refused("root filesystem is not ext4")
    majmin = fs.get("maj:min")

    parents: dict[str, list[dict[str, Any]]] = {}

    def walk(node: dict[str, Any], up: list[dict[str, Any]]) -> None:
        here = [*up, node]
        if node.get("maj:min") == majmin:
            parents.setdefault(majmin, []).append(here)
        for child in node.get("children", []) or []:
            walk(child, here)

    for dev in state["lsblk"].get("blockdevices", []):
        walk(dev, [])
    paths = parents.get(majmin, [])
    if len(paths) != 1:
        raise Refused("root LV does not sit on exactly one partition")
    path = paths[0]
    if [n.get("type") for n in path] != ["disk", "part", "lvm"]:
        raise Refused("root is not an LV on a partition of a disk")
    disk, part, lv_node = path

    # Joined by the device number its path resolves to, as diagnose-o11y-host-storage.py does.
    lv = [r for r in state["lvs"] if device_number(r.get("lv_path", "")) == majmin]
    if len(lv) != 1:
        raise Refused("root LV not found in the LVM report")
    vg_name = lv[0]["vg_name"]
    vg = [r for r in state["vgs"] if r.get("vg_name") == vg_name]
    pv = [r for r in state["pvs"] if r.get("vg_name") == vg_name]
    if len(vg) != 1 or vg[0].get("pv_count") != "1" or len(pv) != 1 or pv[0].get("pv_name") != part["name"]:
        raise Refused("root VG is not exactly one PV on the root's partition")
    num = re.search(r"(\d+)$", part["name"])
    if not num:
        raise Refused("partition number not readable")
    return {"disk": disk, "part": part, "partnum": num.group(1), "pv": pv[0], "vg": vg[0], "lv": lv[0]}


def device_number(path: str) -> str | None:
    try:
        m = os.stat(path)
    except OSError:
        return None
    return f"{os.major(m.st_rdev)}:{os.minor(m.st_rdev)}" if stat.S_ISBLK(m.st_mode) else None


def sizes(state: dict[str, Any], c: dict[str, Any]) -> dict[str, int]:
    st = os.statvfs("/")
    return {"disk_bytes": int(c["disk"]["size"]), "partition_bytes": int(c["part"]["size"]),
            "pv_bytes": int(c["pv"]["pv_size"]), "vg_free_bytes": int(c["vg"]["vg_free"]),
            "lv_bytes": int(c["lv"]["lv_size"]), "root_fs_bytes": st.f_blocks * st.f_frsize,
            "root_fs_free_bytes": st.f_bavail * st.f_frsize}


def growpart(c: dict[str, Any], dry: bool) -> bool:
    """True when growpart would change (dry) or changed the partition."""
    r = run(["growpart", *(["-N"] if dry else []), c["disk"]["name"], c["partnum"]], ok=(0, 1))
    out = r.stdout + r.stderr
    if r.returncode == 1:
        if "NOCHANGE" in out:
            return False
        raise Refused("growpart refused the partition")
    return True


def main(mode: str) -> dict[str, Any]:
    if not shutil.which("growpart"):
        raise Refused("growpart is not installed (cloud-guest-utils)")
    state = collect()
    c = chain(state)
    before = sizes(state, c)
    steps = []
    if growpart(c, dry=True):
        steps.append("growpart")
    # The PV can lag its partition even when the partition is already full-size.
    if steps or before["partition_bytes"] > before["pv_bytes"] + 4 * 1024 * 1024:
        steps.append("pvresize")
    if steps or before["vg_free_bytes"] > 0:
        steps.append("lvextend")
    # lvextend --resizefs can grow the LV and then fail on the filesystem; on a re-run the VG
    # has no free extents, so only this comparison sees it (review of 99666377). ext4's own
    # metadata takes a few percent, a lagging filesystem a whole grow's worth.
    elif before["root_fs_bytes"] < 0.9 * before["lv_bytes"]:
        steps.append("resize2fs")
    result: dict[str, Any] = {"mode": mode, "before": before, "steps": steps}
    if mode == "apply" and steps:
        # Exactly the planned steps, in order.
        if "growpart" in steps:
            growpart(c, dry=False)
        if "pvresize" in steps:
            run(["pvresize", c["pv"]["pv_name"]])
        if "lvextend" in steps:
            vg_free = int(_lvm_report(["vgs", "--readonly", "--reportformat", "json", "--units", "b", "--nosuffix",
                                       "-o", "vg_name,vg_free", c["vg"]["vg_name"]], "vg")[0]["vg_free"])
            if vg_free > 0:
                run(["lvextend", "--resizefs", "--extents", "+100%FREE", c["lv"]["lv_path"]])
        if "resize2fs" in steps:
            run(["resize2fs", c["lv"]["lv_path"]])
        after_state = collect()
        result["after"] = sizes(after_state, chain(after_state))
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(main(sys.argv[1] if len(sys.argv) > 1 else "plan")))
    except Refused as e:
        print(json.dumps({"refused": str(e)}))
        sys.exit(3)
