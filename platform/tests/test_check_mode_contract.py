"""Every playbook task is in one check-mode class (plan/architecture/08-ansible-automation-standards.md).

Under `ansible-playbook --check`, a module without check-mode support is skipped, so:

- a READ that such a module performs must set `check_mode: false`, or verification silently
  does nothing during a dry run: a `uri` GET/HEAD, or any `uri`/`command`/`shell`/`raw`/
  `script` marked read-only by `changed_when: false` (an OpenBao login POST, for example),
  unless it is skipped on purpose by a `when` on `ansible_check_mode` because it reads state
  an earlier skipped write would have made;
- a WRITE that such a module performs must be guarded: a `when` mentioning
  `ansible_check_mode`, `check_mode: true`, or `creates`/`removes` on a command. A write
  under `check_mode: false` runs for real during a dry run and is always a violation.

A FILE write (copy, file, tempfile, lineinfile, template, ...) simulates, so it needs no guard,
but under `check_mode: false` it runs for real. That is allowed only in the runner-scratch class
(plan/architecture/08, decision 2026-09-28), and only as a CLOSED set of exact source forms:
- delegated to the runner, with NO `vars:` on the task or any enclosing block;
- its dest/path one of the pinned targets of the shared task file it sits in
  (materialise-ssh-key.yml, remove-ssh-key.yml, pin-ssh-host-key.yml), or, for the one
  tempfile, exactly the pinned temp-root expression;
- every name those targets are built from defined exactly ONCE in the whole repository, by
  the pinned expression in its own file (a set_fact or the tempfile's register). A second
  definition anywhere — task, block or play `vars:`, include parameters, another set_fact or
  register — is a violation, because Ansible would let it redirect the write while the target
  text stayed the same (PR #319 review: exactly that bypass wrote outside the scratch).
Source analysis is not the only line: the shared tasks re-check their paths at runtime, and
the test harness that executes them runs sandboxed (platform/tests/harness_sandbox.py).

Guards on an enclosing block are inherited. Files still being retrofitted are listed in
check_mode_allowlist.txt; a listed file that has become clean fails the test until its line is
removed, so the list only shrinks (change service-deployment-workflow, tasks 1.4 and 2.4).
"""

import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
# Semaphore's own playbooks and shared tasks run under the same dry-run flag.
SEMAPHORE = REPO / "platform/semaphore"
ALLOWLIST = Path(__file__).with_name("check_mode_allowlist.txt")

COMMANDS = {"command", "shell", "raw", "script"}
READ_METHODS = {"GET", "HEAD"}
# A container-engine lifecycle verb is a write whatever changed_when says. The check-mode
# retrofit trusted changed_when: false on "stop + rm the orb agent", and a dry run removed
# the running agent (docs/MISTAKES.md 5.12).
ENGINE_WRITE = re.compile(
    # Compose's own options may sit between `compose` and the verb (`compose -f x up`).
    r"(?:\bdocker|\bpodman|\{\{[^}]*engine[^}]*\}\})\s+(?:compose(?:\s+-\S+(?:\s+[^-\s]\S*)?)*\s+)?"
    r"(?:stop|rm|rmi|kill|restart|start|run|pull|up|down|create|login|logout|cp|tag|push|build|load|import|commit)\b"
)
# Host filesystem and service writes, whatever changed_when says (PR 203 review: `sudo mkdir`
# + `chmod 1777` under check_mode: false changed a VM during a dry run).
HOST_WRITE = re.compile(
    r"(?:^|[\s;&|(])(?:sudo\s+)?(?:mkdir|chmod|chown|chgrp|mv|ln|tee|touch|install|truncate)\s"
    r"|(?:^|[\s;&|(])(?:sudo\s+)?systemctl\s+(?:--user\s+)?(?:start|stop|restart|reload|enable|disable)\b"
)
TASK_LISTS = ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always")

FILE_WRITES = {"copy", "file", "tempfile", "lineinfile", "template", "blockinfile", "replace",
               "assemble", "get_url", "unarchive"}
