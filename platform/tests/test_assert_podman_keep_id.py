"""tasks/assert-podman-keep-id.yml: the one shared podman >= 4.3 refusal for userns keep-id (AC-702).

Each case runs the real task through harness_sandbox.run_playbook, with a fake engine binary first on PATH,
so the version a case sees is the one the case names. Every refusal is asserted by its message text, not by a
nonzero exit alone: a template or evaluation error also exits nonzero, and a bare return-code check would pass
for the wrong reason. Each refusal case is built so that exactly one condition of the assert can refuse it, so
a mutation of that one condition turns the case red.
"""

import os
import shutil
from pathlib import Path

import harness_sandbox
import pytest
import yaml

SHARED_TASK = harness_sandbox.REPO / "platform/playbooks/tasks/assert-podman-keep-id.yml"
CALLER = "Test caller (compose.test.yml)"


def fake_engine(bin_dir: Path, name: str, version: str | None, rc: int) -> None:
    """A stand-in engine binary: prints `version` (when given) and exits `rc`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    body = f"printf '%s\\n' '{version}'\n" if version is not None else ""
    script = bin_dir / name
    script.write_text(f"#!/bin/sh\n{body}exit {rc}\n", encoding="utf-8")
    script.chmod(0o755)


def write_probe(tmp_path: Path) -> Path:
    """A play that includes the shared task by its absolute path, with the caller's _apk_for."""
    play = [
        {
            "name": "keep-id probe",
            "hosts": "all",
            "connection": "local",
            "gather_facts": False,
            "tasks": [
                {
                    "name": "Include the shared keep-id check",
                    "ansible.builtin.include_tasks": str(SHARED_TASK),
                    "vars": {"_apk_for": CALLER},
                }
            ],
        }
    ]
    path = tmp_path / "keep-id-probe.yml"
    path.write_text(yaml.safe_dump(play, sort_keys=False), encoding="utf-8")
    return path


def run_probe(tmp_path: Path, bin_dir: Path, *args: str, path: str | None = None):
    """Run the probe with bin_dir first on PATH (or the given PATH outright)."""
    search = path if path is not None else f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    return harness_sandbox.run_playbook(write_probe(tmp_path), tmp_path, *args, env={"PATH": search})


def output(done) -> str:
    return done.stdout + done.stderr


@pytest.mark.parametrize("version", ["4.3.0", "4.9.3"])
def test_a_podman_at_or_above_4_3_passes(tmp_path, version):
    bin_dir = tmp_path / "bin"
    fake_engine(bin_dir, "podman", version, rc=0)
    done = run_probe(tmp_path, bin_dir)
    assert done.returncode == 0, output(done)


def test_podman_4_2_1_is_refused_and_the_message_names_the_version(tmp_path):
    bin_dir = tmp_path / "bin"
    fake_engine(bin_dir, "podman", "4.2.1", rc=0)
    done = run_probe(tmp_path, bin_dir)
    out = output(done)
    assert done.returncode != 0, out
    assert f"{CALLER} needs podman 4.3 or later for the userns keep-id mapping." in out, out
    assert "This host runs podman 4.2.1 (read rc 0)." in out, out
    assert "Run Install Podman on this host, then re-run this deploy." in out, out


def test_a_podman_that_exits_1_is_refused_even_when_it_prints_a_passing_version(tmp_path):
    # The version passes on its own, so only the read's return code can refuse this case. Dropping that
    # condition (M1) makes the case pass, which is what this test catches.
    bin_dir = tmp_path / "bin"
    fake_engine(bin_dir, "podman", "4.9.3", rc=1)
    done = run_probe(tmp_path, bin_dir)
    out = output(done)
    assert done.returncode != 0, out
    assert f"{CALLER} needs podman 4.3 or later" in out, out
    assert "This host runs podman 4.9.3 (read rc 1)." in out, out


def test_a_podman_that_exits_1_with_no_output_is_refused_as_no_version_read(tmp_path):
    bin_dir = tmp_path / "bin"
    fake_engine(bin_dir, "podman", None, rc=1)
    done = run_probe(tmp_path, bin_dir)
    out = output(done)
    assert done.returncode != 0, out
    assert f"{CALLER} needs podman 4.3 or later" in out, out
    assert "This host runs podman (no version read) (read rc 1)." in out, out


def test_another_engine_is_refused_even_at_a_passing_version(tmp_path):
    # docker prints 27.3.1, which passes the version comparison on its own, so only the engine condition can
    # refuse it. Dropping that condition (M2) makes the case pass, which is what this test catches.
    bin_dir = tmp_path / "bin"
    fake_engine(bin_dir, "docker", "27.3.1", rc=0)
    done = run_probe(tmp_path, bin_dir, "-e", "container_engine=docker")
    out = output(done)
    assert done.returncode != 0, out
    assert f"{CALLER} needs podman 4.3 or later" in out, out
    assert "This host runs docker 27.3.1 (read rc 0)." in out, out


def test_a_refusal_still_fires_under_check_mode(tmp_path):
    # The read runs under --check (check_mode false), so a dry run refuses the same host a real run would.
    bin_dir = tmp_path / "bin"
    fake_engine(bin_dir, "podman", "4.2.1", rc=0)
    done = run_probe(tmp_path, bin_dir, "--check")
    out = output(done)
    assert done.returncode != 0, out
    assert "This host runs podman 4.2.1 (read rc 0)." in out, out


def test_a_missing_podman_is_refused_by_the_check_not_by_a_failed_read(tmp_path):
    # Before placement installs podman, the engine is absent. The read must not abort the run with a
    # "No such file" failure; the refusal must name the missing version. The PATH is restricted to the
    # ansible-playbook directory and the system bins, so this test can run only where no podman is on that PATH.
    ansible = shutil.which("ansible-playbook")
    if ansible is None:
        pytest.skip("ansible-playbook not on PATH")
    restricted = os.pathsep.join([str(Path(ansible).parent), "/usr/bin", "/bin"])
    if shutil.which("podman", path=restricted) is not None:
        pytest.skip("a podman is on the restricted PATH, so the missing-engine case cannot be built here")
    done = run_probe(tmp_path, tmp_path / "bin", path=restricted)
    out = output(done)
    assert done.returncode != 0, out
    assert f"{CALLER} needs podman 4.3 or later" in out, out
    assert "This host runs podman (no version read) (read rc " in out, out
    assert "No such file" not in out, out
