"""A placement on a non-local run must name its branch: `_branch` in the play's vars or in the placement include's own `vars:`.

tasks/place-monorepo.yml asserts `_monorepo_dir`, `_branch` (unless local_mode) and
`monorepo_repo` (unless local_mode) at place-monorepo.yml:23-29. deploy-console.yml included the
placement unconditionally and defined `_monorepo_dir` and `_deploy_dir` but no `_branch`, so
Semaphore task 9848 (Deploy console (Dev), check mode) failed on the console host with
"Missing required variables for place-monorepo.yml". Local-dev never reached that assert,
because local_mode is set there, so no local run could catch it.

The guard reads every top-level playbook. A placement include whose own `when` does not mention
local_mode must name its branch, either in its play's vars or in the include's own `vars:`. The
caller chooses the branch: the shared task gives no default, so a forgotten choice cannot be
masked. The include-level form is deliberate: deploy-o11y-journal-collector.yml pins
`expected_repository_sha` per include (:56-59 and :361-364). LOCAL_ONLY lists the plays that
place only in local mode, because the play refuses any other run before the include.
"""

import playbook_yaml
import pytest

PLAYBOOKS_DIR = playbook_yaml.REPO / "platform/playbooks"
PLAYBOOKS = sorted(PLAYBOOKS_DIR.glob("*.yml"))
PLACEMENT = "tasks/place-monorepo.yml"
INCLUDE_KEYS = ("include_tasks", "ansible.builtin.include_tasks", "import_tasks", "ansible.builtin.import_tasks")
PLAY_TASK_KEYS = ("pre_tasks", "tasks", "post_tasks", "handlers")
LOCAL_ONLY = {
    "deploy-skynet.yml": "the play refuses any run outside local_mode before the placement include "
                         "(the operator-token stopgap assert, deploy-skynet.yml:138-141; include at :166)",
}
PLACING = [playbook for playbook in PLAYBOOKS if playbook.name not in LOCAL_ONLY]


def _placements(path):
    """One (play name, unguarded, unnamed) row per play that includes the placement task.

    unguarded: some include's `when` does not mention local_mode.
    unnamed:   some unguarded include names no branch, in its play's vars or its own vars.
    """
    rows = []
    for index, play in enumerate(playbook_yaml.load(path) or []):
        if not isinstance(play, dict) or "hosts" not in play:
            continue
        includes = [
            task
            for key in PLAY_TASK_KEYS
            for task in playbook_yaml.tasks(play.get(key))
            if any(task.get(include) == PLACEMENT for include in INCLUDE_KEYS)
        ]
        if not includes:
            continue
        play_names_branch = "_branch" in (play.get("vars") or {})
        unguarded = [
            task for task in includes
            if "local_mode" not in " ".join(playbook_yaml.strings(task.get("when")))
        ]
        rows.append((
            play.get("name") or f"play {index}",
            bool(unguarded),
            any(not play_names_branch and "_branch" not in (task.get("vars") or {}) for task in unguarded),
        ))
    return rows


def _unnamed_branch_plays(path):
    return [name for name, _, unnamed in _placements(path) if unnamed]


def _shape(name):
    return [(unguarded, unnamed) for _, unguarded, unnamed in _placements(PLAYBOOKS_DIR / name)]


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


@pytest.mark.parametrize("playbook", PLACING, ids=lambda p: p.name)
def test_an_unguarded_placement_names_its_branch(playbook):
    assert _unnamed_branch_plays(playbook) == [], (
        playbook.name + ": a play includes " + PLACEMENT + " without a local_mode condition and "
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
        assert any(unnamed for _, _, unnamed in _placements(path)), (
            name + " no longer places the monorepo without a local_mode condition or a named _branch: "
            "drop its LOCAL_ONLY entry"
        )