MATERIALISE = "platform/playbooks/tasks/materialise-ssh-key.yml"
REMOVE = "platform/playbooks/tasks/remove-ssh-key.yml"
PIN = "platform/playbooks/tasks/pin-ssh-host-key.yml"
SCRATCH_ROOT = "{{ lookup('ansible.builtin.env', 'TMPDIR') | default('/tmp', true) | realpath }}"
# The same root, as the runtime checks must compute it: INLINE, never through a variable, so no
# set_fact, include parameter or extra var can move it (PR #319 review: `-e` beat a set_fact).
SCRATCH_ROOT_INLINE = "(lookup('ansible.builtin.env', 'TMPDIR') | default('/tmp', true) | realpath)"
SET_FACT_KEYS = ("ansible.builtin.set_fact", "ansible.legacy.set_fact", "set_fact")
# The only dest/path a forced write may carry, each valid only in its own file.
SCRATCH_TARGETS = {
    MATERIALISE: {"{{ _msk_dir.path }}/id"},
    REMOVE: {"{{ _rsk_dir }}"},
    PIN: {"{{ _pshk_kh }}"},
}
# Every name a scratch target is built from, and its ONE definition: (file, kind, value), where
# value is the exact set_fact expression, or the name of the task whose register defines it.
PINNED_DEFINITIONS = {
    "_msk_dir": (MATERIALISE, "register", "Materialise SSH key: create the runner-local scratch directory (0700)"),
    "_rsk_dir": (REMOVE, "set_fact",
                 "{{ (lookup('ansible.builtin.vars', ssh_key_result_var, default={}) | default({}, true)).dir"
                 " | default('') }}"),
    "_pshk_kh": (PIN, "set_fact", "{{ lookup('ansible.builtin.vars', ssh_key_result_var).known_hosts }}"),
}


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


_SKIP_TERM = re.compile(r"(?:^|\band\s+)\(?\s*not\s+ansible_check_mode\s*\)?(?:\s+and\b|$)")


def _mentions_check_mode(when) -> bool:
    """True only when the condition is PROVEN false under --check: `not ansible_check_mode`
    as a whole list item (Ansible ANDs a list), or as a top-level `and` term of a string with
    no `or`. A mere mention is not enough: `when: ansible_check_mode` runs only IN a dry run,
    and `when: x or not ansible_check_mode` runs in one whenever x holds (PR 203 review)."""
    items = when if isinstance(when, list) else [when]
    for item in items:
        text = " ".join(str(item).split())
        if text == "not ansible_check_mode":
            return True
        if " or " not in f" {text} " and _SKIP_TERM.search(text):
            return True
    return False


def _guarded(task: dict, inherited: bool) -> bool:
    # `check_mode: true` simulates; `check_mode: false` forces a real run, so it is no guard.
    return inherited or task.get("check_mode") is True or _mentions_check_mode(task.get("when"))


def _command_text(args) -> str:
    if isinstance(args, str):
        return args
    if isinstance(args, dict):
        argv = args.get("argv", [])
        # An argv computed in Jinja is one string holding a list literal; read its words, not
        # its characters (recover-netbox-runtime's `compose up` was classified as a read).
        if isinstance(argv, str):
            argv = re.sub(r"[\[\]',\"+]|\{\{|\}\}", " ", argv).split()
        return str(args.get("cmd") or " ".join(map(str, argv)))
    return str(args or "")


def _sandboxed(text: str) -> bool:
    """A command that makes its own `mktemp -d` root and removes it on EXIT (e.g. netplan
    validated in an isolated root) writes only into that throwaway root."""
    return bool(re.search(r"\broot=\$\(mktemp -d\)", text)) and "trap 'rm -rf \"$root\"' EXIT" in text


def _file_write(task: dict) -> tuple[str, object] | tuple[None, None]:
    for key, value in task.items():
        short = key.rsplit(".", 1)[-1]
        core = key == short or key.startswith(("ansible.builtin.", "ansible.legacy."))
        if core and short in FILE_WRITES:
            return short, value
    return None, None


def _in_runner_scratch(module: str, args, rel: str | None) -> bool:
    args = args if isinstance(args, dict) else {}
    if module == "tempfile":
        # Only the shared task may create the scratch root: its own prefix, the pinned root.
        return (rel == MATERIALISE and args.get("state") == "directory" and args.get("prefix") == ".sshkey_"
                and str(args.get("path", "")).strip() == SCRATCH_ROOT)
    target = str(args.get("dest") or args.get("path") or "").strip()
    return target in SCRATCH_TARGETS.get(rel, set())


