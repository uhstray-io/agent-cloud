"""No task `vars:` entry may make a name refer to itself (docs/MISTAKES.md 2.30).

Ansible templates lazily, so a task-level `name: "{{ _copy }}"`, where the play defines
`_copy: "{{ name | default(...) }}"`, resolves `name` through `_copy` back to `name` and the
task fails with "Recursive loop detected in template". The same goes for `name: "{{ name }}"`.
Deploy GitHub Runner failed exactly this way at its template step (Semaphore task 4609).
Pin the copy with set_fact first and map the raw name to the pinned fact instead.
"""

import re

import playbook_yaml

# An identifier not preceded by `.`: `item.name` refers to `item`, never to `name`.
REF = re.compile(r"(?<![.\w])([A-Za-z_]\w*)")
JINJA = re.compile(r"\{\{(.*?)\}\}|\{%(.*?)%\}", re.S)
# Quoted literals inside an expression, so `default('name')` names no variable.
LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def refs(value) -> set[str]:
    out = set()
    for text in playbook_yaml.strings(value):
        for a, b in JINJA.findall(text):
            out |= set(REF.findall(LITERAL.sub("", a or b)))
    return out


def reaches(start: set[str], target: str, scope: dict) -> bool:
    """Whether resolving `start` through `scope` (name -> its value) ever names `target`."""
    seen, todo = set(), list(start)
    while todo:
        name = todo.pop()
        if name == target:
            return True
        if name in seen or name not in scope:
            continue
        seen.add(name)
        todo.extend(refs(scope[name]))
    return False


def violations(doc) -> list[str]:
    found = []
    for play in doc if isinstance(doc, list) else []:
        if not isinstance(play, dict):
            continue
        play_vars = play.get("vars") if isinstance(play.get("vars"), dict) else {}
        for task in playbook_yaml.tasks([play]):
            if task is play or not isinstance(task.get("vars"), dict):
                continue
            for name, value in task["vars"].items():
                # Task entries shadow the play's. Resolve through both, with `name` itself
                # standing for the task-level value, so `a: "{{ b }}"` beside
                # `b: "{{ a }}"` is caught as well.
                scope = {k: v for k, v in {**play_vars, **task["vars"]}.items() if k != name}
                if reaches(refs(value), name, scope):
                    found.append(f"{task.get('name', '?')}: {name}")
    return found


def test_no_task_var_resolves_to_itself():
    bad = {
        str(path): v
        for path in playbook_yaml.files()
        if (v := violations(playbook_yaml.load(path)))
    }
    assert not bad, bad


def test_the_guard_catches_the_runner_shape():
    doc = playbook_yaml.loads(
        """
- hosts: all
  vars:
    _rver: "{{ github_runner_version | default('2.336.0') }}"
  tasks:
    - name: t
      ansible.builtin.debug: {msg: x}
      vars:
        github_runner_version: "{{ _rver }}"
    - name: self
      ansible.builtin.debug: {msg: x}
      vars:
        github_runner_name: "{{ github_runner_name }}"
    - name: pinned
      ansible.builtin.debug: {msg: x}
      vars:
        github_runner_version: "{{ _tpl_rver }}"
    - name: literal
      ansible.builtin.debug: {msg: x}
      vars:
        name: "{{ fallback | default('name') }}"
        title: "{{ item.title }}"
    - name: pair
      ansible.builtin.debug: {msg: x}
      vars:
        a: "{{ b }}"
        b: "{{ a }}"
"""
    )
    assert violations(doc) == [
        "t: github_runner_version",
        "self: github_runner_name",
        "pair: a",
        "pair: b",
    ]
