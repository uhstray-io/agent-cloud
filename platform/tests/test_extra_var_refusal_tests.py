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
    "test_executor_internal_overrides.py::test_every_launchable_playbook_refuses_internal_extra_vars_first":
        "static: reads the playbooks, sends no extra var",
    "test_harden_proves_key_first.py::test_every_gate_name_is_refused_as_an_extra_var_before_anything_runs":
        "static: reads the playbook, sends no extra var",
    "test_postiz_seed_input.py::test_an_address_pin_or_extra_var_refuses_before_any_write":
        "Python staging, not Ansible: refuses ANY extra var in the environment, by presence",
    "test_materialise_ssh_key.py::test_extra_vars_cannot_move_the_pinned_known_hosts":
        "lifted section without the run's first play: tests the inner value check, which holds "
        "plain values only; every real caller imports refuse-internal-extra-vars.yml first "
        "(test_every_playbook_using_the_value_probe_refuses_by_name_first)",
    "test_materialise_ssh_key.py::test_extra_vars_cannot_misdirect_the_key_or_its_wipe":
        "lifted section, as above",
    "test_materialise_ssh_key.py::test_extra_vars_cannot_widen_the_wipe":
        "lifted section, as above",
    "test_o11y_isolated_restore.py::test_prerequisite_chain_rejects_link_forgery_duplicate_tasks_and_stale_artifact":
        "pure Python validator: rejects forged authenticated API receipt fields; no Jinja extra-var sink",
    "test_o11y_isolated_restore.py::test_restore_task_proof_selects_restore_contract_and_rejects_receipt_forgery":
        "pure Python validator: rejects forged authenticated task receipt fields; no Jinja extra-var sink",
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
        helpers = _guard_helpers(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_") and REFUSAL.search(node.name):
                found[f"{path.name}::{node.name}"] = _uses_forgeries(node, src, built, helpers)
    return found


# The run-start guard's own refusal message (refuse-internal-extra-vars.yml).
GUARD_TEXT = "set from outside the playbook"


def _constant_text(expr) -> str | None:
    """The literal text of a string constant or f-string (its constant parts), else None."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.JoinedStr):
        return "".join(v.value for v in expr.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def _positive_guard_compare(expr) -> bool:
    """`"...set from outside the playbook" in <output>`: a positive membership test, never `not in`."""
    return (isinstance(expr, ast.Compare) and len(expr.ops) == 1 and isinstance(expr.ops[0], ast.In)
            and GUARD_TEXT in (_constant_text(expr.left) or ""))


def _guard_helpers(tree) -> set[str]:
    """Module-level functions that return a positive membership test of the guard's message
    (test_agw_conformance.py's `_refused_first`); a same-named function checking anything else
    does not count."""
    return {f.name for f in tree.body if isinstance(f, ast.FunctionDef)
            and any(isinstance(r, ast.Return) and r.value is not None and _positive_guard_compare(r.value)
                    for r in ast.walk(f))}


def _conjuncts(expr):
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.And):
        for value in expr.values:
            yield from _conjuncts(value)
    else:
        yield expr


def _by_name_args(func) -> list:
    """The first argument of every forgeries.by_name call in a test's decorators and body: the
    literal name, or None when it is computed."""
    names = []
    for node in [*func.decorator_list, func]:
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and getattr(call.func, "attr", getattr(call.func, "id", None)) == "by_name":
                arg = call.args[0] if call.args else None
                names.append(arg.value if isinstance(arg, ast.Constant) else None)
    return names


def _asserts_guard_message(func, helpers: set[str], names: list) -> bool:
    """An `assert` (or a conjunct of one) that positively checks the guard's message for the forged
    name: a literal message must name every literal by_name name; a helper's literal name argument
    must equal it. A comment, a docstring or a `not in` never counts."""
    literal = {n for n in names if n is not None}
    for node in ast.walk(func):
        if not isinstance(node, ast.Assert):
            continue
        for expr in _conjuncts(node.test):
            if _positive_guard_compare(expr):
                text = _constant_text(expr.left)
                if isinstance(expr.left, ast.Constant) and any(
                        f"Refusing to run: {n} {GUARD_TEXT}" not in text for n in literal):
                    continue
                return True
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id in helpers:
                arg = expr.args[1] if len(expr.args) > 1 else None
                if isinstance(arg, ast.Constant) and any(n != arg.value for n in literal):
                    continue
                return True
    return False


def _uses_forgeries(func, src: str, built: set[str], helpers: set[str]) -> bool:
    """All three spellings, or `by_name` (plain only) with an assertion of the run-start guard's
    own message: that guard refuses by name and never renders the value (plan 21 D1)."""
    text = " ".join(ast.get_source_segment(src, d) for d in func.decorator_list)
    text += ast.get_source_segment(src, func)
    if "templated_forgeries" in text or any(re.search(rf"\b{n}\b", text) for n in built):
        return True
    names = _by_name_args(func)
    return bool(names) and _asserts_guard_message(func, helpers, names)


def _classify(src: str) -> bool:
    """_uses_forgeries for the one test function in a synthetic module."""
    tree = ast.parse(src)
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"))
    return _uses_forgeries(func, src, set(), _guard_helpers(tree))


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


HELPER = (
    "def _refused_first(out, name):\n"
    "    return f'Refusing to run: {name} set from outside the playbook' in out\n\n"
)


def _case(body: str, helper: str = "") -> str:
    return helper + "def test_forged(out):\n    f = forgeries.by_name('_x', 1, 2)\n" + body


def test_by_name_is_accepted_only_with_a_positive_assert_of_the_guard_message():
    assert _classify(_case("    assert 'Refusing to run: _x set from outside the playbook' in out\n"))
    assert _classify(_case("    assert f'Refusing to run: {n} set from outside the playbook' in out\n"))
    assert _classify(_case("    assert out.rc != 0 and _refused_first(out, '_x'), out\n", HELPER))
    # A comment, a negated check, another name, or a helper that checks nothing: refused.
    comment_only = "    # Refusing to run: _x set from outside the playbook\n    assert 'refused' in out\n"
    assert not _classify(_case(comment_only))
    assert not _classify(_case("    assert 'Refusing to run: _x set from outside the playbook' not in out\n"))
    assert not _classify(_case("    assert 'Refusing to run: _y set from outside the playbook' in out\n"))
    assert not _classify(_case("    assert _refused_first(out, '_y')\n", HELPER))
    fake = "def _refused_first(out, name):\n    return True\n\n"
    assert not _classify(_case("    assert _refused_first(out, '_x')\n", fake))


def test_by_name_refuses_a_name_the_guard_references():
    assert {"_extra_var_guard_nested", "_conn_names", "_conn_forged"} <= forgeries.GUARD_REFERENCED
    for name in ("_extra_var_guard_nested", "_conn_forged", "no_prefix"):
        with pytest.raises(AssertionError):
            forgeries.by_name(name, None, True)
    assert [p.id for p in forgeries.by_name("_forged_by_launch", None, True)] == ["plain"]


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
