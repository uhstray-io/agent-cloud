"""A template whose playbook runs the o11y restore-target preflight declares its own environment.

The preflight (tasks/preflight-o11y-restore-target.yml) refuses the shared Semaphore environment's
plaintext credential extra vars, and the guard authenticates only with BAO_ROLE_ID / BAO_SECRET_ID
from the process environment, which the shared environment does not provide. A template bound to
the shared environment therefore fails on every production launch. PR #728 put the guard in front
of the five general VM writers without moving them, and every production run of Provision, Resize,
Destroy, Snapshot and Create VM Template (Dev) failed from 2026-10-24 (Semaphore tasks 9493, 10259,
10295). The fix gives each template an isolated environment holding an encrypted AppRole, through
Provision Seed Environment (Dev).
"""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform" / "playbooks"
TEMPLATES = REPO / "platform" / "semaphore" / "templates.yml"
GUARDED = re.compile(r"(guard|preflight)-o11y-restore-target(-inputs)?\.yml")
IMPORT = re.compile(r"import_playbook:\s*([\w./-]+\.yml)")


def runs_restore_preflight(path, seen=None):
    """True when the playbook, or a playbook it imports, includes the preflight or the guard."""
    seen = seen if seen is not None else set()
    if path in seen or not path.is_file():
        return False
    seen.add(path)
    text = path.read_text()
    # A playbook named for the preflight is the protected workflow itself.
    if GUARDED.search(text) or GUARDED.fullmatch(path.name):
        return True
    return any(runs_restore_preflight(path.parent / name, seen) for name in IMPORT.findall(text))


def guarded_templates():
    declared = yaml.safe_load(TEMPLATES.read_text())["templates"]
    return [t for t in declared if runs_restore_preflight(REPO / t["playbook"])]


def test_the_five_vm_writers_are_found():
    names = {t["name"] for t in guarded_templates()}
    assert {"Provision VM", "Resize VM", "Destroy VM", "Create VM Template", "Snapshot VM"} <= names


def test_every_template_running_the_restore_preflight_declares_an_isolated_environment():
    missing = [t["name"] for t in guarded_templates() if not t.get("isolated_environment")]
    assert not missing, (
        "These templates run the o11y restore-target preflight, which refuses the shared "
        "environment's credential extra vars; declare isolated_environment and run Provision "
        "Seed Environment (Dev) for each:\n" + "\n".join(missing))


def test_the_detector_follows_imports_and_ignores_unguarded_playbooks(tmp_path):
    (tmp_path / "inner.yml").write_text("- ansible.builtin.import_playbook: preflight-o11y-restore-target-inputs.yml\n")
    (tmp_path / "outer.yml").write_text("- ansible.builtin.import_playbook: inner.yml\n")
    (tmp_path / "plain.yml").write_text("- ansible.builtin.import_playbook: refuse-internal-extra-vars.yml\n")
    assert runs_restore_preflight(tmp_path / "outer.yml")
    assert not runs_restore_preflight(tmp_path / "plain.yml")
