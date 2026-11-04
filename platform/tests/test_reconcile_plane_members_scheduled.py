"""The scheduled Plane member reconcile applies, and only through the reconcile's own guards.

A Semaphore schedule cannot answer a survey, so the scheduled run sets `apply` in a wrapper
(change task 2.7). These pin that the wrapper is a pure import of the real reconcile with
apply on and the mass-removal override off, and that it is the playbook the schedule runs.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "platform/playbooks/reconcile-plane-members-scheduled.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"


def test_the_wrapper_only_imports_the_reconcile_with_apply_on():
    plays = yaml.safe_load(WRAPPER.read_text())
    assert len(plays) == 2, plays
    guard, play = plays
    assert guard["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert play["ansible.builtin.import_playbook"] == "reconcile-plane-members.yml"
    assert play["vars"] == {"apply": "true"}, "the mass-removal override must never be set here"


def test_the_schedule_runs_the_wrapper_from_dev():
    declared = yaml.safe_load(TEMPLATES.read_text())
    templates = declared["templates"] if isinstance(declared, dict) else declared
    (entry,) = [t for t in templates if t.get("name") == "Reconcile Plane Members Scheduled (Dev)"]
    assert entry["playbook"] == "platform/playbooks/reconcile-plane-members-scheduled.yml"
    assert entry["repository"] == "agent-cloud dev"
    assert entry["schedule"]["cron"]
    assert "survey_vars" not in entry, "a schedule cannot answer a survey"


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_the_imported_reconcile_resolves_apply_true_and_an_extra_var_still_wins(tmp_path):
    # The reconcile reads `apply` in its own play vars; run that expression under the wrapper's
    # import to show the value reaches it, and that `-e apply=false` overrides it.
    reconcile = yaml.safe_load((ROOT / "platform/playbooks/reconcile-plane-members.yml").read_text())
    expr = next(p["vars"]["_apply"] for p in reconcile if isinstance(p, dict) and "_apply" in (p.get("vars") or {}))
    inner = tmp_path / "reconcile-plane-members.yml"
    inner.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {"_apply": expr},
                    "tasks": [{"ansible.builtin.debug": {"msg": "APPLY={{ _apply }}"}}],
                }
            ]
        )
    )
    shutil.copy(WRAPPER, tmp_path / WRAPPER.name)
    # The guard is pinned by the structure test above and covered by
    # test_executor_internal_overrides.py; here it is a no-op so only `apply` is exercised.
    (tmp_path / "refuse-internal-extra-vars.yml").write_text(
        yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "tasks": []}])
    )

    def run(*extra):
        out = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(tmp_path / WRAPPER.name), *extra],
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
            env={"PATH": os.environ["PATH"], "ANSIBLE_LOCAL_TEMP": str(tmp_path), "ANSIBLE_NOCOLOR": "1"},
        )
        assert out.returncode == 0, out.stdout + out.stderr
        return out.stdout

    assert "APPLY=True" in run()
    assert "APPLY=False" in run("-e", "apply=false")
