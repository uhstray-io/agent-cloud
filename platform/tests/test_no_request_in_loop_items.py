"""A visible task never loops over, or prints, a protected registered result (MISTAKES 4.6).

Protected: the result of a task that hid itself (`no_log: true`), and of a `uri` request that
sent headers - its `invocation` carries them even when the task is visible. A failed loop item
is printed whole; `loop_control.label` only shortens the summary line. ansible-core 2.16.18,
2.19.13 and 2.20.8 print it at default verbosity, 2.21.0 does not.

The repository stdout callback (callback_plugins/redact_requests.py) strips nested requests
from the display; this guard keeps the playbook shape right on its own, because a secret in a
response body (`json.auth.client_token`, `json.data.data`) is not a request the callback strips.

The safe shapes: loop over the clean input and index into the results (`index_var`), or print
derived fields (`_r.json.count`), never the whole register.

ponytail: a register is matched within one file, by name in `loop`/`with_*` and in a debug
`var`/`msg`; an alias made with set_fact, or a consumer across include_tasks, is not seen.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("platform/playbooks", "platform/semaphore")
URI = {"uri", "ansible.builtin.uri"}
DEBUG = {"debug", "ansible.builtin.debug"}


def _tasks(node):
    """Every task in a play/task list, blocks included, in file order."""
    if not isinstance(node, list):
        return
    for item in node:
        if not isinstance(item, dict):
            continue
        yield item
        for key in ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always"):
            yield from _tasks(item.get(key))


def _bearing(task: dict) -> bool:
    """A register worth protecting: the task hid itself (`no_log: true`), or it is a request
    that sent headers, which its registered `invocation` carries even when the task is visible."""
    return task.get("no_log") is True or any(isinstance(task.get(m), dict) and "headers" in task[m] for m in URI)


def violations(text: str) -> list[str]:
    tasks = list(_tasks(yaml.safe_load(text)))
    bearing = {t["register"] for t in tasks if t.get("register") and _bearing(t)}
    found = []
    for t in tasks:
        if t.get("no_log") is True:
            continue
        loop = " ".join(str(v) for k, v in t.items() if k == "loop" or k.startswith("with_"))
        printed = " ".join(str(t[m].get(k, "")) for m in DEBUG if isinstance(t.get(m), dict) for k in ("var", "msg"))
        for reg in sorted(bearing):
            # `<reg>.results | length` (an index range) is the safe shape, not a violation.
            if re.search(rf"\b{re.escape(reg)}\.results\b(?!\s*\|\s*length)", loop):
                found.append(f"{t.get('name', '<unnamed>')!r} loops over {reg}.results")
            # The whole register: not an attribute, an index or a test (`_r.json.count`, `_r is ok`).
            if re.search(rf"\b{re.escape(reg)}\b(?!\s*[.\[]|\s+is\b)", printed):
                found.append(f"{t.get('name', '<unnamed>')!r} prints {reg}")
    return found


def test_no_visible_task_loops_over_credential_bearing_results():
    found = []
    for base in SCANNED:
        for path in sorted((ROOT / base).rglob("*.yml")):
            try:
                found += [f"{path.relative_to(ROOT)}: {v}" for v in violations(path.read_text())]
            except yaml.YAMLError:
                continue  # templates and non-Ansible YAML; yamllint owns syntax
    assert not found, "\n".join(found)


def test_the_guard_catches_the_shape_it_exists_for():
    leaking = """
- hosts: localhost
  tasks:
    - ansible.builtin.uri: {url: "https://x", headers: {Authorization: "Bearer {{ t }}"}}
      register: _reads
      loop: [a]
    - ansible.builtin.assert: {that: false}
      loop: "{{ _reads.results }}"
      loop_control: {label: "{{ item.item }}"}
"""
    # A visible request that sent headers is protected even without no_log.
    assert violations(leaking) == ["'<unnamed>' loops over _reads.results"]
    assert violations(leaking.replace("{that: false}", "{that: false}\n      no_log: true")) == []
    for safe in ('loop: [a]', 'loop: "{{ range(_reads.results | length) | list }}"'):
        assert violations(leaking.replace('loop: "{{ _reads.results }}"', safe)) == []
    filtered = 'loop: "{{ _reads.results | selectattr(\'json\', \'defined\') }}"'
    assert violations(leaking.replace('loop: "{{ _reads.results }}"', filtered)) == [
        "'<unnamed>' loops over _reads.results"]


def test_any_no_log_source_is_protected_and_whole_register_prints_are_caught():
    play = """
- hosts: localhost
  tasks:
    - ansible.builtin.slurp: {src: /x}
      register: _files
      no_log: true
      loop: [a]
    - ansible.builtin.set_fact: {n: "{{ item.content }}"}
      with_items: "{{ _files.results }}"
    - ansible.builtin.debug: {var: _files}
    - ansible.builtin.debug: {msg: "{{ _files.results | length }} files, {{ _files is succeeded }}"}
    - ansible.builtin.slurp: {src: /y}
      register: _public
    - ansible.builtin.debug: {var: _public}
"""
    assert violations(play) == ["'<unnamed>' loops over _files.results", "'<unnamed>' prints _files"]


# A KV read's response body IS the secret (`json.data.data`), and `-v` prints every task's
# result whether or not it is registered, so the read itself must be hidden; hiding its
# consumers is not enough (sync-secrets-to-openbao.yml "Verify secrets stored", 2026-09-29).
KV_READ = re.compile(r"/v1/secret/data/")


def visible_secret_reads(text: str) -> list[str]:
    found = []
    for t in _tasks(yaml.safe_load(text)):
        for m in URI:
            args = t.get(m)
            if (isinstance(args, dict) and KV_READ.search(str(args.get("url", "")))
                    and str(args.get("method", "GET")).upper() == "GET" and t.get("no_log") is not True):
                found.append(f"{t.get('name', '<unnamed>')!r} reads a secret without no_log")
    return found


def test_no_visible_task_reads_a_secret():
    found = []
    for base in SCANNED:
        for path in sorted((ROOT / base).rglob("*.yml")):
            try:
                found += [f"{path.relative_to(ROOT)}: {v}" for v in visible_secret_reads(path.read_text())]
            except yaml.YAMLError:
                continue
    assert not found, "\n".join(found)


def test_the_secret_read_guard_catches_a_visible_get_and_passes_the_safe_shapes():
    read = """
- hosts: localhost
  tasks:
    - name: read
      ansible.builtin.uri:
        url: "{{ bao }}/v1/secret/data/services/x"
        headers: {X-Vault-Token: t}
      register: _r
"""
    assert visible_secret_reads(read) == ["'read' reads a secret without no_log"]
    assert visible_secret_reads(read + "      no_log: true\n") == []
    assert visible_secret_reads(read.replace("x\"\n", "x\"\n        method: POST\n")) == []
