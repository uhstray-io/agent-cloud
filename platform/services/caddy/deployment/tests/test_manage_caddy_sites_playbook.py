"""Structural tests for manage-caddy-sites.yml.

Asserted against the PARSED playbook, not its text: a grep confirms a string is
present somewhere in the file, which cannot tell a task inside the rollback
block from one outside it — and that distinction is the whole property here.
"""

from pathlib import Path

import pytest
import yaml

PLAYBOOK = (
    Path(__file__).resolve().parents[4] / "playbooks" / "manage-caddy-sites.yml"
)


@pytest.fixture(scope="module")
def play():
    assert PLAYBOOK.is_file(), f"playbook not found at {PLAYBOOK}"
    return yaml.safe_load(PLAYBOOK.read_text())[0]


@pytest.fixture(scope="module")
def guarded(play):
    blocks = [t for t in play["tasks"] if "block" in t]
    assert len(blocks) == 1, "expected exactly one rollback-guarded block"
    return blocks[0]


def _names(tasks):
    return [t.get("name", "") for t in tasks]


def test_every_mutating_step_is_inside_the_rollback_block(guarded, play):
    # The failure this pins: the restore used to be an ordinary task gated on the
    # validate result. If `retire` wrote and `blockinfile` then failed, the play
    # aborted before reaching it — the route was deleted with nothing to put it
    # back. A rescue cannot be bypassed by an earlier task failing.
    inside = " ".join(_names(guarded["block"])).lower()
    for step in ("retire", "insert/update", "validate"):
        assert step in inside, f"'{step}' must run inside the guarded block"

    # ...and none of them may also sit outside it, where a failure skips rollback.
    outside = " ".join(
        t.get("name", "") for t in play["tasks"] if "block" not in t
    ).lower()
    for step in ("retire", "insert/update"):
        assert step not in outside


def test_the_rescue_restores_the_pre_edit_copy_and_then_fails(guarded):
    rescue = guarded.get("rescue")
    assert rescue, "the block must have a rescue; without one nothing rolls back"
    restore = rescue[0]
    assert restore["ansible.builtin.copy"]["src"].endswith(".pre-agent-cloud")
    assert "ansible.builtin.fail" in rescue[-1], "a rollback must still fail the run"


def test_the_backup_is_taken_before_the_guarded_block(play):
    # The rescue restores this file, so it must already exist when the block runs.
    names = _names(play["tasks"])
    backup = next(i for i, n in enumerate(names) if "Back up the Caddyfile" in n)
    block = next(i for i, t in enumerate(play["tasks"]) if "block" in t)
    assert backup < block
