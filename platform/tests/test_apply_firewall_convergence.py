"""apply-firewall.yml converges: it tags every rule it adds and prunes tagged rules no longer declared.

The real play's tasks are lifted (from the SSH-CIDR refusal to the final report) and run
against a stub `ufw` and `podman` on PATH (ufw_stub.py), which keep the firewall in a JSON
file and reproduce the ufw 0.36.2 behaviour the playbook relies on. Left out: the sudo
resolver, fact gathering, the non-Linux skip, the apt install and its dry-run stop, and each
task's own `become` — none of them decides which rule is added or deleted. Runs through
harness_sandbox, so the ansible run cannot write outside the test's temp dir. Cases that
differ only in their inputs share one run over several hosts (_run_many), each host with
its own stub state, since every ansible-playbook spawn costs seconds.
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
CONTROLLER = {"firewall_controller_cidr": SSH}
MUTATING = re.compile(r'^\["ufw", "(?!show|status)')


def _tag(family, port_proto, peer):
    return f"agent-cloud:{family}:{port_proto}:{peer}"


def _run_many(tmp_path: Path, hosts: dict, groups: dict | None = None, *, check: bool = False,
              drop: str | None = None, collateral: str | None = None):
    """Run the lifted play ONCE over several firewalled hosts; returns (CompletedProcess, {host: state}).

    `hosts` maps a host name to {"vars": ..., "state": ..., "podman": ...}. Each host keeps its
    own stub state, log and podman fixture (play-level `environment` from its host vars), so
    cases that differ only in their inputs share one ansible-playbook spawn. A host that fails
    an assert stops alone; the others run on.
    """
    play, = yaml.safe_load(PLAYBOOK.read_text())
    names = [t.get("name") for t in play["tasks"]]
    tasks = [dict(t) for t in play["tasks"][names.index(FIRST):] if t.get("name") not in SKIPPED]
    for task in tasks:
        task.pop("become", None)
    stub_env = {"UFW_STUB_STATE": "{{ ufw_stub_state }}", "UFW_STUB_LOG": "{{ ufw_stub_log }}",
                "PODMAN_STUB": "{{ podman_stub }}"}
    (tmp_path / "play.yml").write_text(yaml.safe_dump(
        [{"hosts": "targets", "gather_facts": False, "become": False, "vars": play["vars"],
          "environment": stub_env, "tasks": tasks}]))
    targets = {}
    for name, spec in hosts.items():
        files = _files(tmp_path, name, len(hosts) == 1)
        files["state"].parent.mkdir(parents=True, exist_ok=True)
        if spec.get("state") is not None:
            files["state"].write_text(json.dumps(spec["state"]))
        files["podman"].write_text(json.dumps(spec.get("podman") or {}))
        targets[name] = {"ansible_connection": "local", "firewall_ssh_cidrs": [SSH], **spec.get("vars", {}),
                         "ufw_stub_state": str(files["state"]), "ufw_stub_log": str(files["log"]),
                         "podman_stub": str(files["podman"])}
    inventory = {"all": {"children": {"targets": {"hosts": targets},
                                      **{g: {"hosts": {h: {"ansible_host": a} for h, a in m.items()}}
                                         for g, m in (groups or {}).items()}}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inventory))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in ("ufw", "podman"):
        (bindir / name).write_text(f'#!/bin/sh\nSTUB_AS={name} exec "{sys.executable}" "{STUB}" "$@"\n')
        (bindir / name).chmod(0o755)
    env = harness_sandbox.env_for(tmp_path)
    # The stubs come first, so neither a real ufw nor a real podman can be reached.
    # The lifted play sits outside platform/playbooks; its filters come from the repository
    # ansible.cfg, which applies because the run's cwd is the repository root.
    env.update(PATH=f"{bindir}:{env['PATH']}")
    if drop:
        env["UFW_STUB_DROP"] = drop
    if collateral:
        env["UFW_STUB_COLLATERAL"] = collateral
    cmd = ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml")]
    result = harness_sandbox.run(cmd + (["--check"] if check else []), tmp_path, cwd=REPO, env=env)
    after = {}
    for name in hosts:
        state_file = _files(tmp_path, name, len(hosts) == 1)["state"]
        after[name] = json.loads(state_file.read_text()) if state_file.exists() else None
    return result, after


def _files(tmp_path: Path, host: str, single: bool) -> dict:
    """A single host keeps its files at the top of tmp_path (a second run reuses them)."""
    base = tmp_path if single else tmp_path / host
    return {"state": base / "ufw.json", "log": base / "ufw.log", "podman": base / "podman.json"}


def _run(tmp_path: Path, host_vars: dict, groups: dict | None = None, *, state: dict | None = None,
         podman: dict | None = None, check: bool = False, drop: str | None = None,
         collateral: str | None = None):
    """Run the lifted play once against the one host `target`; returns (CompletedProcess, state after)."""
    result, after = _run_many(tmp_path, {"target": {"vars": host_vars, "state": state, "podman": podman}},
                              groups, check=check, drop=drop, collateral=collateral)
    return result, after["target"]


def _rules(state):
    return {rule[0]: rule[1] for rule in state["rules"]}


def _changed(result) -> int:
    return int(re.search(r"target\s*:.*changed=(\d+)", result.stdout).group(1))


def _log(tmp_path, host: str | None = None):
    path = (tmp_path / host if host else tmp_path) / "ufw.log"
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
    # a run that changed the firewall reports the state it left, not the one it found
    assert "Status: active" in r.stdout[r.stdout.index("TASK [Report]"):]


def test_a_second_run_changes_nothing(tmp_path):
    host = {"firewall_allow_rules": [{"port": 8080, "from": "192.0.2.5"}],
            "firewall_deny_egress": [{"to": "198.51.100.1"}]}
    first, state = _run(tmp_path, host)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _changed(first) > 0
    (tmp_path / "ufw.log").unlink()
    second, again = _run(tmp_path, host)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _changed(second) == 0, second.stdout
    assert again == state
    # Converged: the two reads taken before adding are the whole run. No add, no re-read,
    # no final status capture (the report reuses the first read).
    ufw_calls = [line for line in _log(tmp_path) if line.startswith('["ufw"')]
    assert ufw_calls == ['["ufw", "show", "added"]', '["ufw", "status", "verbose"]']
    assert "Status: active" in second.stdout[second.stdout.index("TASK [Report]"):]


def test_a_rule_from_the_whole_address_space_converges(tmp_path):
    # ufw prints 0.0.0.0/0 as `any` (the short form `allow 8080/tcp`); compared literally,
    # the declared rule would never match its stored form and fail the drift guard.
    host = {"firewall_allow_rules": [{"port": 8080, "from": "0.0.0.0/0"}]}
    first, state = _run(tmp_path, host)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "allow 8080/tcp" in _rules(state)
    second, again = _run(tmp_path, host, state=state)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _changed(second) == 0, second.stdout
    assert again == state


def test_a_rule_retagged_by_the_add_is_not_then_pruned_as_stale(tmp_path):
    # Stored under the tag an older spelling of its declaration produced. The add retags it;
    # the prune plan must read the rules AFTER the add, or it deletes the declared rule by spec.
    spec = "allow from 192.0.2.5 to any port 8080 proto tcp"
    before = {"active": True, "rules": [[f"allow from {SSH} to any port 22 proto tcp", _tag("in", "22/tcp", SSH)],
                                        [spec, _tag("in", "8080/tcp", "192.0.2.5/32")]]}
    r, state = _run(tmp_path, {"firewall_allow_rules": [{"port": 8080, "from": "192.0.2.5"}]}, state=before)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rules(state)[spec] == _tag("in", "8080/tcp", "192.0.2.5")
    assert not any('"delete"' in line for line in _log(tmp_path))


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
    r, state = _run(tmp_path, CONTROLLER, state={"active": True, "rules": [
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
    r, state = _run(tmp_path, CONTROLLER, state=before, drop=f"from {SSH} to any port 22")
    assert r.returncode != 0
    assert "Declared rules not found" in r.stdout and "Nothing was deleted" in r.stdout
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


def test_narrowing_ssh_without_the_controller_cidr_is_refused_before_any_delete(tmp_path):
    old = "198.51.100.0/24"
    before = {"active": True, "rules": [[f"allow from {old} to any port 22 proto tcp", _tag("in", "22/tcp", old)]]}
    for check in (True, False):
        r, state = _run(tmp_path, {}, state=before, check=check)
        assert r.returncode != 0
        assert f"would prune SSH from {old} on target" in r.stdout and "declare firewall_controller_cidr" in r.stdout
        assert f"allow from {old} to any port 22 proto tcp" in _rules(state)
        assert not any('"delete"' in line for line in _log(tmp_path))


def test_empty_detection_holds_the_upstream_rules_unless_told_it_is_real(tmp_path):
    upstream = "192.0.2.7"
    stale = [["allow from 192.0.2.7 to any port 443 proto tcp", _tag("in", "443/tcp", upstream)],
             ["route allow from 192.0.2.7 to any port 443 proto tcp", _tag("route", "443/tcp", upstream)],
             ["allow from 198.51.100.7 to any port 9000 proto tcp", _tag("in", "9000/tcp", "198.51.100.7")]]
    host = {"firewall_upstream_source": upstream}
    case = {"state": {"active": True, "rules": stale}, "podman": {"ports": {}}}
    r, states = _run_many(tmp_path, {"held": {"vars": host, **case},
                                     "pruned": {"vars": {**host, "firewall_prune_when_detection_empty": True},
                                                **case}})
    assert r.returncode == 0, r.stdout + r.stderr
    rules = _rules(states["held"])
    assert stale[0][0] in rules and stale[1][0] in rules and stale[2][0] not in rules
    assert "Port detection on held found no published ports, so 2 stale rule(s)" in r.stdout
    assert "Port detection on pruned" not in r.stdout
    assert _rules(states["pruned"]) == {f"allow from {SSH} to any port 22 proto tcp": _tag("in", "22/tcp", SSH)}


def test_ssh_cidrs_spelled_non_canonically_are_matched_as_ufw_stores_them(tmp_path):
    # ufw stores 192.0.2.5/32 as 192.0.2.5 and 198.51.100.77/24 as 198.51.100.0/24. Compared
    # raw, both would be planned for deletion and refused (no controller CIDR declared).
    before = {"active": True, "rules": [
        ["allow from 192.0.2.5 to any port 22 proto tcp", "agent-cloud:ssh:legacy"],
        ["allow from 198.51.100.0/24 to any port 22 proto tcp", "agent-cloud:ssh:legacy"],
    ]}
    r, state = _run(tmp_path, {"firewall_ssh_cidrs": ["192.0.2.5/32", "198.51.100.77/24"]},
                    state=before, check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert state == before
    plan = r.stdout[r.stdout.index("TASK [Report the convergence plan]"):]
    assert re.search(r'"delete_stale_tagged": \[\]', plan), plan


def test_the_post_prune_check_fails_when_a_delete_took_an_ssh_allow(tmp_path):
    # The declared SSH allow is in place; the stub's delete of the stale rule also takes it.
    # On the second host a hand rule still carries the SSH allow's tag after the delete: the
    # check reads tag AND spec, so that tag alone does not pass it.
    rules = [[f"allow from {SSH} to any port 22 proto tcp", _tag("in", "22/tcp", SSH)],
             ["allow from 198.51.100.8 to any port 9001 proto tcp", _tag("in", "9001/tcp", "198.51.100.8")]]
    masked = [["allow from 203.0.113.9 to any port 5432 proto tcp", _tag("in", "22/tcp", SSH)], *rules]
    r, _ = _run_many(tmp_path, {"plain": {"state": {"active": True, "rules": rules}},
                                "masked": {"state": {"active": True, "rules": masked}}}, collateral="port 22")
    assert r.returncode != 0
    for host in ("plain", "masked"):
        assert f"After pruning, {host} is missing a declared SSH allow" in r.stdout, host
    assert "TASK [Enable UFW]" not in r.stdout


def test_a_dual_family_rule_stored_for_one_family_only_is_still_pruned(tmp_path):
    before = {"active": True, "rules": [
        ["allow in on podman9 to any port 53 proto udp", _tag("in-on", "53/udp", "podman9"), "v4"]]}
    r, state = _run(tmp_path, {}, state=before)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rules(state) == {f"allow from {SSH} to any port 22 proto tcp": _tag("in", "22/tcp", SSH)}


SSH_SPELLINGS = [
    "allow from 198.51.100.0/24 to any port 22",                 # proto any: ufw prints no proto
    "allow from 198.51.100.0/24 to any port 22,2222 proto tcp",  # multiport
    "allow from 198.51.100.0/24 to any port 20:30 proto tcp",    # range
    "allow from 198.51.100.0/24",                                # no port clause: every port
    "route allow from 198.51.100.0/24 to any port 22 proto tcp",
]


def test_a_stale_rule_that_admits_ssh_in_any_spelling_is_refused(tmp_path):
    # One host per spelling, one run: every host must refuse on its own. The spellings
    # themselves are unit-tested in test_ufw_rules_filter.py; this proves the wiring.
    hosts = {f"h{i}": {"state": {"active": True, "rules": [[spec, "agent-cloud:in:9999/tcp:198.51.100.0/24"]]}}
             for i, spec in enumerate(SSH_SPELLINGS)}
    r, states = _run_many(tmp_path, hosts)
    assert r.returncode != 0
    for i, spec in enumerate(SSH_SPELLINGS):
        host = f"h{i}"
        assert f"would prune SSH from 198.51.100.0/24 on {host};" in r.stdout, (spec, r.stdout)
        assert spec in _rules(states[host]), spec
        assert not any('"delete"' in line for line in _log(tmp_path, host)), spec


def test_a_stale_udp_only_port_22_rule_is_pruned_without_the_controller_cidr(tmp_path):
    spec = "allow from 198.51.100.0/24 to any port 22 proto udp"
    r, state = _run(tmp_path, {}, state={"active": True, "rules": [[spec, _tag("in", "22/udp", "198.51.100.0/24")]]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert spec not in _rules(state)


def test_the_controller_cidr_is_compared_in_the_spelling_ufw_stores(tmp_path):
    # Declared as 192.0.2.5/32, controller given as 192.0.2.5: one source, so the old CIDR's
    # SSH allow may be pruned.
    old = "198.51.100.0/24"
    before = {"active": True, "rules": [[f"allow from {old} to any port 22 proto tcp", _tag("in", "22/tcp", old)]]}
    r, state = _run(tmp_path, {"firewall_ssh_cidrs": ["192.0.2.5/32"], "firewall_controller_cidr": "192.0.2.5"},
                    state=before)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rules(state) == {"allow from 192.0.2.5 to any port 22 proto tcp": _tag("in", "22/tcp", "192.0.2.5")}


def test_rules_tagged_in_a_declared_spelling_are_retagged_then_converge(tmp_path):
    # Tags once carried the peer as DECLARED; they now carry it as ufw stores it. A host whose
    # rules were tagged by the earlier version (`.../32`, a CIDR with host bits set) holds
    # tags this run no longer declares. They are retagged by the add — not pruned before it,
    # SSH included, with no controller CIDR declared — and the next run changes nothing.
    ssh, web, route = ("192.0.2.5/32", "198.51.100.77/24", "192.0.2.6/32")
    host = {"firewall_ssh_cidrs": [ssh], "firewall_rootful": True, "firewall_detect_ports": False,
            "firewall_allow_rules": [{"port": 8080, "from": web}],
            "firewall_route_rules": [{"port": 9000, "from": route}],
            "firewall_deny_egress": [{"to": "198.51.100.1/32", "port": 8200}]}
    specs = {"allow from 192.0.2.5 to any port 22 proto tcp": ("in", "22/tcp", "192.0.2.5"),
             "allow from 198.51.100.0/24 to any port 8080 proto tcp": ("in", "8080/tcp", "198.51.100.0/24"),
             "route allow from 192.0.2.6 to any port 9000 proto tcp": ("route", "9000/tcp", "192.0.2.6"),
             "deny out to 198.51.100.1 port 8200 proto tcp": ("out-deny", "8200/tcp", "198.51.100.1")}
    declared = {"allow from 192.0.2.5 to any port 22 proto tcp": ssh,
                "allow from 198.51.100.0/24 to any port 8080 proto tcp": web,
                "route allow from 192.0.2.6 to any port 9000 proto tcp": route,
                "deny out to 198.51.100.1 port 8200 proto tcp": "198.51.100.1/32"}
    before = {"active": True, "rules": [[spec, _tag(f, pp, declared[spec])] for spec, (f, pp, _) in specs.items()]}
    first, state = _run(tmp_path, host, state=before)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _changed(first) > 0
    assert _rules(state) == {spec: _tag(*fields) for spec, fields in specs.items()}
    assert not any('"delete"' in line for line in _log(tmp_path)), _log(tmp_path)
    (tmp_path / "ufw.log").unlink()
    second, again = _run(tmp_path, host, state=state)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _changed(second) == 0, second.stdout
    assert again == state
    assert [line for line in _log(tmp_path) if line.startswith('["ufw"')] == [
        '["ufw", "show", "added"]', '["ufw", "status", "verbose"]']


def test_a_dry_run_over_older_tags_plans_no_delete_of_a_declared_rule(tmp_path):
    # In --check there is no add and no re-read, so a declared rule still carrying an older
    # tag spelling must not appear in the delete plan, and a static port-22 allow from a
    # source outside firewall_ssh_cidrs must not trip the anti-orphan guard (review of #348).
    host = {"firewall_ssh_cidrs": ["192.0.2.5"], "firewall_detect_ports": False,
            "firewall_allow_rules": [{"port": 8080, "from": "198.51.100.77/24"},
                                     {"port": 22, "from": "203.0.113.9/32"}]}
    before = {"active": True, "rules": [
        ["allow from 192.0.2.5 to any port 22 proto tcp", _tag("in", "22/tcp", "192.0.2.5")],
        ["allow from 198.51.100.0/24 to any port 8080 proto tcp", _tag("in", "8080/tcp", "198.51.100.77/24")],
        ["allow from 203.0.113.9 to any port 22 proto tcp", _tag("in", "22/tcp", "203.0.113.9/32")]]}
    r, state = _run(tmp_path, host, state=before, check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert state == before
    plan = r.stdout[r.stdout.index("TASK [Report the convergence plan]"):]
    assert re.search(r'"delete_stale_tagged": \[\]', plan), plan


def test_an_upstream_declared_non_canonically_still_holds_its_rules_on_empty_detection(tmp_path):
    # The held set is recognised by peer; the tag's peer is ufw's spelling (whichever version
    # wrote it), so the declared upstream is compared in that spelling too.
    stale = [["allow from 192.0.2.7 to any port 443 proto tcp", _tag("in", "443/tcp", "192.0.2.7")],
             ["route allow from 192.0.2.7 to any port 443 proto tcp", _tag("route", "443/tcp", "192.0.2.7/32")]]
    r, state = _run(tmp_path, {"firewall_upstream_source": "192.0.2.7/32"},
                    state={"active": True, "rules": stale}, podman={"ports": {}})
    assert r.returncode == 0, r.stdout + r.stderr
    assert stale[0][0] in _rules(state) and stale[1][0] in _rules(state)
    assert "found no published ports, so 2 stale rule(s)" in r.stdout


def test_a_lost_ip_family_of_a_bridge_rule_is_restored_and_a_whole_one_is_unchanged(tmp_path):
    # `show added` prints a dual-family rule once whichever halves are stored, so presence by
    # tag cannot see a lost half. The rule is re-added every run: the lost half comes back,
    # and on a host holding both ufw only skips, which is not a change.
    rootful = {"firewall_rootful": True, "firewall_detect_ports": False}
    bridge = {"bridges": {"podman": "podman1"}}
    whole = [[f"allow from {SSH} to any port 22 proto tcp", _tag("in", "22/tcp", SSH)],
             ["allow in on podman1 to any port 53 proto udp", _tag("in-on", "53/udp", "podman1")],
             ["allow in on podman1 to any port 53 proto tcp", _tag("in-on", "53/tcp", "podman1")]]
    lost = [list(r) for r in whole]
    lost[1].append("v4")  # the v6 half of the udp rule is gone
    r, states = _run_many(tmp_path, {
        "lost": {"vars": rootful, "podman": bridge, "state": {"active": True, "rules": lost}},
        "whole": {"vars": rootful, "podman": bridge, "state": {"active": True, "rules": whole}}})
    assert r.returncode == 0, r.stdout + r.stderr
    assert states["lost"]["rules"] == whole and states["whole"]["rules"] == whole
    assert re.search(r"\blost\s*:.*changed=[1-9]", r.stdout), r.stdout
    assert re.search(r"\bwhole\s*:.*changed=0", r.stdout), r.stdout
    adds = [line for line in _log(tmp_path, "whole") if MUTATING.search(line)]
    assert len(adds) == 2 and all('"in", "on", "podman1"' in line for line in adds), adds
    # The whole cost of a converged rootful-podman host (apply-firewall.yml header, the
    # playbook README): the two reads plus two skipped adds per bridge; no re-read, no final
    # status capture.
    ufw_calls = [line for line in _log(tmp_path, "whole") if line.startswith('["ufw"')]
    assert ufw_calls == ['["ufw", "show", "added"]', '["ufw", "status", "verbose"]', *adds], ufw_calls


def test_a_declared_tag_over_another_rule_does_not_stand_in_for_the_declared_rule(tmp_path):
    # A hand rule under the reserved prefix carries the SSH allow's tag. The SSH allow is still
    # added; the masking rule is kept (its tag is declared, so it is never pruned) and reported.
    masked = "allow from 203.0.113.9 to any port 5432 proto tcp"
    r, state = _run(tmp_path, {}, state={"active": True, "rules": [[masked, _tag("in", "22/tcp", SSH)]]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rules(state) == {masked: _tag("in", "22/tcp", SSH),
                             f"allow from {SSH} to any port 22 proto tcp": _tag("in", "22/tcp", SSH)}
    assert "holds 1 rule(s) whose comment names a declared rule" in r.stdout and masked in r.stdout
    assert not any('"delete"' in line for line in _log(tmp_path))


def test_an_egress_denial_that_is_not_scoped_is_refused_before_any_rule_is_added(tmp_path):
    # ufw_egress_problems' cases are unit-tested in test_ufw_rules_filter.py; this proves the
    # playbook refuses on it before a single rule is added, one host per shape.
    bad = {"null_port": {"port": None}, "empty_port": {"port": ""}, "port_zero": {"port": 0},
           "port_high": {"port": 70000}, "null_proto": {"port": 8200, "proto": None},
           "icmp": {"port": 8200, "proto": "icmp"},
           # a supernet of the SSH CIDR: refused even flagged broad and justified
           "supernet": {"to": "192.0.0.0/16", "broad": True, "reason": "stated"}}
    hosts = {name: {"vars": {"firewall_deny_egress": [{"to": "198.51.100.1", **entry}]}} for name, entry in bad.items()}
    hosts["good"] = {"vars": {"firewall_deny_egress": [{"to": "198.51.100.1", "port": "8200", "proto": "any"},
                                                       {"to": "198.51.100.2", "port": 53, "proto": "udp"}]}}
    r, states = _run_many(tmp_path, hosts)
    assert r.returncode != 0
    for name in bad:
        assert re.search(rf"\b{name}\s*:.*failed=1", r.stdout), name
        assert not [line for line in _log(tmp_path, name) if MUTATING.search(line)], name
        assert states[name] is None, name  # the stub never ran a write: no state file
    assert re.search(r"\bgood\s*:.*failed=0", r.stdout), r.stdout
    assert _rules(states["good"]) == {
        f"allow from {SSH} to any port 22 proto tcp": _tag("in", "22/tcp", SSH),
        "deny out to 198.51.100.1 port 8200": _tag("out-deny", "8200/any", "198.51.100.1"),
        "deny out to 198.51.100.2 port 53 proto udp": _tag("out-deny", "53/udp", "198.51.100.2")}
