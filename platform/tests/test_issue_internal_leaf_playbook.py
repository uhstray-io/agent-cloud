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
    assert r.returncode != 0 and "resolves through a symbolic link" in r.stdout, r.stdout
    assert (target / "0A1B").is_dir()


def test_a_trailing_slash_in_the_declared_directory_is_refused(tmp_path):
    leaf = _issued(tmp_path)
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path, dir=str(leaf) + "/")])
    assert r.returncode != 0 and "no trailing slash" in r.stdout, r.stdout
    assert (leaf / "0A1B").is_dir()


def test_removing_a_leaf_whose_directory_is_gone_does_nothing(tmp_path):
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path)])
    assert r.returncode == 0 and "nothing (no issued files)" in r.stdout, r.stdout


def test_a_symlinked_ancestor_of_the_leaf_directory_is_refused(tmp_path):
    # Review of #369: a link above the leaf directory redirects it as surely as one at it.
    real = tmp_path / "real"
    (real / "leaf" / "0A1B").mkdir(parents=True)
    (tmp_path / "via").symlink_to(real)
    leaf = {**_leaf(tmp_path), "dir": str(tmp_path / "via" / "leaf")}
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [leaf])
    assert r.returncode != 0 and "resolves through a symbolic link" in r.stdout, r.stdout
    assert (real / "leaf" / "0A1B").is_dir()


def test_a_dangling_symlink_at_the_leaf_directory_is_refused(tmp_path):
    (tmp_path / "leaf").symlink_to(tmp_path / "missing")
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path)])
    assert r.returncode != 0 and "resolves through a symbolic link" in r.stdout, r.stdout


def test_an_entry_of_the_wrong_kind_is_not_taken_for_one_issuance_made(tmp_path):
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    (leaf / "ABCD").write_text("a file named like a serial")
    (leaf / "current").mkdir()  # a directory where issuance keeps a link
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path)])
    assert r.returncode == 0 and "nothing (no issued files)" in r.stdout, r.stdout
    assert sorted(p.name for p in leaf.iterdir()) == ["ABCD", "current"]


def test_removing_an_emptied_directory_reports_the_change(tmp_path):
    # Review of #369: an already-empty leaf directory is removed, and that is a change.
    (tmp_path / "leaf").mkdir()
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path)])
    assert r.returncode == 0 and "changed=1" in r.stdout, r.stdout
    assert not (tmp_path / "leaf").exists()


@pytest.mark.parametrize("bad", ["/../etc", "/./leaf"])
def test_a_dot_or_dotdot_component_is_refused(tmp_path, bad):
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [_leaf(tmp_path, dir=str(tmp_path) + bad)])
    assert r.returncode != 0 and "no trailing slash" in r.stdout, r.stdout


# ── leaf_action=inspect (production-internal-ca task 4.7's proof) ──────────────

def _ossl(*args, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def _chain(tmp: Path, eku: str) -> Path:
    """A root CA and one leaf it signed, with the given extended key usage."""
    ca = tmp / "pki"
    ca.mkdir(exist_ok=True)
    if not (ca / "root.pem").exists():
        _ossl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "1",
              "-subj", "/CN=test root", "-keyout", "root.key", "-out", "root.pem",
              "-addext", "basicConstraints=critical,CA:true", "-addext", "keyUsage=critical,keyCertSign", cwd=ca)
    (ca / f"{eku}.ext").write_text(
        f"basicConstraints=CA:false\nkeyUsage=critical,digitalSignature\nextendedKeyUsage={eku}\n"
        "subjectAltName=DNS:probe.gateway.dc1.example.internal\n")
    _ossl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=probe",
          "-keyout", f"{eku}.key", "-out", f"{eku}.csr", cwd=ca)
    _ossl("x509", "-req", "-in", f"{eku}.csr", "-CA", "root.pem", "-CAkey", "root.key", "-CAcreateserial",
          "-days", "1", "-extfile", f"{eku}.ext", "-out", f"{eku}.pem", cwd=ca)
    return ca


