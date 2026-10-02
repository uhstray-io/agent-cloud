"""reboot-host.yml and probe-reachability.yml: the supported reboot path and the LAN reachability
probe (production-internal-ca tasks 3.3 and 3.4).

Both run on localhost through harness_sandbox. The probe connects to a real listener this test
opens and to a port nothing listens on. The reboot is only ever exercised in check mode and
through its refusals: no test reboots anything.
"""

import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

REPO = playbook_yaml.REPO
PROBE = REPO / "platform/playbooks/probe-reachability.yml"
REBOOT = REPO / "platform/playbooks/reboot-host.yml"


def _run(tmp: Path, playbook: Path, groups: dict, extra: dict, check: bool = False) -> subprocess.CompletedProcess:
    local = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable}
    inv = {"all": {"children": {g: {"hosts": {h: {**local, **hv} for h, hv in hosts.items()}}
                                for g, hosts in groups.items()}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    # One JSON extra-vars object, the way Semaphore passes a survey: `-e k=v` would split a
    # JSON value on its spaces.
    args = ["-e", json.dumps(extra)] if extra else []
    cmd = ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(playbook), *args, *(["--check"] if check else [])]
    return harness_sandbox.run(cmd, tmp, cwd=REPO, env=harness_sandbox.env_for(tmp))


@pytest.fixture
def ports():
    """(an open port with a listener, a port with none)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                srv.accept()[0].close()
            except OSError:
                pass
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    closed = free.getsockname()[1]
    free.close()
    yield srv.getsockname()[1], closed
    stop.set()
    srv.close()


def _probe(tmp, decl, groups=None, **extra):
    groups = groups or {"prober_svc": {"prober": {}}, "target_svc": {"target": {"ansible_host": "127.0.0.1"}}}
    return _run(tmp, PROBE, groups, {"probe_from": "prober_svc", "probe_to": "target",
                                     "probe_ports_json": json.dumps(decl), "probe_timeout": 2, **extra})


def test_ports_matching_their_declaration_pass(tmp_path, ports):
    opened, closed = ports
    r = _probe(tmp_path, [{"port": opened, "expect": "open"}, {"port": closed, "expect": "closed"}])
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{opened} open (expected open)" in r.stdout and f"{closed} closed (expected closed)" in r.stdout


def test_a_port_that_answers_when_declared_closed_fails_naming_it(tmp_path, ports):
    opened, closed = ports
    r = _probe(tmp_path, [{"port": opened, "expect": "closed"}, {"port": closed, "expect": "closed"}])
    assert r.returncode != 0 and f"{opened} was open, expected closed" in r.stdout, r.stdout
    assert f"{closed} was" not in r.stdout


def test_a_closed_port_declared_open_fails(tmp_path, ports):
    _, closed = ports
    r = _probe(tmp_path, [{"port": closed, "expect": "open"}])
    assert r.returncode != 0 and f"{closed} was closed, expected open" in r.stdout, r.stdout


@pytest.mark.parametrize("decl,groups,extra", [
    ([], None, {}),
    ([{"port": 22, "expect": "maybe"}], None, {}),
    ([{"port": 22, "expect": "closed"}], {"prober_svc": {"target": {}}, "target_svc": {}}, {}),
    ([{"port": 22, "expect": "closed"}], None, {"probe_from": "nothing_svc"}),
    ([{"port": 22, "expect": "closed"}], None, {"probe_to": "nobody"}),
], ids=["no-ports", "bad-expect", "probe-itself", "empty-group", "unknown-target"])
def test_an_incomplete_probe_is_refused(tmp_path, decl, groups, extra):
    r = _probe(tmp_path, decl, groups, **extra)
    assert r.returncode != 0 and "Pass -e probe_from" in r.stdout, r.stdout


# ── Reboot ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("groups,confirm", [
    ({"ca_svc": {"ca": {}}}, None),
    ({"ca_svc": {"ca": {}}}, "other"),
    ({"ca_svc": {"ca": {}, "ca2": {}}}, "ca"),
    ({"ca_svc": {}}, "ca"),
], ids=["no-confirm", "wrong-host", "two-hosts", "empty-group"])
def test_a_reboot_not_of_exactly_one_named_host_is_refused(tmp_path, groups, confirm):
    extra = {"target_service": "ca_svc", **({"confirm_reboot": confirm} if confirm else {})}
    r = _run(tmp_path, REBOOT, groups, extra, check=True)
    assert r.returncode != 0 and "Refusing: target_service 'ca_svc'" in r.stdout, r.stdout


def test_a_confirmed_dry_run_reports_and_reboots_nothing(tmp_path):
    r = _run(tmp_path, REBOOT, {"ca_svc": {"ca": {}}}, {"target_service": "ca_svc", "confirm_reboot": "ca"},
             check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would be rebooted (check mode)" in r.stdout


def test_the_reboot_itself_never_runs_in_check_mode_and_is_the_only_escalation():
    tasks = yaml.safe_load(REBOOT.read_text())[1]["tasks"]
    reboot = next(t for t in tasks if "ansible.builtin.reboot" in t)
    assert reboot["when"] == "not ansible_check_mode" and reboot.get("become") is True
    assert [t["name"] for t in tasks if t.get("become")] == [reboot["name"]]