def _definitions(doc, rel: str) -> list[tuple[str, str, str, str]]:
    """(name, rel, kind, value) for every definition of a pinned name in one parsed file."""
    found = []

    def from_vars(mapping, kind):
        for name, value in (mapping or {}).items() if isinstance(mapping, dict) else []:
            if name in PINNED_DEFINITIONS:
                found.append((name, rel, kind, str(value).strip()))

    def from_free_form(text):
        # `set_fact: a=1 b=2` (k=v form). Any pinned name defined this way is a second,
        # unpinned definition (PR #319 review: it redefined the root and the target).
        for name in re.findall(r"(?:^|\s)([A-Za-z_]\w*)=", str(text)):
            if name in PINNED_DEFINITIONS:
                found.append((name, rel, "set_fact k=v", str(text).strip()))

    def walk(tasks):
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            from_vars(task.get("vars"), "vars")
            for key in SET_FACT_KEYS:
                if key not in task:
                    continue
                if isinstance(task[key], dict):
                    from_vars(task[key], "set_fact")
                else:
                    from_free_form(task[key] or "")
                # `set_fact:` with its parameters under the task's `args:`
                from_vars(task.get("args"), "set_fact args")
            if task.get("register") in PINNED_DEFINITIONS:
                found.append((task["register"], rel, "register", str(task.get("name"))))
            for key in TASK_LISTS:
                if isinstance(task.get(key), list):
                    walk(task[key])

    for item in doc if isinstance(doc, list) else []:
        if isinstance(item, dict) and ("hosts" in item or "import_playbook" in item):
            from_vars(item.get("vars"), "vars")
            for key in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                walk(item.get(key))
        else:
            walk([item])
    return found


def pinned_definition_problems(docs: dict | None = None) -> list[str]:
    """Each pinned name must be defined exactly once, by its pinned expression, in its own file."""
    if docs is None:
        docs = {str(p.relative_to(REPO)): _load(p) for p in _files()}
    seen: dict[str, list[tuple[str, str, str]]] = {name: [] for name in PINNED_DEFINITIONS}
    for rel, doc in docs.items():
        for name, where, kind, value in _definitions(doc, rel):
            seen[name].append((where, kind, value))
    problems = []
    for name, expected in PINNED_DEFINITIONS.items():
        if seen[name] != [expected]:
            problems.append(f"{name}: expected exactly {expected}, found {seen[name]}")
    return problems


def _classify(task: dict, module: str, args) -> str:
    # `changed_when: false` is the author's declaration that the task changes nothing, for a
    # command and for an HTTP call alike (an OpenBao AppRole login is a POST that only
    # reads a token, and verification cannot run without it), EXCEPT when the command runs
    # a container-engine lifecycle verb: that is a write whatever the label says.
    text = _command_text(args)
    verb = ENGINE_WRITE.search(text)
    # `run --rm` is a throwaway probe container (e.g. validating a config): it leaves nothing.
    if module in COMMANDS and verb and not (verb.group(0).endswith("run") and "--rm" in text):
        return "write"
    if module in COMMANDS and HOST_WRITE.search(text) and not _sandboxed(text):
        return "write"
    if task.get("changed_when") is False:
        return "read"
    if module == "uri":
        method = str((args or {}).get("method", "GET")) if isinstance(args, dict) else "GET"
        return "read" if method.upper() in READ_METHODS else "write"
    return "write"


