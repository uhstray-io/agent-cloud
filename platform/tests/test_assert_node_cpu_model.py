"""tasks/assert-node-cpu-model.yml refuses a named CPU model the node cannot provide.

The run tests execute the task file's own fact, flag-set and assert tasks against synthetic
node status. The uri read is left out: it needs a live PVE API. A static test pins what the file
may reference, since a uri result carries the request headers. Needs ansible-playbook; the run
tests skip without it.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
NODE_FILE = ROOT / "platform/playbooks/tasks/assert-node-cpu-model.yml"

# The flag names the task requires, in /proc/cpuinfo spelling (see the header of the task file).
V2 = "cx16 lahf_lm popcnt pni sse4_1 sse4_2 ssse3"
V3 = V2 + " avx avx2 bmi1 bmi2 f16c fma abm movbe xsave"
V4 = V3 + " avx512f avx512bw avx512cd avx512dq avx512vl"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def run_node_check(tmp_path, want, cpuinfo, node="pve-node"):
    """Run the task file's non-read tasks for one node. Returns (returncode, output)."""
    tasks = [task for task in yaml.safe_load(NODE_FILE.read_text()) if "ansible.builtin.uri" not in task]
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "vars": {"_ncm_node": node, "_ncm_want_cpu": want,
                      "_ncm_status": {"json": {"data": {"cpuinfo": cpuinfo}}}},
             "tasks": tasks}]
    play_file = tmp_path / "node-check.yml"
    play_file.write_text(yaml.safe_dump(play, sort_keys=False))
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(play_file)], capture_output=True,
                          text=True, env={"ANSIBLE_NOCOLOR": "1", "PATH": os.environ["PATH"],
                                          "ANSIBLE_LOCAL_TEMP": str(tmp_path)},
                          stdin=subprocess.DEVNULL, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def test_a_v3_model_passes_when_the_node_reports_the_v3_flags(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC", "flags": V3})
    assert rc == 0, out


def test_a_v3_model_is_refused_when_avx2_is_missing(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC", "flags": V3.replace(" avx2", "")})
    assert rc != 0 and "lacks avx2" in out and "Nothing was written" in out, out


def test_a_v3_model_is_refused_when_only_fma4_is_present(tmp_path):
    # Exact membership: 'fma4' must not satisfy 'fma'.
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC", "flags": V3.replace(" fma", " fma4")})
    assert rc != 0 and "lacks fma, which" in out, out


def test_the_v2_part_of_v3_is_required_too(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC", "flags": V3.replace(" sse4_2", "")})
    assert rc != 0 and "lacks sse4_2" in out, out


def test_an_empty_flag_list_fails_closed(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC", "flags": ""})
    assert rc != 0 and "reports no CPU flags" in out and "Nothing was written" in out, out


def test_missing_flags_fail_closed(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC"})
    assert rc != 0 and "reports no CPU flags" in out, out


def test_v4_needs_the_avx512_set_on_top_of_v3(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v4", {"model": "EPYC", "flags": V3})
    assert rc != 0 and "lacks avx512bw, avx512cd, avx512dq, avx512f, avx512vl" in out, out


def test_v4_passes_with_v3_and_the_avx512_set(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v4", {"model": "EPYC", "flags": V4})
    assert rc == 0, out


def test_v4_is_refused_when_one_avx512_flag_is_missing(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v4", {"model": "EPYC", "flags": V4.replace(" avx512vl", "")})
    assert rc != 0 and "lacks avx512vl" in out, out


@pytest.mark.parametrize("model", ["host", "kvm64", "x86-64-v2-AES"])
def test_other_models_pass_without_any_flag_read(tmp_path, model):
    # Outside the x86-64-v3/v4 scope nothing is read, so a node that reports no flags still takes them.
    rc, out = run_node_check(tmp_path, model, {"model": "EPYC"})
    assert rc == 0, out


def test_a_named_v3_variant_is_checked_like_v3(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v3-AES", {"model": "EPYC", "flags": V3})
    assert rc == 0, out


def test_the_task_file_reads_the_node_once_and_never_shows_the_request_result():
    # uri results carry the request headers, so visible tasks may use only the facts copied out.
    tasks = yaml.safe_load(NODE_FILE.read_text())
    reads = [task for task in tasks if "ansible.builtin.uri" in task]
    assert len(reads) == 1, [task.get("name") for task in reads]
    read = reads[0]
    assert read["register"] == "_ncm_status"
    assert read["check_mode"] is False and read["changed_when"] is False
    assert read["ansible.builtin.uri"]["url"] == "{{ _pve_host }}/api2/json/nodes/{{ _ncm_node }}/status"
    for task in tasks:
        if "ansible.builtin.set_fact" in task or "ansible.builtin.uri" in task:
            continue
        assert "_ncm_status" not in json.dumps(task), task.get("name")
