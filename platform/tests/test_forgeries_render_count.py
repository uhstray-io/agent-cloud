"""The render-count helpers in forgeries.py (plan 24).

`counting` measures how many times a run reads a templated extra var, and `honest_for` renders the honest
value for a given number of readings and the other value after. Each run here is a localhost play whose
TASKS debug tasks each print the variable once, through harness_sandbox.run_playbook, so the number of
tasks is the number of readings. One run per helper proves it. The stateful spelling is compared with
honest_for(1) byte for byte, with no run. The counter's own shell is run from WORKERS processes at once,
as parallel hosts' forks run it, to prove no rendering is lost or counted twice (AC-902).

These tests prove the helpers, not a refusal, and send no refused input, so the templated-forgery rule
in test_extra_var_refusal_tests.py has nothing to require of them.
"""

import json
import re
import subprocess
from pathlib import Path

import forgeries
import harness_sandbox

TASKS = 3
WORKERS = 8
RENDERS = 25
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


def test_the_counter_neither_loses_nor_repeats_a_rendering_under_concurrency(tmp_path):
    # The pipe lookup runs the counter's shell through /bin/sh in each host's fork, so parallel hosts run it
    # at once. Each of WORKERS shells renders RENDERS times; the counts printed must be every number below
    # the total exactly once, and the file must end at the total. Without the lock a read could see the
    # file truncated by another write and count 0 again (AC-902).
    counter, shell = forgeries._render_counter("probe", tmp_path)
    loop = f"for i in $(seq {RENDERS}); do {shell}; done"
    workers = [subprocess.Popen(["/bin/sh", "-c", loop], stdout=subprocess.PIPE, text=True) for _ in range(WORKERS)]
    printed = [int(line) for worker in workers for line in worker.communicate(timeout=60)[0].split()]
    assert all(worker.returncode == 0 for worker in workers)
    assert sorted(printed) == list(range(WORKERS * RENDERS)), sorted(printed)
    assert int(counter.read_text()) == WORKERS * RENDERS
