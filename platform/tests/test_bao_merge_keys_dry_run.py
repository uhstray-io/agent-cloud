"""tasks/bao-merge-keys.yml's dry-run report works for a remove-only merge.

PR 203 Codex review: `Manage agentgateway Client Key` with action=revoke passes _bm_remove and no
_bm_data, and the report dereferenced _bm_data, so the dry run failed instead of reporting the
revocation. Runs the real task under --check. Requires ansible-playbook.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TASKS = REPO / "platform/playbooks/tasks/bao-merge-keys.yml"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _report(tmp_path, **inputs):
    report = next(t for t in yaml.safe_load(TASKS.read_text()) if str(t.get("name", "")).startswith("Dry run: report"))
    current = {"client_old": "v", "client_kept": "w"}
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_bm_path": "services/demo", "_bm_existing": {"status": 200, "json": {"data": {"data": current}}},
                 "_bm_current": current, "_bm_set": inputs.get("set", {}), "_bm_del": inputs.get("remove", [])},
        "tasks": [report],
    }]
    path = tmp_path / "report.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(["ansible-playbook", "-i", "localhost,", "--check", str(path)], cwd=REPO, env=env,
                          text=True, capture_output=True, stdin=subprocess.DEVNULL)


def test_a_remove_only_merge_reports_the_removal(tmp_path):
    done = _report(tmp_path, remove=["client_old"])
    assert done.returncode == 0, done.stdout[-1500:]
    assert "keys that would be removed: client_old" in done.stdout


def test_a_set_reports_the_changed_key(tmp_path):
    done = _report(tmp_path, set={"client_new": "x"})
    assert done.returncode == 0, done.stdout[-1500:]
    assert "keys that would be set: client_new" in done.stdout
