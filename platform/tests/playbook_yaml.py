"""The repository's Ansible YAML, parsed once per content state and shared by every guard.

The check-mode contract (test_check_mode_contract.py), the loop and secret-read guards
(test_no_request_in_loop_items.py) and the SSH harnesses, which re-check the whole repository
before every ansible run, all read the same ~200 files. Parsing them was ~0.5 s per pass, and
there were three passes per session; a session now pays for one.

Imported as `import playbook_yaml`, like harness_sandbox: pytest puts this directory on
sys.path because it has no __init__.py.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
# Semaphore's own playbooks and shared tasks run under the same flags as the playbooks.
SCANNED = (REPO / "platform/playbooks", REPO / "platform/semaphore")


class Loader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """Tolerates custom tags (for example `!unsafe`) the checkers do not need: a tagged value
    loads as None. On every file in the repository this parses identically to yaml.safe_load
    (2026-09-29, 214 files), which raised on a tag instead. libyaml's CSafeLoader when PyYAML
    has it, the pure-Python SafeLoader otherwise: the same documents in about a tenth of the
    time (0.067 s against 0.635 s for 217 files, 2026-09-29)."""


Loader.add_multi_constructor("!", lambda loader, suffix, node: None)

_PARSED: dict[Path, tuple[tuple[int, int], object]] = {}


def files() -> list[Path]:
    return sorted(p for base in SCANNED for p in base.rglob("*.yml"))


def load(path: Path):
    """Keyed on mtime and size, so an edited file is read again. Callers treat the result as
    read-only (a test that edits a document deep-copies it first)."""
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _PARSED.get(path)
    if hit is None or hit[0] != key:
        hit = _PARSED[path] = (key, yaml.load(path.read_text(), Loader=Loader))  # noqa: S506 - SafeLoader subclass
    return hit[1]


def loads(text: str):
    """A document from text, with the same loader: for the guards' own synthetic cases."""
    return yaml.load(text, Loader=Loader)  # noqa: S506 - SafeLoader subclass


# Where a play or task holds further tasks.
TASK_LISTS = ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always")


def tasks(node):
    """Every play, block and task in a document or task list, in file order. Nothing is
    inherited: a guard that needs a block's or play's settings walks with its own context."""
    if not isinstance(node, list):
        return
    for item in node:
        if isinstance(item, dict):
            yield item
            for key in TASK_LISTS:
                yield from tasks(item.get(key))


def strings(value, keys: bool = False):
    """Every string inside a value, and every dict key too when `keys`."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if keys:
                yield from strings(k, keys)
            yield from strings(v, keys)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v, keys)


SET_FACT = ("ansible.builtin.set_fact", "set_fact")


def defined_names(path: Path) -> dict[str, set[str]]:
    """Every variable name a playbook defines, with how: play `vars`, `vars_files` (literal
    paths, resolved next to the playbook), block or task `vars` (include params too),
    `register`, literal `set_fact` keys and `loop_control.loop_var`. Only this file: imported
    playbooks and included task files are not followed. The override guards' ratchets read
    it to decide which names an extra var must not set."""
    found: dict[str, set[str]] = {}

    def add(name, kind):
        if isinstance(name, str) and "{{" not in name:
            found.setdefault(name, set()).add(kind)

    for play in load(path) or []:
        if not isinstance(play, dict):
            continue
        for name in play.get("vars") or {}:
            add(name, "play vars")
        for ref in play.get("vars_files") or []:
            file = path.parent / ref
            if isinstance(ref, str) and "{{" not in ref and file.is_file():
                for name in load(file) or {}:
                    add(name, "vars_files")
        for key in ("pre_tasks", "tasks", "post_tasks", "handlers"):
            for task in tasks(play.get(key)):
                for name in task.get("vars") or {}:
                    add(name, "block vars" if "block" in task else "task vars")
                add(task.get("register"), "register")
                for module in SET_FACT:
                    if isinstance(task.get(module), dict):
                        for name in task[module]:
                            if name != "cacheable":
                                add(name, "set_fact")
                add((task.get("loop_control") or {}).get("loop_var"), "loop_var")
    return found


# Every workflow executor opens with this import (refuse-internal-extra-vars.yml); the tests
# that read or rebuild an executor's own plays skip it.
OVERRIDE_GUARD = "refuse-internal-extra-vars.yml"


def plays(path: Path) -> list:
    """A playbook's plays without its leading extra-var guard import, freshly parsed so a
    harness may edit them."""
    doc = loads(path.read_text())
    if doc and isinstance(doc[0], dict) and doc[0].get("ansible.builtin.import_playbook") == OVERRIDE_GUARD:
        return doc[1:]
    return doc
