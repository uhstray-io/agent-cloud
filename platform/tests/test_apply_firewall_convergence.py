"""apply-firewall.yml converges: it tags every rule it adds and prunes tagged rules no longer declared.

The real play's tasks are lifted (from the SSH-CIDR refusal to the final report) and run
against a stub `ufw` and `podman` on PATH (ufw_stub.py), which keep the firewall in a JSON
file and reproduce the ufw 0.36.2 behaviour the playbook relies on. Left out: the sudo
resolver, fact gathering, the non-Linux skip, the apt install and its dry-run stop, and each
task's own `become` — none of them decides which rule is added or deleted. Runs through
harness_sandbox, so the ansible run cannot write outside the test's temp dir.
"""

import json
import re
import sys
from pathlib import Path

import harness_sandbox
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/apply-firewall.yml"
STUB = Path(__file__).with_name("ufw_stub.py")
FIRST = "Refuse to proceed without SSH allow CIDRs (anti-lockout)"
SKIPPED = {"Install ufw", "Dry run, ufw not installed: report", "Dry run, ufw not installed: stop this host"}
SSH = "192.0.2.0/24"
MUTATING = re.compile(r'^\["ufw", "(?!show|status)')


def _tag(family, port_proto, peer):
    return f"agent-cloud:{family}:{port_proto}:{peer}"


def _run(tmp_path: Path, host_vars: dict, groups: dict | None = None, *, state: dict | None = None,
         podman: dict | None = None, check: bool = False, drop: str | None = None):
    """Run the lifted play once; returns (CompletedProcess, state after)."""
    play, = yaml.safe_load(PLAYBOOK.read_text())
    names = [t.get("name") for t in play["tasks"]]
    tasks = [dict(t) for t in play["tasks"][names.index(FIRST):] if t.get("name") not in SKIPPED]
    for task in tasks:
        task.pop("become", None)
    (tmp_path / "play.yml").write_text(yaml.safe_dump(
        [{"hosts": "target", "gather_facts": False, "become": False, "vars": play["vars"], "tasks": tasks}]))
    inventory = {"all": {"hosts": {"target": {"ansible_connection": "local",
                                              "firewall_ssh_cidrs": [SSH], **host_vars}},
                         "children": {g: {"hosts": {h: {"ansible_host": a} for h, a in m.items()}}
                                      for g, m in (groups or {}).items()}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inventory))
    state_file, log = tmp_path / "ufw.json", tmp_path / "ufw.log"
    if state is not None:
        state_file.write_text(json.dumps(state))
    (tmp_path / "podman.json").write_text(json.dumps(podman or {}))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in ("ufw", "podman"):
        (bindir / name).write_text(f'#!/bin/sh\nSTUB_AS={name} exec "{sys.executable}" "{STUB}" "$@"\n')
        (bindir / name).chmod(0o755)
    env = harness_sandbox.env_for(tmp_path)
    # The stubs come first, so neither a real ufw nor a real podman can be reached.
    env.update(PATH=f"{bindir}:{env['PATH']}", UFW_STUB_STATE=str(state_file), UFW_STUB_LOG=str(log),
               PODMAN_STUB=str(tmp_path / "podman.json"))
    if drop:
        env["UFW_STUB_DROP"] = drop
    cmd = ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml")]
    result = harness_sandbox.run(cmd + (["--check"] if check else []), tmp_path, cwd=REPO, env=env)
    after = json.loads(state_file.read_text()) if state_file.exists() else None
    return result, after


def _rules(state):
    return dict(state["rules"])


def _changed(result) -> int:
    return int(re.search(r"target\s*:.*changed=(\d+)", result.stdout).group(1))


def _log(tmp_path):
    path = tmp_path / "ufw.log"
    return path.read_text().splitlines() if path.exists() else []


