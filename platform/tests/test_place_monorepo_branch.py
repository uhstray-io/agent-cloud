"""A placement on a non-local run must name its branch, in the play's vars or on the include.

tasks/place-monorepo.yml asserts `_monorepo_dir`, `_branch` (unless local_mode) and
`monorepo_repo` (unless local_mode) at place-monorepo.yml:23-29. deploy-console.yml included the
placement unconditionally and defined `_monorepo_dir` and `_deploy_dir` but no `_branch`, so
Semaphore task 9848 (Deploy console (Dev), check mode) failed on the console host with
"Missing required variables for place-monorepo.yml". Local-dev never reached that assert,
because local_mode is set there, so no local run could catch it.

The guard reads every top-level playbook. A placement include is local-only when one of its
`when` items is a positive local_mode term (`local_mode | default(false) | bool`). `not
local_mode`, a disjunction (`local_mode or ...`) and anything else are not local-only. A
placement that is not local-only must name its branch, in its play's vars or in the include's
own `vars:`. The caller chooses the branch: the shared task gives no default, so a forgotten
choice cannot be masked. The include-level form is deliberate: deploy-o11y-journal-collector.yml
pins `expected_repository_sha` per include (:56-59 and :361-364). Include forms read: the plain
string, the `{file: ...}` dict, and any path ending in tasks/place-monorepo.yml (so
`{{ playbook_dir }}/tasks/...` counts). A playbook whose text names the placement but yields no
placement row fails, so an unrecognised form cannot pass silently. Block-level `when` and `vars`,
and a branch set by `set_fact`, are not read: the guard is conservative there and may flag a
correct play. A positive `local_mode` term followed by a comparison (`local_mode | bool == false`)
would pass; no playbook uses that form. LOCAL_ONLY lists the plays that place only in
local mode, because the play refuses any other run before the include.
"""

import copy
import re

import playbook_yaml
import pytest

PLAYBOOKS_DIR = playbook_yaml.REPO / "platform/playbooks"
PLAYBOOKS = sorted(PLAYBOOKS_DIR.glob("*.yml"))
PLACEMENT = "tasks/place-monorepo.yml"
INCLUDE_KEYS = ("include_tasks", "ansible.builtin.include_tasks", "import_tasks", "ansible.builtin.import_tasks")
PLAY_TASK_KEYS = ("pre_tasks", "tasks", "post_tasks", "handlers")
# A positive local_mode term opens with local_mode, optionally parenthesised. `not local_mode` does not match.
LOCAL_MODE_TERM = re.compile(r"^\(?\s*local_mode\b")
LOCAL_ONLY = {
    "deploy-skynet.yml": "the play refuses any run outside local_mode before the placement include "
                         "(the operator-token stopgap assert, deploy-skynet.yml:138-141; include at :166)",
}
PLACING = [playbook for playbook in PLAYBOOKS if playbook.name not in LOCAL_ONLY]


def _positive_local_mode(condition):
    """True for one condition that is a positive local_mode term. An `or` anywhere disqualifies
    it: `local_mode or x` holds outside local mode."""
    if not isinstance(condition, str):
        return False
    text = condition.strip()
    return bool(LOCAL_MODE_TERM.match(text)) and not re.search(r"\bor\b", text)


def _conditions(value):
    return value if isinstance(value, list) else [value]


def _local_mode_gated(when):
    """Items in a `when` list are ANDed, so one positive local_mode term gates the include."""
    return any(_positive_local_mode(condition) for condition in _conditions(when))


def _is_placement(task):
    for key in INCLUDE_KEYS:
        target = task.get(key)
        if isinstance(target, dict):
            target = target.get("file")
        if isinstance(target, str) and target.endswith(PLACEMENT):
            return True
    return False


def _play_tasks(play):
    return [task for key in PLAY_TASK_KEYS for task in playbook_yaml.tasks(play.get(key))]


def _placements_in(doc):
    """One (play name, unguarded, unnamed) row per play that includes the placement task.

    unguarded: some include is not gated by a positive local_mode term.
    unnamed:   some unguarded include names no branch, in its play's vars or its own vars.
    """
    rows = []
    for index, play in enumerate(doc or []):
        if not isinstance(play, dict) or "hosts" not in play:
            continue
        includes = [task for task in _play_tasks(play) if _is_placement(task)]
        if not includes:
            continue
        play_names_branch = "_branch" in (play.get("vars") or {})
        unguarded = [task for task in includes if not _local_mode_gated(task.get("when"))]
        rows.append((
            play.get("name") or f"play {index}",
            bool(unguarded),
            any(not play_names_branch and "_branch" not in (task.get("vars") or {}) for task in unguarded),
        ))
    return rows


