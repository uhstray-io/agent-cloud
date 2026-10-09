"""A shell task that sets `pipefail` runs under bash (the Ubuntu /bin/sh is dash).

`set -o pipefail` is a bashism. Ansible's shell module runs the command under /bin/sh unless
`executable` says otherwise; on Ubuntu and Debian that is dash, which stops with "Illegal
option -o pipefail". macOS's /bin/sh is bash, so the same task passes on a workstation and
fails on the controller or a target VM (PR #498: bootstrap-local-dev.yml failed on Linux CI
only). The guard makes the shell explicit wherever the command relies on pipefail.

Every task in every YAML file under platform/ is walked, through block/rescue/always. A
mention of `pipefail` in a shell comment does not count; a task that names bash itself in the
command (`bash -c '...'`) is not what this checks, it needs `executable` like the rest.

ponytail: only a literal `pipefail` in the task's own command is seen. A command assembled
from a variable or a script run by `ansible.builtin.script` is not.
"""

import re

import playbook_yaml

ROOT = playbook_yaml.REPO
SHELL = ("shell", "ansible.builtin.shell", "ansible.legacy.shell")


def _files():
    return sorted(p for ext in ("yml", "yaml") for p in (ROOT / "platform").rglob(f"*.{ext}"))


def _command(task: dict, module: str) -> str:
    body = task[module]
    if isinstance(body, dict):
        body = body.get("cmd", "")
    return body if isinstance(body, str) else ""


def _executable(task: dict, module: str):
    for holder in (task.get("args"), task[module] if isinstance(task[module], dict) else None):
        if isinstance(holder, dict) and holder.get("executable"):
            return str(holder["executable"])
    return None


def _uses_pipefail(command: str) -> bool:
    return any(re.search(r"\bpipefail\b", line) for line in command.splitlines() if not line.lstrip().startswith("#"))


def task_violations(doc) -> list[str]:
    found = []
    for task in playbook_yaml.tasks(doc):
        for module in SHELL:
            if module in task and _uses_pipefail(_command(task, module)):
                exe = _executable(task, module)
                if exe is None or not exe.rstrip().endswith("bash"):
                    found.append(f"{task.get('name', '<unnamed>')!r} (executable: {exe!r})")
    return found


def violations(text: str) -> list[str]:
    return task_violations(playbook_yaml.loads(text))


def test_every_pipefail_shell_task_runs_under_bash():
    offenders = [f"{p.relative_to(ROOT)}: {v}" for p in _files() for v in task_violations(playbook_yaml.load(p))]
    assert not offenders, "shell tasks using pipefail without `executable: /bin/bash`:\n" + "\n".join(offenders)


def test_guard_sees_each_shape():
    bad = "- name: t\n  ansible.builtin.shell: |\n    set -o pipefail\n    a | b\n"
    assert violations(bad) == ["'t' (executable: None)"]
    assert violations(bad + "  args:\n    executable: /bin/sh\n") == ["'t' (executable: '/bin/sh')"]
    assert violations(bad + "  args:\n    executable: /bin/bash\n") == []
    nested = "- block:\n    - name: n\n      shell: set -euo pipefail; a | b\n"
    assert violations(nested) == ["'n' (executable: None)"]
    assert violations("- name: c\n  shell: |\n    # pipefail is not set here\n    a | b\n") == []