def test_every_declared_rule_kind_is_added_with_its_tag(tmp_path):
    r, state = _run(tmp_path, {
        "firewall_rootful": True,
        "firewall_allow_rules": [{"port": 8080, "from": "192.0.2.5"}],
        "firewall_allow_groups": [{"port": 53, "proto": "udp", "group": "members"}],
        "firewall_route_rules": [{"port": 9000, "from": "192.0.2.6"}],
        "firewall_upstream_source": "192.0.2.7",
        "firewall_deny_egress": [{"to": "198.51.100.1", "port": 8200}, {"to": "198.51.100.2"}],
    }, {"members": {"a": "192.0.2.10"}},
        podman={"ports": {"c1": ["443/tcp -> 0.0.0.0:443", "80/tcp -> 127.0.0.1:80"]},
                "bridges": {"podman": "podman1"}})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rules(state) == {
        f"allow from {SSH} to any port 22 proto tcp": _tag("in", "22/tcp", SSH),
        "allow from 192.0.2.5 to any port 8080 proto tcp": _tag("in", "8080/tcp", "192.0.2.5"),
        "allow from 192.0.2.10 to any port 53 proto udp": _tag("in", "53/udp", "192.0.2.10"),
        "route allow from 192.0.2.6 to any port 9000 proto tcp": _tag("route", "9000/tcp", "192.0.2.6"),
        "allow from 192.0.2.7 to any port 443 proto tcp": _tag("in", "443/tcp", "192.0.2.7"),
        "route allow from 192.0.2.7 to any port 443 proto tcp": _tag("route", "443/tcp", "192.0.2.7"),
        "allow in on podman1 to any port 53 proto udp": _tag("in-on", "53/udp", "podman1"),
        "allow in on podman1 to any port 53 proto tcp": _tag("in-on", "53/tcp", "podman1"),
        "deny out to 198.51.100.1 port 8200 proto tcp": _tag("out-deny", "8200/tcp", "198.51.100.1"),
        "deny out to 198.51.100.2": _tag("out-deny", "any", "198.51.100.2"),
    }
    assert state["active"] is True


def test_a_second_run_changes_nothing(tmp_path):
    host = {"firewall_allow_rules": [{"port": 8080, "from": "192.0.2.5"}],
            "firewall_deny_egress": [{"to": "198.51.100.1"}]}
    first, state = _run(tmp_path, host)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _changed(first) > 0
    second, again = _run(tmp_path, host)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _changed(second) == 0, second.stdout
    assert again == state


def test_a_stale_tagged_rule_of_every_kind_is_pruned(tmp_path):
    stale = [
        ["allow from 198.51.100.7 to any port 9000 proto tcp", _tag("in", "9000/tcp", "198.51.100.7")],
        ["route allow from 198.51.100.7 to any port 9001 proto tcp", _tag("route", "9001/tcp", "198.51.100.7")],
        ["allow in on podman9 to any port 53 proto udp", _tag("in-on", "53/udp", "podman9")],
        ["deny out to 198.51.100.8", _tag("out-deny", "any", "198.51.100.8")],
    ]
    r, state = _run(tmp_path, {}, state={"active": True, "rules": stale})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rules(state) == {f"allow from {SSH} to any port 22 proto tcp": _tag("in", "22/tcp", SSH)}
    deletes = [line for line in _log(tmp_path) if '"delete"' in line]
    # By spec, never by number: every delete names the rule, and the route one is `route delete`.
    assert len(deletes) == 4 and not any(re.search(r'"delete", "\d+"', line) for line in deletes)
    assert '["ufw", "route", "delete", "allow", "from", "198.51.100.7"' in "\n".join(deletes)


def test_untagged_rules_are_kept_and_reported_and_a_declared_one_is_adopted(tmp_path):
    untagged = [["allow 22/tcp", ""], ["allow from 203.0.113.9 to any port 5432 proto tcp", "set by hand"],
                [f"allow from {SSH} to any port 22 proto tcp", ""]]
    r, state = _run(tmp_path, {}, state={"active": True, "rules": untagged})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rules(state) == {
        "allow 22/tcp": "",
        "allow from 203.0.113.9 to any port 5432 proto tcp": "set by hand",
        # the untagged copy of a declared rule is retagged by the add, not duplicated
        f"allow from {SSH} to any port 22 proto tcp": _tag("in", "22/tcp", SSH),
    }
    report = r.stdout[r.stdout.index("TASK [Report untagged rules"):]
    assert "holds 2 untagged ufw rule(s)" in report
    assert "allow 22/tcp" in report and "allow from 203.0.113.9 to any port 5432 proto tcp" in report


