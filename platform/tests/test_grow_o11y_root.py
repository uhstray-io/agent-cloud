"""grow-o11y-root.yml and files/grow-root-lvm.py: grow the o11y receiver's full root, online.

The helper runs as root on the receiver, so here its commands are faked: each case gives the
findmnt/lsblk/LVM reports and growpart's answers, and checks the plan or the exact commands
an apply would run. The playbook is checked for the guards that make it safe to launch.
"""

import importlib.util
import json

import playbook_yaml
import pytest
import yaml

ROOT = playbook_yaml.REPO
HELPER = ROOT / "platform/playbooks/files/grow-root-lvm.py"
PLAYBOOK = ROOT / "platform/playbooks/grow-o11y-root.yml"
GiB = 1024 ** 3


def _load():
    spec = importlib.util.spec_from_file_location("grow_root_lvm", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _state(fstype="ext4", lv_type="lvm", pv_count="1", vg_free=0, pv_size=18 * GiB, part=18 * GiB):
    # The o11y receiver as task 2115 read it: an ~18 GiB partition of a 100 GiB disk.
    return {
        "root": {"filesystems": [{"source": "/dev/mapper/vg-root", "fstype": fstype, "maj:min": "252:0"}]},
        "lsblk": {"blockdevices": [{"name": "/dev/sda", "type": "disk", "size": 100 * GiB, "maj:min": "8:0",
                                    "children": [
                                        {"name": "/dev/sda1", "type": "part", "size": GiB, "maj:min": "8:1"},
                                        {"name": "/dev/sda3", "type": "part", "size": part, "maj:min": "8:3",
                                         "children": [{"name": "/dev/mapper/vg-root", "type": lv_type,
                                                       "size": 10 * GiB, "maj:min": "252:0"}]}]}]},
        "pvs": [{"pv_name": "/dev/sda3", "vg_name": "vg", "pv_size": str(pv_size), "dev_size": str(part)}],
        "vgs": [{"vg_name": "vg", "vg_free": str(vg_free), "pv_count": pv_count}],
        "lvs": [{"lv_path": "/dev/vg/root", "vg_name": "vg", "lv_size": str(10 * GiB)}],
    }


class _Statvfs:
    f_blocks, f_frsize, f_bavail = 2_500_000, 4096, 0


def _helper(monkeypatch, state, growpart_rc=0, growpart_out="CHANGED: partition=3", after=None):
    mod = _load()
    calls = []
    states = [state, after or state]

    def run(argv, ok=(0,)):
        calls.append(argv)
        if argv[0] == "growpart":
            rc = growpart_rc if "-N" in argv else 0
            r = type("R", (), {"returncode": rc, "stdout": growpart_out, "stderr": ""})()
            if rc not in ok:
                raise mod.Refused("growpart failed")
            return r
        if argv[0] == "vgs":
            return type("R", (), {"returncode": 0, "stdout": json.dumps(
                {"report": [{"vg": [{"vg_name": "vg", "vg_free": str(80 * GiB)}]}]}), "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(mod, "run", run)
    monkeypatch.setattr(mod, "collect", lambda: states.pop(0) if len(states) > 1 else states[0])
    monkeypatch.setattr(mod, "device_number", lambda p: "252:0" if p == "/dev/vg/root" else None)
    monkeypatch.setattr(mod.os, "statvfs", lambda p: _Statvfs())
    monkeypatch.setattr(mod.shutil, "which", lambda n: "/usr/bin/growpart")
    return mod, calls


def test_the_receiver_as_read_plans_all_three_steps(monkeypatch):
    mod, calls = _helper(monkeypatch, _state())
    out = mod.main("plan")
    assert out["steps"] == ["growpart", "pvresize", "lvextend"]
    assert out["before"]["disk_bytes"] == 100 * GiB and out["before"]["lv_bytes"] == 10 * GiB
    # Plan mode runs growpart's dry run only and changes nothing.
    assert [c for c in calls if c[0] != "growpart"] == [] and all("-N" in c for c in calls)


def test_a_fully_grown_root_plans_nothing_and_applies_nothing(monkeypatch):
    mod, calls = _helper(monkeypatch, _state(part=100 * GiB, pv_size=100 * GiB), growpart_rc=1,
                         growpart_out="NOCHANGE: partition 3 is size 1. it cannot be grown")
    out = mod.main("apply")
    assert out["steps"] == [] and "after" not in out
    assert all(c[0] == "growpart" and "-N" in c for c in calls)


def test_apply_runs_growpart_then_pvresize_then_lvextend_with_resizefs(monkeypatch):
    mod, calls = _helper(monkeypatch, _state(), after=_state(part=100 * GiB, pv_size=100 * GiB))
    out = mod.main("apply")
    real = [c for c in calls if "-N" not in c]
    assert real[0] == ["growpart", "/dev/sda", "3"]
    assert real[1] == ["pvresize", "/dev/sda3"]
    assert real[2][0] == "vgs"
    assert real[3] == ["lvextend", "--resizefs", "--extents", "+100%FREE", "/dev/vg/root"]
    assert "after" in out


def test_free_extents_alone_still_extend_the_lv(monkeypatch):
    mod, _ = _helper(monkeypatch, _state(part=100 * GiB, pv_size=100 * GiB, vg_free=5 * GiB), growpart_rc=1,
                     growpart_out="NOCHANGE")
    assert mod.main("plan")["steps"] == ["lvextend"]


@pytest.mark.parametrize("kwargs,reason", [
    ({"fstype": "xfs"}, "not ext4"),
    ({"lv_type": "crypt"}, "not an LV on a partition"),
    ({"pv_count": "2"}, "not exactly one PV"),
], ids=["xfs", "not-lvm", "two-pvs"])
def test_an_unexpected_layout_is_refused_before_anything_runs(monkeypatch, kwargs, reason):
    mod, calls = _helper(monkeypatch, _state(**kwargs))
    with pytest.raises(mod.Refused, match=reason):
        mod.main("apply")
    assert calls == []


def test_growpart_refusing_for_another_reason_is_a_refusal(monkeypatch):
    mod, calls = _helper(monkeypatch, _state(), growpart_rc=1, growpart_out="FAILED: disk is busy")
    with pytest.raises(mod.Refused, match="growpart refused"):
        mod.main("apply")
    assert all("-N" in c for c in calls)


def test_a_missing_growpart_is_refused(monkeypatch):
    mod, _ = _helper(monkeypatch, _state())
    monkeypatch.setattr(mod.shutil, "which", lambda n: None)
    with pytest.raises(mod.Refused, match="growpart is not installed"):
        mod.main("plan")


# ── The playbook ──────────────────────────────────────────────────────────────

PLAYS = yaml.safe_load(PLAYBOOK.read_text())


MAIN = PLAYS[2]


def test_the_preflight_guards_the_literal_receiver_group_under_every_tag():
    pre = PLAYS[0]
    assert pre["ansible.builtin.import_playbook"] == "preflight-target-group.yml"
    assert pre["vars"] == {"preflight_group": "o11y_svc", "preflight_group_expected": "o11y_svc"}
    assert pre["tags"] == ["always"]
    assert MAIN["hosts"] == "o11y_svc"


def test_the_run_is_bound_to_the_reviewed_commit():
    # Review of 99666377: a Dev template's moving checkout could run unreviewed code.
    assert PLAYS[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    tpl = next(t for t in yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
               if t["name"] == "Grow o11y Root (Dev)")
    sha = next(v for v in tpl["survey_vars"] if v["name"] == "expected_repository_sha")
    assert sha["required"] is True


def test_remote_temp_is_tmpfs_and_proven_before_any_module_runs():
    play = MAIN
    assert play["vars"]["ansible_remote_tmp"] == "/dev/shm/ansible-tmp"
    assert play["environment"]["TMPDIR"] == "/dev/shm/ansible-tmp"
    assert play["tasks"][0]["ansible.builtin.import_tasks"] == "tasks/require-tmpfs-remote-tmp.yml"


def test_only_the_helper_tasks_escalate_and_the_apply_is_check_mode_guarded():
    play = MAIN
    assert play["become"] is False
    escalated = [t["name"] for t in play["tasks"] if t.get("become")]
    assert escalated == ["Plan the grow (reads only)", "Grow partition, PV and root LV with its filesystem",
                         "Re-read the chain"]
    apply = next(t for t in play["tasks"] if t["name"].startswith("Grow partition"))
    assert apply["when"].startswith("not ansible_check_mode")
    plan = next(t for t in play["tasks"] if t["name"].startswith("Plan the grow"))
    assert plan["ansible.builtin.command"]["argv"] == ["python3", "-", "plan"] and plan["check_mode"] is False


def test_a_verify_run_reads_and_checks_but_never_applies():
    # Standard 3 (08-ansible-automation-standards.md): --tags verify makes no change.
    tagged = {t["name"]: "verify" in t.get("tags", []) for t in MAIN["tasks"]}
    assert tagged["Grow partition, PV and root LV with its filesystem"] is False
    assert all(tagged[n] for n in ["Require writable tmpfs for Ansible's remote temp",
                                   "Resolve the sudo password through OpenBao", "Plan the grow (reads only)",
                                   "Re-read the chain", "Refuse a root that still has room to grow"])
    # Static, so the tag reaches the tasks inside (a dynamic include's tags stop at the include).
    sudo = next(t for t in MAIN["tasks"] if t["name"] == "Resolve the sudo password through OpenBao")
    assert "ansible.builtin.import_tasks" in sudo


def test_an_lv_grown_without_its_filesystem_is_finished_by_resize2fs(monkeypatch):
    # Review of 99666377: lvextend --resizefs can grow the LV and then fail on ext4; the VG
    # then has no free extents, and only the size comparison sees the lag.
    lagging = _state(part=100 * GiB, pv_size=100 * GiB, vg_free=0)
    lagging["lvs"][0]["lv_size"] = str(99 * GiB)
    mod, calls = _helper(monkeypatch, lagging, growpart_rc=1, growpart_out="NOCHANGE")
    assert mod.main("plan")["steps"] == ["resize2fs"]
    mod, calls = _helper(monkeypatch, lagging, growpart_rc=1, growpart_out="NOCHANGE")
    mod.main("apply")
    real = [c for c in calls if "-N" not in c]
    assert ["resize2fs", "/dev/vg/root"] in real and not any(c[0] in ("lvextend", "pvresize") for c in real)
