"""Local placement reports `changed` only for a difference a deploy can see: a second local
deploy of an unchanged tree must report no change.

tasks/place-monorepo.yml copies the checkout with `rsync -i`; `changed_when` used to be
"rsync printed anything". The read-only checkout mount makes rsync print `.L...p` for every
tracked symlink on every run and `.d..t` for a directory mtime, so the second deploy was never
clean. The unit lines below were captured from rsync 3.5.0 with the checkout bind-mounted
read-only; the end-to-end class runs the task's own script against real GNU rsync (skipped
where only openrsync is installed, whose itemize output differs).
"""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLACEMENT = REPO / "platform/playbooks/tasks/place-monorepo.yml"
TASK = "Copy working tree into place (local mode)"

SPEC = importlib.util.spec_from_file_location(
    "rsync_itemize", REPO / "platform/playbooks/filter_plugins/rsync_itemize.py")
filt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(filt)


def task():
    return next(t for t in yaml.safe_load(PLACEMENT.read_text()) if t.get("name") == TASK)


@pytest.mark.parametrize("line", [
    ">f+++++++++ deployment/CLAUDE.md",
    ">f..t...... deployment/compose.yml",
    ">f.st...... deployment/compose.yml",
    "cd+++++++++ deployment/",
    "cL+++++++++ deployment/AGENTS.md -> CLAUDE.md",
    "cLc.t...... deployment/AGENTS.md -> OTHER.md",
    "cLc........ deployment/AGENTS.md -> OTHER.md",
    ".f...p..... deployment/deploy.sh",
    ".d...p..... deployment/",
    "*deleting   deployment/retired.yml",
    "hf+++++++++ a => b",
])
def test_a_real_change_counts(line):
    assert filt.rsync_itemize_changed(line + "\n")


@pytest.mark.parametrize("line", [
    ".L...p..... deployment/AGENTS.md -> CLAUDE.md",
    ".L..t...... deployment/AGENTS.md -> CLAUDE.md",
    ".d..t...... ./",
    ".d..t...... deployment/configuration/",
    'skipping non-regular file "deployment/sock"',
    "",
])
def test_attribute_noise_does_not_count(line):
    assert not filt.rsync_itemize_changed(line + "\n")


def test_noise_next_to_noise_stays_clean_and_one_real_line_flips_it():
    noise = ".L...p..... a -> b\n.d..t...... ./\n"
    assert not filt.rsync_itemize_changed(noise)
    assert filt.rsync_itemize_changed(noise + ">f+++++++++ new\n")


def test_it_is_registered_under_its_name():
    assert filt.FilterModule().filters() == {"rsync_itemize_changed": filt.rsync_itemize_changed}


def test_the_task_uses_the_filter_and_drops_directory_times():
    t = task()
    assert t["changed_when"] == "_rsync_result.stdout | rsync_itemize_changed"
    assert "rsync -rlpti --omit-dir-times --delete" in t["ansible.builtin.shell"]


def _gnu_rsync():
    exe = shutil.which("rsync")
    if not exe:
        return False
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    return "openrsync" not in out


@pytest.mark.skipif(not _gnu_rsync(), reason="needs GNU rsync (openrsync itemizes differently)")
class TestRealRsync:
    @pytest.fixture
    def tree(self, tmp_path):
        src, dst = tmp_path / "src", tmp_path / "dst"
        (src / "dir").mkdir(parents=True)
        (src / "dir/a.txt").write_text("a\n")
        (src / "dir/deploy.sh").write_text("#!/bin/sh\n")
        (src / "dir/deploy.sh").chmod(0o755)
        (src / "TARGET.md").write_text("t\n")
        (src / "AGENTS.md").symlink_to("TARGET.md")
        script = task()["ansible.builtin.shell"].replace("{{ _monorepo_dir }}", str(dst)).replace(
            "{{ playbook_dir | dirname | dirname }}", str(src))

        def sync():
            out = subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True).stdout
            return out, filt.rsync_itemize_changed(out)

        return src, dst, sync

    def test_first_sync_changes_and_the_second_is_silent(self, tree):
        _, dst, sync = tree
        assert sync()[1]
        assert (dst / "dir/deploy.sh").read_text() == "#!/bin/sh\n"
        assert sync() == ("", False)

    def test_a_directory_mtime_alone_prints_nothing(self, tree):
        src, _, sync = tree
        sync()
        os.utime(src / "dir", (1, 1))
        assert sync() == ("", False)

    def test_a_content_edit_at_the_same_size_is_changed(self, tree):
        src, dst, sync = tree
        sync()
        (src / "dir/a.txt").write_text("b\n")
        os.utime(src / "dir/a.txt", (5, 5))
        assert sync()[1]
        assert (dst / "dir/a.txt").read_text() == "b\n"

    def test_a_new_file_is_changed(self, tree):
        src, _, sync = tree
        sync()
        (src / "new.txt").write_text("n\n")
        assert sync()[1]

    def test_a_deletion_is_changed(self, tree):
        src, dst, sync = tree
        sync()
        (src / "dir/a.txt").unlink()
        assert sync()[1]
        assert not (dst / "dir/a.txt").exists()

    def test_a_deploy_script_losing_its_exec_bit_is_changed(self, tree):
        src, dst, sync = tree
        sync()
        (src / "dir/deploy.sh").chmod(0o644)
        assert sync()[1]
        assert not os.access(dst / "dir/deploy.sh", os.X_OK)

    def test_a_retargeted_symlink_is_changed(self, tree):
        src, dst, sync = tree
        sync()
        (src / "AGENTS.md").unlink()
        (src / "AGENTS.md").symlink_to("dir/a.txt")
        assert sync()[1]
        assert os.readlink(dst / "AGENTS.md") == "dir/a.txt"