def _placed(tmp: Path, eku: str, key_mode: int = 0o600, other_key: bool = False) -> None:
    ca = _chain(tmp, eku)
    serial = tmp / "leaf" / "0A1B"
    serial.mkdir(parents=True)
    (serial / "cert.pem").write_text((ca / f"{eku}.pem").read_text())
    key = (ca / ("root.key" if other_key else f"{eku}.key")).read_text()
    (serial / "key.pem").write_text(key)
    (serial / "key.pem").chmod(key_mode)
    (tmp / "leaf" / "current").symlink_to("0A1B")


def _ca_engine(tmp: Path, leftovers: int = 0) -> Path:
    stub = tmp / "ca-engine"
    stub.write_text(f'#!/bin/sh\ncat "{tmp}/pki/root.pem"; echo "LEFTOVERS={leftovers}"\n')
    stub.chmod(0o755)
    return stub


def _inspect(tmp: Path, check: bool = False, leftovers: int = 0) -> subprocess.CompletedProcess:
    if not (tmp / "pki").exists():
        _chain(tmp, "clientAuth")
    local = {"ansible_connection": "local"}
    ca = {**local, "container_engine": str(_ca_engine(tmp, leftovers))}
    inv = {"all": {"vars": {**SITE, "internal_leaves": [_leaf(tmp)]},
                   "children": {"gw_svc": {"hosts": {"gw": local}}, "step_ca_svc": {"hosts": {"ca": ca}}}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    cmd = ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(PLAYBOOK), "-e", "target_service=gw_svc",
           "-e", "leaf_name=probe", "-e", "leaf_action=inspect", *(["--check"] if check else [])]
    return harness_sandbox.run(cmd, tmp, cwd=playbook_yaml.REPO, env=harness_sandbox.env_for(tmp))


def test_inspect_proves_a_client_leaf_and_runs_under_check(tmp_path):
    _placed(tmp_path, "clientAuth")
    for check in (False, True):
        r = _inspect(tmp_path, check=check)
        assert r.returncode == 0, r.stdout + r.stderr
        assert '"verifies_sslclient": true' in r.stdout and '"verifies_sslserver": false' in r.stdout
        assert '"key_matches": true' in r.stdout and '"ca_leftovers": 0' in r.stdout
        assert "PRIVATE KEY" not in r.stdout + r.stderr


@pytest.mark.parametrize("case", ["server-leaf", "both-usages", "open-key", "other-key", "ca-leftovers",
                                  "no-current", "current-escapes", "files-missing", "serial-dir-link",
                                  "cert-file-link"])
def test_inspect_refuses_a_leaf_that_is_not_its_declaration(tmp_path, case):
    if case == "server-leaf":
        _placed(tmp_path, "serverAuth")
    elif case == "both-usages":
        # What a JWK provisioner without a profile template issued (production, 2026-09-29).
        _placed(tmp_path, "serverAuth,clientAuth")
    elif case == "open-key":
        _placed(tmp_path, "clientAuth", key_mode=0o644)
    elif case == "other-key":
        _placed(tmp_path, "clientAuth", other_key=True)
    elif case == "current-escapes":
        # A valid leaf placed outside the declared directory, linked from `current`.
        _placed(tmp_path, "clientAuth")
        outside = tmp_path / "outside"
        (tmp_path / "leaf" / "0A1B").rename(outside)
        (tmp_path / "leaf" / "current").unlink()
        (tmp_path / "leaf" / "current").symlink_to(outside)
    elif case == "serial-dir-link":
        # `current` names a hex entry, but that entry is a link to a directory elsewhere.
        _placed(tmp_path, "clientAuth")
        outside = tmp_path / "outside"
        (tmp_path / "leaf" / "0A1B").rename(outside)
        (tmp_path / "leaf" / "0A1B").symlink_to(outside)
    elif case == "cert-file-link":
        _placed(tmp_path, "clientAuth")
        cert = tmp_path / "leaf" / "0A1B" / "cert.pem"
        (tmp_path / "elsewhere.pem").write_text(cert.read_text())
        cert.unlink()
        cert.symlink_to(tmp_path / "elsewhere.pem")
    elif case == "files-missing":
        _placed(tmp_path, "clientAuth")
        (tmp_path / "leaf" / "0A1B" / "key.pem").unlink()
    elif case != "no-current":
        _placed(tmp_path, "clientAuth")
    r = _inspect(tmp_path, leftovers=1 if case == "ca-leftovers" else 0)
    assert r.returncode != 0 and "does not match its client declaration" in r.stdout, r.stdout


def test_the_leftover_count_covers_every_temporary_name_the_ca_side_tasks_write():
    import re
    script = next(t for t in playbook_yaml.tasks(playbook_yaml.load(PLAYBOOK))
                  if t.get("name") == "Read the CA root and the issuance's leftovers on the CA host")
    pattern = re.search(r"grep -cE '([^']+)'", script["ansible.builtin.command"]["argv"][-1]).group(1)
    names = set()
    for rel in ("platform/playbooks/deploy-step-ca.yml", "platform/playbooks/tasks/issue-internal-leaf.yml",
                "platform/playbooks/tasks/mint-internal-cert.yml"):
        names |= set(re.findall(r"/tmp/([A-Za-z0-9._$]+)", (playbook_yaml.REPO / rel).read_text()))
    names = {n.replace("$$", "4242").rstrip(".;") for n in names}
    assert names, "no /tmp names found"
    missed = sorted(n for n in names if not re.search(pattern, n))
    assert not missed, f"leftover count misses {missed}"


def test_inspect_verifies_a_leaf_issued_through_an_intermediate(tmp_path):
    # step ca sign returns the leaf followed by the intermediate; the root alone is trusted.
    ca = _chain(tmp_path, "clientAuth")  # makes the root
    (ca / "inter.ext").write_text("basicConstraints=critical,CA:true,pathlen:0\nkeyUsage=critical,keyCertSign\n")
    _ossl("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=inter",
          "-keyout", "inter.key", "-out", "inter.csr", cwd=ca)
    _ossl("x509", "-req", "-in", "inter.csr", "-CA", "root.pem", "-CAkey", "root.key", "-CAcreateserial",
          "-days", "1", "-extfile", "inter.ext", "-out", "inter.pem", cwd=ca)
    _ossl("x509", "-req", "-in", "clientAuth.csr", "-CA", "inter.pem", "-CAkey", "inter.key", "-CAcreateserial",
          "-days", "1", "-extfile", "clientAuth.ext", "-out", "leaf.pem", cwd=ca)
    serial = tmp_path / "leaf" / "0A1B"
    serial.mkdir(parents=True)
    (serial / "cert.pem").write_text((ca / "leaf.pem").read_text() + (ca / "inter.pem").read_text())
    (serial / "key.pem").write_text((ca / "clientAuth.key").read_text())
    (serial / "key.pem").chmod(0o600)
    (tmp_path / "leaf" / "current").symlink_to("0A1B")
    r = _inspect(tmp_path)
    assert r.returncode == 0 and '"verifies_sslclient": true' in r.stdout, r.stdout


def test_a_dotted_directory_name_is_still_a_valid_leaf_directory(tmp_path):
    leaf = {**_leaf(tmp_path), "dir": str(tmp_path / "leaf.d" / "certs.v1")}
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "remove"}, [leaf])
    assert r.returncode == 0 and "nothing (no issued files)" in r.stdout, r.stdout


