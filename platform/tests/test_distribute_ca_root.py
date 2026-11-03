"""tasks/distribute-ca-root.yml: the trust bundle is read from the CA host, written on the consumer.

The task file runs as it is on localhost, with two inventory hosts: `consumer` (the play host)
and `ca` (where the CA runs, reached through delegate_to). Each host's container engine is a
stub that records which host ran it and prints fixed certificates, so the read, the write
and the delegation run for real (openspec production-internal-ca task 4.3).
"""

import subprocess
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

TASKS = playbook_yaml.REPO / "platform/playbooks/tasks/distribute-ca-root.yml"
PEM = "-----BEGIN CERTIFICATE-----\nMIIB{n}\n-----END CERTIFICATE-----"


def _engine(tmp: Path, name: str, certs: int = 2) -> Path:
    stub = tmp / f"engine-{name}"
    body = "\n".join(PEM.format(n=i) for i in range(certs))
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{tmp}/ran-on-{name}"\ncat <<EOF\n{body}\nEOF\n')
    stub.chmod(0o755)
    return stub


def _run(tmp: Path, consumer: dict, ca: dict | None = None, check: bool = False) -> subprocess.CompletedProcess:
    hosts = {"consumer": {"ansible_connection": "local", **consumer}}
    if ca is not None:
        hosts["ca"] = {"ansible_connection": "local", **ca}
    (tmp / "inv.yml").write_text(yaml.safe_dump({"all": {"hosts": hosts}}))
    play = [{"hosts": "consumer", "gather_facts": False,
             "tasks": [{"ansible.builtin.include_tasks": str(TASKS)}]}]
    (tmp / "play.yml").write_text(yaml.safe_dump(play))
    cmd = ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(tmp / "play.yml"), *(["--check"] if check else [])]
    return harness_sandbox.run(cmd, tmp, cwd=playbook_yaml.REPO, env=harness_sandbox.env_for(tmp))


def test_the_bundle_is_read_on_the_ca_host_and_written_on_the_consumer(tmp_path):
    dest = tmp_path / "leaf" / "bundle.pem"
    consumer = {"_ca_host": "ca", "_ca_bundle_dest": str(dest), "container_engine": str(_engine(tmp_path, "consumer"))}
    r = _run(tmp_path, consumer, {"container_engine": str(_engine(tmp_path, "ca"))})
    assert r.returncode == 0, r.stdout + r.stderr
    # The CA host's own engine ran the read; the consumer's never did.
    assert "exec step-ca cat /home/step/certs/root_ca.crt /home/step/certs/intermediate_ca.crt" in (
        tmp_path / "ran-on-ca").read_text()
    assert not (tmp_path / "ran-on-consumer").exists()
    assert dest.read_text().count("-----BEGIN CERTIFICATE-----") == 2
    assert dest.stat().st_mode & 0o777 == 0o644


def test_the_local_single_host_default_is_unchanged_in_effect(tmp_path):
    deploy = tmp_path / "deploy"
    r = _run(tmp_path, {"_ca_root_dest_dir": str(deploy), "_ca_engine": str(_engine(tmp_path, "local"))})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (deploy / "certs" / "step-ca-bundle.crt").read_text().count("BEGIN CERTIFICATE") == 2


def test_a_second_run_changes_nothing(tmp_path):
    consumer = {"_ca_root_dest_dir": str(tmp_path / "deploy"), "_ca_engine": str(_engine(tmp_path, "local"))}
    assert _run(tmp_path, consumer).returncode == 0
    r = _run(tmp_path, consumer)
    assert r.returncode == 0 and "changed=0" in r.stdout, r.stdout


def test_a_dry_run_reads_but_writes_nothing(tmp_path):
    deploy = tmp_path / "deploy"
    r = _run(tmp_path, {"_ca_root_dest_dir": str(deploy), "_ca_engine": str(_engine(tmp_path, "local"))}, check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "ran-on-local").exists() and not deploy.exists()


@pytest.mark.parametrize("certs", [1, 3])
def test_a_read_that_is_not_root_and_intermediate_is_refused(tmp_path, certs):
    deploy = tmp_path / "deploy"
    r = _run(tmp_path, {"_ca_root_dest_dir": str(deploy), "_ca_engine": str(_engine(tmp_path, "local", certs))})
    assert r.returncode != 0 and "did not read back as two certificates" in r.stdout
    assert not (deploy / "certs" / "step-ca-bundle.crt").exists()


def test_a_changed_bundle_is_rewritten_in_place(tmp_path):
    # Review of #367: a service that bind-mounts the single bundle file keeps the inode it
    # started with, so the bundle must be rewritten in place, never replaced by a rename.
    bundle = tmp_path / "deploy" / "certs" / "step-ca-bundle.crt"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("an old root\n")
    inode = bundle.stat().st_ino
    r = _run(tmp_path, {"_ca_root_dest_dir": str(tmp_path / "deploy"), "_ca_engine": str(_engine(tmp_path, "local"))})
    assert r.returncode == 0, r.stdout + r.stderr
    assert bundle.read_text().count("BEGIN CERTIFICATE") == 2
    assert bundle.stat().st_ino == inode


