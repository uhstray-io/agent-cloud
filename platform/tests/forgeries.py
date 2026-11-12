"""Forged extra vars for the tests that prove an extra-var refusal holds.

An extra var is not a value but a Jinja template, rendered afresh wherever it is read. A
refusal that checks what the name RENDERS to can be shown one value while the work reads
another, so the strongest member of the input class is a template (docs/MISTAKES.md 1.15):

  plain     the forged value itself
  context   the honest value while a loop variable is set or the play's name says it
            refuses (where a probe or a guard play looks), the forgery everywhere else
  stateful  the honest value on its first rendering only, then the forgery: no check that
            renders the name before the work reads it can tell the difference

test_extra_var_refusal_tests.py requires every refusal test to parametrize over these, or to use
`by_name` (plain only) for a refusal the run-start guard makes by name (plan 21, D1).

Two helpers sit beside them, not among them: `counting` measures how often a run renders a name, and
`honest_for` is honest for a given number of renderings, then forged (plan 24). See their docstrings.

Imported as `import forgeries`, like playbook_yaml: pytest puts this directory on sys.path.
"""

import json
import re
from pathlib import Path

import playbook_yaml
import pytest

# Every underscore name the run-start guard itself references, read from the guard file (strings
# and keys, comments excluded by parsing). The guard reads the forged value of these names (its
# own `when:` reads _extra_var_guard_nested), so a test forging one keeps all three spellings.
# Over-inclusion only keeps more spellings: the refusal pattern's `_pass` fragments land here too.
_GUARD = playbook_yaml.REPO / "platform/playbooks" / playbook_yaml.OVERRIDE_GUARD
GUARD_REFERENCED = frozenset(
    m.group(0)
    for s in playbook_yaml.strings(playbook_yaml.load(_GUARD), keys=True)
    for m in re.finditer(r"(?<![A-Za-z0-9])_[A-Za-z][A-Za-z0-9_]*", s)
)


def _string(text: str) -> str:
    """A single-quoted Jinja string; refuses text that would need escaping."""
    assert "'" not in text and "\\" not in text, f"no quote or backslash in a forgery: {text!r}"
    return "'" + text + "'"


def _literal(value) -> str:
    """A Jinja expression for a JSON-able value, whatever its type."""
    return "(" + _string(json.dumps(value)) + " | from_json)"


def _render_counter(name: str, tmp_path: Path) -> tuple[Path, str]:
    """The counter file for one name's renderings, and the shell that counts one rendering: it prints
    the number of renderings BEFORE this one and leaves the file one higher."""
    counter = tmp_path / f"forgery-renders-{name}"
    assert " " not in str(counter), counter
    return counter, f"n=$(cat {counter} 2>/dev/null || echo 0); echo $((n+1)) > {counter}; echo $n"


def templated_forgeries(name: str, honest, forged) -> list:
    """pytest params, ids plain/context/stateful. Each is a function of the test's tmp_path
    (the stateful template counts its renderings in a file there) returning the -e JSON.
    `honest` and `forged` may themselves be functions of tmp_path, for a value that names a
    path inside it."""

    def values(tmp_path: Path):
        return tuple(v(tmp_path) if callable(v) else v for v in (honest, forged))

    def plain(tmp_path: Path) -> str:
        return json.dumps({name: values(tmp_path)[1]})

    def context(tmp_path: Path) -> str:
        good, bad = values(tmp_path)
        cond = "(ansible_loop_var is defined or (ansible_play_name | default('')) is search('(?i)refuse'))"
        return json.dumps({name: "{{ " + cond + " | ternary(" + _literal(good) + ", " + _literal(bad) + ") }}"})

    def stateful(tmp_path: Path) -> str:
        good, bad = values(tmp_path)
        _, shell = _render_counter(name, tmp_path)
        cond = "(lookup('ansible.builtin.pipe', " + _string(shell) + ") | int < 1)"
        return json.dumps({name: "{{ " + cond + " | ternary(" + _literal(good) + ", " + _literal(bad) + ") }}"})

    return [pytest.param(plain, id="plain"), pytest.param(context, id="context"),
            pytest.param(stateful, id="stateful")]


def by_name(name: str, honest, forged) -> list:
    """The plain spelling only, for a refusal the run-start guard makes BY NAME.

    refuse-internal-extra-vars.yml refuses an underscore name by its presence in hostvars and
    never renders its value, so the templated spellings cannot change the outcome; the guard's
    own test proves it (test_the_guard_ends_the_run_before_any_other_play asserts the stateful
    forgery is never rendered). Plan 21 D1, docs/MISTAKES.md 1.15. A name the guard itself
    references, or one without the underscore prefix, keeps all three spellings: refused here."""
    assert name.startswith("_"), f"by_name is for underscore names the guard refuses by name: {name}"
    assert name not in GUARD_REFERENCED, (
        f"{name} is referenced by the guard itself, which may render it: use templated_forgeries")
    return templated_forgeries(name, honest, forged)[:1]


def counting(name: str, value, tmp_path: Path) -> tuple[str, Path]:
    """-e JSON that renders `value` at every reading of the name, and counts those renderings in a file.

    Returns (the -e JSON, the counter path). After a run the counter holds how many times the name was
    rendered. Plan 24: a run's readings are measured with this first, and that count goes to honest_for.
    """
    counter, shell = _render_counter(name, tmp_path)
    cond = "(lookup('ansible.builtin.pipe', " + _string(shell) + ") | int >= 0)"
    rendered = "{{ " + cond + " | ternary(" + _literal(value) + ", " + _literal(value) + ") }}"
    return json.dumps({name: rendered}), counter


def honest_for(name: str, honest, forged, renders: int, tmp_path: Path) -> str:
    """-e JSON that renders `honest` for the first `renders` renderings of the name, then `forged`.

    stateful is the renders=1 case. Plan 24: the identity check reads local_mode more than once, and
    test_local_mode_identity.py refuses the stateful spelling on a production inventory, so the check
    itself refuses `stateful`. Give this the count `counting` measured for the check: any reader after the
    check then gets the forgery, so a raw read shows through. Not a parametrized spelling: a refusal test
    that uses it also parametrizes over templated_forgeries.
    """
    assert isinstance(renders, int) and renders >= 1, renders
    _, shell = _render_counter(name, tmp_path)
    cond = "(lookup('ansible.builtin.pipe', " + _string(shell) + ") | int < " + str(renders) + ")"
    return json.dumps({name: "{{ " + cond + " | ternary(" + _literal(honest) + ", " + _literal(forged) + ") }}"})
