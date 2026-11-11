"""Build test plays from o11y's real preflight assertions, never deploy workflows."""

from copy import deepcopy
from pathlib import Path

import playbook_yaml

ALLOWED_TASK_KEYS = {
    "name", "ansible.builtin.assert", "ansible.builtin.set_fact", "ansible.builtin.debug",
    "ansible.builtin.command", "when", "vars", "no_log", "register", "changed_when",
    "failed_when", "check_mode",
}
READ_ONLY_GIT_ARGV = ["git", "rev-parse", "HEAD"]
PLAYBOOK_DIR_EXPR = "{{ playbook_dir | dirname | dirname }}"


class UnsafeFixtureTask(ValueError):
    """A selected source task exceeds the fixture's assertion-only boundary."""


def _tasks(path: Path):
    return list(playbook_yaml.tasks(playbook_yaml.load(path)))


def task(path: Path, name: str) -> dict:
    matches = [item for item in _tasks(path) if item.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"expected one task named {name!r} in {path}, found {len(matches)}")
    return deepcopy(matches[0])


def play_vars(path: Path, play_name: str, names: tuple[str, ...]) -> dict:
    plays = playbook_yaml.plays(path)
    matches = [play for play in plays if isinstance(play, dict) and play.get("name") == play_name]
    if len(matches) != 1:
        raise ValueError(f"expected one play named {play_name!r} in {path}, found {len(matches)}")
    source_vars = matches[0].get("vars", {})
    if any(name not in source_vars for name in names):
        raise ValueError(f"source play {play_name!r} is missing a requested variable")
    return {name: deepcopy(source_vars[name]) for name in names}


def _check_task(item: dict, repo_root: Path) -> dict:
    unknown = set(item) - ALLOWED_TASK_KEYS
    if unknown:
        raise UnsafeFixtureTask(f"unsupported task fields: {', '.join(sorted(unknown))}")

    actions = set(item) & {
        "ansible.builtin.assert", "ansible.builtin.set_fact", "ansible.builtin.debug",
        "ansible.builtin.command",
    }
    if len(actions) != 1:
        raise UnsafeFixtureTask("fixture task must contain exactly one allowed action")

    if "ansible.builtin.command" in actions:
        command = item["ansible.builtin.command"]
        exact_revision_read = (
            set(command) == {"argv", "chdir"}
            and command.get("argv") == READ_ONLY_GIT_ARGV
            and command.get("chdir") == PLAYBOOK_DIR_EXPR
        )
        if not exact_revision_read:
            raise UnsafeFixtureTask("only the source Git revision read is allowed as a command")
        # The extracted task lives in a temp playbook, so point its read-only Git command at
        # this checkout instead of the temp playbook's parent directories.
        command["chdir"] = str(repo_root)
    return item


def build_play(
    output: Path,
    repo_root: Path,
    host: str,
    source_tasks: list[tuple[str, str] | tuple[str, str, str, str]],
    *,
    source_vars: list[tuple[str, str, tuple[str, ...]]] | None = None,
    public_vars: dict | None = None,
) -> None:
    """Write a local test play from named source tasks and explicit public variables.

    source_tasks holds (relative source file, exact task name), optionally followed by the
    context source file and include task name for the fixed TLS guard. Source vars are selected
    by exact play/name; no source play's tasks are copied wholesale.
    """
    root = repo_root.resolve()
    tasks = []
    for source_ref in source_tasks:
        relative, name = source_ref[:2]
        context_relative, context_name = source_ref[2:] if len(source_ref) == 4 else (None, None)
        source = (root / relative).resolve()
        if root not in source.parents:
            raise ValueError("source task path escapes the repository")
        selected = task(source, name)
        if context_name:
            context_source = (root / context_relative).resolve()
            if root not in context_source.parents:
                raise ValueError("context task path escapes the repository")
            context = task(context_source, context_name)
            include_path = context.get("ansible.builtin.include_tasks")
            included_source = (context_source.parent / include_path).resolve() if include_path else None
            if included_source != source:
                raise UnsafeFixtureTask("only the fixed o11y TLS guard include can supply context")
            if "when" in context:
                if "when" in selected:
                    raise UnsafeFixtureTask("task and include both define conditions")
                selected["when"] = deepcopy(context["when"])
        if selected.get("ansible.builtin.include_tasks") == "tasks/require-parsed-inventory.yml":
            if set(selected) - {"name", "ansible.builtin.include_tasks", "vars"}:
                raise UnsafeFixtureTask("the inventory include has unexpected task controls")
            included = (source.parent / selected["ansible.builtin.include_tasks"]).resolve()
            if root not in included.parents:
                raise UnsafeFixtureTask("the inventory include escapes the repository")
            nested = _tasks(included)
            if len(nested) != 1:
                raise UnsafeFixtureTask("the parsed-inventory include must remain one assertion")
            parsed = deepcopy(nested[0])
            parsed["vars"] = deepcopy(selected.get("vars", {}))
            tasks.append(_check_task(parsed, root))
        else:
            tasks.append(_check_task(selected, root))

    variables = {}
    for relative, play_name, names in source_vars or []:
        source = (root / relative).resolve()
        if root not in source.parents:
            raise ValueError("source variable path escapes the repository")
        variables.update(play_vars(source, play_name, names))
    variables.update(public_vars or {})

    play = {
        "name": "o11y source-derived preflight fixture",
        "hosts": host,
        "gather_facts": False,
        "connection": "local",
        "vars": variables,
        "tasks": tasks,
    }
    output.write_text(playbook_yaml.yaml.safe_dump([play], sort_keys=False), encoding="utf-8")
