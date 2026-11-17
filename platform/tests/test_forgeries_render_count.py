"""The render-count helpers in forgeries.py (plan 24).

`counting` measures how many times a run reads a templated extra var, and `honest_for` renders the honest
value for a given number of readings and the other value after. Each run here is a play on local hosts
whose TASKS debug tasks each print the variable once, through harness_sandbox.run_playbook, so the number
of tasks times the number of hosts is the number of readings. One run per helper proves it, and one run on
HOSTS hosts forked at once proves the counter holds when renderings race (AC-902). The stateful spelling is
compared with honest_for(1) byte for byte, with no run.

These tests prove the helpers, not a refusal, and send no refused input, so the templated-forgery rule
in test_extra_var_refusal_tests.py has nothing to require of them.
"""

import json
import re
from pathlib import Path

import forgeries
import harness_sandbox

TASKS = 3
HOSTS = 8
PLAY = [{"hosts": "all", "connection": "local", "gather_facts": False, "tasks": [
    {"ansible.builtin.debug": {"msg": "READ " + str(i) + " {{ probe }}"}} for i in range(TASKS)]}]


def _values(directory: Path, extra: str, hosts: int = 1) -> list[str]:
    """The value each debug task printed, in output order, from one run of PLAY with the -e JSON `extra`
    on `hosts` local hosts, forked all at once."""
    directory.mkdir(parents=True, exist_ok=True)
    playbook = directory / "play.yml"
    playbook.write_text(json.dumps(PLAY))  # JSON is YAML
    inventory = ",".join(f"h{i}" for i in range(hosts)) + ","
    proc = harness_sandbox.run_playbook(playbook, directory, "-e", extra, "-f", str(hosts), inventory=inventory)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    values = re.findall(r'"msg": "READ \d+ ([a-z]+)"', proc.stdout)
    assert len(values) == TASKS * hosts, proc.stdout
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


def test_honest_for_is_honest_once_across_parallel_hosts(tmp_path):
    extra = forgeries.honest_for("probe", "honest", "other", 1, tmp_path)
    values = _values(tmp_path, extra, HOSTS)
    assert values.count("honest") == 1, values
    # And no rendering was lost: the counter, named as forgeries._render_counter names it, saw every one.
    assert int((tmp_path / "forgery-renders-probe").read_text()) == HOSTS * TASKS
