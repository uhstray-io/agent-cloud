"""Structure of refresh-console-github-token.yml and its schedule (plan 17, task 8.0b).

What these hold is what goes wrong quietly: a grant that widens (a write level, an added
permission, a second repository), a key or token that reaches argv or a visible task, a write to a
store path other than the one the console reads, and a schedule that runs `main` or the wrong
cadence. The playbook is read as data; no OpenBao, GitHub or Ansible run is started.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/refresh-console-github-token.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
PLAYBOOK_PATH = "platform/playbooks/refresh-console-github-token.yml"
READ_ONLY_KEYS = {"contents", "checks", "actions", "pull_requests", "metadata"}
MODULE_PREFIXES = ("", "ansible.builtin.", "ansible.legacy.")


def _plays():
    return yaml.safe_load(PLAYBOOK.read_text())


def _play():
    return next(p for p in _plays() if p.get("hosts") == "localhost")


def _tasks():
    return _play()["tasks"]


def _module_key(task, short):
    """The key a task uses for a module, short or fully qualified, or None."""
    return next((f"{p}{short}" for p in MODULE_PREFIXES if f"{p}{short}" in task), None)


def _args(task, short):
    key = _module_key(task, short)
    return task[key] if key else None


def _find(predicate):
    hits = [t for t in _tasks() if predicate(t)]
    assert len(hits) == 1, f"expected exactly one matching task, found {len(hits)}"
    return hits[0]


def _is_uri_with_token(t):
    args = _args(t, "uri")
    return args is not None and "X-Vault-Token" in (args.get("headers") or {})


def _sets(t, fact):
    args = _args(t, "set_fact")
    return isinstance(args, dict) and fact in args


def _mint():
    return _find(lambda t: "installation-token" in ((_args(t, "command") or {}).get("argv") or []))


def _argv_value(argv, flag):
    """The element that follows a flag in an argv list."""
    return argv[argv.index(flag) + 1]


def test_the_playbook_imports_the_internal_name_refusal_before_anything_else():
    first = _plays()[0]
    assert any(k.endswith("import_playbook") for k in first), first
    assert first[next(k for k in first if k.endswith("import_playbook"))] == "refuse-internal-extra-vars.yml"


def test_the_grant_is_declared_once_and_is_the_five_read_permissions():
    grant = _play()["vars"]["_console_permissions"]
    assert set(grant) == READ_ONLY_KEYS
    assert all(level == "read" for level in grant.values()), grant


def test_the_mint_requests_the_declared_grant_and_only_the_one_repository():
    argv = _args(_mint(), "command")["argv"]
    assert _argv_value(argv, "--permissions") == "{{ _console_permissions | to_json }}"
    assert _argv_value(argv, "--repository") == "{{ _console_repository }}"
    assert _play()["vars"]["_console_repository"] == "agent-cloud"
    assert argv.count("--repository") == 1


def test_the_signing_key_arrives_on_stdin_and_never_on_argv():
    mint = _mint()
    command = _args(mint, "command")
    assert _argv_value(command["argv"], "--key") == "-"
    assert command["stdin"] == "{{ _cg_app_fields.console_app_key }}"
    # The client id is the issuer and is public (github_app_token.py docstring); the key and the
    # minted token are the two values that must never be an argument.
    assert not any("console_app_key" in a or "_cg_token" in a for a in command["argv"]), command["argv"]


def test_the_token_is_not_put_on_argv_or_printed_by_any_task():
    for task in _tasks():
        if _module_key(task, "debug"):
            text = yaml.safe_dump(task)
            assert "_cg_token" not in text and "_cg_mint" not in text and "_cg_app_fields" not in text, task
    assert not any("_cg_token" in a for a in _args(_mint(), "command")["argv"])


def test_every_task_that_touches_the_credential_is_hidden_and_pins_check_mode():
    login = _find(lambda t: "auth/approle/login" in str((_args(t, "uri") or {}).get("url", "")))
    read = _find(_is_uri_with_token)
    mint = _mint()
    fields = _find(lambda t: _sets(t, "_cg_app_fields"))
    token = _find(lambda t: _sets(t, "_cg_token"))
    store = _find(lambda t: _args(t, "include_tasks") == "tasks/bao-merge-keys.yml")
    for task in (login, read, mint, fields, token, store):
        assert task.get("no_log") is True, task.get("name")
    for task in (login, read, mint):
        assert task.get("check_mode") is False, task.get("name")
    assert mint.get("changed_when") is False


def test_the_store_read_takes_only_the_declared_github_path():
    read = _find(_is_uri_with_token)
    assert _args(read, "uri")["url"] == "{{ _bao_url }}/v1/secret/data/{{ _console_github_path }}"
    assert _play()["vars"]["_console_github_path"] == "services/github"


def test_the_token_is_stored_under_the_console_path_by_a_merge_with_one_timestamp():
    store = _find(lambda t: _args(t, "include_tasks") == "tasks/bao-merge-keys.yml")
    vars_ = store["vars"]
    assert _play()["vars"]["_console_secret_path"] == "services/console/github"
    assert vars_["_bm_path"] == "{{ _console_secret_path }}"
    assert set(vars_["_bm_data"]) == {"token", "refreshed_at"}
    assert vars_["_bm_data"]["token"] == "{{ _cg_token }}"
    # The stamp is set once before the write, so the merge's two renders cannot disagree.
    tasks = _tasks()
    stamp = next(i for i, t in enumerate(tasks) if _sets(t, "_cg_refreshed_at"))
    assert stamp < tasks.index(store)
    assert vars_["_bm_data"]["refreshed_at"] == "{{ _cg_refreshed_at }}"


def test_the_mint_does_not_fail_itself_and_a_visible_assert_names_the_exit_code():
    # The mint's stdout is the token, so the task stays no_log and cannot name its own failure. The signer's
    # exit code (6 = over-grant) reaches a visible assert through a no_log fact, and the message holds the code only.
    tasks = _tasks()
    mint = _mint()
    assert mint.get("failed_when") is False
    at = tasks.index(mint)
    rc_fact, check = tasks[at + 1], tasks[at + 2]
    assert _sets(rc_fact, "_cg_mint_rc") and rc_fact.get("no_log") is True, rc_fact.get("name")
    assert "ansible.builtin.assert" in check and "no_log" not in check, check.get("name")
    assert check["ansible.builtin.assert"]["that"] == ["_cg_mint_rc == 0"]
    msg = check["ansible.builtin.assert"]["fail_msg"]
    assert "GitHub granted a wider scope than requested" in msg
    assert "fix the console App's permissions" in msg
    assert 'if _cg_mint_rc == 6 else "mint failed (exit " ~ _cg_mint_rc ~ ")"' in msg
    assert "_cg_mint.stdout" not in msg and "_cg_mint.stderr" not in msg
    assert tasks.index(check) < next(i for i, t in enumerate(tasks) if _sets(t, "_cg_token"))


def _templates():
    return yaml.safe_load(TEMPLATES.read_text())["templates"]


def test_the_playbook_is_scheduled_dev_bound_every_45_minutes_and_nowhere_else():
    entries = [t for t in _templates() if t.get("playbook") == PLAYBOOK_PATH]
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["name"] == "Refresh Console GitHub Token (Dev)"
    assert entry["repository"] == "agent-cloud dev"
    assert entry["schedule"] == {"cron": "*/45 * * * *"}
    assert "survey_vars" not in entry


def test_the_playbook_file_exists_where_the_template_points():
    assert PLAYBOOK.is_file()
