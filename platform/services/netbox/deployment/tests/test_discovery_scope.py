"""Local discovery never scans outside local-dev (platform/netbox-local). RFC 5737 ranges."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "lib" / "discovery_scope.py"
LOCAL = ["192.0.2.0/24", "198.51.100.0/25"]


def _run(allowed, targets):
    return subprocess.run(
        [sys.executable, str(SCRIPT), json.dumps(allowed), json.dumps(targets)],
        text=True, capture_output=True, check=False,
    )


def test_targets_inside_local_networks_pass():
    proc = _run(LOCAL, ["192.0.2.0/24", "198.51.100.16/28", "192.0.2.7"])
    assert proc.returncode == 0, proc.stderr


def test_a_target_outside_is_refused_and_named():
    proc = _run(LOCAL, ["192.0.2.0/24", "203.0.113.0/24"])
    assert proc.returncode == 1
    assert "203.0.113.0/24" in proc.stderr
    assert "192.0.2.0/24'" not in proc.stderr.split(":")[-1]


def test_a_supernet_of_a_local_network_is_refused():
    # A /16 that CONTAINS a local /24 is not inside it: that scan would leave local-dev.
    proc = _run(LOCAL, ["192.0.0.0/16"])
    assert proc.returncode == 1


def test_nothing_allowed_refuses_everything():
    assert _run([], ["192.0.2.1"]).returncode == 1


def test_bad_input_is_an_error_not_a_pass():
    assert _run(LOCAL, ["not-an-address"]).returncode == 2
