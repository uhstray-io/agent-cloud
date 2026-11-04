"""tasks/assert-node-cpu-model.yml refuses a named CPU model the node cannot provide.

The run tests execute the task file's own tasks against synthetic node status. The flag lists are
Proxmox's (qemu-server CPUConfig.pm, the x86-64-v3 and x86-64-v4 entries), spelled as /proc/cpuinfo
names. A gate test points the one read at a closed port: a model outside the check must not read the
node, and a v3/v4 model must. A static test pins what the file may reference, since a uri result
carries the request headers. Needs ansible-playbook; the run tests skip without it.
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
FLAGS_FILE = ROOT / "platform/playbooks/vars/proxmox-cpu-model-flags.yml"

# Proxmox's x86-64-v3 flags (CPUConfig.pm:75-79), in /proc/cpuinfo spelling: sse4.1 -> sse4_1.
V3_FLAGS = ["aes", "popcnt", "pni", "sse4_1", "sse4_2", "ssse3", "avx", "avx2", "bmi1", "bmi2",
            "f16c", "fma", "abm", "movbe", "xsave"]
V4_EXTRA = ["avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"]
V3 = " ".join(V3_FLAGS)
V4 = " ".join(V3_FLAGS + V4_EXTRA)
CLOSED_PORT = "http://127.0.0.1:9"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def without(flags, name):
    return " ".join(flag for flag in flags.split() if flag != name)


def run_node_check(tmp_path, want, cpuinfo, node="pve-node", pve_host=None):
    """Run the task file for one node. Without pve_host the read is left out (synthetic status is
    used); with it the read runs against that URL. Returns (returncode, output)."""
    tasks = yaml.safe_load(NODE_FILE.read_text())
    if pve_host is None:
        tasks = [task for task in tasks if "ansible.builtin.uri" not in task]
    play_vars = {"_ncm_node": node, "_ncm_want_cpu": want, "_pve_host": pve_host or ""}
    if pve_host is None:
        play_vars["_ncm_status"] = {"json": {"data": {"cpuinfo": cpuinfo}}}
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "vars": play_vars, "tasks": tasks}]
    # playbook_dir is the play file's directory (a play-level value is ignored), so the task's
    # include of vars/ resolves against a symlink to the real flag file.
    (tmp_path / "vars").mkdir()
    (tmp_path / "vars" / FLAGS_FILE.name).symlink_to(FLAGS_FILE)
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


def test_a_v3_model_is_refused_when_aes_is_missing(tmp_path):
    # Proxmox's x86-64-v3 asks for aes (CPUConfig.pm:76); a node without it is refused.
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC", "flags": without(V3, "aes")})
    assert rc != 0 and "lacks aes, which" in out and "Nothing was written" in out, out


def test_a_v3_model_is_refused_when_avx2_is_missing(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC", "flags": without(V3, "avx2")})
    assert rc != 0 and "lacks avx2" in out and "Nothing was written" in out, out


def test_a_v3_model_is_refused_when_only_fma4_is_present(tmp_path):
    # Exact membership: 'fma4' must not satisfy 'fma'.
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC", "flags": without(V3, "fma") + " fma4"})
    assert rc != 0 and "lacks fma, which" in out, out


def test_the_flags_proxmox_does_not_ask_for_are_not_required(tmp_path):
    # cx16 and lahf_lm are glibc's ISA-level v2 entries, not Proxmox's x86-64-v3 list: a node
    # without them still passes.
    flags = V3 + " popcnt"
    assert "cx16" not in flags and "lahf_lm" not in flags
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC", "flags": flags})
    assert rc == 0, out


def test_sse4_2_is_required_as_v3_asks_for_it(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v3", {"model": "EPYC", "flags": without(V3, "sse4_2")})
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
    rc, out = run_node_check(tmp_path, "x86-64-v4", {"model": "EPYC", "flags": without(V4, "avx512vl")})
    assert rc != 0 and "lacks avx512vl" in out, out


@pytest.mark.parametrize("model", ["host", "kvm64", "x86-64-v2-AES"])
def test_other_models_pass_without_any_node_read(tmp_path, model):
    # Outside the x86-64-v3/v4 scope the node is not read at all: the read URL is closed, and the
    # run still passes.
    rc, out = run_node_check(tmp_path, model, None, pve_host=CLOSED_PORT)
    assert rc == 0, out


def test_a_v3_model_does_read_the_node(tmp_path):
    # The same closed URL fails a v3 model: the read is attempted, so the gate above is real.
    rc, out = run_node_check(tmp_path, "x86-64-v3", None, pve_host=CLOSED_PORT)
    assert rc != 0, out


def test_a_named_v3_variant_is_checked_like_v3(tmp_path):
    rc, out = run_node_check(tmp_path, "x86-64-v3-AES", {"model": "EPYC", "flags": V3})
    assert rc == 0, out


def test_the_task_file_reads_the_node_once_only_for_v3_and_v4_and_never_shows_the_result():
    # uri results carry the request headers, so visible tasks may use only the facts copied out.
    tasks = yaml.safe_load(NODE_FILE.read_text())
    reads = [task for task in tasks if "ansible.builtin.uri" in task]
    assert len(reads) == 1, [task.get("name") for task in reads]
    read = reads[0]
    assert read["register"] == "_ncm_status"
    assert read["check_mode"] is False and read["changed_when"] is False
    assert read["when"] == "(_ncm_level | int) > 0"
    assert read["ansible.builtin.uri"]["url"] == "{{ _pve_host }}/api2/json/nodes/{{ _ncm_node }}/status"
    for task in tasks:
        if "ansible.builtin.set_fact" in task or "ansible.builtin.uri" in task:
            continue
        assert "_ncm_status" not in json.dumps(task), task.get("name")


def test_the_flag_lists_come_from_the_shared_vars_file():
    # The lists are one file, shared with proxmox-validate.yml's CPU level report. The task file
    # carries no inline copy, and the file carries exactly Proxmox's v3 list and the v4 extras.
    tasks = yaml.safe_load(NODE_FILE.read_text())
    include = next(task for task in tasks if "ansible.builtin.include_vars" in task)
    assert include["ansible.builtin.include_vars"]["file"] == "{{ playbook_dir }}/vars/proxmox-cpu-model-flags.yml"
    text = NODE_FILE.read_text()
    assert "'avx512f'" not in text and "'sse4_1'" not in text  # no inline copy of either list
    flags = yaml.safe_load(FLAGS_FILE.read_text())["proxmox_cpu_level_flags"]
    assert flags == {3: V3_FLAGS, 4: V4_EXTRA}
