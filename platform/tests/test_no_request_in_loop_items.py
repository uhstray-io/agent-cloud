"""Two guards on what a visible task can put in the task output.

1. A visible task never loops over, or prints, a protected registered result (MISTAKES 4.6).

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

2. A task that receives a secret is itself hidden. See the section below.

Both guards read every playbook in one pass, parsed once per session (playbook_yaml.py).
"""

import functools
import re

import playbook_yaml

ROOT = playbook_yaml.REPO
URI = {"uri", "ansible.builtin.uri", "ansible.legacy.uri"}
DEBUG = {"debug", "ansible.builtin.debug"}
TASK_LISTS = ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always")


def _tasks(node):
    """Every task in a play/task list, blocks included, in file order."""
    if not isinstance(node, list):
        return
    for item in node:
        if not isinstance(item, dict):
            continue
        yield item
        for key in TASK_LISTS:
            yield from _tasks(item.get(key))


def _bearing(task: dict) -> bool:
    """A register worth protecting: the task hid itself (`no_log: true`), or it is a request
    that sent headers, which its registered `invocation` carries even when the task is visible."""
    return task.get("no_log") is True or any(isinstance(task.get(m), dict) and "headers" in task[m] for m in URI)


def loop_violations(doc) -> list[str]:
    tasks = list(_tasks(doc))
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


def violations(text: str) -> list[str]:
    return loop_violations(playbook_yaml.loads(text))


# 2. A task that receives a secret is itself hidden. `-v` prints every task's result whether or
# not it is registered, so hiding the consumers is not enough (sync-secrets-to-openbao.yml
# "Verify secrets stored", 2026-09-29). Deny by default: a visible task (no `no_log: true` on
# it, an enclosing block or its play) is refused when it
#   a. makes an OpenBao request - a `uri` carrying an X-Vault-Token header, or whose path under
#      /v1/ is OpenBao-shaped (sys/, auth/, identity/, cubbyhole/, <mount>/data|metadata/, or
#      a path built from variables) - unless that request is on NON_SECRET_RESPONSES;
#   b. runs the OpenBao CLI (`bao`/`vault` with any subcommand but status/version);
#   c. calls a hashi_vault lookup anywhere in its parameters (set_fact prints the fact at -v),
#      unless every lookup selects a public field, or the task only passes the value down
#      (include/import `vars:`); or runs a community.hashi_vault module.
# The request itself (its token header) reaches the output only at -vvv, the callback's
# concern above; this guard is about the RESPONSE.
#
# ponytail, accepted gaps (none occurs in the repository, 2026-09-29): play- and block-level
# `vars:` are not refused (templated lazily, they print only through a consumer, and neither
# guard traces a variable to its consumer, including one in an included file); a token header
# passed as a variable (`headers: "{{ h }}"`) with no /v1/ path; curl to OpenBao inside a shell
# task; the short `vault_*` module names; free-form `uri: url=...` arguments; and a `{{ }}` only
# at the START of the path counts as "built from variables".
OPENBAO_PATH = re.compile(r"(?:sys|auth|identity|cubbyhole)/|[^/\s{}]+/(?:data|metadata)/|\{\{")
# Requests whose response carries no secret, each one found visible in the repository
# (2026-09-29). Path after /v1/, query string dropped, matched whole. Everything else that
# returns material stays refused: auth/*/login (client_token), .../secret-id, .../role-id
# (half an AppRole credential: with a secret_id it IS the login, and the store keeps the two
# together), auth/token/create, sys/wrapping/*, sys/init, and every KV read or write.
NON_SECRET_RESPONSES = (
    ({"GET", "HEAD"}, r"sys/(?:health|seal-status)"),  # seal and health status
    ({"PUT", "POST"}, r"sys/policies/acl/[^/]+"),  # a policy write: the policy came from the repo
    ({"POST"}, r"sys/(?:mounts|auth)/[^/]+"),  # enable a secrets engine / auth method
    ({"POST"}, r"auth/[^/]+/role/[^/]+"),  # write a role's configuration
)
COMMANDS = {f"{prefix}{m}" for prefix in ("", "ansible.builtin.", "ansible.legacy.")
            for m in ("command", "shell", "raw")}
BAO_CLI = re.compile(r"(?:^|[\s;&|(`'\"])(?:\S*/)?(?:bao|vault)\s+(?!(?:status|version|-version|--version)\b)[a-z-]")
VAULT_LOOKUP = re.compile(r"\b(?:lookup|query|q)\(\s*['\"](?:community\.hashi_vault\.)?(?:hashi_vault|vault_\w+)['\"]")
# A lookup whose term ends in one of these selects a field that is not a secret.
PUBLIC_FIELDS = (":public_key",)
PASS_THROUGH = {f"{prefix}{m}" for prefix in ("", "ansible.builtin.")
                for m in ("include_tasks", "import_tasks", "include_role", "import_role")}