def test_ssh_is_never_pruned_while_declared_and_an_old_cidr_goes(tmp_path):
    old = "198.51.100.0/24"
    r, state = _run(tmp_path, {}, state={"active": True, "rules": [
        [f"allow from {old} to any port 22 proto tcp", _tag("in", "22/tcp", old)],
        # a declared CIDR's SSH allow under a tag this playbook no longer writes
        [f"allow from {SSH} to any port 22 proto tcp", "agent-cloud:ssh:legacy"],
    ]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rules(state) == {f"allow from {SSH} to any port 22 proto tcp": _tag("in", "22/tcp", SSH)}


def test_a_declared_rule_that_did_not_land_stops_the_run_before_any_delete(tmp_path):
    # The new SSH allow silently fails to land while the old CIDR's tagged allow is stale:
    # pruning would leave ZERO SSH allows. The drift guard must refuse first.
    old = "198.51.100.0/24"
    before = {"active": True, "rules": [[f"allow from {old} to any port 22 proto tcp", _tag("in", "22/tcp", old)]]}
    r, state = _run(tmp_path, {}, state=before, drop=f"from {SSH} to any port 22")
    assert r.returncode != 0
    assert "Nothing was deleted" in r.stdout
    assert state["rules"] == before["rules"]
    assert not any('"delete"' in line for line in _log(tmp_path))


def test_check_mode_reports_the_plan_and_changes_nothing(tmp_path):
    before = {"active": False, "rules": [
        ["allow from 198.51.100.7 to any port 9000 proto tcp", _tag("in", "9000/tcp", "198.51.100.7")],
        ["allow 22/tcp", ""],
        [f"allow from {SSH} to any port 22 proto tcp", "agent-cloud:ssh:legacy"],
    ]}
    r, state = _run(tmp_path, {"firewall_allow_rules": [{"port": 8080, "from": "192.0.2.5"}]},
                    state=before, check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert state == before
    assert not [line for line in _log(tmp_path) if MUTATING.search(line)], _log(tmp_path)
    plan = r.stdout[r.stdout.index("TASK [Report the convergence plan]"):]
    assert _tag("in", "8080/tcp", "192.0.2.5") in plan and _tag("in", "22/tcp", SSH) in plan
    assert "allow from 198.51.100.7 to any port 9000 proto tcp" in plan
    # the declared SSH allow under an old tag is retagged by a real run, never planned for deletion
    delete = re.search(r'"delete_stale_tagged": \[(.*?)\]', plan, re.S).group(1)
    assert "port 22" not in delete
    assert '"would_set_defaults_or_enable": true' in plan


def test_narrowing_a_subnet_rule_to_group_rules_removes_only_the_tagged_subnet_rule(tmp_path):
    subnet = "192.0.2.0/24"
    wide = {"firewall_allow_rules": [{"port": 53, "proto": p, "from": subnet} for p in ("udp", "tcp")]}
    first, _ = _run(tmp_path, wide, state={"active": True, "rules": [["allow 8443/tcp", ""]]})
    assert first.returncode == 0, first.stdout + first.stderr
    narrow = {"firewall_allow_groups": [{"port": 53, "proto": p, "group": "members"} for p in ("udp", "tcp")]}
    members = {"members": {"a": "192.0.2.10", "b": "192.0.2.11"}}
    r, state = _run(tmp_path, narrow, members)
    assert r.returncode == 0, r.stdout + r.stderr
    expected = {"allow 8443/tcp": "", f"allow from {SSH} to any port 22 proto tcp": _tag("in", "22/tcp", SSH)}
    for host in ("192.0.2.10", "192.0.2.11"):
        for p in ("udp", "tcp"):
            expected[f"allow from {host} to any port 53 proto {p}"] = _tag("in", f"53/{p}", host)
    assert _rules(state) == expected
