"""The read-only inspector reports stopped containers and refuses empty results."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/inspect-service-runtime.yml"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _run(tmp_path, names):
    engine = tmp_path / "engine"
    engine.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        f"  ps) printf '%s' '{names}' ;;\n"
        "  inspect) printf 'exited 1 0\\n' ;;\n"
        "  *) exit 99 ;;\n"
        "esac\n"
    )
    engine.chmod(0o755)
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "[demo_svc]\ndemo ansible_connection=local\n\n[demo_svc:vars]\n"
        f"container_engine={engine}\n"
        "local_monorepo_dir=/tmp/demo\n"
        "monorepo_deploy_path=platform/services/demo/deployment\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_LOCAL_TEMP"] = str(tmp_path)
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(
        ["ansible-playbook", "--check", "-i", str(inventory), str(PLAYBOOK),
         "-e", "target_service=demo_svc"],
        cwd=REPO, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL,
    )


def test_stopped_container_is_reported_in_check_mode(tmp_path):
    done = _run(tmp_path, "demo-server\n")
    assert done.returncode == 0, done.stdout[-2000:]
    assert "demo-server: exited 1 0" in done.stdout


def test_empty_listing_fails_closed(tmp_path):
    done = _run(tmp_path, "")
    assert done.returncode != 0
    assert "No service containers could be inspected" in done.stdout
