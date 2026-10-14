"""A templated `delegate_to` must resolve even when its task is skipped.

ansible-core 2.18 templated `delegate_to` with `fail_on_undefined=False` and left an
unresolvable expression as literal text, so a task its `when` skipped never noticed. 2.19+
templates it strictly, BEFORE the task's own `when` and the `when` a block hands down: an
expression that raises (`_exporter.host` on `{}`, `groups['o11y_svc'][0]` with no such
group) fails the run on a task that would have been skipped. Production dry run 4888
(ansible-core 2.20.8): `o11y-fault-drill.yml drill=probe` died with "object of type 'dict'
has no attribute 'host'" on the exporter-only stop task; the same drill passed on 2.18.15
(task 3295).

The rule: a templated `delegate_to` carries a `default(...)`, or is listed in AUDITED with the
reason its value always resolves wherever its task can be reached.

Both guards read the repository's playbooks parsed once per session (playbook_yaml.py).
"""

from pathlib import Path

import playbook_yaml

# (file relative to platform/playbooks, the exact delegate_to value) -> why it always resolves.
AUDITED = {
    (
        "tasks/distribute-caddy-site.yml",
        "{{ _caddy_target }}",
    ): "_caddy_target is set by an unconditional set_fact in the same file, which itself fails on an empty group",
    (
        "tasks/issue-internal-leaf.yml",
        "{{ _leaf_ca_host }}",
    ): "defined as `_mint_ca_host | default(inventory_hostname)` in the same file",
    (
        "tasks/distribute-ca-root.yml",
        "{{ _ca_root_host }}",
    ): "defined as `_ca_host | default(inventory_hostname)` in the same file",
    ("tasks/pin-ssh-host-key.yml", "{{ inventory_hostname }}"): "a magic variable, always defined",
    (
        "renew-internal-certs.yml",
        "{{ _ca_host }}",
    ): "the play only has hosts when the plan holds leaves, which needs exactly one step_ca_svc host",
    (
        "issue-internal-leaf.yml",
        "{{ groups['step_ca_svc'] | first }}",
    ): "the first task of the play asserts exactly one step_ca_svc host, with no `when`",
    (
        "o11y-fault-drill.yml",
        "{{ _probe_health_from }}",
    ): "a play var that is always defined (inventory_hostname or 'localhost')",
}


def unguarded(doc, rel: str) -> list[str]:
    found = []
    for task in playbook_yaml.tasks(doc):
        target = task.get("delegate_to")
        if not (isinstance(target, str) and "{{" in target):
            continue
        if "default(" in target or (rel, target) in AUDITED:
            continue
        found.append(f"{rel}: {task.get('name', '<unnamed>')!r} delegates to {target}")
    return found


def test_no_templated_delegation_can_fail_on_a_skipped_task():
    base = Path(playbook_yaml.REPO / "platform/playbooks")
    found = []
    for path in playbook_yaml.files():
        if base in path.parents:
            found += unguarded(playbook_yaml.load(path), str(path.relative_to(base)))
    assert not found, "add `| default(inventory_hostname)` (or audit it in AUDITED):\n" + "\n".join(found)


def test_the_guard_flags_the_shapes_that_broke_the_drill():
    for bad in ("{{ _exporter.host }}", "{{ groups['o11y_svc'][0] }}"):
        doc = playbook_yaml.loads(f'- name: t\n  ansible.builtin.debug: {{msg: x}}\n  delegate_to: "{bad}"\n')
        assert unguarded(doc, "x.yml"), bad
    guarded = "{{ _e.host | default(inventory_hostname) }}"
    good = playbook_yaml.loads(f'- name: t\n  ansible.builtin.debug: {{msg: x}}\n  delegate_to: "{guarded}"\n')
    assert not unguarded(good, "x.yml")


def test_every_audited_entry_still_exists():
    """An entry whose delegation was removed or reworded would silently excuse its replacement."""
    base = Path(playbook_yaml.REPO / "platform/playbooks")
    seen = set()
    for path in playbook_yaml.files():
        if base in path.parents:
            rel = str(path.relative_to(base))
            seen |= {
                (rel, t["delegate_to"])
                for t in playbook_yaml.tasks(playbook_yaml.load(path))
                if isinstance(t.get("delegate_to"), str)
            }
    assert set(AUDITED) <= seen, sorted(set(AUDITED) - seen)
