"""No task reads `secrets` outside the task that binds it (docs/MISTAKES.md 10.17).

tasks/manage-secrets.yml defines `secrets` only as a `vars:` entry of its own template task;
at play level the name does not exist. Its resolved values are the `_resolved` fact. Twice a
task outside that template read `secrets` and failed in production: the agentgateway key guard
(task 1177) and the step-ca issuer add (task 1971), the second hidden by no_log. Jinja
templates are out of scope: they are rendered through manage-secrets' `_env_templates`, where
`secrets` is bound.
"""

import re

import playbook_yaml

# `secrets.x`, `secrets[...]`, `secrets | filter` and a bare `{{ secrets }}` all read the name.
READ = re.compile(r"\bsecrets\s*(?:\.|\[|\||\}\})")
BARE = ("when", "failed_when", "changed_when", "until")
TASK_LISTS = ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always")


def _tasks(node):
    if not isinstance(node, list):
        return
    for item in node:
        if isinstance(item, dict):
            yield item
            for key in TASK_LISTS:
                yield from _tasks(item.get(key))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def reads_outside_binding(doc) -> list[str]:
    found = []
    for task in _tasks(doc):
        binds = isinstance(task.get("vars"), dict) and "secrets" in task["vars"]
        # A task's own `vars:` (include vars too) are read unless the task binds `secrets`;
        # a play's `vars:` are scanned the same way, since a play binds nothing here.
        skip = (*TASK_LISTS, "name") + (("vars",) if binds else ())
        body = {k: v for k, v in task.items() if k not in skip}
        # Conditions take a bare Jinja expression (no braces); everything else is templated.
        bare = [s for k in BARE for s in _strings(body.get(k))]
        for mod in body.values():
            if isinstance(mod, dict):
                bare += list(_strings(mod.get("that")))
        exprs = bare + [s for s in _strings(body) if "{{" in s or "{%" in s]
        if not binds and any(READ.search(s) for s in exprs):
            found.append(str(task.get("name", "<unnamed>")))
    return found


def test_no_task_reads_secrets_outside_the_task_that_binds_it():
    found = []
    for path in playbook_yaml.files():
        rel = path.relative_to(playbook_yaml.REPO)
        found += [f"{rel}: {n}" for n in reads_outside_binding(playbook_yaml.load(path))]
    assert not found, "read `_resolved` (the fact manage-secrets sets), not `secrets`:\n" + "\n".join(found)


def test_the_guard_catches_both_recorded_shapes_and_passes_the_binding_task():
    doc = playbook_yaml.loads("""
- hosts: all
  tasks:
    - name: guard
      ansible.builtin.assert:
        that: secrets.vllm_api_key | length > 0
    - name: add
      ansible.builtin.command:
        argv: [x]
        stdin: "{{ secrets[item.secret] }}"
    - name: template
      ansible.builtin.template: {src: a, dest: "{{ secrets.path }}"}
      vars:
        secrets: "{{ _resolved }}"
    - name: include
      ansible.builtin.include_tasks: x.yml
      vars:
        _data: "{{ {'k': secrets.x} }}"
    - name: filter
      ansible.builtin.debug:
        msg: "{{ secrets | dict2items | length }}"
    - name: fine
      ansible.builtin.debug:
        msg: "{{ _resolved.x }} secret/services/x secrets are managed"
""")
    # The template task reads `secrets` inside its own binding, so it passes.
    assert reads_outside_binding(doc) == ["guard", "add", "include", "filter"]
