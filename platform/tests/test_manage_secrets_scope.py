"""No task reads `secrets` outside the task that binds it (docs/MISTAKES.md 10.17).

tasks/manage-secrets.yml defines `secrets` only as a `vars:` entry of its own template task;
at play level the name does not exist. Its resolved values are the `_resolved` fact. Twice a
task outside that template read `secrets` and failed in production: the agentgateway key guard
(task 1177) and the step-ca issuer add (task 1971), the second hidden by no_log. Jinja
templates are out of scope: they are rendered through manage-secrets' `_env_templates`, where
`secrets` is bound.

The same file's outputs, `_resolved` and `_shared`, are held here too: every task that sets
them hides its result, and each include starts them empty.
"""

import json
import re

import harness_sandbox
import playbook_yaml
import yaml

MANAGE_SECRETS = playbook_yaml.REPO / "platform/playbooks/tasks/manage-secrets.yml"
OUTPUTS = {"_resolved", "_shared"}

# `secrets.x`, `secrets[...]`, `secrets | filter` and a bare `{{ secrets }}` all read the name.
READ = re.compile(r"\bsecrets\s*(?:\.|\[|\||\}\})")
BARE = ("when", "failed_when", "changed_when", "until")


def reads_outside_binding(doc) -> list[str]:
    found = []
    for task in playbook_yaml.tasks(doc):
        binds = isinstance(task.get("vars"), dict) and "secrets" in task["vars"]
        # A task's own `vars:` (include vars too) are read unless the task binds `secrets`;
        # a play's `vars:` are scanned the same way, since a play binds nothing here.
        skip = (*playbook_yaml.TASK_LISTS, "name") + (("vars",) if binds else ())
        body = {k: v for k, v in task.items() if k not in skip}
        # Conditions take a bare Jinja expression (no braces); everything else is templated.
        bare = [s for k in BARE for s in playbook_yaml.strings(body.get(k))]
        for mod in body.values():
            if isinstance(mod, dict):
                bare += list(playbook_yaml.strings(mod.get("that")))
        exprs = bare + [s for s in playbook_yaml.strings(body) if "{{" in s or "{%" in s]
        if not binds and any(READ.search(s) for s in exprs):
            found.append(str(task.get("name", "<unnamed>")))
    return found


def test_no_task_reads_secrets_outside_the_task_that_binds_it():
    found = []
    for path in playbook_yaml.files():
        rel = path.relative_to(playbook_yaml.REPO)
        found += [f"{rel}: {n}" for n in reads_outside_binding(playbook_yaml.load(path))]
    assert not found, (
        "read `_resolved` (the service's own secrets) or `_shared` (its shared "
        "reads), not `secrets`:\n" + "\n".join(found)
    )


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
    - name: bare
      ansible.builtin.copy: {content: "{{ secrets }}", dest: /x}
    - name: fine
      ansible.builtin.debug:
        msg: "{{ _resolved.x }} secret/services/x secrets are managed"
""")
    # The template task reads `secrets` inside its own binding, so it passes.
    assert reads_outside_binding(doc) == ["guard", "add", "include", "filter", "bare"]


def test_every_task_that_sets_the_outputs_hides_its_result():
    # A set_fact result carries the new value: at -v an unhidden one prints every secret
    # ("Add service URL" did, until this guard).
    shown = []
    for path in playbook_yaml.files():
        for task in playbook_yaml.tasks(playbook_yaml.load(path)):
            fact = next((v for k, v in task.items() if k.endswith("set_fact")), None)
            if isinstance(fact, dict) and OUTPUTS & fact.keys() and task.get("no_log") is not True:
                shown.append(f"{path.relative_to(playbook_yaml.REPO)}: {task.get('name', '<unnamed>')}")
    assert not shown, "set `no_log: true` on:\n" + "\n".join(shown)


def test_a_second_include_does_not_carry_the_first_services_secrets(tmp_path):
    # The store step writes all of `_resolved` to secret/services/<service_name>: a value
    # left from an earlier include on the same host would be stored under this service.
    tasks = {t["name"]: t for t in playbook_yaml.load(MANAGE_SECRETS)}
    lifted = [tasks["Start from empty outputs"], tasks["Resolve secrets (reuse existing, generate missing)"]]
    first = [
        {
            "name": "first service",
            "ansible.builtin.set_fact": {
                "_existing": {"a": "1"},
                "_secret_definitions": [{"name": "a", "type": "random"}],
            },
        },
        *lifted,
    ]
    second = [
        {
            "name": "second service",
            "ansible.builtin.set_fact": {
                "_existing": {"b": "2"},
                "_secret_definitions": [{"name": "b", "type": "random"}],
            },
        },
        *lifted,
    ]
    out = tmp_path / "resolved.json"
    dump = {
        "name": "dump",
        "ansible.builtin.copy": {"content": "{{ _resolved | to_json }}", "dest": str(out), "mode": "0600"},
    }
    play = [{"hosts": "localhost", "gather_facts": False, "tasks": [*first, *second, dump]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    r = harness_sandbox.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(tmp_path / "play.yml")],
        tmp_path,
        cwd=playbook_yaml.REPO,
        env=harness_sandbox.env_for(tmp_path),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(out.read_text()) == {"b": "2"}


def test_the_reset_is_the_first_task_and_unconditional():
    first = playbook_yaml.load(MANAGE_SECRETS)[0]
    assert first["name"] == "Start from empty outputs"
    assert first["ansible.builtin.set_fact"] == {"_resolved": {}, "_shared": {}}
    assert "when" not in first