def _visible(node, hidden: bool = False):
    """Every play, block and task whose output Ansible does not hide. A task's own `no_log`
    wins over an enclosing block's or play's: `no_log: false` inside a hidden block prints
    (checked on ansible-core 2.21, review of #347), so only an unset value inherits."""
    if not isinstance(node, list):
        return
    for item in node:
        if not isinstance(item, dict):
            continue
        inner = (item["no_log"] is True) if "no_log" in item else hidden
        if not inner:
            yield item
        for key in TASK_LISTS:
            yield from _visible(item.get(key), inner)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _secret_lookup(text: str) -> bool:
    for m in VAULT_LOOKUP.finditer(text):
        # The term: everything up to the first keyword argument or the closing parenthesis.
        term = re.split(r",\s*\w+\s*=|\)", text[m.end():], maxsplit=1)[0]
        literals = re.findall(r"['\"]([^'\"]*)['\"]", term)
        if not (literals and literals[-1].endswith(PUBLIC_FIELDS)):
            return True
    return False


def _openbao_request(args: dict) -> str | None:
    """`METHOD path` of an OpenBao request whose response is not known to be secret-free."""
    url = " ".join(str(args.get("url", "")).split())
    headers = args.get("headers")
    token = isinstance(headers, dict) and any(str(k).lower() == "x-vault-token" for k in headers)
    path = url.split("/v1/", 1)[1].split("?", 1)[0] if "/v1/" in url else None
    if not token and not (path is not None and OPENBAO_PATH.match(path)):
        return None
    method = str(args.get("method", "GET")).upper()
    if path is not None and any(method in methods and re.fullmatch(p, path) for methods, p in NON_SECRET_RESPONSES):
        return None
    return f"{method} {path if path is not None else url}"


def _command_text(args) -> str:
    if isinstance(args, str):
        return args
    if isinstance(args, dict):
        argv = args.get("argv")
        if isinstance(argv, list):
            return " ".join(str(a) for a in argv)
        return " ".join(str(args.get(k, "")) for k in ("cmd", "_raw_params", "argv"))
    return ""


def secret_exposures(doc) -> list[str]:
    found = []
    for t in _visible(doc):
        if "hosts" in t or "import_playbook" in t or any(k in t for k in ("block", "rescue", "always")):
            continue  # a play or a block: its vars pass down, its tasks are read on their own
        name = repr(t.get("name", "<unnamed>"))
        for key, args in t.items():
            if key in URI and isinstance(args, dict) and (request := _openbao_request(args)):
                found.append(f"{name} sends OpenBao {request} without no_log")
            elif key in COMMANDS and BAO_CLI.search(_command_text(args)):
                found.append(f"{name} runs the OpenBao CLI without no_log")
            elif key.startswith("community.hashi_vault."):
                found.append(f"{name} runs {key} without no_log")
        passed_down = any(k in PASS_THROUGH for k in t)
        params = {k: v for k, v in t.items() if not (passed_down and k == "vars")}
        if any(_secret_lookup(s) for s in _strings(params)):
            found.append(f"{name} calls a hashi_vault lookup without no_log")
    return found


def visible_secret_reads(text: str) -> list[str]:
    return secret_exposures(playbook_yaml.loads(text))


@functools.cache
def _repo_findings() -> tuple[list[str], list[str]]:
    """Both guards over every playbook, in one pass over the shared parse."""
    loops, secrets = [], []
    for path in playbook_yaml.files():
        doc, rel = playbook_yaml.load(path), path.relative_to(ROOT)
        loops += [f"{rel}: {v}" for v in loop_violations(doc)]
        secrets += [f"{rel}: {v}" for v in secret_exposures(doc)]
    return loops, secrets


def test_no_visible_task_loops_over_credential_bearing_results():
    found = _repo_findings()[0]
    assert not found, "\n".join(found)


def test_no_visible_task_reads_a_secret():
    found = _repo_findings()[1]
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


def _one(task: str, hide: str = "") -> list[str]:
    """The guard's findings for one task (indented four spaces), in a play."""
    return visible_secret_reads(f"- hosts: localhost\n{hide}  tasks:\n    - name: t\n{task}")


def test_the_secret_read_guard_catches_a_visible_get_and_passes_the_safe_shapes():
    read = """      ansible.builtin.uri:
        url: "{{ bao }}/v1/secret/data/services/x"
        headers: {X-Vault-Token: t}
      register: _r
"""
    assert _one(read) == ["'t' sends OpenBao GET secret/data/services/x without no_log"]
    assert _one(read + "      no_log: true\n") == []
    # The request body of a KV write is the secret too, and its response is refused by default.
    assert _one(read.replace("x\"\n", "x\"\n        method: POST\n")) == [
        "'t' sends OpenBao POST secret/data/services/x without no_log"]


