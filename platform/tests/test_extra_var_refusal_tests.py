"""Every test that proves an extra-var refusal also tries a templated forgery.

docs/MISTAKES.md 1.15: a refusal was called closed three times after tests that sent plain
values only, and each time a TEMPLATED extra var got through, because a check that reads what
a name renders to can be shown one value while the work reads another. forgeries.py builds the
plain, context-keyed and stateful forgeries; this file requires every refusal test to
parametrize over them, or to be listed below with the limit that stands instead.
"""

import ast
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import forgeries
import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"

# A test that claims an extra var is refused, or cannot do something, or is forged.
REFUSAL = re.compile(r"(refuse|refused|refuses|cannot).*extra_var|extra_var.*(refuse|refused|refuses|cannot)|forg")

# Refusal tests that do not parametrize over templated_forgeries, each with the reason. Exact:
# a test that starts using the helper must leave this list, and a new one must justify itself.
NOT_TEMPLATED = {
    "test_executor_internal_overrides.py::test_every_executor_refuses_internal_extra_vars_first":
        "static: reads the playbooks, sends no extra var",
    "test_harden_proves_key_first.py::test_every_gate_name_is_refused_as_an_extra_var_before_anything_runs":
        "static: reads the playbook, sends no extra var",
    "test_postiz_seed_input.py::test_an_address_pin_or_extra_var_refuses_before_any_write":
        "Python staging, not Ansible: refuses ANY extra var in the environment, by presence",
    "test_postiz_access_only.py::test_injected_extra_vars_cannot_move_the_login_or_the_write":
        "value check, plain values only: a templated override is a stated limit in the header "
        "of tasks/assert-bao-addr-declared.yml (docs/MISTAKES.md 1.15 occurrence 2)",
    "test_materialise_ssh_key.py::test_extra_vars_cannot_move_the_pinned_known_hosts":
        "lifted section without the run's first play: tests the inner value check, which holds "
        "plain values only; every real caller imports refuse-internal-extra-vars.yml first "
        "(test_every_playbook_using_the_value_probe_refuses_by_name_first)",
    "test_materialise_ssh_key.py::test_extra_vars_cannot_misdirect_the_key_or_its_wipe":
        "lifted section, as above",
    "test_materialise_ssh_key.py::test_extra_vars_cannot_widen_the_wipe":
        "lifted section, as above",
}


def _test_files() -> list[Path]:
    paths = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["testpaths"]
    return sorted(f for p in paths for f in (REPO / p).glob("test_*.py"))


def _refusal_tests() -> dict[str, bool]:
    """file::test -> whether it parametrizes over templated_forgeries (in its decorators or
    body, directly or through a module-level name built from it)."""
    found = {}
    for path in _test_files():
        src = path.read_text()
        tree = ast.parse(src)
        built = {t.id for node in tree.body if isinstance(node, ast.Assign)
                 and "templated_forgeries" in ast.get_source_segment(src, node)
                 for t in node.targets if isinstance(t, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_") and REFUSAL.search(node.name):
                text = " ".join(ast.get_source_segment(src, d) for d in node.decorator_list)
                text += ast.get_source_segment(src, node)
                used = "templated_forgeries" in text or any(re.search(rf"\b{n}\b", text) for n in built)
                found[f"{path.name}::{node.name}"] = used
    return found


def test_every_refusal_test_tries_a_templated_forgery():
    found = _refusal_tests()
    assert len(found) >= len(NOT_TEMPLATED), found
    untemplated = {k for k, used in found.items() if not used}
    assert untemplated == set(NOT_TEMPLATED), (
        f"parametrize over forgeries.templated_forgeries, or list with the limit that stands: "
        f"missing {sorted(untemplated - set(NOT_TEMPLATED))}, stale {sorted(set(NOT_TEMPLATED) - untemplated)}")


def test_the_scan_finds_a_refusal_test_by_its_name():
    assert REFUSAL.search("test_an_input_set_as_an_extra_var_is_refused")
    assert REFUSAL.search("test_refuses_an_extra_var_forging_a_verdict")
    assert REFUSAL.search("test_extra_vars_cannot_widen_the_wipe")
    assert not REFUSAL.search("test_extra_vars_the_task_does_not_own_are_still_accepted")


VALUE_PROBE = "refuse-var-overrides.yml"


def _includes(path: Path) -> set[str]:
    return {Path(s).name for t in playbook_yaml.tasks(playbook_yaml.load(path)) for s in playbook_yaml.strings(
        [t.get(k) for k in ("ansible.builtin.include_tasks", "ansible.builtin.import_tasks",
                            "include_tasks", "import_tasks")]) if s.endswith(".yml")}


def test_every_playbook_using_the_value_probe_refuses_by_name_first():
    # tasks/refuse-var-overrides.yml compares what a name renders to, so a template defeats it
    # (proved below). A playbook that relies on it, directly or through a task file, must open
    # with refuse-internal-extra-vars.yml, which refuses by name before anything renders.
    task_files = {p.name for p in (PLAYBOOKS / "tasks").glob("*.yml") if VALUE_PROBE in _includes(p)}
    users = [p for p in sorted(PLAYBOOKS.glob("*.yml")) if _includes(p) & (task_files | {VALUE_PROBE})]
    assert users, task_files
    for path in users:
        first = playbook_yaml.load(path)[0]
        assert first.get("ansible.builtin.import_playbook") == playbook_yaml.OVERRIDE_GUARD, path.name


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("_x", "__override_probe__", "FORGED"))
def test_the_templates_defeat_a_value_probe(tmp_path, forge):
    # The adversary has teeth: against tasks/refuse-var-overrides.yml alone, the plain forgery
    # is refused and both templates reach the work with the forged value.
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": [
        {"ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks" / VALUE_PROBE), "loop": ["_x"],
         "loop_control": {"loop_var": "_rvo_name"}},
        {"ansible.builtin.set_fact": {"_x": "honest"}},
        {"ansible.builtin.debug": {"msg": "USED {{ _x }}"}}]}]
    path = tmp_path / "play.yml"
    path.write_text(yaml.safe_dump(play))
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path), "-e", forge(tmp_path)],
                          cwd=REPO, text=True, capture_output=True, check=False, timeout=120)
    if forge.__name__ == "plain":
        assert proc.returncode != 0 and "_x is internal to this play" in proc.stdout, proc.stdout
    else:
        assert proc.returncode == 0 and '"msg": "USED FORGED"' in proc.stdout, proc.stdout + proc.stderr