def test_inspect_refuses_a_leaf_declared_with_an_unknown_profile(tmp_path):
    # Review of #370: with neither profile, both purpose expectations would be false.
    _placed(tmp_path, "clientAuth")
    r = _run(tmp_path, {"leaf_name": "probe", "leaf_action": "inspect"}, [_leaf(tmp_path, profile="both")])
    assert r.returncode != 0 and "Pass -e leaf_name" in r.stdout, r.stdout


def test_an_unreadable_ca_tmp_fails_instead_of_counting_zero(tmp_path):
    _placed(tmp_path, "clientAuth")
    stub = tmp_path / "ca-engine"
    stub.write_text(f'#!/bin/sh\ncat "{tmp_path}/pki/root.pem"; echo "cannot read /tmp" >&2; exit 1\n')
    stub.chmod(0o755)
    local = {"ansible_connection": "local"}
    inv = {"all": {"vars": {**SITE, "internal_leaves": [_leaf(tmp_path)]},
                   "children": {"gw_svc": {"hosts": {"gw": local}},
                                "step_ca_svc": {"hosts": {"ca": {**local, "container_engine": str(stub)}}}}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    cmd = ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(PLAYBOOK), "-e", "target_service=gw_svc",
           "-e", "leaf_name=probe", "-e", "leaf_action=inspect"]
    r = harness_sandbox.run(cmd, tmp_path, cwd=playbook_yaml.REPO, env=harness_sandbox.env_for(tmp_path))
    assert r.returncode != 0 and "ca_leftovers" not in r.stdout, r.stdout


def test_the_inspector_writes_nothing_on_the_host(tmp_path):
    # Review of #370: inspect runs under --check, where nothing may be written. Run the
    # inspector itself with an empty TMPDIR and compare everything before and after.
    import json
    import os
    _placed(tmp_path, "clientAuth")
    inspector = next(p for p in playbook_yaml.load(PLAYBOOK) if p.get("hosts") != "localhost")["vars"][
        "_leaf_inspector"]
    scratch = tmp_path / "tmpdir"
    scratch.mkdir()
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    root = (tmp_path / "pki/root.pem").read_text()
    r = subprocess.run(["python3", "-c", inspector, str(tmp_path / "leaf")], input=root, capture_output=True,
                       text=True, env={**os.environ, "TMPDIR": str(scratch)})
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["verifies_sslclient"] is True
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before and not list(scratch.iterdir())


def test_tags_verify_runs_the_inspection_and_changes_nothing(tmp_path):
    # plan/architecture/08 standard 3: `--tags verify` proves the state without changing it,
    # whatever the action (review of #370).
    _placed(tmp_path, "clientAuth")
    before = sorted(str(p) for p in (tmp_path / "leaf").rglob("*"))
    local = {"ansible_connection": "local"}
    ca = {**local, "container_engine": str(_ca_engine(tmp_path))}
    inv = {"all": {"vars": {**SITE, "internal_leaves": [_leaf(tmp_path)]},
                   "children": {"gw_svc": {"hosts": {"gw": local}}, "step_ca_svc": {"hosts": {"ca": ca}}}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    cmd = ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(PLAYBOOK), "--tags", "verify",
           "-e", "target_service=gw_svc", "-e", "leaf_name=probe", "-e", "leaf_reissue=true"]
    r = harness_sandbox.run(cmd, tmp_path, cwd=playbook_yaml.REPO, env=harness_sandbox.env_for(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"verifies_sslclient": true' in r.stdout and "changed=0" in r.stdout
    assert sorted(str(p) for p in (tmp_path / "leaf").rglob("*")) == before


def test_an_input_larger_than_a_pipe_buffer_does_not_hang_the_inspection(tmp_path):
    # Review of #370: a write before openssl starts would block on a full pipe. Text outside
    # the PEM block is ignored by openssl, so 256 KiB of it in front of the root is harmless.
    _placed(tmp_path, "clientAuth")
    root = tmp_path / "pki" / "root.pem"
    root.write_text("# padding\n" * 26000 + root.read_text())
    r = _inspect(tmp_path)
    assert r.returncode == 0 and '"verifies_sslclient": true' in r.stdout, r.stdout[-2000:]
