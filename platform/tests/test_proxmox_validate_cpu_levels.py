"""proxmox-validate.yml reports each online node's CPU model and x86-64 level, read-only.

The run tests execute the playbook's own tasks: the vars include, "Derive node CPU levels" on
synthetic reads, and the read itself against a closed port, so a failed read is exercised for real.
The static tests pin the read's shape: a loop over the clean node names with an index into the
results, no_log, and a failure that never fails the play. The levels are a report, so they never
enter _validation_results. Needs ansible-playbook; the run tests skip without it.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import playbook_yaml
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
VALIDATE = ROOT / "platform/playbooks/proxmox-validate.yml"
FLAGS_FILE = ROOT / "platform/playbooks/vars/proxmox-cpu-model-flags.yml"
LOAD = "Load the Proxmox CPU level flag lists"
READ = "Read each online node's CPU"
DERIVE = "Derive node CPU levels"
REPORT = "Report node CPU levels"
CLOSED_PORT = "http://127.0.0.1:9"
CLEAN_NODES = "{{ _pve_nodes.json.data | selectattr('status', 'equalto', 'online') | map(attribute='node') | list }}"

# Proxmox's x86-64-v3 flags and the v4 extras, in /proc/cpuinfo spelling (CPUConfig.pm:75-83).
V3_FLAGS = ["aes", "popcnt", "pni", "sse4_1", "sse4_2", "ssse3", "avx", "avx2", "bmi1", "bmi2",
            "f16c", "fma", "abm", "movbe", "xsave"]
V4_EXTRA = ["avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"]
V3 = " ".join(V3_FLAGS)
V4 = " ".join(V3_FLAGS + V4_EXTRA)

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def without(flags, name):
    return " ".join(flag for flag in flags.split() if flag != name)


def find_task(name):
    for play in playbook_yaml.plays(VALIDATE):
        for task in playbook_yaml.tasks(play.get("tasks", [])):
            if task.get("name") == name:
                return task
    raise AssertionError(f"task {name!r} not found in proxmox-validate.yml")


def task_names():
    return [task.get("name") for play in playbook_yaml.plays(VALIDATE)
            for task in playbook_yaml.tasks(play.get("tasks", []))]


def dump_levels(path):
    return {"name": "dump", "ansible.builtin.copy": {"content": "{{ _node_cpu | to_json }}", "dest": str(path)}}


def run_play(tmp_path, play_vars, tasks):
    """Run tasks as one localhost play. The play file's directory is playbook_dir, so the vars
    file is staged there as the playbook's own include expects. Returns (returncode, output)."""
    (tmp_path / "vars").mkdir()
    (tmp_path / "vars" / FLAGS_FILE.name).symlink_to(FLAGS_FILE)
    play_file = tmp_path / "play.yml"
    play_file.write_text(yaml.safe_dump([{"hosts": "localhost", "connection": "local",
                                          "gather_facts": False, "vars": play_vars, "tasks": tasks}],
                                        sort_keys=False))
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", str(play_file)], capture_output=True,
                          text=True, env={"ANSIBLE_NOCOLOR": "1", "PATH": os.environ["PATH"],
                                          "ANSIBLE_LOCAL_TEMP": str(tmp_path)},
                          stdin=subprocess.DEVNULL, timeout=180)
    return proc.returncode, proc.stdout + proc.stderr


def online_nodes(*rows):
    return {"json": {"data": [{"node": name, "status": status} for name, status in rows]}}


def read_ok(model, flags):
    return {"json": {"data": {"cpuinfo": {"model": model, "flags": flags}}}}


