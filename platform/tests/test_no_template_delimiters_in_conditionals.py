"""No conditional may carry template delimiters.

ansible-core 2.20 evaluates a conditional (`that`, `when`, `failed_when`, `changed_when`,
`until`) as one bare expression and refuses a delimiter inside it: "Syntax error in expression.
Template delimiters are not supported in expressions". Earlier versions tolerated the
`'{{ '{{' }}'` spelling of a literal brace pair. Production task 4995 ("Deploy Authentik (Dev)",
check mode, ansible-core 2.20.8) died on exactly that in the promotion guard of
deploy-authentik.yml, and the same line on `main` meant the main-bound deploy was broken too
(docs/MISTAKES.md 5.18; same upgrade class as 5.17).

Two guards:

1. Static: no conditional value anywhere under platform/playbooks, platform/services or
   platform/semaphore contains `{{`, `{%` or `{#`. Spell a literal brace pair as
   `('{' ~ '{')`.
2. Runtime: the real guard task from deploy-authentik.yml is run through the installed
   ansible-playbook with a value holding a literal brace pair (must fail the assert) and a clean
   URL (must pass), so the expression is proven to evaluate, not just to be delimiter-free.
"""

import re
import subprocess
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

REPO = playbook_yaml.REPO
ROOTS = (REPO / "platform/playbooks", REPO / "platform/services", REPO / "platform/semaphore")
# Roots whose YAML is all Ansible: an unparseable file there is a failure, not a skip.
STRICT_ROOTS = (REPO / "platform/playbooks", REPO / "platform/semaphore")
CONDITIONALS = ("that", "when", "failed_when", "changed_when", "until")
DELIMITERS = ("{{", "{%", "{#")
GUARD_TASK = "Promotion guard (prod): OIDC prod URLs are set + not local-dev defaults"
AUTHENTIK = REPO / "platform/playbooks/deploy-authentik.yml"


def yaml_files() -> list[Path]:
    return sorted(p for root in ROOTS for ext in ("*.yml", "*.yaml") for p in root.rglob(ext))


def offenders(node, rel: str, path: str = "") -> list[str]:
    """Every conditional value in a parsed document that carries a delimiter, named by file
    and key path."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key in CONDITIONALS:
                for text in playbook_yaml.strings(value):
                    if any(d in text for d in DELIMITERS):
                        found.append(f"{rel}: {here}: {text!r}")
            else:
                found += offenders(value, rel, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += offenders(value, rel, f"{path}[{i}]")
    return found


def test_no_conditional_carries_template_delimiters():
    found, skipped = [], []
    for path in yaml_files():
        rel = str(path.relative_to(REPO))
        try:
            doc = playbook_yaml.loads(path.read_text())
        except yaml.YAMLError as err:
            if any(root in path.parents for root in STRICT_ROOTS):
                found.append(f"{rel}: not parseable as YAML ({type(err).__name__}), so it cannot be checked")
            else:
                skipped.append(rel)
            continue
        found += offenders(doc, rel)
    if skipped:
        print(f"skipped (unparseable, outside the playbook roots): {skipped}")
    assert not found, (
        "ansible-core 2.20 rejects template delimiters inside a conditional; write a literal "
        "brace pair as ('{' ~ '{') (docs/MISTAKES.md 5.18):\n" + "\n".join(found)
    )


def test_the_scan_flags_the_shape_that_broke_task_4995():
    bad = playbook_yaml.loads(
        "- name: t\n  ansible.builtin.assert:\n    that:\n"
        "      - \"'{{ '{{' }}' not in (lookup('vars', item, default=''))\"\n"
    )
    assert offenders(bad, "x.yml")
    for key in ("when", "failed_when", "changed_when", "until"):
        doc = playbook_yaml.loads(f'- name: t\n  ansible.builtin.debug: {{msg: x}}\n  {key}: "{{{{ x }}}} == 1"\n')
        assert offenders(doc, "x.yml"), key
    good = playbook_yaml.loads(
        "- name: t\n  ansible.builtin.assert:\n    that:\n"
        "      - \"('{' ~ '{') not in (lookup('vars', item, default=''))\"\n"
        "    fail_msg: 'value {{ item }} is bad'\n"
    )
    assert not offenders(good, "x.yml")


def _core_version() -> tuple[int, int]:
    out = subprocess.run(["ansible-playbook", "--version"], text=True, capture_output=True, check=False).stdout
    match = re.search(r"\[core (\d+)\.(\d+)", out)
    assert match, out
    print(f"ansible-core {match.group(1)}.{match.group(2)} ({out.splitlines()[0]})")
    return int(match.group(1)), int(match.group(2))


def _guard_that() -> list[str]:
    for task in playbook_yaml.tasks(playbook_yaml.load(AUTHENTIK)):
        if task.get("name") == GUARD_TASK:
            return task["ansible.builtin.assert"]["that"]
    raise AssertionError(f"task {GUARD_TASK!r} not found in {AUTHENTIK}")


def _run_guard(tmp_path: Path, that: list[str], value: str) -> subprocess.CompletedProcess:
    play = [
        {
            "hosts": "localhost",
            "gather_facts": False,
            "connection": "local",
            "vars": {"probe_url": value},
            "tasks": [
                {
                    "name": "guard",
                    "ansible.builtin.assert": {"that": that, "quiet": True},
                    "loop": ["probe_url"],
                }
            ],
        }
    ]
    playbook = tmp_path / "guard.yml"
    playbook.write_text(yaml.safe_dump(play))
    return harness_sandbox.run(
        ["ansible-playbook", str(playbook)], tmp_path, cwd=tmp_path, env=harness_sandbox.env_for(tmp_path)
    )


def test_the_real_guard_expression_evaluates(tmp_path):
    _core_version()
    that = _guard_that()
    clean = _run_guard(tmp_path, that, "https://auth.example.org/settings")
    assert clean.returncode == 0, clean.stdout + clean.stderr
    # A value that resolves to text holding a literal brace pair: the guard exists to refuse it.
    braces = _run_guard(tmp_path, that, "{{ '{{' }} unresolved")
    assert braces.returncode != 0, braces.stdout
    assert "Assertion failed" in braces.stdout or "evaluated to false" in braces.stdout, braces.stdout
    assert "Template delimiters" not in braces.stdout + braces.stderr
    local = _run_guard(tmp_path, that, "https://app.agent-cloud.test/x")
    assert local.returncode != 0


def test_ansible_20_rejects_the_old_spelling(tmp_path):
    """The reproduction of task 4995, so the guard above is known to matter. On ansible-core
    older than 2.20 the old spelling still evaluated; there the static scan is the only
    enforcement."""
    if _core_version() < (2, 20):
        pytest.skip("this ansible-core predates the 2.20 delimiter ban; the static scan is the guard")
    old = ["'{{ '{{' }}' not in (lookup('vars', item, default=''))"]
    result = _run_guard(tmp_path, old, "https://auth.example.org/settings")
    assert result.returncode != 0
    assert "Template delimiters are not supported in expressions" in result.stdout + result.stderr
