"""Every playbook task is in one check-mode class (plan/architecture/08-ansible-automation-standards.md).

Under `ansible-playbook --check`, a module without check-mode support is skipped, so:

- a READ that such a module performs must set `check_mode: false`, or verification silently
  does nothing during a dry run: a `uri` GET/HEAD, or any `uri`/`command`/`shell`/`raw`/
  `script` marked read-only by `changed_when: false` (an OpenBao login POST, for example);
- a WRITE that such a module performs must be guarded: a `when` mentioning
  `ansible_check_mode`, `check_mode: true`, or `creates`/`removes` on a command. A write
  under `check_mode: false` runs for real during a dry run and is always a violation.

Guards on an enclosing block are inherited. Files still being retrofitted are listed in
check_mode_allowlist.txt; a listed file that has become clean fails the test until its line is
removed, so the list only shrinks (change service-deployment-workflow, tasks 1.4 and 2.4).
"""

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
ALLOWLIST = Path(__file__).with_name("check_mode_allowlist.txt")

COMMANDS = {"command", "shell", "raw", "script"}
READ_METHODS = {"GET", "HEAD"}
TASK_LISTS = ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always")


class _Loader(yaml.SafeLoader):
    """Tolerates custom tags (for example `!unsafe`) the checker does not need."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: None)


def _module(task: dict) -> tuple[str, object] | tuple[None, None]:
    for key, value in task.items():
        short = key.rsplit(".", 1)[-1]
        core = key == short or key.startswith(("ansible.builtin.", "ansible.legacy."))
        if core and (short in COMMANDS or short == "uri"):
            return short, value
    return None, None


def _mentions_check_mode(when) -> bool:
    items = when if isinstance(when, list) else [when]
    return any("ansible_check_mode" in str(item) for item in items)


def _guarded(task: dict, inherited: bool) -> bool:
    # `check_mode: true` simulates; `check_mode: false` forces a real run, so it is no guard.
    return inherited or task.get("check_mode") is True or _mentions_check_mode(task.get("when"))


def _classify(task: dict, module: str, args) -> str:
    # `changed_when: false` is the author's declaration that the task changes nothing, for a
    # command and for an HTTP call alike (an OpenBao AppRole login is a POST that only
    # reads a token, and verification cannot run without it).
    if task.get("changed_when") is False:
        return "read"
    if module == "uri":
        method = str((args or {}).get("method", "GET")) if isinstance(args, dict) else "GET"
        return "read" if method.upper() in READ_METHODS else "write"
    return "write"


def violations_in(doc) -> list[str]:
    found: list[str] = []

    def walk(tasks, inherited: bool, inherited_off: bool):
        for index, task in enumerate(tasks or []):
            if not isinstance(task, dict):
                continue
            name = task.get("name", f"#{index}")
            guard = _guarded(task, inherited)
            off = inherited_off or task.get("check_mode") is False
            for key in TASK_LISTS:
                if key in task and isinstance(task[key], list):
                    walk(task[key], guard, off)
            module, args = _module(task)
            if module is None:
                continue
            kind = _classify(task, module, args)
            if kind == "read" and not off:
                found.append(f"{name}: read-only {module} without check_mode: false")
            elif kind == "write" and off:
                found.append(f"{name}: {module} write forced to run in check mode (check_mode: false)")
            elif kind == "write" and not guard:
                extra = task.get("args") if isinstance(task.get("args"), dict) else {}
                params = {**(args if isinstance(args, dict) else {}), **extra}
                if module in COMMANDS and {"creates", "removes"} & set(params):
                    continue
                found.append(f"{name}: {module} write without a check-mode guard")

    if isinstance(doc, list):
        # a playbook is a list of plays; a task file is a list of tasks
        if doc and isinstance(doc[0], dict) and ("hosts" in doc[0] or "import_playbook" in doc[0]):
            for play in doc:
                if isinstance(play, dict):
                    for key in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                        walk(play.get(key), play.get("check_mode") is True, play.get("check_mode") is False)
        else:
            walk(doc, False, False)
    return found


def _files() -> list[Path]:
    return sorted(PLAYBOOKS.rglob("*.yml"))


def _allowlist() -> set[str]:
    lines = ALLOWLIST.read_text().splitlines() if ALLOWLIST.exists() else []
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def _load(path: Path):
    return yaml.load(path.read_text(), Loader=_Loader)  # noqa: S506 - SafeLoader subclass


@pytest.mark.parametrize("path", _files(), ids=lambda p: str(p.relative_to(REPO)))
def test_playbook_honours_check_mode(path):
    rel = str(path.relative_to(REPO))
    found = violations_in(_load(path))
    if rel in _allowlist():
        assert found, f"{rel} is clean now: remove it from {ALLOWLIST.name}"
    else:
        assert not found, f"{rel}:\n  " + "\n  ".join(found)


def test_allowlist_names_only_existing_files():
    missing = [entry for entry in _allowlist() if not (REPO / entry).exists()]
    assert not missing, f"stale entries in {ALLOWLIST.name}: {missing}"


# The checker's own behaviour, against fixtures (tasks 1.5: an unguarded write goes red).

def _tasks(yaml_text: str):
    return violations_in(yaml.safe_load(yaml_text))


def test_unguarded_write_is_caught():
    found = _tasks("- name: restart it\n  ansible.builtin.command: podman restart x\n")
    assert found == ["restart it: command write without a check-mode guard"]


def test_guarded_write_passes():
    assert not _tasks(
        "- name: restart it\n  ansible.builtin.command: podman restart x\n"
        "  when: not ansible_check_mode\n"
    )


def test_creates_guard_passes():
    assert not _tasks(
        "- name: init\n  ansible.builtin.command:\n    cmd: init.sh\n    creates: /x\n"
    )


def test_get_probe_without_check_mode_false_is_caught():
    found = _tasks("- name: health\n  ansible.builtin.uri:\n    url: http://x/health\n")
    assert found == ["health: read-only uri without check_mode: false"]


def test_get_probe_with_check_mode_false_passes():
    assert not _tasks(
        "- name: health\n  ansible.builtin.uri:\n    url: http://x/health\n  check_mode: false\n"
    )


def test_post_is_a_write():
    found = _tasks("- name: create\n  ansible.builtin.uri:\n    url: http://x\n    method: POST\n")
    assert found == ["create: uri write without a check-mode guard"]


def test_read_only_command_needs_check_mode_false():
    found = _tasks("- name: probe\n  ansible.builtin.command: cat /etc/os-release\n  changed_when: false\n")
    assert found == ["probe: read-only command without check_mode: false"]


def test_block_guard_is_inherited():
    assert not _tasks(
        "- name: writes\n  when: not ansible_check_mode\n  block:\n"
        "    - name: restart\n      ansible.builtin.command: podman restart x\n"
    )


def test_block_check_mode_false_covers_reads():
    assert not _tasks(
        "- name: verify\n  check_mode: false\n  block:\n"
        "    - name: health\n      ansible.builtin.uri:\n        url: http://x/health\n"
    )


def test_playbook_structure_is_walked():
    found = violations_in(
        yaml.safe_load(
            "- hosts: all\n  tasks:\n    - name: restart\n      ansible.builtin.shell: systemctl restart x\n"
        )
    )
    assert found == ["restart: shell write without a check-mode guard"]


def test_write_forced_into_check_mode_is_caught():
    found = _tasks(
        "- name: restart it\n  ansible.builtin.command: podman restart x\n  check_mode: false\n"
    )
    assert found == ["restart it: command write forced to run in check mode (check_mode: false)"]


def test_login_post_marked_read_only_needs_check_mode_false():
    found = _tasks(
        "- name: login\n  ansible.builtin.uri:\n    url: http://x/v1/auth/approle/login\n"
        "    method: POST\n  changed_when: false\n"
    )
    assert found == ["login: read-only uri without check_mode: false"]