def violations_in(doc, rel: str | None = None, all_writes: bool = False) -> list[str]:
    """`all_writes` treats every file write as forced: the test harness uses it, since it runs
    the tasks it lifts for real whatever their check_mode."""
    found: list[str] = []

    def walk(tasks, inherited: bool, inherited_off: bool, inherited_runner: bool = False,
             inherited_vars: bool = False):
        for index, task in enumerate(tasks or []):
            if not isinstance(task, dict):
                continue
            name = task.get("name", f"#{index}")
            guard = _guarded(task, inherited)
            off = inherited_off or task.get("check_mode") is False
            runner = inherited_runner or task.get("delegate_to") == "localhost"
            has_vars = inherited_vars or bool(task.get("vars"))
            for key in TASK_LISTS:
                if key in task and isinstance(task[key], list):
                    walk(task[key], guard, off, runner, has_vars)
            file_module, file_args = _file_write(task)
            if file_module and (off or all_writes):
                if has_vars:
                    found.append(f"{name}: {file_module} write forced to run in check mode carries vars:, "
                                 "which could redirect it")
                elif not (runner and _in_runner_scratch(file_module, file_args, rel)):
                    found.append(f"{name}: {file_module} write forced to run in check mode outside the runner "
                                 "scratch directory")
            module, args = _module(task)
            if module is None:
                continue
            kind = _classify(task, module, args)
            # A read either runs in check mode (check_mode: false) or is skipped ON PURPOSE
            # (a `when` on ansible_check_mode), e.g. a read of a clone the dry run never made.
            if kind == "read" and not off and not _mentions_check_mode(task.get("when")) and not inherited:
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
                        walk(play.get(key), play.get("check_mode") is True, play.get("check_mode") is False,
                             play.get("delegate_to") == "localhost", bool(play.get("vars")))
        else:
            walk(doc, False, False)
    return found


def _files() -> list[Path]:
    return sorted([*PLAYBOOKS.rglob("*.yml"), *SEMAPHORE.rglob("*.yml")])


def _allowlist() -> set[str]:
    lines = ALLOWLIST.read_text().splitlines() if ALLOWLIST.exists() else []
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def _load(path: Path):
    return yaml.load(path.read_text(), Loader=_Loader)  # noqa: S506 - SafeLoader subclass


@pytest.mark.parametrize("path", _files(), ids=lambda p: str(p.relative_to(REPO)))
def test_playbook_honours_check_mode(path):
    rel = str(path.relative_to(REPO))
    found = violations_in(_load(path), rel)
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


def test_read_skipped_on_purpose_passes():
    assert not _tasks(
        "- name: anything to commit\n  ansible.builtin.command: git status --porcelain\n"
        "  changed_when: false\n  when: not ansible_check_mode\n"
    )


def test_engine_lifecycle_marked_read_is_still_a_write():
    # The exact shape that removed the local orb agent under --check.
    found = _tasks(
        "- name: stop agent\n  ansible.builtin.shell: |\n"
        "    {{ container_engine | default('docker') }} stop netbox-orb-agent || true\n"
        "  changed_when: false\n  check_mode: false\n"
    )
    assert found == ["stop agent: shell write forced to run in check mode (check_mode: false)"]


def test_engine_reads_stay_reads():
    assert not _tasks(
        "- name: inspect\n  ansible.builtin.command: podman inspect x --format x\n"
        "  changed_when: false\n  check_mode: false\n"
    )


def test_throwaway_probe_container_is_a_read():
    assert not _tasks(
        "- name: validate\n  ansible.builtin.command: podman run --rm caddy caddy validate\n"
        "  changed_when: false\n  check_mode: false\n"
    )


def test_registry_login_and_host_writes_are_writes():
    # PR 203 review: `podman login` and `sudo mkdir` + `chmod` ran under --check.
    for cmd in ("podman login ghcr.io -u x --password-stdin",
                'podman machine ssh "sudo mkdir -p /d && sudo chmod 1777 /d"',
                "{{ _engine }} cp /f c:/tmp/f"):
        found = _tasks(f"- name: w\n  ansible.builtin.shell: {cmd!r}\n  changed_when: false\n  check_mode: false\n")
        assert found == ["w: shell write forced to run in check mode (check_mode: false)"], cmd


def test_a_command_that_writes_only_its_own_temp_root_is_a_read():
    assert not _tasks(
        "- name: validate\n  ansible.builtin.shell: |\n"
        "    root=$(mktemp -d)\n    trap 'rm -rf \"$root\"' EXIT\n    mkdir -p \"$root/etc\"\n"
        "  changed_when: false\n  check_mode: false\n"
    )


def test_a_mention_of_check_mode_is_not_a_guard():
    # Both of these still run the write in a dry run.
    for when in ("ansible_check_mode", "allowed or not ansible_check_mode"):
        found = _tasks(f"- name: w\n  ansible.builtin.command: podman restart x\n  when: {when}\n")
        assert found == ["w: command write without a check-mode guard"], when