def test_each_online_node_gets_the_highest_level_its_flags_back(tmp_path):
    nodes = online_nodes(("v4-node", "online"), ("v3-node", "online"), ("no-avx512vl", "online"),
                         ("sandy", "online"), ("no-flags", "online"), ("dark", "online"),
                         ("down", "offline"))
    reads = {"results": [
        read_ok("Xeon-v4", V4),
        read_ok("EPYC-v3", V3),
        read_ok("EPYC-v3-short", without(V4, "avx512vl")),
        read_ok("E5-Sandy", without(V3, "avx2")),
        read_ok("no-flags-model", ""),
        {"failed": True},
    ]}
    levels_file = tmp_path / "levels.json"
    rc, out = run_play(tmp_path, {"_pve_nodes": nodes, "_node_cpu_reads": reads},
                       [find_task(LOAD), find_task(DERIVE), dump_levels(levels_file)])
    assert rc == 0, out
    assert json.loads(levels_file.read_text()) == [
        {"node": "v4-node", "cpu_model": "Xeon-v4", "flags_reported": True, "x86_64_level": 4},
        {"node": "v3-node", "cpu_model": "EPYC-v3", "flags_reported": True, "x86_64_level": 3},
        {"node": "no-avx512vl", "cpu_model": "EPYC-v3-short", "flags_reported": True, "x86_64_level": 3},
        {"node": "sandy", "cpu_model": "E5-Sandy", "flags_reported": True, "x86_64_level": 2},
        {"node": "no-flags", "cpu_model": "no-flags-model", "flags_reported": False, "x86_64_level": "unknown"},
        {"node": "dark", "cpu_model": "unknown", "flags_reported": False, "x86_64_level": "unknown"},
    ]


def test_a_failed_read_is_unknown_and_never_fails_the_play(tmp_path):
    # The read runs for real against a closed port: every request fails, and the play still passes.
    nodes = online_nodes(("a", "online"), ("b", "online"), ("off", "offline"))
    levels_file = tmp_path / "levels.json"
    rc, out = run_play(tmp_path, {"_pve_nodes": nodes, "_pve_host": CLOSED_PORT},
                       [find_task(LOAD), find_task(READ), find_task(DERIVE), dump_levels(levels_file)])
    assert rc == 0, out
    levels = json.loads(levels_file.read_text())
    assert [entry["node"] for entry in levels] == ["a", "b"], levels
    assert [entry["x86_64_level"] for entry in levels] == ["unknown", "unknown"], levels
    assert all(entry["flags_reported"] is False for entry in levels), levels


def test_the_report_prints_levels_and_no_flag_names(tmp_path):
    nodes = online_nodes(("v4-node", "online"))
    rc, out = run_play(tmp_path, {"_pve_nodes": nodes, "_node_cpu_reads": {"results": [read_ok("Xeon-v4", V4)]}},
                       [find_task(LOAD), find_task(DERIVE), find_task(REPORT)])
    assert rc == 0, out
    assert "Xeon-v4" in out and "x86_64_level" in out, out
    assert "avx512f" not in out and "avx2" not in out, out


def test_the_read_loops_over_node_names_and_indexes_the_results():
    read = find_task(READ)
    assert read["ansible.builtin.uri"] == {"url": "{{ _pve_host }}/api2/json/nodes/{{ item }}/status",
                                           "method": "GET"}
    assert read["loop"] == CLEAN_NODES
    assert read["loop_control"] == {"index_var": "_ni", "label": "{{ item }}"}
    assert read["register"] == "_node_cpu_reads"
    assert read["no_log"] is True and read["check_mode"] is False and read["changed_when"] is False
    assert read["failed_when"] is False


def test_the_derive_walks_the_same_node_list_and_indexes_the_reads():
    derive = find_task(DERIVE)
    assert "loop" not in derive
    text = derive["ansible.builtin.set_fact"]["_node_cpu"]
    assert CLEAN_NODES.strip("{} ") in text
    assert "_node_cpu_reads.results[loop.index0]" in text


def test_the_levels_are_a_report_and_never_enter_the_validation_results():
    for name in (LOAD, READ, DERIVE, REPORT):
        assert "_validation_results" not in json.dumps(find_task(name)), name


def test_the_levels_are_derived_and_reported_after_the_read_and_the_flag_include():
    names = task_names()
    assert names.index(LOAD) < names.index(READ) < names.index(DERIVE) < names.index(REPORT)


def test_the_report_prints_only_the_derived_levels():
    msg = find_task(REPORT)["ansible.builtin.debug"]["msg"]
    assert msg == "{{ _node_cpu | map('to_json') | list }}"
