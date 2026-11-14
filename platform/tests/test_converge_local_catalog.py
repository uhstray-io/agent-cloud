"""The local catalog entry point publishes, then retires, and only on a loopback controller.

platform/semaphore/converge-local-catalog.yml is what local-dev runs after every publish. The
static guards read the files. The functional cases run the entry point's refusals with
ansible-playbook and never reach a Semaphore: every controller URL is refused before anything is
sent, or is a loopback port with no listener.

- the order: the internal-extra-vars guard, the refusal, the publish, then the retirement;
- the retirement runs only when SEMAPHORE_URL is exactly the destination retire sends its token to;
- both local callers run this entry point, with the same flags, and neither runs setup-templates.yml;
- a remote controller, or a semaphore_url override, is refused before anything is published.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible
from fake_http import refused_port
from harness_sandbox import run_playbook

REPO = Path(__file__).resolve().parents[2]
CONVERGE = REPO / "platform/semaphore/converge-local-catalog.yml"
RETIRE = REPO / "platform/playbooks/retire-semaphore-templates.yml"
BOOTSTRAP = REPO / "platform/playbooks/bootstrap-local-dev.yml"
LOCAL_DEV = REPO / "scripts/local-dev.sh"
ENTRY = "semaphore/converge-local-catalog.yml"
PUBLISHER = "semaphore/setup-templates.yml"
GUARD = "../playbooks/refuse-internal-extra-vars.yml"
RETIRE_IMPORT = "../playbooks/retire-semaphore-templates.yml"
NESTED = "_extra_var_guard_nested"
REFUSAL = "Refusing: the local catalog"
PUBLISH_STARTED = "TASK [Resolve approved Semaphore runtime access]"
NOT_APPLIED = "Retirements NOT applied"


def _code(path: Path) -> str:
    """The file without comment lines, so a comment naming a path or flag is not a use of it."""
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def _plays() -> list[dict]:
    return yaml.safe_load(CONVERGE.read_text())


def _retire_destination() -> str:
    """The fixed destination retire-semaphore-templates.yml sends its token to, read from its vars."""
    for entry in yaml.safe_load(RETIRE.read_text()):
        if "vars" in entry and "_semaphore_url" in entry["vars"]:
            return entry["vars"]["_semaphore_url"]
    raise AssertionError("retire-semaphore-templates.yml declares no _semaphore_url")


# ── (a) order ────────────────────────────────────────────────────────────────


def test_plays_run_in_order_guard_refusal_publish_then_retire():
    plays = _plays()
    assert len(plays) == 4, [p.get("name") for p in plays]
    guard, refusal, publish, retire = plays
    assert guard["ansible.builtin.import_playbook"] == GUARD, "the internal-extra-vars guard is the first entry"
    assert refusal["hosts"] == "localhost" and "ansible.builtin.import_playbook" not in refusal
    # Import targets are relative to this file's directory, platform/semaphore.
    assert publish["ansible.builtin.import_playbook"] == "setup-templates.yml"
    assert retire["ansible.builtin.import_playbook"] == RETIRE_IMPORT


def test_the_refusal_names_the_url_the_override_and_the_flag():
    refusal = _plays()[1]
    asserts = [task for task in refusal["tasks"] if "ansible.builtin.assert" in task]
    assert len(asserts) == 1, "one refusal assert"
    that = " ".join(asserts[0]["ansible.builtin.assert"]["that"])
    assert "SEMAPHORE_URL" in that
    assert "semaphore_url is not defined" in that, "a semaphore_url extra var would move the publish"
    assert "semaphore_include_local_templates" in that


def test_the_retirement_import_passes_the_nested_guard_flag():
    retire = _plays()[3]
    assert retire.get("vars") == {NESTED: True}, (
        "the guard would find the publish's own facts in hostvars and refuse an honest run"
    )


# ── (b) the retirement runs only at the retire destination ───────────────────


def test_retirement_runs_only_when_the_url_is_the_retire_destination():
    destination = _retire_destination()
    assert destination.startswith("http://127.0.0.1:"), destination
    assert _plays()[3]["when"] == f"lookup('env', 'SEMAPHORE_URL') == '{destination}'"
    # The notice names the same destination, so "retire skipped" and "notice shown" cannot drift apart.
    notices = [task for task in _plays()[1]["tasks"] if "ansible.builtin.debug" in task]
    assert len(notices) == 1, "one not-applied notice in the refusal play"
    assert notices[0]["when"] == f"lookup('env', 'SEMAPHORE_URL') != '{destination}'"


# ── (c) both local callers run the entry point with the same flags ───────────


@pytest.mark.parametrize("path", [BOOTSTRAP, LOCAL_DEV], ids=lambda p: p.name)
def test_local_callers_run_the_entry_point_and_not_the_publisher(path):
    code = _code(path)
    assert ENTRY in code, f"{path.name} does not run {ENTRY}"
    assert PUBLISHER not in code, f"{path.name} still runs {PUBLISHER} directly"
    for flag in ("semaphore_project_id=", "semaphore_inventory_id=", "semaphore_environment_id=",
                 "semaphore_include_local_templates=true"):
        assert flag in code, f"{path.name} lost {flag}"


# ── (d) functional: the refusals, run by ansible-playbook ────────────────────


def _run(tmp_path: Path, url: str, *extra: str, token: str = "") -> subprocess.CompletedProcess:
    # run_playbook overlays the environment and cannot unset a key; an empty value reads as unset.
    env = {"SEMAPHORE_URL": url, "SEMAPHORE_TOKEN": token, "OPENBAO_ADDR": ""}
    return run_playbook(CONVERGE, tmp_path, *extra, env=env, timeout=180)


@needs_ansible
@pytest.mark.parametrize("url, extra", [
    ("https://semaphore.example.com", ["-e", "semaphore_include_local_templates=true"]),
    ("http://127.0.0.1:3000", []),
    ("http://127.0.0.1:3000", ["-e", "semaphore_include_local_templates=true",
                               "-e", "semaphore_url=https://semaphore.example.com"]),
], ids=["remote-controller", "no-local-flag", "semaphore-url-override"])
def test_a_refused_run_stops_before_anything_is_published(tmp_path, url, extra):
    proc = _run(tmp_path, url, *extra)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert REFUSAL in out, out
    assert PUBLISH_STARTED not in out, "the publish started before the refusal"


@needs_ansible
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_a_loopback_controller_on_another_port_is_accepted_and_skips_the_retirement(tmp_path, host):
    # Nothing listens on refused_port(), so the publish stops at its first request, after the refusal
    # and the notice. The token goes only to this loopback port, and nothing else is contacted.
    proc = _run(tmp_path, f"http://{host}:{refused_port()}", "-e", "semaphore_include_local_templates=true",
                token="not-a-real-token")
    out = proc.stdout + proc.stderr
    assert REFUSAL not in out, out
    assert NOT_APPLIED in out, out
    assert PUBLISH_STARTED in out, out