def test_proven_skips_are_guards():
    for when in ('"not ansible_check_mode"', '"x and not ansible_check_mode"',
                 '"not ansible_check_mode and x"', '[x, "not ansible_check_mode"]'):
        assert not _tasks(f"- name: w\n  ansible.builtin.command: podman restart x\n  when: {when}\n"), when


def test_compose_options_before_the_verb_are_still_a_write():
    found = _tasks(
        "- name: up\n  ansible.builtin.command:\n"
        "    argv: [docker, compose, --project-name, netbox, -f, docker-compose.yml, up, -d]\n"
        "  changed_when: false\n"
    )
    assert found and "write" in found[0], found


def test_an_argv_computed_in_jinja_is_read_as_words():
    found = _tasks(
        "- name: up\n  ansible.builtin.command:\n"
        "    argv: \"{{ ['docker', 'compose', '-f', 'x.yml', 'up', '-d'] + [item] }}\"\n"
        "  changed_when: false\n"
    )
    assert found and "write" in found[0], found


# Task keywords that, placed between a comment and the next `- name:`, belong to the PREVIOUS
# task while reading as the next one's.
_TASK_KEY = re.compile(r"\s+(when|check_mode|changed_when|failed_when|no_log|register|delegate_to|"
                       r"become|ignore_errors|until|retries|delay|loop|tags):")


def test_no_task_key_is_stranded_under_the_next_tasks_comment():
    # The check-mode retrofit left eight `when:` guards and a `check_mode: false` stranded that way
    # (PR 203 Codex review).
    stranded = []
    for path in sorted((REPO / "platform").rglob("*.yml")):
        lines = path.read_text().split("\n")
        for i in range(1, len(lines) - 1):
            if (_TASK_KEY.match(lines[i]) and lines[i - 1].strip().startswith("#")
                    and lines[i + 1].strip().startswith("- name:")):
                stranded.append(f"{path.relative_to(REPO)}:{i + 1}")
    assert not stranded, stranded


# The runner-scratch class (plan/architecture/08, decision 2026-09-28).

def _scratch(yaml_text: str, rel=None):
    return violations_in(yaml.safe_load(yaml_text), rel)


OUTSIDE = " write forced to run in check mode outside the runner scratch directory"
CARRIES_VARS = " write forced to run in check mode carries vars:, which could redirect it"
FORCED = "  delegate_to: localhost\n  check_mode: false\n"


def test_forced_file_write_outside_the_scratch_is_caught():
    for task in ('ansible.builtin.copy:\n    content: x\n    dest: /etc/motd',
                 'ansible.builtin.lineinfile:\n    path: /etc/ssh/sshd_config\n    line: x',
                 'ansible.builtin.file:\n    path: "{{ _rsk_dir }}/../x"\n    state: absent',
                 # a caller's result fact is not a target: it can be overwritten
                 'ansible.builtin.copy:\n    content: x\n    dest: "{{ _probe_key.known_hosts }}"',
                 'ansible.builtin.tempfile:\n    state: directory\n    prefix: .sshkey_'):
        found = _scratch(f"- name: w\n  {task}\n{FORCED}", PIN)
        assert found == ["w: " + task.split(":")[0].rsplit(".", 1)[-1] + OUTSIDE], task


def test_a_pinned_target_passes_only_in_its_own_file_on_the_runner():
    task = '- name: pin\n  ansible.builtin.copy:\n    content: x\n    dest: "{{ _pshk_kh }}"\n'
    assert not _scratch(task + FORCED, PIN)
    assert _scratch(task + FORCED, REMOVE), "another file's target"
    assert _scratch(task + "  check_mode: false\n", PIN), "a forced write on the TARGET is not runner scratch"


def test_vars_on_a_forced_write_or_its_block_are_refused():
    # PR #319 review: `_pshk_kh: <elsewhere>` in the task's vars redirected the write while
    # its dest text stayed `{{ _pshk_kh }}`.
    task = '- name: pin\n  ansible.builtin.copy:\n    content: x\n    dest: "{{ _pshk_kh }}"\n'
    assert _scratch(task + FORCED + "  vars:\n    _pshk_kh: /tmp/x\n", PIN) == ["pin: copy" + CARRIES_VARS]
    nested = ("- name: outer\n  vars:\n    anything: 1\n  block:\n"
              '    - name: pin\n      ansible.builtin.copy:\n        content: x\n        dest: "{{ _pshk_kh }}"\n'
              "      delegate_to: localhost\n      check_mode: false\n")
    assert _scratch(nested, PIN) == ["pin: copy" + CARRIES_VARS]