def test_a_dry_run_says_when_the_bundle_would_change(tmp_path):
    bundle = tmp_path / "deploy" / "certs" / "step-ca-bundle.crt"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("an old root\n")
    r = _run(tmp_path, {"_ca_root_dest_dir": str(tmp_path / "deploy"), "_ca_engine": str(_engine(tmp_path, "local"))},
             check=True)
    assert r.returncode == 0 and "would be rewritten" in r.stdout, r.stdout
    assert bundle.read_text() == "an old root\n"


# ── Combined bundle: step-ca root + intermediate, then the play host's public roots ───────────
# SSL_CERT_FILE replaces a consumer's whole trust store, so the local gateway (whose upstream is
# a public-CA host) names this file. The stand-in public roots are PEM blocks named PUB<n>, so a
# test can tell them from the CA's MIIB<n> certificates.


def _combined(deploy: Path, system: list[str], engine: Path) -> dict:
    combined = str(deploy / "certs" / "ca-bundle-combined.crt")
    return {"_ca_root_dest_dir": str(deploy), "_ca_root_combined_dest": combined,
            "_ca_root_system_bundles": system, "_ca_engine": str(engine)}


def _public_roots(path: Path, certs: int = 2) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(PEM.format(n=f"PUB{i}") for i in range(certs)) + "\n")
    return path


def test_the_combined_bundle_is_the_ca_root_and_intermediate_then_the_public_roots(tmp_path):
    deploy = tmp_path / "deploy"
    public = _public_roots(tmp_path / "system" / "ca-certificates.crt", certs=3)
    consumer = _combined(deploy, [str(public)], _engine(tmp_path, "local"))
    r = _run(tmp_path, consumer)
    assert r.returncode == 0, r.stdout + r.stderr
    combined = deploy / "certs" / "ca-bundle-combined.crt"
    assert combined.read_text() == (deploy / "certs" / "step-ca-bundle.crt").read_text() + public.read_text()
    assert combined.read_text().count("-----BEGIN CERTIFICATE-----") == 5
    assert combined.stat().st_mode & 0o777 == 0o644
    # The step-ca-only bundle is still written, unchanged, for the other consumers.
    assert (deploy / "certs" / "step-ca-bundle.crt").read_text().count("BEGIN CERTIFICATE") == 2


def test_the_first_public_root_path_that_exists_is_the_one_read(tmp_path):
    deploy = tmp_path / "deploy"
    present = _public_roots(tmp_path / "pki" / "ca-bundle.crt", certs=1)
    consumer = _combined(deploy, [str(tmp_path / "absent.crt"), str(present)], _engine(tmp_path, "local"))
    r = _run(tmp_path, consumer)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (deploy / "certs" / "ca-bundle-combined.crt").read_text().count("-----BEGIN CERTIFICATE-----") == 3


def test_a_combined_bundle_with_no_public_roots_on_the_play_host_is_refused(tmp_path):
    deploy = tmp_path / "deploy"
    consumer = _combined(deploy, [str(tmp_path / "absent.crt")], _engine(tmp_path, "local"))
    r = _run(tmp_path, consumer)
    assert r.returncode != 0 and "no public trust roots" in r.stdout, r.stdout
    assert not (deploy / "certs" / "ca-bundle-combined.crt").exists()


def test_without_a_combined_destination_no_public_roots_are_read(tmp_path):
    deploy = tmp_path / "deploy"
    consumer = {"_ca_root_dest_dir": str(deploy), "_ca_root_system_bundles": [str(tmp_path / "absent.crt")],
                "_ca_engine": str(_engine(tmp_path, "local"))}
    r = _run(tmp_path, consumer)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (deploy / "certs" / "ca-bundle-combined.crt").exists()


def test_a_second_combined_run_changes_nothing(tmp_path):
    deploy = tmp_path / "deploy"
    public = _public_roots(tmp_path / "system" / "ca-certificates.crt")
    consumer = _combined(deploy, [str(public)], _engine(tmp_path, "local"))
    assert _run(tmp_path, consumer).returncode == 0
    r = _run(tmp_path, consumer)
    assert r.returncode == 0 and "changed=0" in r.stdout, r.stdout


def test_a_dry_run_reads_the_public_roots_but_writes_no_combined_bundle(tmp_path):
    deploy = tmp_path / "deploy"
    public = _public_roots(tmp_path / "system" / "ca-certificates.crt")
    consumer = _combined(deploy, [str(public)], _engine(tmp_path, "local"))
    r = _run(tmp_path, consumer, check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (deploy / "certs" / "ca-bundle-combined.crt").exists()
