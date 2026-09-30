"""issue-internal-leaf.yml, the playbook: its refusals, a dry-run issue through the real include
chain, and a removal that takes only what issuance created (production-internal-ca task 4.7).

The playbook file itself runs, on localhost, with a consumer group and a step_ca_svc group.
A real issue reads the issuer password from OpenBao, so issuing is run in check mode here
(tasks/issue-internal-leaf.yml has its own real-mode tests and an end-to-end proof).
"""

import subprocess
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

PLAYBOOK = playbook_yaml.REPO / "platform/playbooks/issue-internal-leaf.yml"
SITE = {"dns_site": "dc1", "dns_zone": "example.internal", "openbao_addr": "https://bao.example.test"}


def _leaf(tmp: Path, **over) -> dict:
    return {"name": "probe", "host": "gw", "dir": str(tmp / "leaf"), "profile": "client",
            "sans": ["probe.gateway.dc1.example.internal"], **over}


def _run(tmp: Path, extra: dict, leaves: list, ca_hosts: tuple = ("ca",), check: bool = False,
         consumers: tuple = ("gw",)) -> subprocess.CompletedProcess:
    local = {"ansible_connection": "local"}
    inv = {"all": {"vars": {**SITE, "internal_leaves": leaves},
                   "children": {"gw_svc": {"hosts": dict.fromkeys(consumers, local)},
                                "step_ca_svc": {"hosts": dict.fromkeys(ca_hosts, local)}}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    args = [f"{k}={v}" for k, v in {"target_service": "gw_svc", **extra}.items()]
    cmd = ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(PLAYBOOK), *[a for x in args for a in ("-e", x)],
           *(["--check"] if check else [])]
    return harness_sandbox.run(cmd, tmp, cwd=playbook_yaml.REPO, env=harness_sandbox.env_for(tmp))


@pytest.mark.parametrize("extra,leaves,ca_hosts,consumers,message", [
    ({}, None, ("ca",), ("gw",), "Pass -e leaf_name"),
    ({"leaf_name": "other"}, None, ("ca",), ("gw",), "Pass -e leaf_name"),
    ({"leaf_name": "probe", "leaf_action": "delete"}, None, ("ca",), ("gw",), "Pass -e leaf_name"),
    ({"leaf_name": "probe"}, None, ("ca", "ca2"), ("gw",), "exactly one step_ca_svc host"),
    ({"leaf_name": "probe"}, None, ("ca",), ("elsewhere",), "(it names gw)"),
    ({"target_service": "nothing_svc", "leaf_name": "probe"}, None, ("ca",), ("gw",), "matches no hosts"),
], ids=["no-name", "undeclared", "bad-action", "two-cas", "host-not-in-group", "empty-group"])
def test_an_incomplete_or_unknown_request_is_refused(tmp_path, extra, leaves, ca_hosts, consumers, message):
    r = _run(tmp_path, extra, leaves if leaves is not None else [_leaf(tmp_path)], ca_hosts, consumers=consumers)
    assert r.returncode != 0 and message in r.stdout, r.stdout


def test_a_dry_run_issue_passes_the_whole_include_chain_and_writes_nothing(tmp_path):
    r = _run(tmp_path, {"leaf_name": "probe"}, [_leaf(tmp_path)], check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "probe (client, probe.gateway.dc1.example.internal): issuing a new certificate" in r.stdout
    assert "check mode: nothing is written" in r.stdout
    assert not (tmp_path / "leaf").exists()


def _issued(tmp: Path, extra_entry: str | None = None) -> Path:
    leaf = tmp / "leaf"
    (leaf / "0A1B").mkdir(parents=True)
    (leaf / "0A1B" / "key.pem").write_text("k")
    (leaf / "0C2D").mkdir()
    (leaf / "current").symlink_to("0A1B")
    (leaf / ".placement.lock").write_text("")
    (leaf / ".pending.xyz").mkdir()
    (leaf / ".current.new").symlink_to("0C2D")  # an interrupted swap's link
    if extra_entry:
        (leaf / extra_entry).write_text("not issued here")
    return leaf


def test_removal_takes_only_what_issuance_created_and_keeps_anything_else(tmp_path):
    leaf = _issued(tmp_path, "keep.txt")
    (leaf / "Archive").mkdir()  # the find glob matches it (leading A); only the hex filter keeps it
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(p.name for p in leaf.iterdir()) == ["Archive", "keep.txt"]


def test_removal_of_only_issued_files_removes_the_directory(tmp_path):
    leaf = _issued(tmp_path)
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert not leaf.exists()


def test_a_dry_run_removal_lists_and_removes_nothing(tmp_path):
    leaf = _issued(tmp_path)
    before = sorted(p.name for p in leaf.iterdir())
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path)], check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would remove" in r.stdout and "0A1B" in r.stdout
    assert sorted(p.name for p in leaf.iterdir()) == before


def test_a_symlinked_leaf_directory_is_refused_and_its_target_left_alone(tmp_path):
    # Review of #369: find follows a symlinked starting directory.
    target = tmp_path / "elsewhere"
    (target / "0A1B").mkdir(parents=True)
    (tmp_path / "leaf").symlink_to(target)
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path)])
    assert r.returncode != 0 and "is a symbolic link" in r.stdout, r.stdout
    assert (target / "0A1B").is_dir()


def test_a_trailing_slash_in_the_declared_directory_is_refused(tmp_path):
    leaf = _issued(tmp_path)
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path, dir=str(leaf) + "/")])
    assert r.returncode != 0 and "no trailing slash" in r.stdout, r.stdout
    assert (leaf / "0A1B").is_dir()


def test_removing_a_leaf_whose_directory_is_gone_does_nothing(tmp_path):
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path)])
    assert r.returncode == 0 and "nothing (no issued files)" in r.stdout, r.stdout
