"""A visible task never loops over the registered results of a credential-bearing request.

A failed loop item is printed whole, and a registered `uri` result carries the request it
made, headers included (`invocation.module_args`). `loop_control.label` only shortens the
summary line. Reproduced on ansible-core 2.16.18, 2.19.13 and 2.20.8 at default verbosity,
not on 2.21.0, so a local run on a newer core cannot catch it (docs/MISTAKES.md 4.6).

The safe shape loops over the clean input and indexes into the results (`index_var`).

ponytail: a register is matched within one file only; a register consumed across an
include_tasks boundary is not seen. Extend the walk when such a consumer appears.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("platform/playbooks", "platform/semaphore")
URI = {"uri", "ansible.builtin.uri"}
CREDENTIAL_ARGS = {"headers", "url_password"}


def _tasks(node):
    """Every task in a play/task list, blocks included, in file order."""
    if not isinstance(node, list):
        return
    for item in node:
        if not isinstance(item, dict):
            continue
        yield item
        for key in ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always"):
            yield from _tasks(item.get(key))


def violations(text: str) -> list[str]:
    tasks = list(_tasks(yaml.safe_load(text)))
    bearing = {
        t["register"] for t in tasks
        if t.get("register") and any(isinstance(t.get(m), dict) and CREDENTIAL_ARGS & set(t[m]) for m in URI)
    }
    found = []
    for t in tasks:
        loop = str(t.get("loop", "")) + str(t.get("with_items", ""))
        for reg in bearing:
            # `<reg>.results | length` (an index range) is the safe shape, not a violation.
            if re.search(rf"\b{re.escape(reg)}\.results\b(?!\s*\|\s*length)", loop) and t.get("no_log") is not True:
                found.append(f"{t.get('name', '<unnamed>')!r} loops over {reg}.results")
    return found


def test_no_visible_task_loops_over_credential_bearing_results():
    found = []
    for base in SCANNED:
        for path in sorted((ROOT / base).rglob("*.yml")):
            try:
                found += [f"{path.relative_to(ROOT)}: {v}" for v in violations(path.read_text())]
            except yaml.YAMLError:
                continue  # templates and non-Ansible YAML; yamllint owns syntax
    assert not found, "\n".join(found)


def test_the_guard_catches_the_shape_it_exists_for():
    leaking = """
- hosts: localhost
  tasks:
    - ansible.builtin.uri: {url: "https://x", headers: {Authorization: "Bearer {{ t }}"}}
      register: _reads
      no_log: true
      loop: [a]
    - ansible.builtin.assert: {that: false}
      loop: "{{ _reads.results }}"
      loop_control: {label: "{{ item.item }}"}
"""
    assert violations(leaking) == ["'<unnamed>' loops over _reads.results"]
    assert violations(leaking.replace("{that: false}", "{that: false}\n      no_log: true")) == []
    for safe in ('loop: [a]', 'loop: "{{ range(_reads.results | length) | list }}"'):
        assert violations(leaking.replace('loop: "{{ _reads.results }}"', safe)) == []
    filtered = 'loop: "{{ _reads.results | selectattr(\'json\', \'defined\') }}"'
    assert violations(leaking.replace('loop: "{{ _reads.results }}"', filtered)) == [
        "'<unnamed>' loops over _reads.results"]
