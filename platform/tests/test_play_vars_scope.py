"""A play's `vars:` do not reach the next play (docs/MISTAKES.md 10.17).

Ansible scopes a play's `vars:` to that play. A later play that reads the same name sees it
undefined, and a `when:` on it fails the run. deploy-console.yml defined
`_console_launch_gate_declared` in Phase 1 and read it in Phase 3, so every console deploy after
agent-cloud #934 failed at its verify play (Semaphore task 14234, check mode). The structure tests
that shipped with it never evaluated Phase 3.

The guard: in every playbook, an internal name (leading underscore) that some play defines in
its `vars:` may be read by another play only when that play defines it too (play, block or task
`vars`, `register`, `set_fact`, `loop_var`), or when the file sets it as a host fact
(`set_fact`, `register` or an `add_host` variable, which do carry across plays). Inventory names are out of scope: a
name without the underscore may come from inventory.
"""

import re

import playbook_yaml

PLAY_TASK_KEYS = ("pre_tasks", "tasks", "post_tasks", "handlers")
# add_host variables become the new host's variables, so they reach later plays.
ADD_HOST = ("ansible.builtin.add_host", "add_host")


def _defined_in_play(play: dict) -> set[str]:
    names = set(play.get("vars") or {})
    for key in PLAY_TASK_KEYS:
        for task in playbook_yaml.tasks(play.get(key)):
            names.update(task.get("vars") or {})
            if isinstance(task.get("register"), str):
                names.add(task["register"])
            for module in playbook_yaml.SET_FACT:
                if isinstance(task.get(module), dict):
                    names.update(task[module])
            loop_var = (task.get("loop_control") or {}).get("loop_var")
            if isinstance(loop_var, str):
                names.add(loop_var)
    return names


def _host_facts(doc: list) -> set[str]:
    """Names a play sets as host facts: they outlive the play."""
    facts: set[str] = set()
    for play in doc:
        if not isinstance(play, dict):
            continue
        for key in PLAY_TASK_KEYS:
            for task in playbook_yaml.tasks(play.get(key)):
                if isinstance(task.get("register"), str):
                    facts.add(task["register"])
                for module in playbook_yaml.SET_FACT:
                    if isinstance(task.get(module), dict):
                        facts.update(task[module])
                for module in ADD_HOST:
                    if isinstance(task.get(module), dict):
                        facts.update(task[module])
    return facts


def leaks(doc) -> list[str]:
    """`play N reads _name, defined only in another play's vars`, for one parsed playbook."""
    if not isinstance(doc, list):
        return []
    plays = [p for p in doc if isinstance(p, dict) and ("hosts" in p)]
    play_vars = {name for play in plays for name in (play.get("vars") or {}) if str(name).startswith("_")}
    facts = _host_facts(plays)
    found = []
    for index, play in enumerate(plays):
        own = _defined_in_play(play)
        text = "\n".join(playbook_yaml.strings(play))
        for name in sorted(play_vars - own - facts):
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text):
                label = f"play {index + 1} ({play.get('name', '?')})"
                found.append(f"{label} reads {name}, defined only in another play's vars")
    return found


def test_no_play_reads_another_plays_vars():
    offenders = []
    for path in playbook_yaml.files():
        for leak in leaks(playbook_yaml.load(path)):
            offenders.append(f"{path.relative_to(playbook_yaml.REPO)}: {leak}")
    assert not offenders, "\n".join(offenders)


def test_the_guard_catches_a_play_var_read_in_the_next_play():
    doc = playbook_yaml.loads(
        """
- name: one
  hosts: all
  vars:
    _flag: true
  tasks:
    - ansible.builtin.debug: {msg: ok}
      when: _flag
- name: two
  hosts: all
  tasks:
    - ansible.builtin.debug: {msg: ok}
      when: _flag | bool
"""
    )
    assert leaks(doc) == ["play 2 (two) reads _flag, defined only in another play's vars"]


def test_a_name_both_plays_define_or_a_host_fact_is_not_a_leak():
    doc = playbook_yaml.loads(
        """
- name: one
  hosts: all
  vars:
    _flag: true
    _shared: 1
  tasks:
    - ansible.builtin.set_fact: {_shared: 2}
- name: two
  hosts: all
  vars:
    _flag: false
  tasks:
    - ansible.builtin.debug: {msg: "{{ _flag }} {{ _shared }}"}
"""
    )
    assert leaks(doc) == []