def test_the_tempfile_root_is_pinned():
    # PR #319 review: pointing tempfile's `path` elsewhere created key directories outside the
    # temp root, which the wipe then (correctly) refused to delete.
    base = "- name: t\n  ansible.builtin.tempfile:\n    state: directory\n    prefix: .sshkey_\n"
    assert not _scratch(base + f'    path: "{SCRATCH_ROOT}"\n' + FORCED, MATERIALISE)
    assert _scratch(base + '    path: "/tmp/elsewhere"\n' + FORCED, MATERIALISE) == ["t: tempfile" + OUTSIDE]


def test_the_harness_mode_treats_every_file_write_as_forced():
    assert not _scratch("- name: w\n  ansible.builtin.copy:\n    content: x\n    dest: /etc/motd\n")
    found = violations_in(yaml.safe_load("- name: w\n  ansible.builtin.copy:\n    content: x\n    dest: /etc/motd\n"),
                          None, all_writes=True)
    assert found == ["w: copy" + OUTSIDE]


def test_every_pinned_name_has_exactly_its_one_definition():
    assert pinned_definition_problems() == []


def _repo_docs() -> dict:
    return {str(p.relative_to(REPO)): _load(p) for p in _files()}


def test_a_second_definition_anywhere_is_a_violation():
    for extra in (
        # task vars in a caller
        [{"name": "x", "ansible.builtin.debug": {"msg": 1}, "vars": {"_pshk_kh": "/tmp/x"}}],
        # include parameters, which outrank the shared task's set_fact
        [{"name": "x", "ansible.builtin.include_tasks": "tasks/pin-ssh-host-key.yml",
          "vars": {"_pshk_kh": "/tmp/x"}}],
        # another set_fact
        [{"name": "x", "ansible.builtin.set_fact": {"_rsk_dir": "/tmp/x"}}],
        # another register
        [{"name": "x", "ansible.builtin.command": "true", "register": "_msk_dir"}],
        # play vars
        [{"hosts": "all", "vars": {"_rsk_dir": "/tmp/x"}, "tasks": []}],
    ):
        docs = {**_repo_docs(), "platform/playbooks/intruder.yml": extra}
        assert pinned_definition_problems(docs), extra


def test_a_changed_pinned_expression_is_a_violation():
    docs = _repo_docs()
    pin = json.loads(json.dumps(docs[PIN]))
    for task in pin:
        facts = task.get("ansible.builtin.set_fact") or {}
        if "_pshk_kh" in facts:
            facts["_pshk_kh"] = "/tmp/elsewhere/known_hosts"
    assert pinned_definition_problems({**docs, PIN: pin})


def test_a_k_equals_v_or_args_set_fact_is_a_second_definition():
    # PR #319 review (B3): `set_fact: _pshk_root=… _pshk_kh=…` was invisible to a scanner that
    # read only the mapping form, and the write followed it.
    for extra in (
        [{"name": "x", "ansible.builtin.set_fact": "_pshk_kh=/tmp/x/.sshkey_q/known_hosts"}],
        [{"name": "x", "set_fact": "cacheable=true _rsk_dir=/tmp/x"}],
        [{"name": "x", "ansible.builtin.set_fact": None, "args": {"_pshk_kh": "/tmp/x"}}],
    ):
        docs = {**_repo_docs(), "platform/playbooks/intruder.yml": extra}
        assert pinned_definition_problems(docs), extra


def test_every_runtime_root_check_computes_the_root_inline():
    # B4: an extra var outranks every set_fact, so a root held in a variable could be moved.
    for rel, marker in ((MATERIALISE, "_msk_dir.path | realpath | dirname"),
                        (REMOVE, "_rsk_dir | realpath | dirname"),
                        (PIN, "_pshk_kh | realpath | dirname | dirname")):
        text = (REPO / rel).read_text()
        (line,) = [ln for ln in text.splitlines() if marker in ln]
        assert line.strip().endswith("== " + SCRATCH_ROOT_INLINE), (rel, line)
        assert not re.search(r"_\w*root\b", text), f"{rel} holds the temp root in a variable"