REFUSED = {
    "login, no token header": 'uri: {url: "{{ b }}/v1/auth/approle/login", method: POST, body: {}}',
    "secret-id": 'uri: {url: "{{ b }}/v1/auth/approle/role/x/secret-id", method: POST, headers: {X-Vault-Token: t}}',
    "role-id": 'uri: {url: "{{ b }}/v1/auth/approle/role/x/role-id", headers: {X-Vault-Token: t}}',
    "token create": 'uri: {url: "{{ b }}/v1/auth/token/create", method: POST, headers: {X-Vault-Token: t}}',
    "unwrap": 'uri: {url: "{{ b }}/v1/sys/wrapping/unwrap", method: POST, headers: {X-Vault-Token: t}}',
    "sys/init": 'uri: {url: "{{ b }}/v1/sys/init", method: PUT}',
    "KV at another mount": 'uri: {url: "{{ b }}/v1/kv/data/x"}',
    "path from vars": 'uri: {url: "{{ b }}/v1/{{ p }}"}',
    "token, no /v1/ in the url": 'uri: {url: "{{ u }}", headers: {x-vault-token: t}}',
    "templated method": 'uri: {url: "{{ b }}/v1/sys/policies/acl/x", method: "{{ m }}", headers: {X-Vault-Token: t}}',
    "bao kv get": 'ansible.builtin.command: podman exec openbao bao kv get secret/x',
    "vault read": 'ansible.builtin.shell: "set -e; /usr/bin/vault read -field=x secret/x"',
    "argv": 'ansible.builtin.command: {argv: [bao, read, auth/approle/role/x/role-id]}',
    "set_fact lookup": "set_fact: {x: \"{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/s:token') }}\"}",
    "task vars lookup": "ansible.builtin.copy: {content: \"{{ x }}\", dest: /y}\n"
                        "      vars: {x: \"{{ lookup('hashi_vault', 'secret/data/s') }}\"}",
    "hashi_vault module": 'community.hashi_vault.vault_kv2_get: {path: s}',
}

ALLOWED = {
    "health": 'uri: {url: "{{ b }}/v1/sys/health?standbyok=true"}',
    "seal status": 'uri: {url: "{{ b }}/v1/sys/seal-status"}',
    "policy write": 'uri: {url: "{{ b }}/v1/sys/policies/acl/x", method: PUT, headers: {X-Vault-Token: t}}',
    "enable engine": 'uri: {url: "{{ b }}/v1/sys/mounts/secret", method: POST, headers: {X-Vault-Token: t}}',
    "enable auth": 'uri: {url: "{{ b }}/v1/sys/auth/approle", method: POST, headers: {X-Vault-Token: t}}',
    "role write": 'uri: {url: "{{ b }}/v1/auth/approle/role/x", method: POST, headers: {X-Vault-Token: t}}',
    "OPA decision": 'uri: {url: "{{ o }}/v1/data/agentcloud/decision", method: POST}',
    "n8n API": 'uri: {url: "{{ n }}/api/v1/credentials", headers: {X-N8N-API-KEY: k}}',
    "bao status": 'ansible.builtin.command: bao status',
    "ansible-vault": 'ansible.builtin.command: ansible-vault view x',
    "public key": "set_fact: {x: \"{{ lookup('community.hashi_vault.hashi_vault',\n"
                  "            'secret/data/ssh/' + s + ':public_key', url=u) }}\"}",
    "include vars": "ansible.builtin.include_tasks: x.yml\n"
                    "      vars: {x: \"{{ lookup('hashi_vault', 'secret/data/s') }}\"}",
}


def test_the_secret_read_guard_refuses_every_secret_returning_shape():
    for label, task in REFUSED.items():
        assert len(_one(f"      {task}\n")) == 1, label
        assert _one(f"      {task}\n      no_log: true\n") == [], label


def test_the_secret_read_guard_passes_non_secret_responses():
    for label, task in ALLOWED.items():
        assert _one(f"      {task}\n") == [], label


def test_an_enclosing_block_or_play_no_log_hides_the_task():
    task = REFUSED["set_fact lookup"]
    block = f"      block:\n        - {task}\n"
    assert len(_one(block)) == 1
    assert _one(block + "      no_log: true\n") == []
    assert _one(f"      {task}\n", hide="  no_log: true\n") == []


def test_a_task_that_sets_no_log_false_inside_a_hidden_block_or_play_is_visible():
    task = REFUSED["set_fact lookup"]
    override = f"          {task.strip()}\n          no_log: false\n"
    block = f"      block:\n        - name: inner\n{override}      no_log: true\n"
    assert len(_one(block)) == 1
    assert len(_one(f"      {task}\n      no_log: false\n", hide="  no_log: true\n")) == 1
