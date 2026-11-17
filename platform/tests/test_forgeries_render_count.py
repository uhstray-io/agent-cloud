"""The render-count helpers in forgeries.py (plan 24).

`counting` measures how many times a run reads a templated extra var, and `honest_for` renders the honest
value for a given number of readings and the other value after. Each run here is a localhost play whose
TASKS debug tasks each print the variable once, through harness_sandbox.run_playbook, so the number of
tasks is the number of readings. One run per helper proves it. The stateful spelling is compared with
honest_for(1) byte for byte, with no run.

These tests prove the helpers, not a refusal, and send no refused input, so the templated-forgery rule
in test_extra_var_refusal_tests.py has nothing to require of them.

The parallel-host tests run the same kind of play on HOSTS local hosts, forked HOSTS at a time, so the
renderings race. They guard the lock that serializes the counter (AC-902): without it a concurrent read
of the truncated counter file counted 0 and rendered the honest value again.
"""

import json
import re
from pathlib import Path

import forgeries
import harness_sandbox

TASKS = 3
PLAY = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": [
    {"ansible.builtin.debug": {"msg": "READ " + str(i) + " {{ probe }}"}} for i in range(TASKS)]}]
HOSTS = 8
PARALLEL_PLAY = [{"hosts": "all", "gather_facts": False, "tasks": [
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


def _run_on_parallel_hosts(directory: Path, extra: str):
    """One run of PARALLEL_PLAY on HOSTS local hosts, forked HOSTS at a time: every host reads the name
    TASKS times, from parallel workers. Returns the process."""
    directory.mkdir(parents=True, exist_ok=True)
    inventory = directory / "hosts.ini"
    inventory.write_text("".join(f"h{i} ansible_connection=local\n" for i in range(HOSTS)))
    playbook = directory / "parallel.yml"
    playbook.write_text(json.dumps(PARALLEL_PLAY))  # JSON is YAML
    proc = harness_sandbox.run_playbook(playbook, directory, "-e", extra, "-f", str(HOSTS),
                                        inventory=inventory)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def test_counting_counts_every_reading_across_parallel_hosts(tmp_path):
    extra, counter = forgeries.counting("probe", "seen", tmp_path)
    _run_on_parallel_hosts(tmp_path, extra)
    assert int(counter.read_text()) == HOSTS * TASKS


def test_honest_for_is_honest_once_across_parallel_hosts(tmp_path):
    extra = forgeries.honest_for("probe", "honest", "other", 1, tmp_path)
    proc = _run_on_parallel_hosts(tmp_path, extra)
    assert len(re.findall(r'"msg": "READ \d+ honest"', proc.stdout)) == 1, proc.stdout
    assert len(re.findall(r'"msg": "READ \d+ other"', proc.stdout)) == HOSTS * TASKS - 1, proc.stdout
