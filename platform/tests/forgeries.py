"""Forged extra vars for the tests that prove an extra-var refusal holds.

An extra var is not a value but a Jinja template, rendered afresh wherever it is read. A
refusal that checks what the name RENDERS to can be shown one value while the work reads
another, so the strongest member of the input class is a template (docs/MISTAKES.md 1.15):

  plain     the forged value itself
  context   the honest value while a loop variable is set or the play's name says it
            refuses (where a probe or a guard play looks), the forgery everywhere else
  stateful  the honest value on its first rendering only, then the forgery: no check that
            renders the name before the work reads it can tell the difference

test_extra_var_refusal_tests.py requires every refusal test to parametrize over these.

Imported as `import forgeries`, like playbook_yaml: pytest puts this directory on sys.path.
"""

import json
from pathlib import Path

import pytest


def _string(text: str) -> str:
    """A single-quoted Jinja string; refuses text that would need escaping."""
    assert "'" not in text and "\\" not in text, f"no quote or backslash in a forgery: {text!r}"
    return "'" + text + "'"


def _literal(value) -> str:
    """A Jinja expression for a JSON-able value, whatever its type."""
    return "(" + _string(json.dumps(value)) + " | from_json)"


def templated_forgeries(name: str, honest, forged) -> list:
    """pytest params, ids plain/context/stateful. Each is a function of the test's tmp_path
    (the stateful template counts its renderings in a file there) returning the -e JSON."""

    def plain(tmp_path: Path) -> str:
        return json.dumps({name: forged})

    def context(tmp_path: Path) -> str:
        cond = "(ansible_loop_var is defined or (ansible_play_name | default('')) is search('(?i)refuse'))"
        return json.dumps({name: "{{ " + cond + " | ternary(" + _literal(honest) + ", " + _literal(forged) + ") }}"})

    def stateful(tmp_path: Path) -> str:
        counter = tmp_path / f"forgery-renders-{name}"
        assert " " not in str(counter), counter
        shell = f"n=$(cat {counter} 2>/dev/null || echo 0); echo $((n+1)) > {counter}; echo $n"
        cond = "(lookup('ansible.builtin.pipe', " + _string(shell) + ") | int < 1)"
        return json.dumps({name: "{{ " + cond + " | ternary(" + _literal(honest) + ", " + _literal(forged) + ") }}"})

    return [pytest.param(plain, id="plain"), pytest.param(context, id="context"),
            pytest.param(stateful, id="stateful")]
