"""Structure of release-version.yml and its two templates (plan 20).

The playbook pushes version tags with a GitHub App token that holds contents and workflows write. What
goes wrong quietly is: a grant that widens, the token reaching argv or a visible task, a push that can
move or delete a ref, a planned name reaching a refspec before it is validated, a credential that a
system helper can answer for, a scratch directory or token left behind, and a template that schedules
the major release. The playbook is read as data; no OpenBao, GitHub, git or Ansible run is started
(the notes task's small decoding program is run on fixed input).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import harness_sandbox
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/release-version.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
PLAYBOOK_PATH = "platform/playbooks/release-version.yml"
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
# Names that hold the token, the App credentials, an OpenBao client token, or a response that carries one.
SECRET_NAME = re.compile(
    r"\b(_rv_(token|app_fields|auth|github_secret|mint|release_get|release_post)|_bao_(role|secret)_id)\b"
)
KEYWORDS = {
    "name",
    "register",
    "when",
    "no_log",
    "changed_when",
    "failed_when",
    "check_mode",
    "environment",
    "vars",
    "tags",
    "become",
    "loop",
    "ignore_errors",
    "delegate_to",
    "connection",
    "loop_control",
}
FORCE_ARGV = {"-f", "--force", "--force-with-lease", "-d", "--delete", "--mirror"}


def _plays():
    return yaml.safe_load(PLAYBOOK.read_text())


def _play():
    return _plays()[1]


def _walk(tasks, section, hidden, env, out):
    """Every task that runs, in document order, with what Ansible inherits: no_log and environment
    from the enclosing block (a task's own no_log wins), and the section it sits in."""
    for task in tasks:
        if any(k in task for k in ("block", "rescue", "always")):
            inner_hidden = task.get("no_log", hidden)
            inner_env = {**env, **(task.get("environment") or {})}
            for key in ("block", "rescue", "always"):
                _walk(task.get(key) or [], key, inner_hidden, inner_env, out)
            continue
        out.append(
            {
                "task": task,
                "name": task.get("name", ""),
                "section": section,
                "no_log": task.get("no_log", hidden),
                "env": {**env, **(task.get("environment") or {})},
            }
        )
    return out


LEAVES = _walk(_play()["tasks"], "tasks", False, {}, [])


def _module(task):
    keys = [k for k in task if k not in KEYWORDS]
    assert len(keys) == 1, task.get("name")
    return keys[0].rsplit(".", 1)[-1], task[keys[0]]


def _argv(task):
    module, args = _module(task)
    return args.get("argv") if module == "command" else None


def _one(pred, what):
    hits = [leaf for leaf in LEAVES if pred(leaf)]
    assert len(hits) == 1, f"{what}: expected one task, found {len(hits)}"
    return hits[0]


def _index(leaf):
    return LEAVES.index(leaf)


def _by_module(module, pred=lambda args: True):
    return [leaf for leaf in LEAVES if _module(leaf["task"])[0] == module and pred(_module(leaf["task"])[1])]


def _registered(name):
    return _one(lambda leaf: leaf["task"].get("register") == name, name)


def _sets(leaf, fact):
    module, args = _module(leaf["task"])
    return module == "set_fact" and fact in args


def _tag_regex():
    return _values()["_rv_tag_re"]


def _values():
    """The play's values: the first task's set_fact (the play carries no vars: block, see below)."""
    first = _play()["tasks"][0]
    assert first.get("name") == "Set the play's values", first.get("name")
    return first["ansible.builtin.set_fact"]


def test_the_play_has_no_vars_block_so_no_forced_write_can_be_redirected():
    # test_check_mode_contract.py refuses a forced check-mode write under vars:, play-level included.
    assert "vars" not in _play()


def test_the_playbook_imports_the_internal_name_guard_first():
    first = _plays()[0]
    key = next(k for k in first if k.endswith("import_playbook"))
    assert first[key] == "refuse-internal-extra-vars.yml"


def test_the_release_identity_is_literal_and_not_overridable():
    vars_ = _values()
    assert vars_["_rv_org"] == "uhstray-io"
    assert vars_["_rv_repository"] == "agent-cloud"
    assert vars_["_rv_clone_url"] == "https://github.com/uhstray-io/agent-cloud.git"
    for name in ("_rv_org", "_rv_repository", "_rv_clone_url", "_rv_permissions"):
        assert "{{" not in str(vars_[name]), name


def test_the_grant_is_exactly_contents_workflows_and_metadata_and_is_declared_once():
    assert _values()["_rv_permissions"] == {"contents": "write", "workflows": "write", "metadata": "read"}


def test_the_mint_requests_the_declared_grant_and_one_repository_of_one_org():
    argv = _argv(_registered("_rv_mint")["task"])
    for flag, value in (
        ("--permissions", "{{ _rv_permissions | to_json }}"),
        ("--repository", "{{ _rv_repository }}"),
        ("--org", "{{ _rv_org }}"),
    ):
        assert argv[argv.index(flag) + 1] == value, flag
        assert argv.count(flag) == 1, flag


def test_the_signing_key_arrives_on_stdin_and_never_on_argv():
    mint = _registered("_rv_mint")["task"]
    argv = _argv(mint)
    # Isolated mode: the signer's directory is not on sys.path while the key is on stdin.
    assert argv[:3] == ["python3", "-I", "{{ playbook_dir }}/../lib/github_app_token.py"], argv[:3]
    assert argv[argv.index("--key") + 1] == "-"
    assert (
        mint["command" if "command" in mint else "ansible.builtin.command"]["stdin"]
        == "{{ _rv_app_fields.release_app_key }}"
    )
    # The client id is the public issuer and may be an argument; the key and the token may not.
    assert not any("release_app_key" in a or "_rv_token" in a for a in argv), argv


def test_the_token_is_never_an_argument_and_no_visible_debug_names_a_secret():
    for leaf in LEAVES:
        argv = _argv(leaf["task"])
        if argv is not None:
            assert not any("_rv_token" in str(a) for a in argv), leaf["name"]
        module, args = _module(leaf["task"])
        if module == "debug" and not leaf["no_log"]:
            assert not SECRET_NAME.search(yaml.safe_dump(leaf["task"])), leaf["name"]


def test_every_task_that_holds_a_secret_is_hidden_and_no_assert_names_one():
    for leaf in LEAVES:
        module, args = _module(leaf["task"])
        text = yaml.safe_dump(leaf["task"])
        if module == "assert":
            # An assert prints its expression at failure; its fail_msg must not name a secret.
            assert not SECRET_NAME.search(str(args.get("fail_msg", ""))), leaf["name"]
            continue
        holds = SECRET_NAME.search(text) or "Authorization" in text or "X-Vault-Token" in text
        if holds:
            assert leaf["no_log"] is True, f"not hidden: {leaf['name']}"


def test_the_openbao_login_and_secret_read_are_hidden_and_pin_check_mode():
    login = _one(
        lambda lf: (
            _module(lf["task"])[0] == "uri"
            and str(_module(lf["task"])[1].get("url", "")).endswith("/v1/auth/approle/login")
        ),
        "login",
    )
    read = _one(
        lambda lf: (
            _module(lf["task"])[0] == "uri" and "X-Vault-Token" in str(_module(lf["task"])[1].get("headers", {}))
        ),
        "read",
    )
    for leaf in (login, read):
        assert leaf["no_log"] is True, leaf["name"]
        assert leaf["task"].get("check_mode") is False, leaf["name"]
    assert _values()["_rv_github_path"] == "services/github"


def test_the_app_fields_the_exit_code_and_the_token_are_taken_by_hidden_set_facts():
    for fact in ("_bao_role_id", "_bao_secret_id", "_rv_app_fields", "_rv_mint_rc", "_rv_token"):
        leaf = _one(lambda lf, f=fact: _sets(lf, f), fact)
        assert leaf["no_log"] is True, fact
    assert "release_app_key" in str(_play()["tasks"]) and "release_app_client_id" in str(_play()["tasks"])


def test_every_command_is_argv_and_every_command_carries_the_three_git_variables():
    for leaf in LEAVES:
        module, args = _module(leaf["task"])
        assert module != "shell", leaf["name"]
        if module == "command":
            assert isinstance(args, dict) and "argv" in args, leaf["name"]
            for key, value in GIT_ENV.items():
                assert leaf["env"].get(key) == value, f"{leaf['name']}: {key}"


def test_no_command_names_a_global_or_system_git_config():
    for leaf in _by_module("command"):
        assert "--global" not in str(leaf["task"]) and "--system" not in str(leaf["task"]), leaf["name"]


def test_the_clone_is_anonymous_and_the_credential_helper_is_set_on_the_clone_only():
    clone = _one(lambda lf: "clone" in (_argv(lf["task"]) or []), "clone")
    assert "x-access-token" not in str(clone["task"]) and "_rv_token" not in str(clone["task"])
    helper = _one(lambda lf: "credential.helper" in (_argv(lf["task"]) or []), "helper")
    argv = _argv(helper["task"])
    assert argv[:3] == ["git", "-C", "{{ _rv_tmp.path }}/repo"], argv
    assert argv[-1] == "store --file={{ _rv_tmp.path }}/cred", argv


def test_the_credential_file_is_0600_hidden_and_written_after_the_plan_is_validated_and_before_any_push():
    cred = _one(
        lambda lf: (
            _module(lf["task"])[0] == "copy" and "x-access-token" in str(_module(lf["task"])[1].get("content", ""))
        ),
        "cred",
    )
    assert cred["no_log"] is True
    copy_args = _module(cred["task"])[1]
    assert str(copy_args["mode"]) == "0600"
    assert cred["task"].get("check_mode") is False
    assert copy_args["dest"] == "{{ _rv_tmp.path }}/cred"
    planner = _registered("_rv_planner")
    validate = _one(
        lambda lf: (
            lf["task"].get("ansible.builtin.assert", {}).get("that")
            and "_rv_tag_re" in str(lf["task"]["ansible.builtin.assert"]["that"])
        ),
        "validate",
    )
    push = _registered("_rv_push")
    assert _index(planner) < _index(validate) < _index(cred) < _index(push)


def test_the_planner_and_the_push_are_visible_and_the_mint_clone_and_planner_run_in_check_mode():
    planner, push = _registered("_rv_planner"), _registered("_rv_push")
    assert planner["no_log"] is False and push["no_log"] is False
    assert planner["task"].get("check_mode") is False
    assert planner["task"].get("failed_when") is False
    assert push["task"].get("failed_when") is False
    assert "not ansible_check_mode" in str(push["task"].get("when"))
    mint = _registered("_rv_mint")
    assert mint["task"].get("check_mode") is False and mint["no_log"] is True
    clone = _one(lambda lf: "clone" in (_argv(lf["task"]) or []), "clone")
    assert clone["task"].get("check_mode") is False


def test_the_planner_command_is_the_contract_and_the_major_flag_is_added_only_when_given():
    base = _one(lambda lf: lf["task"].get("name", "").startswith("Build the planner command"), "base")
    argv = base["task"]["ansible.builtin.set_fact"]["_rv_planner_base"]
    assert argv[:2] == ["python3", "-I"]
    assert argv[2] == "{{ playbook_dir }}/../../scripts/semver.py"
    assert argv[argv.index("release-tags") :] == [
        "release-tags",
        "--main-ref",
        "origin/main",
        "--dev-ref",
        "origin/dev",
    ]
    assert argv[argv.index("--repo") + 1] == "{{ _rv_tmp.path }}/repo"
    add = _one(lambda lf: _sets(lf, "_rv_planner_argv"), "add")
    assert add["task"]["ansible.builtin.set_fact"]["_rv_planner_argv"] == (
        "{{ _rv_planner_base + (['--major', _rv_major] if _rv_major | length > 0 else []) }}"
    )
    assert _argv(_registered("_rv_planner")["task"]) == "{{ _rv_planner_argv }}"
    assert _values()["_rv_major"] == "{{ release_major | default('') | trim }}"


def test_the_major_must_be_empty_or_vN_0_0_by_the_assert_that_runs_first():
    # Tasks 0 and 1 set the play's values and the hidden AppRole credential (no network, no write); the major
    # check is the first task that can refuse.
    first = _play()["tasks"][2]
    assert first["ansible.builtin.assert"]["that"] == [
        "_rv_major | length == 0 or _rv_major is match('^v[0-9]+[.]0[.]0$')"
    ]
    assert re.match("^v[0-9]+[.]0[.]0$", "v1.0.0")
    for bad in ("1.0.0", "v1.2.0", "v1.0.0 x", "v1.0.0-dev.1"):
        assert not re.match("^v[0-9]+[.]0[.]0$", bad), bad


def test_a_planner_refusal_names_the_manual_major_run_and_otherwise_shows_stderr():
    check = _one(
        lambda lf: lf["task"].get("ansible.builtin.assert", {}).get("that") == ["_rv_planner.rc == 0"], "planner assert"
    )
    msg = check["task"]["ansible.builtin.assert"]["fail_msg"]
    assert "needs the manual major release" in msg
    assert "release_major=vN.0.0" in msg
    # A major cut before the breaking change is promoted leaves it behind, and the schedule stays red (#996 review).
    assert "to main first, then run Release Major Version (Dev)" in msg
    assert "_rv_planner.rc == 3" in msg
    assert "_rv_planner.stderr" in msg


def test_the_planned_names_are_validated_before_any_refspec_is_built_and_only_release_form_passes():
    tag_re = _tag_regex()
    assert tag_re == "^v[0-9]+[.][0-9]+[.][0-9]+(-dev[.][0-9]+)?$"
    for good in ("v0.1.0", "v12.3.45", "v0.1.1-dev.1"):
        assert re.match(tag_re, good), good
    for bad in (
        "x:refs/heads/dev",
        "--force",
        "+refs/heads/dev",
        "-f",
        "refs/heads/dev",
        "v0.1",
        "v0.1.0-dev",
        "v0.1.0 x",
        "v0.1.0:refs/heads/main",
        "v0.1.0-dev.1-x",
        "--mirror",
    ):
        assert not re.match(tag_re, bad), bad
    validate = _one(
        lambda lf: (
            lf["task"].get("ansible.builtin.assert", {}).get("that")
            and "_rv_tag_re" in str(lf["task"]["ansible.builtin.assert"]["that"])
        ),
        "validate",
    )
    that = str(validate["task"]["ansible.builtin.assert"]["that"])
    assert "_rv_tag_re" in that and "_rv_sha_re" in that and "_rv_plan.release is none" in that
    assert "nothing was pushed" in validate["task"]["ansible.builtin.assert"]["fail_msg"]
    assert _values()["_rv_sha_re"] == "^[0-9a-f]{40}$"
    push_argv = _one(lambda lf: _sets(lf, "_rv_push_argv"), "push argv")
    assert _index(validate) < _index(push_argv)
    assert _index(push_argv) < _index(_registered("_rv_push"))


def test_the_push_is_one_atomic_argv_of_refs_tags_and_never_forced():
    push_argv = _one(lambda lf: _sets(lf, "_rv_push_argv"), "push argv")["task"]["ansible.builtin.set_fact"][
        "_rv_push_argv"
    ]
    assert push_argv.startswith("{{ ['git', '-C', _rv_tmp.path ~ '/repo', 'push', '--atomic', 'origin'] + ")
    assert "map('regex_replace', '^', 'refs/tags/')" in push_argv
    push = _registered("_rv_push")
    assert _argv(push["task"]) == "{{ _rv_push_argv }}"
    for leaf in _by_module("command"):
        argv = _argv(leaf["task"])
        if isinstance(argv, list):
            assert not (set(argv) & FORCE_ARGV), leaf["name"]
            assert not any(str(a).startswith("--force") or str(a).startswith("+") for a in argv), leaf["name"]
    assert "--force" not in yaml.safe_dump(_play()["tasks"])


def test_the_push_and_the_release_write_are_skipped_in_check_mode():
    assert "not ansible_check_mode" in str(_registered("_rv_push")["task"]["when"])
    post = _one(
        lambda lf: (
            _module(lf["task"])[0] == "uri"
            and _module(lf["task"])[1].get("method") == "POST"
            and str(_module(lf["task"])[1].get("url", "")).endswith("/releases")
        ),
        "POST",
    )
    assert post["no_log"] is True
    assert "not ansible_check_mode" in str(post["task"]["when"])
    assert post["task"]["ansible.builtin.uri"]["status_code"] == [201]
    assert post["task"]["ansible.builtin.uri"]["body"]["tag_name"] == "{{ _rv_plan.release.tag }}"


def test_the_release_lookup_is_hidden_and_accepts_only_200_or_404():
    get = _one(
        lambda lf: _module(lf["task"])[0] == "uri"
        and "/releases/tags/{{ _rv_plan.release.tag }}" in str(_module(lf["task"])[1].get("url", "")),
        "GET",
    )
    assert get["no_log"] is True
    assert get["task"]["ansible.builtin.uri"]["status_code"] == [200, 404]
    assert get["task"]["ansible.builtin.uri"]["url"] == (
        "https://api.github.com/repos/{{ _rv_org }}/{{ _rv_repository }}/releases/tags/{{ _rv_plan.release.tag }}"
    )


def test_the_scratch_directory_is_made_first_in_the_block_and_removed_in_always_with_the_token_revoked():
    # First in the block, so the always: below removes it whatever fails after it is made.
    make = _one(lambda lf: _module(lf["task"])[0] == "tempfile", "tempfile")
    block_first = next(lf for lf in LEAVES if lf["section"] == "block")
    assert make is block_first
    args = _module(make["task"])[1]
    assert args["prefix"] == ".release-version-"
    assert args["path"] == "{{ lookup('ansible.builtin.env', 'TMPDIR') | default('/tmp', true) | realpath }}"
    assert make["task"].get("delegate_to") == "localhost"
    removal = _one(lambda lf: lf["section"] == "always" and _module(lf["task"])[0] == "file", "remove")
    assert _module(removal["task"])[1]["state"] == "absent"
    assert _module(removal["task"])[1]["path"] == "{{ _rv_tmp.path }}"
    assert removal["task"].get("check_mode") is False
    assert removal["task"].get("delegate_to") == "localhost"
    revoke = _one(lambda lf: lf["section"] == "always" and _module(lf["task"])[0] == "uri", "revoke")
    args = _module(revoke["task"])[1]
    assert args["url"] == "https://api.github.com/installation/token"
    assert args["method"] == "DELETE"
    assert args["status_code"] == [204]
    assert args["headers"]["Authorization"] == "Bearer {{ _rv_token }}"
    assert revoke["no_log"] is True and revoke["task"].get("failed_when") is False
    assert revoke["task"].get("check_mode") is False
    assert "_rv_token is defined" in str(revoke["task"]["when"])


def test_the_block_does_not_hide_its_tasks_so_each_secret_task_carries_its_own_no_log():
    block_task = next(t for t in _play()["tasks"] if "block" in t)
    assert "no_log" not in block_task


def test_every_register_and_set_fact_carries_the_underscore_prefix():
    for leaf in LEAVES:
        reg = leaf["task"].get("register")
        if reg:
            assert reg.startswith("_rv_"), leaf["name"]
        module, args = _module(leaf["task"])
        if module == "set_fact":
            assert all(k.startswith("_") for k in args), leaf["name"]


def test_the_play_vars_are_all_internal_or_the_openbao_connection():
    for name in _values():
        assert name.startswith(("_rv_", "_bao_")), name


def test_the_only_inputs_read_from_outside_are_each_named_value_read_once():
    code = yaml.safe_dump(_play())
    assert code.count("{{ release_major") == 1
    assert code.count("{{ release_refresh_tag") == 1
    assert code.count("{{ release_notes_from_task") == 1
    assert code.count("{{ release_semaphore_project_id") == 1
    assert _values()["_rv_major"] == "{{ release_major | default('') | trim }}"
    assert _values()["_rv_refresh"] == "{{ release_refresh_tag | default('') | trim }}"
    assert "survey" not in code


def _templates():
    return yaml.safe_load(TEMPLATES.read_text())["templates"]


def test_the_version_template_runs_every_15_minutes_dev_bound_with_no_survey():
    entries = [t for t in _templates() if t.get("name") == "Release Version (Dev)"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["playbook"] == PLAYBOOK_PATH
    assert entry["repository"] == "agent-cloud dev"
    assert entry["schedule"] == {"cron": "*/15 * * * *"}
    assert "survey_vars" not in entry


def test_the_major_template_has_no_schedule_and_one_typed_version_input():
    entries = [t for t in _templates() if t.get("name") == "Release Major Version (Dev)"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["playbook"] == PLAYBOOK_PATH
    assert entry["repository"] == "agent-cloud dev"
    assert "schedule" not in entry
    assert entry["survey_vars"] == [
        {
            "name": "release_major",
            "title": "Major version to cut",
            "description": (
                "Type the version this run must cut, e.g. v1.0.0. "
                "The run refuses unless the computed next major is exactly this."
            ),
            "type": "string",
            "required": True,
        }
    ]


def test_the_three_release_templates_are_the_only_ones_on_this_playbook_and_carry_the_dev_suffix():
    entries = [t for t in _templates() if t.get("playbook") == PLAYBOOK_PATH]
    assert sorted(t["name"] for t in entries) == [
        "Refresh Release Notes (Dev)",
        "Release Major Version (Dev)",
        "Release Version (Dev)",
    ]
    assert all(t["name"].endswith(" (Dev)") for t in entries)


def test_the_playbook_file_exists_where_the_templates_point():
    assert PLAYBOOK.is_file()


# --- Release notes and refresh (plan 20). The playbook's refresh run, the notes gate and the templates. ----------

NOTES = REPO / "platform/playbooks/tasks/release-notes-body.yml"
NOTES_PATH = "platform/playbooks/tasks/release-notes-body.yml"
GATEWAY_SUFFIX = "/v1/chat/completions"
FILE_WRITE_MODULES = {
    "copy", "file", "tempfile", "lineinfile", "template", "blockinfile", "replace", "assemble", "get_url", "unarchive",
}
# Results that hold the gateway key or a gateway reply. A visible task names none of them.
PROTECTED_NOTES_NAMES = (
    "_rn_calls",
    "_rn_agw",
    "_rn_agw_fields",
    "_rn_key",
    "_rn_overview",
    "_rn_overview_check",
    "_rn_overview_parsed",
    "_rn_category_check",
    "_rn_category_parsed",
)
NOTES_LEAVES = _walk(yaml.safe_load(NOTES.read_text()), "tasks", False, {}, [])


def _pick(leaves, pred, what):
    hits = [leaf for leaf in leaves if pred(leaf)]
    assert len(hits) == 1, f"{what}: expected one task, found {len(hits)}"
    return hits[0]


def _notes(pred, what):
    return _pick(NOTES_LEAVES, pred, what)


def _holds(leaf, fact):
    return leaf["task"].get("register") == fact or _sets(leaf, fact)


def _gateway_leaf():
    """The category call: the gateway call that carries the loop over the three categories."""
    return _notes(
        lambda lf: _module(lf["task"])[0] == "uri"
        and str(_module(lf["task"])[1].get("url", "")).endswith(GATEWAY_SUFFIX)
        and "loop" in lf["task"],
        "gateway",
    )


def _overview_leaf():
    """The overview call: the gateway call with no loop, one for the whole release."""
    return _notes(
        lambda lf: _module(lf["task"])[0] == "uri"
        and str(_module(lf["task"])[1].get("url", "")).endswith(GATEWAY_SUFFIX)
        and "loop" not in lf["task"],
        "overview",
    )


def _guarded_tasks(when_text):
    """The tasks of the nested block whose own when is when_text, inside the playbook's outer block."""
    outer = next(t for t in _play()["tasks"] if "block" in t)
    for task in outer["block"]:
        if "block" in task and str(task.get("when", "")) == when_text:
            return task["block"]
    raise AssertionError(when_text)


def _enclosing_whens(tasks, name, chain=()):
    """The block-level when values around the named task, outermost first; None when the task is not here."""
    for task in tasks:
        if any(k in task for k in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                found = _enclosing_whens(task.get(key) or [], name, chain + (str(task.get("when", "")),))
                if found is not None:
                    return found
        elif task.get("name") == name:
            return chain
    return None


def test_the_normal_run_sits_under_the_no_refresh_guard_and_the_refresh_run_under_the_refresh_guard():
    for name in (
        "Build the planner command (the fixed part)",
        "Push the planned tags (visible: the token is only in the credential file)",
        "Create the GitHub Release for the planned tag (only when missing, never in check mode)",
    ):
        assert "_rv_refresh | length == 0" in _enclosing_whens(_play()["tasks"], name), name
    for name in (
        "Look up the GitHub Release for the refresh tag (404 means there is none)",
        "Take the categorised notes for the refresh tag",
        "Rewrite the Release body (hidden: the request carries the token; never in check mode)",
    ):
        assert "_rv_refresh | length > 0" in _enclosing_whens(_play()["tasks"], name), name


def test_the_refresh_run_plans_tags_pushes_and_creates_nothing():
    for leaf in _walk(_guarded_tasks("_rv_refresh | length > 0"), "block", False, {}, []):
        module, args = _module(leaf["task"])
        assert module != "copy", leaf["name"]
        assert "_rv_plan" not in yaml.safe_dump(leaf["task"]), leaf["name"]
        assert not (module == "uri" and args.get("method") == "POST"), leaf["name"]
        if module == "command":
            assert "push" not in str(args.get("argv", "")), leaf["name"]
            assert "release-tags" not in str(args.get("argv", "")), leaf["name"]


def test_the_refresh_reads_the_release_by_tag_and_patches_it_by_id_hidden_and_never_in_check_mode():
    leaves = _walk(_guarded_tasks("_rv_refresh | length > 0"), "block", False, {}, [])
    get = _pick(
        leaves,
        lambda lf: _module(lf["task"])[0] == "uri" and _module(lf["task"])[1].get("method") == "GET",
        "refresh GET",
    )
    assert get["no_log"] is True
    assert get["task"]["ansible.builtin.uri"]["url"] == (
        "https://api.github.com/repos/{{ _rv_org }}/{{ _rv_repository }}/releases/tags/{{ _rv_refresh }}"
    )
    assert get["task"]["ansible.builtin.uri"]["status_code"] == [200, 404]
    assert get["task"].get("check_mode") is False
    patch = _pick(
        leaves,
        lambda lf: _module(lf["task"])[0] == "uri" and _module(lf["task"])[1].get("method") == "PATCH",
        "PATCH",
    )
    assert patch["no_log"] is True
    assert patch["task"]["ansible.builtin.uri"]["url"] == (
        "https://api.github.com/repos/{{ _rv_org }}/{{ _rv_repository }}/releases/{{ _rv_refresh_id }}"
    )
    assert patch["task"]["ansible.builtin.uri"]["body"] == {"body": "{{ _rn_body }}"}
    assert patch["task"]["ansible.builtin.uri"]["status_code"] == [200]
    assert patch["task"].get("when") == "not ansible_check_mode"


def test_the_refresh_refuses_a_tag_with_no_release_by_name_before_any_write():
    leaves = _walk(_guarded_tasks("_rv_refresh | length > 0"), "block", False, {}, [])
    names = [leaf["name"] for leaf in leaves]
    refusal = _pick(leaves, lambda lf: _module(lf["task"])[0] == "assert", "refresh refusal")
    assert refusal["task"]["ansible.builtin.assert"]["that"] == ["_rv_refresh_state == 'exists'"]
    assert "nothing was rewritten" in refusal["task"]["ansible.builtin.assert"]["fail_msg"]
    patch = next(
        lf["name"]
        for lf in leaves
        if _module(lf["task"])[0] == "uri" and _module(lf["task"])[1].get("method") == "PATCH"
    )
    assert names.index(refusal["name"]) < names.index(patch)


def test_the_refresh_tag_is_checked_by_form_and_refused_beside_a_major_before_any_network_call():
    tasks = _play()["tasks"]
    form = next(t for t in tasks if "release_refresh_tag must be empty" in str(t.get("ansible.builtin.assert", {})))
    assert form["ansible.builtin.assert"]["that"] == [
        "_rv_refresh | length == 0 or _rv_refresh is match('^v[0-9]+[.][0-9]+[.][0-9]+$')"
    ]
    beside = next(t for t in tasks if "cannot both be set" in str(t.get("ansible.builtin.assert", {})))
    assert beside["ansible.builtin.assert"]["that"] == ["_rv_major | length == 0 or _rv_refresh | length == 0"]
    transport = next(t for t in tasks if "include_tasks" in str(t) and "assert-bao-transport" in str(t))
    assert tasks.index(form) < tasks.index(beside) < tasks.index(transport)


def test_the_notes_are_taken_only_for_a_missing_release_and_before_the_post_that_uses_them():
    # The playbook includes the file relative to itself, so the argument is not the repository path.
    includes = [
        lf
        for lf in LEAVES
        if _module(lf["task"])[0] == "include_tasks" and _module(lf["task"])[1] == "tasks/release-notes-body.yml"
    ]
    assert len(includes) == 2, "one include in the normal run, one in the refresh run"
    normal = _pick(
        includes,
        lambda lf: "_rv_refresh | length == 0" in _enclosing_whens(_play()["tasks"], lf["name"]),
        "normal include",
    )
    assert normal["task"].get("when") == "_rv_plan.release is not none and _rv_release_state == 'missing'"
    assert normal["task"]["vars"] == {"_rn_tag": "{{ _rv_plan.release.tag }}"}
    post = _one(
        lambda lf: _module(lf["task"])[0] == "uri" and _module(lf["task"])[1].get("method") == "POST"
        and str(_module(lf["task"])[1].get("url", "")).endswith("/releases"),
        "POST",
    )
    assert _index(normal) < _index(post)
    assert post["task"]["ansible.builtin.uri"]["body"]["body"] == "{{ _rn_body }}"
    assert "_rv_release_state == 'missing'" in post["task"]["when"]
    refresh = _pick(includes, lambda lf: lf is not normal, "refresh include")
    assert refresh["task"]["vars"] == {"_rn_tag": "{{ _rv_refresh }}"}


def test_the_notes_model_is_a_literal_value_that_no_survey_or_extra_var_can_change():
    assert _values()["_rv_notes_model"] == "qwen3.8-flash-next"
    assert "release_notes_model" not in yaml.safe_dump(_play())
    assert all(
        "release_notes_model" not in str(t.get("survey_vars", "")) for t in _templates()
    )


def test_the_refresh_template_is_declared_with_its_two_inputs_no_schedule_and_sits_after_the_major():
    templates = _templates()
    entries = [t for t in templates if t.get("name") == "Refresh Release Notes (Dev)"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["playbook"] == PLAYBOOK_PATH
    assert entry["repository"] == "agent-cloud dev"
    assert "schedule" not in entry
    assert entry["survey_vars"] == [
        {
            "name": "release_refresh_tag",
            "title": "Release to refresh",
            "description": (
                "An existing release tag, for example v0.14.0. Rewrites that GitHub Release's notes in the "
                "categorised format; check mode prints the new notes and changes nothing."
            ),
            "type": "string",
            "required": True,
        },
        {
            "name": "release_notes_from_task",
            "title": "Publish a reviewed check run (task id)",
            "description": (
                "Optional. The id of a successful check-mode run of this template for the same tag; its printed "
                "notes are published exactly, instead of generating new ones."
            ),
            "type": "integer",
            "required": False,
        },
    ]
    names = [t["name"] for t in templates]
    assert names.index("Refresh Release Notes (Dev)") == names.index("Release Major Version (Dev)") + 1


def test_only_the_version_template_is_scheduled_among_the_release_templates():
    entries = [t for t in _templates() if t.get("playbook") == PLAYBOOK_PATH]
    assert [t["name"] for t in entries if "schedule" in t] == ["Release Version (Dev)"]


def test_the_gateway_call_is_hidden_and_checked_as_a_read_on_a_bounded_timeout():
    args = _gateway_leaf()["task"]["ansible.builtin.uri"]
    gw = _gateway_leaf()
    assert gw["no_log"] is True
    assert args["method"] == "POST"
    assert args["status_code"] == [200]
    assert args["headers"]["Authorization"] == "Bearer {{ _rn_key }}"
    assert args["timeout"] <= 110
    assert gw["task"]["failed_when"] is False
    assert gw["task"]["changed_when"] is False
    assert gw["task"]["check_mode"] is False


def test_the_gateway_request_is_the_schema_constrained_thinking_off_shape_the_gateway_verifies():
    body = _gateway_leaf()["task"]["ansible.builtin.uri"]["body"]
    assert body["model"] == "{{ _rv_notes_model }}"
    assert body["temperature"] == 0
    assert body["max_tokens"] <= 600
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["required"] == ["summary"]
    assert schema["additionalProperties"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "No lists" in body["messages"][0]["content"]


def test_both_release_notes_system_prompts_tell_the_model_the_renderer_allowlist():
    # The renderer drops a summary that holds a slash, an ampersand, a hash sign, a number or a GH- reference, so the
    # model is told each one. Without the sentence, common prose (CI/CD, R&D, (#10)) costs the whole summary.
    for leaf in (_gateway_leaf(), _overview_leaf()):
        system = leaf["task"]["ansible.builtin.uri"]["body"]["messages"][0]["content"].lower()  # sentence-initial Never
        assert "never use slashes" in system
        assert "hash signs" in system and "the letters gh followed by a dash and a number" in system


def test_the_gateway_loops_over_a_literal_category_list_gated_on_key_route_and_pull_requests():
    gw = _gateway_leaf()["task"]
    assert [item["name"] for item in gw["loop"]] == ["features", "fixes", "documentation"]
    assert all("{{" not in str(item) for item in gw["loop"])
    for part in ("_rn_has_key", "_rn_host | length > 0", "_rn_sections.counts[item.name] | int > 0"):
        assert part in gw["when"], part


def test_the_key_is_read_hidden_and_every_fact_that_holds_the_key_or_a_reply_is_hidden():
    read = _notes(
        lambda lf: _module(lf["task"])[0] == "uri"
        and "X-Vault-Token" in str(_module(lf["task"])[1].get("headers", {})),
        "OpenBao read",
    )
    assert read["no_log"] is True
    assert read["task"]["ansible.builtin.uri"]["url"] == "{{ _bao_url }}/v1/secret/data/services/agentgateway"
    assert read["task"]["ansible.builtin.uri"]["status_code"] == [200, 404]
    assert read["task"].get("check_mode") is False and read["task"].get("changed_when") is False
    key = _notes(lambda lf: _sets(lf, "_rn_key"), "key fact")
    assert "client_release-notes" in str(key["task"]["ansible.builtin.set_fact"]["_rn_key"])
    for fact in (
        "_rn_agw",
        "_rn_agw_fields",
        "_rn_key",
        "_rn_has_key",
        "_rn_calls",
        "_rn_summaries",
        "_rn_statuses",
        "_rn_overview",
        "_rn_overview_check",
        "_rn_overview_parsed",
        "_rn_overview_summary",
        "_rn_overview_highlights",
        "_rn_category_check",
        "_rn_category_parsed",
    ):
        leaf = _notes(lambda lf, f=fact: _holds(lf, f), fact)
        assert leaf["no_log"] is True, fact


def test_no_visible_task_names_a_protected_key_or_reply_result():
    for leaf in NOTES_LEAVES:
        if leaf["no_log"] is True:
            continue
        text = yaml.safe_dump(leaf["task"])
        for name in PROTECTED_NOTES_NAMES:
            assert not re.search(rf"\b{name}\b", text), f"{leaf['name']} names {name}"


def _category_program():
    check = _notes(lambda lf: lf["task"].get("register") == "_rn_category_check", "category check")
    return check, check["task"]["ansible.builtin.command"]


def test_the_category_summaries_are_decoded_by_a_hidden_program_not_a_regex():
    check, command = _category_program()
    assert check["no_log"] is True
    argv = command["argv"]
    assert argv[:3] == ["python3", "-I", "-c"]
    # Argv is templated by Ansible: the program must carry no Jinja markers.
    assert "{{" not in argv[3] and "{%" not in argv[3] and "{#" not in argv[3]
    for index in range(3):
        assert f"_rn_calls.results[{index}].json" in command["stdin"], index
    assert command["stdin"].rstrip().endswith("| to_json }}")
    assert check["task"].get("check_mode") is False and check["task"].get("changed_when") is False
    assert check["task"].get("failed_when") is False
    parsed = _notes(lambda lf: _sets(lf, "_rn_category_parsed"), "category parsed")
    assert parsed["no_log"] is True
    assert parsed["task"]["ansible.builtin.set_fact"]["_rn_category_parsed"] == (
        "{{ (_rn_category_check.stdout | default('{}', true)) | from_json }}"
    )
    summaries = _notes(lambda lf: _sets(lf, "_rn_summaries"), "summaries")
    text = summaries["task"]["ansible.builtin.set_fact"]["_rn_summaries"]
    assert "regex_findall" not in text and "regex_search" not in text and "from_json" not in text
    for key in ("features", "fixes", "documentation"):
        assert f"'{key}': _rn_category_parsed.{key} | default('', true)" in text, key
    assert "dict2items" in text and "items2dict" in text


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        # An escaped quote is decoded, so the renderer sees the quote and not a backslash.
        (['{"summary": "Adds a \\"quoted\\" change."}', "", ""], {"features": 'Adds a "quoted" change.'}),
        (
            ['{"summary": "A."}', '{"summary": "B."}', '{"summary": "C."}'],
            {"features": "A.", "fixes": "B.", "documentation": "C."},
        ),
        (['{"summary": "trunc', "not json", '{"summary": ""}'], {}),  # truncated, malformed, empty: all left out
        (['{"summary": 7}', '["summary"]', '{"other": "x"}'], {}),  # wrong types and shapes: left out
        (["", '{"summary": "Fix."}', ""], {"fixes": "Fix."}),  # a skipped call leaves only its category out
    ],
)
def test_the_category_program_decodes_valid_summaries_and_leaves_the_rest_out(contents, expected):
    _check, command = _category_program()
    result = subprocess.run(
        [sys.executable, "-I", "-c", command["argv"][3]],
        input=json.dumps(contents),
        capture_output=True,
        text=True,
        check=True,
        timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS,
    )
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("stdin", ["", "not json", '{"features": "x"}'])
def test_the_category_program_prints_an_empty_object_for_input_that_is_not_a_list(stdin):
    _check, command = _category_program()
    result = subprocess.run(
        [sys.executable, "-I", "-c", command["argv"][3]], input=stdin, capture_output=True, text=True, check=True,
        timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS
    )
    assert json.loads(result.stdout) == {}


def test_the_summary_state_names_each_outcome_and_is_visible():
    state = _notes(lambda lf: _sets(lf, "_rn_summary_state"), "state")
    assert state["no_log"] is not True
    text = str(state["task"]["ansible.builtin.set_fact"])
    for word in (
        "none (no pull requests)",
        "summarised",
        "skipped: no key",
        "skipped: no route",
        "skipped: no summary in the reply",
        "skipped: no answer from the gateway",
        "skipped: gateway HTTP",
    ):
        assert word in text, word


def test_the_render_takes_the_summaries_on_stdin_visibly_and_is_checked_by_its_exit_code():
    render = _notes(
        lambda lf: _module(lf["task"])[0] == "command" and "--render" in (_argv(lf["task"]) or []),
        "render",
    )
    assert render["no_log"] is not True
    command = render["task"]["ansible.builtin.command"]
    assert command["stdin"] == "{{ _rn_summaries | to_json }}"
    assert "--tag" in command["argv"] and "{{ _rn_tag }}" in command["argv"]
    assert "--sections" not in command["argv"]
    assert render["task"]["failed_when"] is False
    assert render["task"]["changed_when"] is False
    assert render["task"]["check_mode"] is False
    rc = _notes(
        lambda lf: _module(lf["task"])[0] == "assert"
        and lf["task"]["ansible.builtin.assert"].get("that") == ["_rn_render.rc == 0"],
        "rc",
    )
    assert "_rn_render.stderr" in rc["task"]["ansible.builtin.assert"]["fail_msg"]
    body = _notes(lambda lf: _sets(lf, "_rn_body"), "body fact")
    assert body["task"]["ansible.builtin.set_fact"]["_rn_body"] == "{{ _rn_render.stdout }}"


def test_the_body_and_the_state_are_printed_visibly_in_every_mode():
    body = _notes(
        lambda lf: _module(lf["task"])[0] == "debug" and _module(lf["task"])[1].get("msg") == "{{ _rn_body }}",
        "body debug",
    )
    assert body["no_log"] is not True
    state = _notes(
        lambda lf: _module(lf["task"])[0] == "debug" and "_rn_summary_state" in str(_module(lf["task"])[1].get("msg")),
        "state debug",
    )
    assert state["no_log"] is not True
    msg = str(state["task"]["ansible.builtin.debug"]["msg"])
    for category in ("features", "fixes", "documentation"):
        assert f"_rn_summary_state.{category}" in msg, category


def test_every_command_in_the_notes_file_is_argv_read_only_and_every_semver_call_carries_the_git_variables():
    commands = [lf for lf in NOTES_LEAVES if _module(lf["task"])[0] == "command"]
    assert len(commands) == 4, "the sections read, the category decode, the overview check and the render"
    for lf in commands:
        assert isinstance(_argv(lf["task"]), list), lf["name"]
        assert lf["task"].get("check_mode") is False and lf["task"].get("changed_when") is False, lf["name"]
        if "semver.py" in str(_argv(lf["task"])):
            for key, value in GIT_ENV.items():
                assert lf["env"].get(key) == value, f"{lf['name']}: {key}"
    # The overview check runs no git and reads no repository: it is python3 -I -c with the reply on stdin.
    check = _notes(lambda lf: lf["task"].get("register") == "_rn_overview_check", "overview check")
    assert _argv(check["task"])[:3] == ["python3", "-I", "-c"]
    decode, _ = _category_program()
    assert _argv(decode["task"])[:3] == ["python3", "-I", "-c"]


def test_the_notes_file_holds_no_forced_write_and_no_shell_and_refuses_a_bad_tag_first():
    for lf in NOTES_LEAVES:
        module, _ = _module(lf["task"])
        assert module not in FILE_WRITE_MODULES, lf["name"]
        assert module not in ("shell", "raw", "script"), lf["name"]
    first = NOTES_LEAVES[0]
    assert _module(first["task"])[0] == "assert"
    assert first["task"]["ansible.builtin.assert"]["that"] == ["_rn_tag is match('^v[0-9]+[.][0-9]+[.][0-9]+$')"]


def test_every_register_and_fact_in_the_notes_file_is_underscore_prefixed():
    for lf in NOTES_LEAVES:
        reg = lf["task"].get("register")
        if reg:
            assert reg.startswith("_rn_"), lf["name"]
        module, args = _module(lf["task"])
        if module == "set_fact":
            assert all(k.startswith("_rn_") for k in args), lf["name"]


def test_the_route_is_the_caddy_hosts_declared_inference_address_and_nothing_else():
    host = _notes(lambda lf: _sets(lf, "_rn_host"), "host")["task"]["ansible.builtin.set_fact"]["_rn_host"]
    assert "groups['caddy_svc'][0]" in host
    assert ".inference_route_address" in host
    assert host.count("hostvars") == 1


# --- The overview (scope addition, plan 20): one call for the whole release, its shape checked, its parts kept. ---


def test_the_overview_call_is_hidden_checked_as_a_read_and_bounded():
    overview = _overview_leaf()
    assert overview["no_log"] is True
    args = overview["task"]["ansible.builtin.uri"]
    assert args["method"] == "POST"
    assert args["status_code"] == [200]
    assert args["timeout"] <= 110
    assert args["headers"]["Authorization"] == "Bearer {{ _rn_key }}"
    assert overview["task"]["failed_when"] is False
    assert overview["task"]["changed_when"] is False
    assert overview["task"]["check_mode"] is False


def test_the_overview_request_is_the_strict_schema_with_at_most_five_highlights_and_the_same_model_and_thinking_off():
    body = _overview_leaf()["task"]["ansible.builtin.uri"]["body"]
    assert body["model"] == "{{ _rv_notes_model }}"
    assert body["temperature"] == 0
    assert body["max_tokens"] <= 800
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    response_format = body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "release_overview"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["required"] == ["summary", "highlights"]
    assert schema["additionalProperties"] is False
    highlights = schema["properties"]["highlights"]
    assert highlights["type"] == "array"
    assert highlights["maxItems"] == 5
    item = highlights["items"]
    assert item["properties"] == {"number": {"type": "integer"}, "text": {"type": "string"}}
    assert item["required"] == ["number", "text"]
    assert item["additionalProperties"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "Use only the pull requests listed" in body["messages"][0]["content"]
    assert body["messages"][1]["content"] == "{{ _rn_overview_list }}"


def test_the_overview_runs_only_with_a_key_a_route_and_at_least_one_pull_request():
    when = _overview_leaf()["task"]["when"]
    for part in ("_rn_has_key", "_rn_host | length > 0", "_rn_pr_total | int > 0"):
        assert part in when, part


def test_the_overview_list_is_built_from_every_category_in_order_and_holds_no_secret():
    listing = _notes(lambda lf: _sets(lf, "_rn_overview_list"), "list")
    assert listing["no_log"] is not True
    text = listing["task"]["ansible.builtin.set_fact"]["_rn_overview_list"]
    for name in ("features", "fixes", "documentation", "other"):
        assert "_rn_sections.sections[name]" in text, name
    assert "#{{ pr.number }} {{ pr.title }}" in text


def test_the_overview_reply_is_checked_by_a_hidden_program_that_prints_only_the_valid_parts():
    check = _notes(lambda lf: lf["task"].get("register") == "_rn_overview_check", "overview check")
    assert check["no_log"] is True
    command = check["task"]["ansible.builtin.command"]
    argv = command["argv"]
    assert argv[:3] == ["python3", "-I", "-c"]
    code = argv[3]
    # Argv is templated by Ansible: the program must carry no Jinja markers.
    assert "{{" not in code and "{%" not in code and "{#" not in code
    for needle in (
        '"summary"',
        '"highlights"',
        "isinstance(summary, str)",
        "0 < len(items) <= 5",
        'set(i) == {"number", "text"}',
        'type(i["number"]) is int',
        'isinstance(i["text"], str)',
        "except ValueError",
        "print(json.dumps(out))",
    ):
        assert needle in code, needle
    assert command["stdin"].startswith("{{ ((((_rn_overview.json | default({}, true))")
    assert command["stdin"].endswith(".content | default('', true) }}")
    assert check["task"].get("check_mode") is False and check["task"].get("changed_when") is False
    assert check["task"].get("failed_when") is False


def test_the_overview_parts_are_taken_hidden_from_the_program_output_and_fall_back_to_empty():
    parsed = _notes(lambda lf: _sets(lf, "_rn_overview_parsed"), "parsed")
    assert parsed["no_log"] is True
    assert parsed["task"]["ansible.builtin.set_fact"]["_rn_overview_parsed"] == (
        "{{ (_rn_overview_check.stdout | default('{}', true)) | from_json }}"
    )
    parts = _notes(lambda lf: _sets(lf, "_rn_overview_summary"), "parts")
    assert parts["no_log"] is True
    facts = parts["task"]["ansible.builtin.set_fact"]
    assert facts["_rn_overview_summary"] == "{{ _rn_overview_parsed.summary | default('', true) }}"
    assert facts["_rn_overview_highlights"] == "{{ _rn_overview_parsed.highlights | default('', true) }}"


def test_the_overview_parts_reach_the_summaries_the_render_and_the_state():
    summaries = _notes(lambda lf: _sets(lf, "_rn_summaries"), "summaries")["task"]["ansible.builtin.set_fact"]
    text = summaries["_rn_summaries"]
    assert "'overview': _rn_overview_summary" in text
    assert "'highlights': _rn_overview_highlights" in text
    statuses = _notes(lambda lf: _sets(lf, "_rn_statuses"), "statuses")["task"]["ansible.builtin.set_fact"]
    assert "_rn_overview.status" in statuses["_rn_statuses"]
    state = _notes(lambda lf: _sets(lf, "_rn_summary_state"), "state")["task"]["ansible.builtin.set_fact"]
    assert "overview" in state["_rn_summary_state"]
    assert "_rn_statuses[3]" in str(state["_rn_summary_state"]["overview"])
    debug = _notes(
        lambda lf: _module(lf["task"])[0] == "debug" and "_rn_summary_state.overview" in str(_module(lf["task"])[1]),
        "state debug",
    )
    assert "overview {{ _rn_summary_state.overview }}" in debug["task"]["ansible.builtin.debug"]["msg"]


def test_a_run_whose_inventory_did_not_parse_is_refused_before_openbao_is_reached():
    # Task 14770 (2026-11-10) lost its inventory to the shared-file race: every group read empty, the notes
    # took "no route" and rendered the grouped lists only, a body a real run would have published. The shared
    # guard refuses that run instead, before OpenBao is reached; the next scheduled run retries.
    guard = _one(
        lambda leaf: _module(leaf["task"]) == ("include_tasks", "tasks/require-parsed-inventory.yml"),
        "require-parsed-inventory include",
    )
    assert guard["task"].get("when") is None, "the guard is unconditional"
    assert _index(guard) < _index(_registered("_rv_auth"))  # the AppRole login, found by its register
    refresh_check = _one(lambda leaf: leaf["name"].startswith("Refuse a refresh beside a major"), "refresh/major check")
    assert _index(refresh_check) < _index(guard)


# --- Publish a reviewed body (release_notes_from_task). The optional input, its two asserts, the refresh block's two
# includes, the publish task file and the record the notes task writes. Nothing here starts a run. --------------------

FROM_TASK = REPO / "platform/playbooks/tasks/release-notes-from-task.yml"
# The include argument is relative to the playbook, as tasks/release-notes-body.yml is.
FROM_TASK_INCLUDE = "tasks/release-notes-from-task.yml"
HELPER = REPO / "platform/playbooks/files/release_notes_from_task.py"
HELPER_ARGV = ["python3", "-I", "{{ playbook_dir }}/files/release_notes_from_task.py"]
FROM_TASK_LEAVES = _walk(yaml.safe_load(FROM_TASK.read_text()), "tasks", False, {}, [])
# Results that hold the Semaphore token, a Semaphore reply or the body. A visible task in the publish file names none.
FROM_TASK_PROTECTED = (
    "_rnt_semaphore_read",
    "_rnt_semaphore",
    "_rnt_task_read",
    "_rnt_output_read",
    "_rnt_template_read",
    "_rnt_check",
    "_rnt_result",
    "_rn_body",
)
PUBLISH_MODULES = {"uri", "command", "set_fact", "assert", "debug"}


def _from_task(pred, what):
    return _pick(FROM_TASK_LEAVES, pred, what)


def _from_task_fact(name):
    return _from_task(lambda lf: _sets(lf, name), name)


def test_the_publish_input_is_one_optional_integer_on_the_refresh_template_only():
    carrying = [t["name"] for t in _templates() if "release_notes_from_task" in str(t.get("survey_vars", ""))]
    assert carrying == ["Refresh Release Notes (Dev)"]


def test_the_publish_values_are_internal_read_once_and_the_project_defaults_to_the_instances_one():
    vals = _values()
    assert vals["_rv_from_task"] == "{{ release_notes_from_task | default('', true) | string | trim }}"
    assert vals["_rv_semaphore_project_id"] == "{{ release_semaphore_project_id | default(1) }}"
    code = yaml.safe_dump(_play())
    assert code.count("{{ release_notes_from_task") == 1
    assert code.count("{{ release_semaphore_project_id") == 1


def test_the_publish_id_is_checked_by_form_and_needs_a_refresh_tag_before_any_network_call():
    tasks = _play()["tasks"]
    number = next(
        t for t in tasks if "release_notes_from_task must be empty" in str(t.get("ansible.builtin.assert", {}))
    )
    pair = next(
        t
        for t in tasks
        if "release_notes_from_task needs release_refresh_tag" in str(t.get("ansible.builtin.assert", {}))
    )
    assert number["ansible.builtin.assert"]["that"] == [
        "_rv_from_task | length == 0 or _rv_from_task is match('^[1-9][0-9]*$')"
    ]
    assert pair["ansible.builtin.assert"]["that"] == ["_rv_from_task | length == 0 or _rv_refresh | length > 0"]
    beside = next(t for t in tasks if "cannot both be set" in str(t.get("ansible.builtin.assert", {})))
    guard = next(t for t in tasks if t.get("ansible.builtin.include_tasks") == "tasks/require-parsed-inventory.yml")
    login = next(t for t in tasks if t.get("register") == "_rv_auth")
    assert tasks.index(beside) < tasks.index(number) < tasks.index(pair) < tasks.index(guard) < tasks.index(login)


def test_the_refresh_block_generates_the_notes_or_publishes_the_reviewed_run_never_both():
    leaves = _walk(_guarded_tasks("_rv_refresh | length > 0"), "block", False, {}, [])
    generated = _pick(
        leaves,
        lambda lf: _module(lf["task"]) == ("include_tasks", "tasks/release-notes-body.yml"),
        "refresh body include",
    )
    assert generated["task"].get("when") == "_rv_from_task | length == 0"
    assert generated["task"]["vars"] == {"_rn_tag": "{{ _rv_refresh }}"}
    published = _pick(
        leaves,
        lambda lf: _module(lf["task"]) == ("include_tasks", FROM_TASK_INCLUDE),
        "publish include",
    )
    assert published["task"].get("when") == "_rv_from_task | length > 0"
    assert published["task"]["vars"] == {"_rnt_tag": "{{ _rv_refresh }}", "_rnt_task_id": "{{ _rv_from_task | int }}"}
    # Included nowhere else: a normal run never publishes a reviewed body.
    everywhere = _by_module("include_tasks", lambda args: args == FROM_TASK_INCLUDE)
    assert [lf["name"] for lf in everywhere] == [published["name"]]


def test_the_publish_file_is_read_only_and_every_request_and_the_helper_is_hidden_and_checked_as_a_read():
    modules = {_module(lf["task"])[0] for lf in FROM_TASK_LEAVES}
    assert modules <= PUBLISH_MODULES, modules
    for lf in FROM_TASK_LEAVES:
        module, args = _module(lf["task"])
        if module == "uri":
            assert args.get("method", "GET") == "GET", lf["name"]
            assert args["follow_redirects"] == "none", lf["name"]
        if module in ("uri", "command"):
            assert lf["no_log"] is True, lf["name"]
            assert lf["task"].get("check_mode") is False, lf["name"]
            assert lf["task"].get("changed_when") is False, lf["name"]
    reads = [lf for lf in FROM_TASK_LEAVES if _module(lf["task"])[0] in ("uri", "command")]
    assert len(reads) == 5, "the secret read, the task, its output, its template and the helper"


def test_the_semaphore_routes_are_the_three_gets_and_carry_the_bearer_token_in_headers_only():
    uris = [(lf["name"], _module(lf["task"])[1]) for lf in FROM_TASK_LEAVES if _module(lf["task"])[0] == "uri"]
    semaphore = [(name, args) for name, args in uris if "/api/project/" in args["url"]]
    assert [args["url"] for _, args in semaphore] == [
        "{{ _rnt_semaphore.url }}/api/project/{{ _rv_semaphore_project_id }}/tasks/{{ _rnt_task_id | int }}",
        "{{ _rnt_semaphore.url }}/api/project/{{ _rv_semaphore_project_id }}/tasks/{{ _rnt_task_id | int }}/output",
        "{{ _rnt_semaphore.url }}/api/project/{{ _rv_semaphore_project_id }}/templates/{{ _rnt_template_id | int }}",
    ]
    for name, args in semaphore:
        assert args["headers"] == {"Authorization": "Bearer {{ _rnt_semaphore.api_token }}"}, name
        assert args["unredirected_headers"] == ["Authorization"], name
        assert args["use_netrc"] is False and args["use_proxy"] is False, name
    bao = next(args for _, args in uris if args["url"] == "{{ _bao_url }}/v1/secret/data/services/semaphore")
    assert bao["headers"] == {"X-Vault-Token": "{{ _rv_auth.json.auth.client_token }}"}
    assert bao["unredirected_headers"] == ["X-Vault-Token"]
    for lf in FROM_TASK_LEAVES:
        if _module(lf["task"])[0] == "command":
            stdin = str(_module(lf["task"])[1].get("stdin", ""))
            assert "api_token" not in stdin and "X-Vault" not in stdin and "client_token" not in stdin, lf["name"]


def test_the_endpoint_is_refused_unless_https_and_named_by_requirement_not_by_value():
    check = _from_task(
        lambda lf: "(_rnt_semaphore.url | default('', true)) is match('^https://[^/]+$')"
        in str(lf["task"].get("ansible.builtin.assert", {}).get("that", "")),
        "endpoint assert",
    )
    assert check["no_log"] is True
    expected = "(_rnt_semaphore.api_token | default('', true)) | length > 0"
    assert check["task"]["ansible.builtin.assert"]["that"][1] == expected
    assert "secret/services/semaphore must hold an https url and an api_token" in (
        check["task"]["ansible.builtin.assert"]["fail_msg"]
    )


def test_the_helper_is_the_one_isolated_command_and_takes_the_run_on_stdin():
    assert HELPER.is_file()
    commands = [lf for lf in FROM_TASK_LEAVES if _module(lf["task"])[0] == "command"]
    assert len(commands) == 1
    args = _module(commands[0]["task"])[1]
    assert args["argv"] == HELPER_ARGV
    assert args.get("stdin_add_newline") is False
    for key in ("'task'", "'output_rows'", "'template'", "'expected_tag'", "'expected_task_id'"):
        assert key in args["stdin"], key
    assert "_rnt_task_id | int" in args["stdin"]


def test_a_refusal_names_one_rule_token_and_is_checked_before_any_body_is_taken():
    rule = _from_task_fact("_rnt_rule")
    assert rule["no_log"] is True
    text = rule["task"]["ansible.builtin.set_fact"]["_rnt_rule"]
    assert "select('match', '^release-notes-from-task: [a-z0-9_]{1,80}$')" in text
    assert "'no named rule'" in text
    refusal = _from_task(
        lambda lf: lf["task"].get("ansible.builtin.assert", {}).get("that") == ["_rnt_rc == 0"],
        "refusal",
    )
    assert refusal["no_log"] is not True
    assert "{{ _rnt_rule }}" in refusal["task"]["ansible.builtin.assert"]["fail_msg"]
    assert "Nothing was written" in refusal["task"]["ansible.builtin.assert"]["fail_msg"]
    body = _from_task_fact("_rn_body")
    assert FROM_TASK_LEAVES.index(rule) < FROM_TASK_LEAVES.index(refusal) < FROM_TASK_LEAVES.index(body)


def test_no_visible_task_in_the_publish_file_names_a_protected_result():
    for lf in FROM_TASK_LEAVES:
        if lf["no_log"] is True:
            continue
        text = yaml.safe_dump(lf["task"])
        for name in FROM_TASK_PROTECTED:
            assert not re.search(rf"\b{name}\b", text), f"{lf['name']} names {name}"


def test_the_body_is_taken_hidden_and_its_record_holds_only_the_task_tag_digest_and_size():
    body = _from_task_fact("_rn_body")
    assert body["no_log"] is True
    assert body["task"]["ansible.builtin.set_fact"]["_rn_body"] == "{{ _rnt_result.body }}"
    summary = _from_task_fact("_rnt_summary")
    assert summary["no_log"] is True
    assert sorted(summary["task"]["ansible.builtin.set_fact"]["_rnt_summary"]) == ["bytes", "sha256", "tag", "task_id"]
    digest = _from_task_fact("_rnt_body_sha256")
    assert digest["no_log"] is True
    assert digest["task"]["ansible.builtin.set_fact"]["_rnt_body_sha256"] == "{{ _rn_body | hash('sha256') }}"


def test_the_published_body_is_checked_against_its_record_for_digest_tag_and_task_id_before_publishing():
    check = _from_task(
        lambda lf: "_rnt_body_sha256 == _rnt_summary.sha256"
        in str(lf["task"].get("ansible.builtin.assert", {}).get("that", "")),
        "digest check",
    )
    assert check["task"]["ansible.builtin.assert"]["that"] == [
        "_rnt_body_sha256 == _rnt_summary.sha256",
        "_rnt_summary.tag == _rnt_tag",
        "_rnt_summary.task_id | int == _rnt_task_id | int",
    ]
    assert "nothing was published" in check["task"]["ansible.builtin.assert"]["fail_msg"]
    assert FROM_TASK_LEAVES.index(_from_task_fact("_rn_body")) < FROM_TASK_LEAVES.index(check)


def test_the_publish_file_checks_the_tag_and_task_id_form_first_and_names_only_allowed_facts_visibly():
    first = FROM_TASK_LEAVES[0]
    assert _module(first["task"])[0] == "assert"
    assert first["task"]["ansible.builtin.assert"]["that"] == [
        "_rnt_tag is match('^v[0-9]+[.][0-9]+[.][0-9]+$')",
        "_rnt_task_id | int > 0",
    ]
    for lf in FROM_TASK_LEAVES:
        reg = lf["task"].get("register")
        if reg:
            assert reg.startswith("_rnt_"), lf["name"]
        module, args = _module(lf["task"])
        if module == "set_fact":
            for key in args:
                assert key.startswith("_rnt_") or key == "_rn_body", f"{lf['name']}: {key}"


def test_the_notes_record_is_a_set_stats_of_tag_digest_and_body_written_after_the_body_is_taken():
    record = _notes(lambda lf: _module(lf["task"])[0] == "set_stats", "record")
    assert _module(record["task"])[1] == {
        "data": {
            "release_notes": {
                "tag": "{{ _rn_tag }}",
                "sha256": "{{ _rn_body | hash('sha256') }}",
                "body": "{{ _rn_body }}",
            }
        },
        "per_host": False,
        "aggregate": False,
    }
    assert record["no_log"] is not True
    body_fact = _notes(lambda lf: _sets(lf, "_rn_body"), "body fact")
    assert NOTES_LEAVES.index(body_fact) < NOTES_LEAVES.index(record)


def test_the_notes_sha256_is_printed_visibly_so_a_reviewer_can_match_a_later_publish():
    shown = _notes(
        lambda lf: _module(lf["task"])[0] == "debug" and "hash('sha256')" in str(_module(lf["task"])[1].get("msg", "")),
        "digest debug",
    )
    assert shown["no_log"] is not True
    assert "{{ _rn_tag }}" in shown["task"]["ansible.builtin.debug"]["msg"]
