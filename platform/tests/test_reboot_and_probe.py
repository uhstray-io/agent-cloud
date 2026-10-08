"""reboot-host.yml and probe-reachability.yml: the supported reboot path and the LAN reachability
probe (production-internal-ca tasks 3.3 and 3.4).

Both run on localhost through harness_sandbox. The probe connects to a real listener this test
opens and to a port nothing listens on. The reboot is only ever exercised in check mode and
through its refusals: no test reboots anything.
"""

import contextlib
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


def _run(tmp: Path, playbook: Path, groups: dict, extra: dict, check: bool = False,
         limit: str | None = None) -> subprocess.CompletedProcess:
    local = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable}
    inv = {"all": {"children": {g: {"hosts": {h: {**local, **hv} for h, hv in hosts.items()}}
                                for g, hosts in groups.items()}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    # One JSON extra-vars object, the way Semaphore passes a survey: `-e k=v` would split a
    # JSON value on its spaces.
    args = ["-e", json.dumps(extra)] if extra else []
    cmd = ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(playbook), *args, *(["--check"] if check else []),
           *(["--limit", limit] if limit else [])]
    return harness_sandbox.run(cmd, tmp, cwd=REPO, env=harness_sandbox.env_for(tmp))


@contextlib.contextmanager
def _listen(family, addr):
    """An accepting listener on addr; yields its port."""
    srv = socket.socket(family)
    srv.bind((addr, 0))
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
    try:
        yield srv.getsockname()[1]
    finally:
        stop.set()
        srv.close()


@pytest.fixture
def ports():
    """(an open port with a listener, a port with none)."""
    with _listen(socket.AF_INET, "127.0.0.1") as opened:
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        closed = free.getsockname()[1]
        free.close()
        yield opened, closed


@pytest.fixture
def ipv6_port():
    """A listener on the IPv6 loopback, or a skip where the machine has none."""
    try:
        probe = socket.socket(socket.AF_INET6)
        probe.bind(("::1", 0))
        probe.close()
    except OSError as e:
        pytest.skip(f"no IPv6 loopback here: {e}")
    with _listen(socket.AF_INET6, "::1") as port:
        yield port


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


def test_an_ipv6_target_is_probed_on_its_own_family(tmp_path, ipv6_port):
    # The first version made an AF_INET socket whatever the address, so an IPv6 target could
    # only ever come back `closed`.
    groups = {"prober_svc": {"prober": {}}, "target_svc": {"target": {"ansible_host": "::1"}}}
    r = _probe(tmp_path, [{"port": ipv6_port, "expect": "open"}], groups)
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{ipv6_port} open (expected open)" in r.stdout


def test_a_target_that_does_not_resolve_is_a_probe_error_not_a_closed_port(tmp_path):
    # `expect: closed` against a name that resolves to nothing used to pass: every nonzero
    # connect result counted as closed. RFC 6761 reserves .invalid, so it never resolves.
    groups = {"prober_svc": {"prober": {}}, "target_svc": {"target": {"ansible_host": "no-such-host.invalid"}}}
    r = _probe(tmp_path, [{"port": 22, "expect": "closed"}], groups)
    assert r.returncode != 0, r.stdout
    assert "could not be probed" in r.stdout and "22: probe error: cannot resolve" in r.stdout, r.stdout
    assert "22 closed" not in r.stdout and "was closed" not in r.stdout


def _probe_script() -> str:
    plays = playbook_yaml.load(PROBE)
    task = next(t for p in plays for t in p.get("tasks", []) if t.get("name") == "Connect to each declared port")
    return task["vars"]["_probe_script"]


# A firewall's default deny (ufw: `default deny (incoming)`) DROPS the SYN, so from the LAN a
# firewalled port is a connect TIMEOUT, not a refusal — the case every "a LAN host cannot reach
# X" gate actually meets in production, and one a loopback test cannot produce. The connect is
# patched to raise what the kernel would; an unreachable host must stay an error, never `closed`.
@pytest.mark.parametrize("raised,rc,verdict", [
    ("TimeoutError('timed out')", 1, "closed"),
    ("OSError(errno.EHOSTUNREACH, 'No route to host')", 2, "probe error"),
], ids=["dropped-is-closed", "unreachable-is-error"])
def test_the_probe_reads_a_dropped_syn_as_closed_and_an_unreachable_host_as_an_error(raised, rc, verdict):
    patch = (f"import errno, socket, sys\n"
             f"def _connect(self, addr): raise {raised}\n"
             f"socket.socket.connect = _connect\n"
             f"sys.argv = ['probe', '127.0.0.1', '9', '1']\n")
    r = subprocess.run([sys.executable, "-c", patch + _probe_script()], capture_output=True, text=True,
                       timeout=30, check=False)
    assert r.returncode == rc, r.stdout + r.stderr
    assert verdict in r.stdout + r.stderr


@pytest.mark.parametrize("decl,extra", [
    ([{"port": 0, "expect": "closed"}], {}),
    ([{"port": 65536, "expect": "closed"}], {}),
    ([{"port": "ssh", "expect": "closed"}], {}),
    ([{"port": True, "expect": "closed"}], {}),
    ([{"port": 22.5, "expect": "closed"}], {}),
    ([{"expect": "closed"}], {}),
    ([{"port": 22, "expect": "closed"}], {"probe_timeout": 0}),
    ([{"port": 22, "expect": "closed"}], {"probe_timeout": -1}),
    ([{"port": 22, "expect": "closed"}], {"probe_timeout": "soon"}),
], ids=["port-0", "port-65536", "port-name", "port-bool", "port-float", "port-missing",
        "timeout-0", "timeout-negative", "timeout-word"])
def test_an_invalid_port_or_timeout_is_refused_before_probing(tmp_path, decl, extra):
    r = _probe(tmp_path, decl, **extra)
    assert r.returncode != 0 and "Pass -e probe_from" in r.stdout, r.stdout
    assert "Connect to each declared port" not in r.stdout


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


# `--limit <host>` leaves localhost out, so Ansible skips the guard play outright: the reboot
# play has to refuse on its own, before anything resolves a password or reboots.
@pytest.mark.parametrize("groups,confirm", [
    ({"ca_svc": {"ca": {}}}, None),
    ({"ca_svc": {"ca": {}}}, "other"),
    ({"ca_svc": {"ca": {}, "ca2": {}}}, "ca"),
], ids=["limit-no-confirm", "limit-wrong-host", "limit-one-of-two"])
def test_a_limited_run_cannot_route_around_the_guard(tmp_path, groups, confirm):
    extra = {"target_service": "ca_svc", **({"confirm_reboot": confirm} if confirm else {})}
    r = _run(tmp_path, REBOOT, groups, extra, check=True, limit="ca")
    assert r.returncode != 0 and "Refusing on ca: target_service 'ca_svc'" in r.stdout, r.stdout
    assert "skipping: no hosts matched" in r.stdout, r.stdout  # the premise: the guard play never ran
    assert "Resolve the sudo password" not in r.stdout and "would be rebooted" not in r.stdout


def test_a_limited_confirmed_dry_run_still_passes(tmp_path):
    r = _run(tmp_path, REBOOT, {"ca_svc": {"ca": {}}}, {"target_service": "ca_svc", "confirm_reboot": "ca"},
             check=True, limit="ca")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would be rebooted (check mode)" in r.stdout


def test_the_in_play_guard_comes_before_anything_that_escalates():
    tasks = playbook_yaml.plays(REBOOT)[1]["tasks"]
    assert "ansible.builtin.assert" in tasks[0], tasks[0]["name"]
    assert "include_tasks" in str(tasks[1]) and "resolve-become-password" in str(tasks[1])


def test_the_reboot_itself_never_runs_in_check_mode_and_is_the_only_escalation():
    tasks = playbook_yaml.plays(REBOOT)[1]["tasks"]
    reboot = next(t for t in tasks if "ansible.builtin.reboot" in t)
    assert reboot["when"] == "not ansible_check_mode" and reboot.get("become") is True
    assert [t["name"] for t in tasks if t.get("become")] == [reboot["name"]]