def _placements(path):
    return _placements_in(playbook_yaml.load(path))


def _unnamed_in(doc):
    return [name for name, _, unnamed in _placements_in(doc) if unnamed]


def _shape(name):
    return [(unguarded, unnamed) for _, unguarded, unnamed in _placements(PLAYBOOKS_DIR / name)]


def _asserts_local_mode(task):
    module = task.get("ansible.builtin.assert", task.get("assert"))
    return isinstance(module, dict) and any(_positive_local_mode(c) for c in _conditions(module.get("that")))


def _local_mode_asserted_before_placement(doc):
    """True when the document places the monorepo and every placing play asserts a positive
    local_mode term before its first placement include."""
    placing = False
    for play in doc or []:
        if not isinstance(play, dict) or "hosts" not in play:
            continue
        asserted = False
        for task in _play_tasks(play):
            if _is_placement(task):
                placing = True
                if not asserted:
                    return False
                break
            asserted = asserted or _asserts_local_mode(task)
    return placing


def _unscanned_mentions(paths):
    """Playbooks whose raw text names the placement but yield no placement row: an include form
    the scanner does not read."""
    return [path.name for path in paths if "place-monorepo.yml" in path.read_text() and not _placements(path)]


def test_the_scanner_reads_the_placements_it_guards():
    # A renamed key or a new include form would make the guard pass for every file. These pins
    # keep it honest against known playbooks: tududi names its branch in play vars, skynet is
    # unguarded and unnamed (local-only by allowlist), the o11y playbooks gate placement on
    # local_mode, the journal collector names its branch on each include, console has one placement.
    assert _shape("deploy-tududi.yml") == [(True, False)]
    assert _shape("deploy-skynet.yml") == [(True, True)]
    assert _shape("drill-o11y-alert-canary.yml") == [(False, False)]
    assert _shape("restore-o11y-alert-baseline.yml") == [(False, False)]
    assert _shape("deploy-o11y-journal-collector.yml") == [(True, False), (True, False)]
    assert [unguarded for unguarded, _ in _shape("deploy-console.yml")] == [True]


def test_every_playbook_that_names_the_placement_is_scanned():
    assert _unscanned_mentions(PLAYBOOKS) == [], (
        "these playbooks name " + PLACEMENT + " but the guard reads no placement from them: an "
        "include form it does not recognise"
    )


@pytest.mark.parametrize("playbook", PLACING, ids=lambda p: p.name)
def test_an_unguarded_placement_names_its_branch(playbook):
    assert _unnamed_in(playbook_yaml.load(playbook)) == [], (
        playbook.name + ": a play includes " + PLACEMENT + " without a positive local_mode term and "
        "names no `_branch`, in its vars or on the include. place-monorepo.yml:23-29 asserts it "
        "(task 9848). Add `_branch: \"{{ service_branch | default('main') }}\"` to that play's vars "
        "(or the caller's chosen branch on the include), or list the play in LOCAL_ONLY with the "
        "reason it only ever places locally."
    )


def test_the_local_only_allowlist_names_only_live_exemptions():
    for name, reason in LOCAL_ONLY.items():
        path = PLAYBOOKS_DIR / name
        assert path.is_file(), name + " no longer exists: drop its LOCAL_ONLY entry"
        assert reason.strip(), name + ": LOCAL_ONLY needs the reason the placement is local-only"
        doc = copy.deepcopy(playbook_yaml.load(path))
        assert any(unnamed for _, _, unnamed in _placements_in(doc)), (
            name + " no longer places the monorepo without a local_mode term or a named _branch: "
            "drop its LOCAL_ONLY entry"
        )
        assert _local_mode_asserted_before_placement(doc), (
            name + ": LOCAL_ONLY needs a positive local_mode assert before the placement include in "
            "the same play (deploy-skynet.yml:138-141, include at :166)"
        )


# In-memory cases: each builds a play dict and feeds the same functions the file checks use.


