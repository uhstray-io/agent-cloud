"""The render-count helpers in forgeries.py (plan 24).

`counting` measures how many times a run reads a templated extra var, and `honest_for` renders the honest
value for a given number of readings and the other value after. Each run here is a localhost play whose
TASKS debug tasks each print the variable once, through harness_sandbox.run_playbook, so the number of
tasks is the number of readings. One run per helper proves it. The stateful spelling is compared with
honest_for(1) byte for byte, with no run.

These tests prove the helpers, not a refusal, and send no refused input, so the templated-forgery rule
in test_extra_var_refusal_tests.py has nothing to require of them.
"""

import json
import re
from pathlib import Path

import forgeries
import harness_sandbox

TASKS = 3
PLAY = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": [
    {"ansible.builtin.debug": {"msg": "READ " + str(i) + " {{ probe }}"}} for i in range(TASKS)]}]


def _values(directory: Path, extra: str) -> list[str]:
    """The value each debug task printed, in order, from one run of PLAY with the -e JSON `extra`."""
    directory.mkdir(parents=True, exist_ok=True)
    playbook = directory / "play.yml"
    playbook.write_text(json.dumps(PLAY))  # JSON is YAML
    proc = harness_sandbox.run_playbook(playbook, directory, "-e", extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    values = re.findall(r'"msg": "READ \d+ ([a-z]+)"', proc.stdout)
    assert len(values) == TASKS, proc.stdout
    return values


def test_counting_counts_each_reading_of_the_name(tmp_path):
    extra, counter = forgeries.counting("probe", "seen", tmp_path)
    assert _values(tmp_path, extra) == ["seen"] * TASKS
    assert int(counter.read_text()) == TASKS


def test_honest_for_renders_the_honest_value_first_then_the_other(tmp_path):
    extra = forgeries.honest_for("probe", "honest", "other", 2, tmp_path)
    assert _values(tmp_path, extra) == ["honest"] * 2 + ["other"] * (TASKS - 2)


def test_the_stateful_spelling_is_honest_for_one_byte_for_byte(tmp_path):
    # No run: both come from the one builder at one rendering, so the two -e strings must be equal.
    spellings = {p.id: p.values[0] for p in forgeries.templated_forgeries("probe", "honest", "other")}
    assert spellings["stateful"](tmp_path) == forgeries.honest_for("probe", "honest", "other", 1, tmp_path)