def _doc(*tasks, play_vars=None):
    play = {"name": "play", "hosts": "all", "tasks": list(tasks)}
    if play_vars is not None:
        play["vars"] = play_vars
    return [play]


def _placement_task(**keys):
    return {"ansible.builtin.include_tasks": PLACEMENT, **keys}


@pytest.mark.parametrize("when", [
    "local_mode | default(false) | bool",
    "(local_mode | default(false) | bool)",
    "local_mode and not ansible_check_mode",
    ["ansible_check_mode", "local_mode | default(false) | bool"],
])
def test_a_positive_local_mode_term_is_local_only(when):
    assert _placements_in(_doc(_placement_task(when=when))) == [("play", False, False)]


@pytest.mark.parametrize("when", [
    "not (local_mode | default(false) | bool)",
    "not local_mode",
    "(not local_mode)",
    "local_mode | default(false) | bool or ansible_check_mode",
    ["ansible_check_mode", "not local_mode"],
    None,
])
def test_anything_else_is_not_local_only(when):
    assert _placements_in(_doc(_placement_task(when=when))) == [("play", True, True)]


def test_a_prod_only_condition_without_a_branch_is_flagged():
    # The substring rule read `local_mode` inside this condition as guarded.
    doc = _doc(_placement_task(when="not (local_mode | default(false) | bool)"))
    assert _unnamed_in(doc) == ["play"]


@pytest.mark.parametrize("include", [
    {"ansible.builtin.include_tasks": {"file": PLACEMENT}},
    {"include_tasks": "{{ playbook_dir }}/tasks/place-monorepo.yml"},
    {"ansible.builtin.import_tasks": PLACEMENT},
    {"import_tasks": "{{ playbook_dir }}/" + PLACEMENT},
])
def test_every_include_form_of_the_placement_is_read(include):
    assert _placements_in(_doc(include)) == [("play", True, True)]


def test_another_task_file_is_not_the_placement():
    assert _placements_in(_doc({"include_tasks": "tasks/place-monorepo-extra.yml"})) == []


def test_a_branch_named_on_the_include_is_accepted():
    doc = _doc(_placement_task(vars={"_branch": "{{ expected_repository_sha }}"}))
    assert _placements_in(doc) == [("play", True, False)]


def test_a_branch_named_in_play_vars_is_accepted():
    assert _placements_in(_doc(_placement_task(), play_vars={"_branch": "main"})) == [("play", True, False)]


def test_an_unrecognised_form_is_reported_by_the_backstop(tmp_path):
    recognised = tmp_path / "recognised.yml"
    recognised.write_text(
        "- hosts: all\n  tasks:\n    - ansible.builtin.include_tasks: tasks/place-monorepo.yml\n"
    )
    unrecognised = tmp_path / "unrecognised.yml"
    unrecognised.write_text(
        "- hosts: all\n  tasks:\n    - ansible.builtin.include_tasks: [tasks/place-monorepo.yml]\n"
    )
    assert _unscanned_mentions([recognised, unrecognised]) == ["unrecognised.yml"]


# LOCAL_ONLY is backed by a real assert. These run on an in-memory copy of deploy-skynet.yml.


def _skynet_doc():
    return copy.deepcopy(playbook_yaml.load(PLAYBOOKS_DIR / "deploy-skynet.yml"))


def _without_local_assert(doc):
    for play in doc:
        play["tasks"] = [task for task in play.get("tasks") or [] if not _asserts_local_mode(task)]


def _local_assert_after_placement(doc):
    for play in doc:
        tasks = play.get("tasks") or []
        if not any(_is_placement(task) for task in tasks):
            continue
        asserts = [task for task in tasks if _asserts_local_mode(task)]
        rest = [task for task in tasks if not _asserts_local_mode(task)]
        at = next(i for i, task in enumerate(rest) if _is_placement(task)) + 1
        play["tasks"] = rest[:at] + asserts + rest[at:]


def test_the_real_skynet_asserts_local_mode_before_placing():
    assert _local_mode_asserted_before_placement(_skynet_doc())


def test_removing_the_local_mode_assert_fails_the_check():
    doc = _skynet_doc()
    _without_local_assert(doc)
    assert not _local_mode_asserted_before_placement(doc)


def test_moving_the_local_mode_assert_after_the_placement_fails_the_check():
    doc = _skynet_doc()
    _local_assert_after_placement(doc)
    assert not _local_mode_asserted_before_placement(doc)
