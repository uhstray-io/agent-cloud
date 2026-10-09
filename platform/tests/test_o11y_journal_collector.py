"""Safety contract for the bounded receiver-host journald pilot."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]
O11Y = ROOT / "platform/services/o11y/deployment"
PLAYBOOK = ROOT / "platform/playbooks/deploy-o11y-journal-collector.yml"
HELPER = ROOT / "platform/playbooks/files/probe-o11y-journal-directory.py"
SPEC = importlib.util.spec_from_file_location("o11y_journal_probe", HELPER)
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)
SURVEY_HELPER = ROOT / "platform/playbooks/files/survey-o11y-journal-directory.py"
SURVEY_SPEC = importlib.util.spec_from_file_location("o11y_journal_survey", SURVEY_HELPER)
SURVEY = importlib.util.module_from_spec(SURVEY_SPEC)
SURVEY_SPEC.loader.exec_module(SURVEY)
POSITIONS_HELPER = ROOT / "platform/playbooks/files/o11y-journal-positions.py"
POSITIONS_SPEC = importlib.util.spec_from_file_location("o11y_journal_positions", POSITIONS_HELPER)
POSITIONS = importlib.util.module_from_spec(POSITIONS_SPEC)
POSITIONS_SPEC.loader.exec_module(POSITIONS)


def _positions_state(
    owner=(88, 88), mode=0o700, children=(), acl=False,
    root_mount=False, child_mount=False, component_layout=True,
):
    return {
        "status": "observed",
        "root": {"uid": owner[0], "gid": owner[1], "mode": mode, "dev": 7, "ino": 11},
        "children": list(children),
        "child_count": len(children),
        "acl": acl,
        "root_is_mount": root_mount,
        "child_mount": child_mount,
        "component_layout": component_layout,
    }


def _positions_found(metadata=None, users=0):
    return {
        "status": "observed",
        "volume_name": "o11y_journal-collector-state",
        "mountpoint": "/private/podman/volume/_data",
        "volume_use_count": users,
        "mount_count": users,
        "collector_present": bool(users),
        "project": "o11y",
        "metadata": metadata or _positions_state(),
    }


def test_positions_survey_returns_only_bounded_metadata_categories():
    report = POSITIONS.survey(
        lambda: _positions_found(_positions_state(owner=(9001, 9001), children=[
            {"kind": "file", "uid": 0, "gid": 0, "nlink": 1, "mode": 0o600}
        ]))
    )

    assert report == {
        "status": "observed",
        "volume_use_count": 0,
        "collector_present": False,
        "entry_count": 1,
        "root_owner": "mismatch",
        "root_mode_access": "blocked",
        "acl": "absent",
        "mount": "clear",
        "children": "safe_regular_files",
    }
    assert "/private" not in json.dumps(report)
    assert "9001" not in json.dumps(report)


def test_positions_discovery_requires_exact_compose_labels_and_unused_volume(monkeypatch):
    volume = {
        "Name": "o11y_journal-collector-state",
        "Driver": "local",
        "Scope": "local",
        "Options": {},
        "Mountpoint": "/private/podman/volume/_data",
        "Labels": {
            "com.docker.compose.project": "o11y",
            "com.docker.compose.volume": "journal-collector-state",
        },
        "MountCount": 0,
        "NeedsChown": False,
        "NeedsCopyUp": False,
    }
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:3] == ["podman", "inspect", "--type"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {
                "com.docker.compose.project": "o11y", "com.docker.compose.service": "alloy"
            }}}]))
        if argv[:3] == ["podman", "volume", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError("unexpected discovery command")

    monkeypatch.setattr(POSITIONS, "_inspect_metadata", lambda path, run: _positions_state(owner=(0, 0)))
    found = POSITIONS.discover(run=run)
    assert found["status"] == "observed"
    assert found["volume_use_count"] == 0
    assert all(argv[0:2] != ["podman", "run"] for argv in calls)
    assert all("volume create" not in " ".join(argv) and "chown" not in " ".join(argv) for argv in calls)

    volume["Labels"].pop("com.docker.compose.volume")
    assert POSITIONS.discover(run=run)["status"] == "volume_missing"


@pytest.mark.parametrize(
    "field,value",
    [("NeedsChown", True), ("NeedsCopyUp", True), ("NeedsChown", None), ("NeedsCopyUp", None)],
)
def test_positions_discovery_refuses_volume_initialization_flags(field, value, monkeypatch):
    volume = {
        "Name": "o11y_journal-collector-state",
        "Driver": "local",
        "Scope": "local",
        "Options": {},
        "Mountpoint": "/private/podman/volume/_data",
        "Labels": {
            "com.docker.compose.project": "o11y",
            "com.docker.compose.volume": "journal-collector-state",
        },
        "MountCount": 0,
        "NeedsChown": False,
        "NeedsCopyUp": False,
    }
    if value is None:
        volume.pop(field)
    else:
        volume[field] = value

    def run(argv, **_kwargs):
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:3] == ["podman", "inspect", "--type"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {
                "com.docker.compose.project": "o11y", "com.docker.compose.service": "alloy"
            }}}]))
        if argv[:3] == ["podman", "volume", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError("initialization refusal must happen before a container mount")

    monkeypatch.setattr(POSITIONS, "_inspect_metadata", lambda path, run: _positions_state(owner=(0, 0)))
    assert POSITIONS.discover(run=run)["status"] == "volume_initialization_unverified"


@pytest.mark.parametrize("source_matches", [True, False])
def test_positions_discovery_verifies_live_collector_volume_source(source_matches, monkeypatch):
    volume = {
        "Name": "o11y_journal-collector-state", "Driver": "local", "Scope": "local",
        "Options": {}, "Mountpoint": "/private/podman/volume/_data", "MountCount": 1,
        "NeedsChown": False, "NeedsCopyUp": False,
        "Labels": {"com.docker.compose.project": "o11y", "com.docker.compose.volume": "journal-collector-state"},
    }

    def run(argv, **_kwargs):
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:3] == ["podman", "inspect", "--type"] and argv[-1] == POSITIONS.RECEIVER:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {
                "com.docker.compose.project": "o11y", "com.docker.compose.service": "alloy"
            }}}]))
        if argv[:3] == ["podman", "volume", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Names": [POSITIONS.COLLECTOR]}]))
        if argv[:3] == ["podman", "inspect", "--type"] and argv[-1] == POSITIONS.COLLECTOR:
            mount_source = volume["Mountpoint"] if source_matches else "/private/other-volume/_data"
            return SimpleNamespace(returncode=0, stdout=json.dumps([{
                "Mounts": [{
                    "Destination": POSITIONS.DATA_PATH, "Name": volume["Name"], "Type": "volume",
                    "Source": mount_source, "RW": True,
                }],
                "Config": {"User": "0:0"},
            }]))
        raise AssertionError("unexpected live positions discovery command")

    monkeypatch.setattr(POSITIONS, "_inspect_metadata", lambda _path, run: _positions_state(owner=(0, 0)))
    assert POSITIONS.discover(run=run)["status"] == (
        "observed" if source_matches else "collector_mount_unverified"
    )


def test_positions_repair_is_idempotent_and_refuses_volume_in_use():
    already = _positions_found(_positions_state(owner=(0, 0)))
    assert POSITIONS.repair_positions(lambda: already, lambda _found: pytest.fail("must not chown")) == {
        "status": "already_correct", "reason": "owner_matches"
    }
    in_use = _positions_found(users=1)
    assert POSITIONS.repair_positions(lambda: in_use, lambda _found: pytest.fail("must not chown")) == {
        "status": "refused", "reason": "volume_in_use"
    }


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (_positions_state(acl=True), "metadata_ambiguous"),
        (_positions_state(root_mount=True), "metadata_ambiguous"),
        (_positions_state(children=[{"kind": "symlink", "uid": 0, "gid": 0, "nlink": 1}]), "metadata_ambiguous"),
        (_positions_state(mode=0o500), "unsupported_ownership_state"),
        (
            _positions_state(children=[{"kind": "file", "uid": 8, "gid": 0, "nlink": 1, "mode": 0o600}]),
            "metadata_ambiguous",
        ),
    ],
)
def test_positions_repair_refuses_ambiguous_or_unsupported_state(metadata, reason):
    assert POSITIONS.repair_positions(
        lambda: _positions_found(metadata),
        lambda _found: pytest.fail("refusal must precede mutation"),
    ) == {"status": "refused", "reason": reason}


def test_positions_repair_rechecks_fresh_evidence_before_chown_and_verifies_access():
    original = _positions_found(_positions_state(owner=(88, 88), mode=0o700))
    changed = _positions_found(_positions_state(owner=(0, 0), mode=0o700))
    observations = iter((original, original, changed, changed))
    calls = []
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda found: calls.append(("chown", found["volume_name"])) or True,
        lambda name: calls.append(("access-test", name)) or True,
        image_available_fn=lambda: True,
    )

    assert result == {"status": "repaired", "reason": "verified"}
    assert calls == [
        ("chown", "o11y_journal-collector-state"),
        ("access-test", "o11y_journal-collector-state"),
    ]


def test_positions_repair_stops_when_fresh_metadata_changes():
    original = _positions_found(_positions_state(owner=(88, 88)))
    changed = _positions_found(_positions_state(owner=(88, 88), mode=0o710))
    observations = iter((original, changed))
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda _found: pytest.fail("stale survey must not chown"),
        image_available_fn=lambda: True,
    )
    assert result == {"status": "refused", "reason": "evidence_changed"}


def test_positions_repair_refuses_before_chown_when_probe_image_is_not_cached():
    found = _positions_found(_positions_state(owner=(88, 88)))
    assert POSITIONS.repair_positions(
        lambda: found,
        lambda _found: pytest.fail("an uncached image must refuse before mutation"),
        image_available_fn=lambda: False,
    ) == {"status": "refused", "reason": "probe_unavailable"}


def test_positions_repair_restores_original_root_owner_after_failed_access_test():
    original = _positions_found(_positions_state(owner=(88, 88), mode=0o700))
    corrected = _positions_found(_positions_state(owner=(0, 0), mode=0o700))
    observations = iter((original, original, corrected, corrected, corrected, original))
    restored = []
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda _found: True,
        lambda _name: False,
        restore_fn=lambda _found, uid, gid: restored.append((uid, gid)) or True,
        image_available_fn=lambda: True,
    )
    assert result == {"status": "refused", "reason": "access_test_failed"}
    assert restored == [(88, 88)]


def test_positions_repair_restores_when_change_command_reports_failure_after_mutating():
    original = _positions_found(_positions_state(owner=(88, 88), mode=0o700))
    corrected = _positions_found(_positions_state(owner=(0, 0), mode=0o700))
    observations = iter((original, original, corrected, original))
    restored = []
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda _found: False,
        restore_fn=lambda _found, uid, gid: restored.append((uid, gid)) or True,
        image_available_fn=lambda: True,
    )
    assert result == {"status": "refused", "reason": "root_change_not_verified"}
    assert restored == [(88, 88)]


def test_positions_repair_reports_uncertain_when_post_mutation_identity_cannot_be_read():
    original = _positions_found(_positions_state(owner=(88, 88), mode=0o700))
    observations = iter((original, original, {"status": "unavailable"}))
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda _found: False,
        restore_fn=lambda *_args: pytest.fail("must not mutate when current identity is unknown"),
        image_available_fn=lambda: True,
    )
    assert result == {"status": "uncertain", "reason": "rollback_identity_unverified"}


def test_positions_repair_does_not_restore_an_unexpected_post_mutation_owner():
    original = _positions_found(_positions_state(owner=(88, 88), mode=0o700))
    unexpected = _positions_found(_positions_state(owner=(77, 77), mode=0o700))
    observations = iter((original, original, unexpected))
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda _found: False,
        restore_fn=lambda *_args: pytest.fail("unexpected ownership must not be overwritten"),
        image_available_fn=lambda: True,
    )
    assert result == {"status": "uncertain", "reason": "rollback_owner_unexpected"}


def test_positions_repair_reports_uncertain_when_restore_readback_fails():
    original = _positions_found(_positions_state(owner=(88, 88), mode=0o700))
    corrected = _positions_found(_positions_state(owner=(0, 0), mode=0o700))
    observations = iter((original, original, corrected, corrected, corrected, corrected))
    restored = []
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda _found: True,
        lambda _name: False,
        restore_fn=lambda _found, uid, gid: restored.append((uid, gid)) or False,
        image_available_fn=lambda: True,
    )
    assert result == {"status": "uncertain", "reason": "rollback_unverified"}
    assert restored == [(88, 88)]


def test_positions_verify_returns_ready_and_matches_successful_playbook_contract():
    ready = POSITIONS.verify(lambda: _positions_found(_positions_state(owner=(0, 0))))
    assert ready["status"] == "ready"
    assert ready["reason"] == "positions_identity_verified"

    source = PLAYBOOK.read_text()
    assert "(_journal_positions_gate.stdout | default('{}') | from_json).status == 'ready'" in source
    assert "(_journal_positions_pre_mount.stdout | default('{}') | from_json).status == 'ready'" in source
    assert '"children": "alloy_component_layout"' in POSITIONS_HELPER.read_text()


def test_positions_verify_accepts_the_bounded_journal_component_directory_but_repair_refuses_it():
    component = _positions_found(_positions_state(owner=(0, 0), children=[{
        "name": "component-hash", "kind": "dir", "uid": 0, "gid": 0,
        "mode": 0o700, "nlink": 2, "dev": 7, "ino": 12,
    }]))

    ready = POSITIONS.verify(lambda: component)
    assert ready["status"] == "ready"
    assert ready["children"] == "alloy_component_layout"
    assert POSITIONS.repair_positions(
        lambda: component,
        lambda _found: pytest.fail("component directories must never be repaired"),
    ) == {"status": "refused", "reason": "metadata_ambiguous"}


@pytest.mark.parametrize(
    "metadata",
    [
        _positions_state(owner=(0, 0), acl=True),
        _positions_state(owner=(0, 0), root_mount=True),
        _positions_state(owner=(0, 0), child_mount=True),
        _positions_state(owner=(0, 0), component_layout=False, children=[{
            "name": "symlink-hash", "kind": "symlink", "uid": 0, "gid": 0,
            "mode": 0o777, "nlink": 1, "dev": 7, "ino": 12,
        }]),
    ],
)
def test_positions_verify_refuses_ambiguous_live_layout(metadata):
    assert POSITIONS.verify(lambda: _positions_found(metadata)) == {
        "status": "refused", "reason": "metadata_ambiguous"
    }


def test_embedded_positions_metadata_retries_only_the_expected_rename_race(tmp_path):
    def inspect(component, disappearance=None):
        root = component.parent
        mountpoint = root.parent
        temp = component / ".positions.yml123456789"
        positions = component / "positions.yml"
        prelude = f'''import builtins,io,os,types
original_open=builtins.open
def fake_open(path,*args,**kwargs):
    if path=="/proc/self/mountinfo":
        return io.StringIO("1 0 0:1 / {mountpoint} rw - testfs /dev/test rw\\n")
    return original_open(path,*args,**kwargs)
builtins.open=fake_open
os.listxattr=lambda *_args,**_kwargs: []
original_lstat=os.lstat
disappeared={{"done":False}}
def fake_lstat(path):
    if path=={str(temp)!r} and {disappearance!r}:
        if {disappearance!r}=="always" or not disappeared["done"]:
            disappeared["done"]=True
            if {disappearance!r}=="once": os.rename({str(temp)!r},{str(positions)!r})
            raise FileNotFoundError(path)
    if path=={str(positions)!r} and {disappearance!r}=="unexpected":
        raise FileNotFoundError(path)
    st=original_lstat(path)
    if path in ({str(component)!r},{str(temp)!r},{str(positions)!r}):
        return types.SimpleNamespace(st_mode=st.st_mode,st_uid=0,st_gid=0,
            st_nlink=2 if path=={str(component)!r} else st.st_nlink,
            st_dev=st.st_dev,st_ino=st.st_ino)
    return st
os.lstat=fake_lstat
exec({POSITIONS._METADATA_SCRIPT!r})
'''
        result = subprocess.run(
            [sys.executable, "-c", prelude, str(root)],
            check=False, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def make_component(name):
        root = tmp_path / name
        component = root / "loki.source.journal.o11y_alloy"
        component.mkdir(mode=0o700, parents=True)
        os.chmod(root, 0o700)
        os.chmod(component, 0o700)
        return component

    component = make_component("race")
    temp = component / ".positions.yml123456789"
    with temp.open("w") as writer:
        writer.write("partial positions")
        writer.flush()
        os.chmod(temp, 0o600)
        accepted = inspect(component, disappearance="once")
        assert accepted["status"] == "observed"
        assert accepted["component_layout"] is True, json.dumps(accepted, indent=2)
        assert accepted["root_is_mount"] is False
        assert accepted["child_mount"] is False
        assert accepted["acl"] is False
    assert (component / "positions.yml").read_text() == "partial positions"

    for name, disappearance, expected in (
        ("persistent", "always", "unavailable"),
        ("unexpected", "unexpected", "unavailable"),
    ):
        component = make_component(name)
        temp = component / ".positions.yml123456789"
        if disappearance == "unexpected":
            (component / "positions.yml").write_text("unexpected")
            os.chmod(component / "positions.yml", 0o600)
        else:
            temp.write_text("pending")
            os.chmod(temp, 0o600)
        report = inspect(component, disappearance=disappearance)
        assert report["status"] == expected, (name, report)

    component = make_component("pending")
    temp = component / ".positions.yml123456789"
    temp.write_text("still pending")
    os.chmod(temp, 0o600)
    refused = inspect(component)
    assert refused["status"] == "observed"
    assert refused["component_layout"] is False

    component = make_component("multiple")
    for suffix in ("123", "456"):
        temp = component / f".positions.yml{suffix}"
        temp.write_text("pending")
        os.chmod(temp, 0o600)
    multiple = inspect(component)
    assert multiple["status"] == "observed"
    assert multiple["component_layout"] is False


def test_embedded_positions_metadata_refuses_public_files_and_writable_directories(tmp_path):
    def inspect(component):
        root = component.parent
        mountpoint = root.parent
        prelude = f'''import builtins,io,os
original_open=builtins.open
def fake_open(path,*args,**kwargs):
    if path=="/proc/self/mountinfo":
        return io.StringIO("1 0 0:1 / {mountpoint} rw - testfs /dev/test rw\\n")
    return original_open(path,*args,**kwargs)
builtins.open=fake_open
os.listxattr=lambda *_args,**_kwargs: []
exec({POSITIONS._METADATA_SCRIPT!r})
'''
        result = subprocess.run(
            [sys.executable, "-c", prelude, str(root)],
            check=False, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    for name, target, mode in (
        ("positions-0644", "positions", 0o644),
        ("temp-0644", "temp", 0o644),
        ("root-0777", "root", 0o777),
        ("component-0777", "component", 0o777),
    ):
        root = tmp_path / name
        component = root / "loki.source.journal.o11y_alloy"
        component.mkdir(mode=0o700, parents=True)
        os.chmod(root, 0o700)
        os.chmod(component, 0o700)
        if target == "root":
            os.chmod(root, mode)
        elif target == "component":
            os.chmod(component, mode)
        else:
            file = component / ("positions.yml" if target == "positions" else ".positions.yml123")
            file.write_text("positions")
            os.chmod(file, mode)
        report = inspect(component)
        assert report["status"] == "observed"
        assert report["component_layout"] is False, f"{name}: {report}"


def test_positions_access_test_uses_isolated_collector_restrictions_and_cleans_container():
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[:3] == ["podman", "run", "--pull=never"]:
            return SimpleNamespace(returncode=0, stdout="")
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError("unexpected access-test command")

    assert POSITIONS._access_test("o11y_journal-collector-state", run=run)
    probe = calls[0]
    assert probe[probe.index("--entrypoint") + 1] == "/bin/bash"
    assert "--network" in probe and probe[probe.index("--network") + 1] == "none"
    assert "--read-only" in probe and "--cap-drop" in probe and probe[probe.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in probe and "no-new-privileges" in probe
    assert "--user" in probe and probe[probe.index("--user") + 1] == "0:0"
    script = probe[-1]
    assert all(step in script for step in ("printf x", "mv --", "rm --", "trap"))
    assert "--pull=always" not in probe


def test_positions_helper_mutates_only_root_and_fails_closed_on_inner_recheck():
    assert "os.chown(root_fd,target_uid,target_gid)" in POSITIONS_HELPER.read_text()
    assert "os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)" in POSITIONS_HELPER.read_text()
    assert "os.fstat(root_fd)" in POSITIONS_HELPER.read_text()
    assert "str(target_uid), str(target_gid)" in POSITIONS_HELPER.read_text()
    assert "chown -R" not in POSITIONS_HELPER.read_text()
    assert "chmod" not in POSITIONS_HELPER.read_text()
    assert "listxattr" in POSITIONS_HELPER.read_text()
    assert "os.listdir(root_fd)" in POSITIONS_HELPER.read_text()
    assert "open(os.path.join" not in POSITIONS_HELPER.read_text()


def test_positions_chown_pins_the_checked_inode_when_mountpoint_path_is_replaced(tmp_path):
    root = tmp_path / "positions"
    moved = tmp_path / "pinned-positions"
    root.mkdir(mode=0o700)
    (root / "positions.db").write_text("not-read")
    stat = root.stat()
    child = (root / "positions.db").stat()
    metadata = _positions_state(owner=(stat.st_uid, stat.st_gid), mode=0o700, children=[{
        "name": __import__("hashlib").sha256(b"positions.db").hexdigest(),
        "kind": "file", "uid": child.st_uid, "gid": child.st_gid,
        "mode": child.st_mode & 0o777, "nlink": child.st_nlink,
        "dev": child.st_dev, "ino": child.st_ino,
    }])
    metadata["root"].update(dev=stat.st_dev, ino=stat.st_ino)
    expected = json.dumps(metadata)
    prelude = f'''import builtins,io,os
original_open=os.open
original_builtin_open=builtins.open
triggered={{"value":False}}
def fake_open(path,*args,**kwargs):
    if path.startswith("/proc/self/fdinfo/"):
        return io.StringIO("mnt_id:\\t1\\n")
    if path=="/proc/self/mountinfo":
        return io.StringIO("1 0 0:1 / / rw - testfs /dev/test rw\\n")
    return original_builtin_open(path,*args,**kwargs)
builtins.open=fake_open
os.listxattr=lambda *_args,**_kwargs: []
def race_open(path,flags,*args,**kwargs):
    fd=original_open(path,flags,*args,**kwargs)
    if path=={str(root)!r} and not triggered["value"]:
        os.rename(path,{str(moved)!r})
        os.mkdir(path)
        triggered["value"]=True
    return fd
os.open=race_open
'''
    script = prelude + "exec(" + repr(POSITIONS._CHOWN_SCRIPT) + ")"
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), expected, str(stat.st_uid), str(stat.st_gid)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "changed"}
    assert moved.stat().st_ino == stat.st_ino
    assert root.stat().st_ino != stat.st_ino
    assert (root / "positions.db").exists() is False


def test_journal_probe_reports_only_a_sanitized_exact_target_count():
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "CONTAINER_NAME": ["o11y-alloy"],
                    "_SYSTEMD_UNIT": "private-unit.service",
                    "MESSAGE": "private log message",
                    "CONTAINER_ID_FULL": "private-id",
                }
            ),
        )
    )

    report = PROBE.probe("/var/log/journal", run=runner)

    assert report == {"status": "observed", "matching_entries": 1}
    assert "private" not in json.dumps(report)
    argv = runner.call_args.args[0]
    assert "--output-fields=CONTAINER_NAME" in argv
    assert argv[-1] == "CONTAINER_NAME=o11y-alloy"


def test_journal_probe_fails_closed_for_unmatched_or_malformed_entries():
    unmatched = Mock(
        return_value=SimpleNamespace(returncode=0, stdout='{"CONTAINER_NAME":"other"}\n')
    )
    malformed = Mock(return_value=SimpleNamespace(returncode=0, stdout='not-json\n'))
    failed = Mock(return_value=SimpleNamespace(returncode=1, stdout=""))

    assert PROBE.probe("/var/log/journal", run=unmatched) == {
        "status": "unavailable",
        "matching_entries": 0,
    }
    assert PROBE.probe("/var/log/journal", run=malformed)["status"] == "unavailable"
    assert PROBE.probe("/var/log/journal", run=failed)["status"] == "unavailable"


def _journal_survey_runner(viable_paths, rootless="true"):
    def run(argv, **_kwargs):
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout=rootless)
        if argv[:3] == ["podman", "image", "exists"]:
            assert argv[3] == "docker.io/grafana/alloy:v1.5.1"
            return SimpleNamespace(returncode=0, stdout="")
        if argv[0] == "journalctl":
            directory = argv[1].split("=", 1)[1]
            stdout = '{"CONTAINER_NAME":"o11y-alloy","MESSAGE":"private"}\n'
            return SimpleNamespace(returncode=0 if directory in viable_paths else 1, stdout=stdout)
        if argv[:2] == ["podman", "run"]:
            directory = next(arg.split("src=", 1)[1].split(",", 1)[0] for arg in argv if arg.startswith("type=bind,"))
            return SimpleNamespace(returncode=0 if directory in viable_paths else 1, stdout="")
        raise AssertionError("unexpected survey command")

    return run


def test_standard_journal_survey_reports_only_single_ambiguous_or_none():
    fixed = {"/var/log/journal", "/run/log/journal"}

    def isdir(path):
        return path in fixed

    def access(path, _mode):
        return path in fixed

    one = SURVEY.survey(
        run=_journal_survey_runner({"/var/log/journal"}), isdir=isdir, access=access
    )
    both = SURVEY.survey(
        run=_journal_survey_runner(fixed), isdir=isdir, access=access
    )
    none = SURVEY.survey(
        run=_journal_survey_runner(fixed, rootless="false"), isdir=isdir, access=access
    )

    assert one == {"result": "/var/log/journal"}
    assert both == {"result": "ambiguous"}
    assert none == {"result": "none"}
    assert "private" not in json.dumps(one)


def test_survey_file_probe_uses_rootless_uid_zero_and_never_pulls():
    calls = []
    runner = _journal_survey_runner({"/var/log/journal"})

    def capture_run(argv, **kwargs):
        calls.append(argv)
        return runner(argv, **kwargs)

    report = SURVEY.survey(
        run=capture_run,
        isdir=lambda path: path in SURVEY.DIRECTORIES,
        access=lambda path, _mode: path in SURVEY.DIRECTORIES,
    )

    probe = next(argv for argv in calls if argv[:2] == ["podman", "run"])
    assert report == {"result": "/var/log/journal"}
    assert probe[probe.index("--user") + 1] == "0:0"
    assert "--pull=never" in probe
    assert not any(argv[:2] == ["podman", "pull"] for argv in calls)


def test_standard_journal_survey_uses_only_fixed_paths_and_cached_probe_image():
    source = SURVEY_HELPER.read_text()

    assert 'DIRECTORIES = ("/var/log/journal", "/run/log/journal")' in source
    assert '"--pull=never"' in source
    assert 'IMAGE = "docker.io/grafana/alloy:v1.5.1"' in source
    assert '"--user",\n            "0:0"' in source
    assert '"podman", "pull"' not in source
    assert '"--output-fields=CONTAINER_NAME"' in source
    assert '"--output-fields=CONTAINER_NAME,MESSAGE"' in source
    assert '"--since=-24h"' in source
    assert 'f"--lines={MAX_DIAGNOSTIC_ENTRIES}"' in source


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("permission denied reading /var/log/journal/private", "journal"),
        ("permission denied writing /var/lib/alloy/data/private", "positions"),
        ("permission denied opening /etc/alloy/journal.alloy", "config"),
        ("permission denied opening /private/path", "other"),
        ("collector started", "none"),
    ],
)
def test_permission_diagnostic_emits_only_fixed_target_category(message, expected):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"CONTAINER_NAME": "o11y-journal-collector", "MESSAGE": message}
            ),
            stderr="private error at 203.0.113.7 token=secret",
        )

    report = SURVEY.permission_diagnostic(run=run)

    assert report == {"entry_count": 1, "permission_denied_target": expected}
    assert not any(
        private in json.dumps(report)
        for private in (message, "/var/log/journal", "/private/path", "203.0.113.7", "secret")
    )
    assert calls == [
        [
            "journalctl",
            "--no-pager",
            "--quiet",
            "--output=json",
            "--output-fields=CONTAINER_NAME,MESSAGE",
            "--since=-24h",
            "--lines=80",
            "CONTAINER_NAME=o11y-journal-collector",
        ]
    ]


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json\n",
        '{"CONTAINER_NAME":"other","MESSAGE":"permission denied /private/path"}\n',
        '{"CONTAINER_NAME":"o11y-journal-collector","MESSAGE":3}\n',
        '{"CONTAINER_NAME":"o11y-journal-collector"}\n',
        (
            '{"CONTAINER_NAME":"other","CONTAINER_NAME":"o11y-journal-collector",'
            '"MESSAGE":"permission denied /private/path token=secret"}\n'
        ),
        (
            '{"CONTAINER_NAME":"o11y-journal-collector","MESSAGE":"collector started",'
            '"MESSAGE":"permission denied /private/path token=secret"}\n'
        ),
    ],
)
def test_permission_diagnostic_refuses_malformed_or_wrong_identity(stdout):
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr="raw error"))

    report = SURVEY.permission_diagnostic(run=runner)
    assert report == {
        "entry_count": 0,
        "permission_denied_target": "unavailable",
    }
    assert "private/path" not in json.dumps(report)
    assert "secret" not in json.dumps(report)


def test_permission_diagnostic_caps_records_and_hides_command_errors():
    too_many = "\n".join(
        json.dumps({"CONTAINER_NAME": "o11y-journal-collector", "MESSAGE": "private"})
        for _ in range(81)
    )
    over_limit = Mock(return_value=SimpleNamespace(returncode=0, stdout=too_many, stderr=""))
    denied = Mock(
        return_value=SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="permission denied /private/path token=secret",
        )
    )

    expected = {"entry_count": 0, "permission_denied_target": "unavailable"}
    assert SURVEY.permission_diagnostic(run=over_limit) == expected
    assert SURVEY.permission_diagnostic(run=denied) == expected
    assert "private" not in json.dumps(expected)
    assert "secret" not in json.dumps(expected)


def test_permission_diagnostic_conflicting_targets_collapse_to_other():
    entries = [
        {"CONTAINER_NAME": "o11y-journal-collector", "MESSAGE": "permission denied /var/log/journal/a"},
        {"CONTAINER_NAME": "o11y-journal-collector", "MESSAGE": "permission denied /etc/alloy/journal.alloy"},
    ]
    runner = Mock(
        return_value=SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, entries)), stderr="")
    )

    assert SURVEY.permission_diagnostic(run=runner) == {
        "entry_count": 2,
        "permission_denied_target": "other",
    }


def test_survey_cli_reports_only_allowlisted_diagnostic_fields(monkeypatch, capsys):
    monkeypatch.setattr(SURVEY, "survey", lambda: {"result": "ambiguous"})
    monkeypatch.setattr(
        SURVEY,
        "permission_diagnostic",
        lambda: {"entry_count": 4, "permission_denied_target": "journal"},
    )

    SURVEY.main()

    assert json.loads(capsys.readouterr().out) == {
        "result": "ambiguous",
        "entry_count": 4,
        "permission_denied_target": "journal",
    }


def test_standard_journal_survey_refuses_when_cached_probe_image_is_unavailable():
    calls = []

    def missing_image_run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:3] == ["podman", "image", "exists"]:
            return SimpleNamespace(returncode=1, stdout="private podman diagnostic")
        raise AssertionError("survey must not pull or run an unavailable probe image")

    report = SURVEY.survey(
        run=missing_image_run,
        isdir=lambda path: path in SURVEY.DIRECTORIES,
        access=lambda path, _mode: path in SURVEY.DIRECTORIES,
    )

    assert report == {"result": "probe_unavailable"}
    assert "private" not in json.dumps(report)
    assert not any(argv[:2] == ["podman", "pull"] for argv in calls)
    assert not any(argv[:2] == ["podman", "run"] for argv in calls)


def test_compose_mount_is_read_only_and_state_is_separate():
    compose = yaml.safe_load((O11Y / "compose.journal.yml").read_text())
    service = compose["services"]["journal-collector"]

    assert service["image"] == "docker.io/grafana/alloy:v1.9.2"
    assert service["restart"] == "always"
    assert service["read_only"] is True
    assert service["user"] == "0:0"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert "ports" not in service
    assert "privileged" not in service
    assert "network_mode" not in service
    assert "depends_on" not in service
    assert not any("docker.sock" in str(volume) for volume in service["volumes"])
    journal = next(volume for volume in service["volumes"] if isinstance(volume, dict))
    assert journal["target"] == "/var/log/journal"
    assert journal["read_only"] is True
    assert journal["bind"]["create_host_path"] is False
    assert "journal-collector-state:/var/lib/alloy/data" in service["volumes"]
    assert "journal-collector-state" in compose["volumes"]


def test_alloy_config_uses_only_the_fixed_bounded_selector_and_otlp():
    config = (O11Y / "templates/journal.alloy.j2").read_text()

    assert 'matches    = "CONTAINER_NAME=o11y-alloy"' in config
    assert 'max_age    = "15m"' in config
    assert 'labels     = { service = "o11y/alloy", signal = "container" }' in config
    assert "otelcol.exporter.otlp" in config
    assert "o11y_otlp_bind" in config
    assert "loki.write" not in config
    assert "__journal" not in config
    assert "CONTAINER_ID" not in config
    assert "MESSAGE" not in config


def test_playbook_guards_validation_delivery_and_non_destructive_rollback():
    source = PLAYBOOK.read_text()
    plays = yaml.safe_load(source)
    guard = source.index("import_playbook: refuse-internal-extra-vars.yml")
    reviewed = source.index("import_playbook: require-reviewed-checkout.yml")
    place = source.index("tasks/place-monorepo.yml")
    validate = source.index("Validate the candidate config")
    apply = source.index("Start only the journal collector service")
    verify = source.index("Verify the exact bounded journal stream reached Loki")
    stop = source.index("Force-remove only the failed collector while retaining positions")
    positions_gate = source.index("Verify positions volume identity and access before apply")

    assert guard < reviewed < place < positions_gate < validate < apply < verify < stop
    assert "groups.get('o11y_svc', []) | length == 1" in source
    assert "journal_collector_action | default('apply') in ['survey', 'repair-positions', 'apply', 'stop']" in source
    assert source.index("Verify positions volume identity and access before apply") < apply
    assert "check_mode_unverified" in source
    assert "no_log: true" in source[source.index("Verify positions volume identity and access before apply"):apply]
    apply_play = next(play for play in plays if play.get("hosts") == "o11y_svc")
    pre_apply_gate = next(
        task for task in apply_play["tasks"]
        if task.get("name") == "Verify positions volume identity and access before apply"
    )
    assert pre_apply_gate["ansible.builtin.command"]["argv"] == ["python3", "-", "verify"]
    assert "Require the actual runtime to be rootless" in source
    assert "read -r -N 1 _ < \"$file\"" in source
    assert "expected_repository_sha" in source
    assert "argv: [podman, rm, --force, --time, \"10\", o11y-journal-collector]" in source
    assert "podman rm -v" not in source
    assert "compose down" not in source
    assert "up, --no-deps, -d, journal-collector" in source
    assert "Require all existing receiver containers to remain unchanged" in source
    assert "_journal_verification_start" in source
    assert "values[0][0]" in source
    assert "State.Health.Status" in source
    assert "image: docker.io/grafana/alloy:v1.9.2" in (O11Y / "compose.journal.yml").read_text()
    assert "_journal_image: docker.io/grafana/alloy:v1.9.2" in source
    assert 'IMAGE = "docker.io/grafana/alloy:v1.5.1"' in SURVEY_HELPER.read_text()

    survey_play = next(
        play for play in plays
        if play.get("name") == "Survey fixed standard journal directory candidates"
    )
    positions_survey = next(
        task for task in survey_play["tasks"]
        if task.get("name") == "Survey existing positions volume metadata without mounting it"
    )
    assert positions_survey["no_log"] is True
    assert positions_survey["when"] == "not ansible_check_mode"
    assert not any("volume create" in str(task) or "chown" in str(task) for task in survey_play["tasks"])

    repair_play = next(
        play for play in plays
        if play.get("name") == "Repair only the journal positions volume root ownership"
    )
    repair_task = next(
        task for task in repair_play["tasks"]
        if task.get("name") == "Run the separately selected guarded positions repair"
    )
    assert repair_task["no_log"] is True
    assert repair_task["when"] == "not ansible_check_mode"
    assert repair_task["ansible.builtin.command"]["argv"] == ["python3", "-", "repair-positions"]

    apply_block = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Apply the collector and require exact-target Loki delivery"
    )
    assert apply_block["when"] == "not ansible_check_mode"
    apply_task_names = [task.get("name") for task in apply_block["block"]]
    before_mount = apply_task_names.index("Recheck positions volume initialization flags immediately before mount")
    start_collector = apply_task_names.index("Start only the journal collector service")
    assert before_mount < start_collector
    assert apply_task_names.index("Start only the journal collector service") < apply_task_names.index(
        "Verify the live collector positions mount and access"
    ) < apply_task_names.index("Read the Podman rootless UID mapping") < apply_task_names.index(
        "Verify effective write and rename access in the live collector namespace"
    )
    live_write_probe = next(
        task for task in apply_block["block"]
        if task.get("name") == "Verify effective write and rename access in the live collector namespace"
    )
    live_probe_script = " ".join(live_write_probe["ansible.builtin.command"]["argv"])
    assert "uid_map" in live_probe_script and "mv -n" in live_probe_script and "printf x" in live_probe_script
    assert "_journal_collector_uid_map.stdout | trim == _journal_host_uid_map.stdout | trim" in source
    image_exists = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Check whether the pinned Alloy validation image is already available"
    )
    image_pull = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Acquire the pinned Alloy validation image when it is not cached"
    )
    config_validation = next(
        task
        for task in apply_play["tasks"]
        if task.get("name", "").startswith("Validate the candidate config")
    )
    assert image_exists["when"] == "not ansible_check_mode"
    assert "not ansible_check_mode" in image_pull["when"]
    assert "_journal_image_exists.rc | default(1) != 0" in image_pull["when"]
    assert "--pull=never" in config_validation["ansible.builtin.command"]["argv"]
    loki_query = next(
        task
        for task in apply_block["block"]
        if task.get("name") == "Verify the exact bounded journal stream reached Loki"
    )
    assert loki_query["retries"] == 36
    assert loki_query["delay"] == 5
    assert "'&start='" in source  # keep freshness bound part of the Loki range request

    health_poll = next(task for task in apply_block["block"] if task.get("name") == "Read the collector state")
    assert health_poll["retries"] == 60
    assert health_poll["delay"] == 3

    placement = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Place the reviewed monorepo on the receiver through the shared mechanism"
    )
    revision = next(
        task for task in apply_play["tasks"] if task.get("name") == "Read the receiver checkout revision"
    )
    candidate_render = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Render a candidate journal config with the private receiver endpoint"
    )
    config_promote = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Promote the validated candidate config for the collector"
    )
    for task in (placement, revision, candidate_render, config_promote):
        assert task["when"] == "not ansible_check_mode"

    stop_play = next(
        play for play in plays if play.get("name", "").startswith("Stop only the journal collector")
    )
    stop_gate = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Leave the stop play when the reviewed action is survey or apply"
    )

    start_task = next(
        task
        for task in apply_block["block"]
        if task.get("name") == "Start only the journal collector service"
    )
    rollback_state = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Capture collector runtime state and automatic health results before rollback"
    )
    rollback_config = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Capture collector effective health schedule before rollback"
    )
    rollback_action = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Capture collector health failure action before rollback"
    )
    rollback_logs = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Capture at most 80 collector log lines before rollback"
    )
    rollback_failure = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Fail closed after stopping the journal collector"
    )
    rollback_classifier = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Classify collector health probes without exposing stderr"
    )
    assert "check_mode" not in start_task
    assert start_task["when"] == "not ansible_check_mode"
    evidence_formats = [
        " ".join(task["ansible.builtin.command"]["argv"])
        for task in (rollback_state, rollback_config, rollback_action)
    ]
    assert all(
        task["check_mode"] is False
        for task in (rollback_state, rollback_config, rollback_action)
    )
    assert ".State.Health" in evidence_formats[0]
    assert ".RestartCount" in evidence_formats[0]
    assert ".FailingStreak" in evidence_formats[0]
    assert ".ExitCode" in evidence_formats[0]
    assert ".Output" not in " ".join(evidence_formats)
    assert ".Config.Healthcheck" in evidence_formats[1]
    assert ".Interval" in evidence_formats[1]
    assert ".Config.HealthcheckOnFailureAction" in evidence_formats[2]
    assert rollback_logs["ansible.builtin.command"]["argv"] == [
        "podman",
        "logs",
        "--tail",
        "80",
        "o11y-journal-collector",
    ]
    assert rollback_logs["no_log"] is True
    assert rollback_logs["failed_when"] is False
    assert rollback_logs["check_mode"] is False
    failure_message = rollback_failure["ansible.builtin.fail"]["msg"]
    classifications = rollback_classifier["ansible.builtin.set_fact"]
    assert "container_missing" in classifications["_journal_rollback_state_class"]
    assert "permission_denied" in classifications["_journal_rollback_state_class"]
    assert "state_probe_failed" in classifications["_journal_rollback_state_class"]
    assert "config_probe_failed" in classifications["_journal_rollback_health_config_class"]
    assert "healthcheck_absent" in classifications["_journal_rollback_health_config_class"]
    assert "action_probe_failed" in classifications["_journal_rollback_health_action_class"]
    assert "action_unknown" in classifications["_journal_rollback_health_action_class"]
    assert "permission_denied" in classifications["_journal_rollback_log_class"]
    assert "config_error" in classifications["_journal_rollback_log_class"]
    assert "missing_file" in classifications["_journal_rollback_log_class"]
    assert "connection_refused" in classifications["_journal_rollback_log_class"]
    assert "other" in classifications["_journal_rollback_log_class"]
    assert "unavailable" in classifications["_journal_rollback_log_class"]
    assert "empty" in classifications["_journal_rollback_log_class"]
    assert "_journal_rollback_logs.stdout" not in failure_message
    assert "collector logs classification/count=" in failure_message
    assert "state/config/action classification=" in failure_message
    assert "Podman state/config/action rc=" in failure_message
    assert "_journal_rollback_state_evidence.stdout" in failure_message
    assert "_journal_rollback_health_config.stdout" in failure_message
    assert "_journal_rollback_health_action.stdout" in failure_message
    assert ".stderr" not in failure_message
    assert "startup_check" not in failure_message
    rollback_remove = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Force-remove only the failed collector while retaining positions"
    )
    rollback_volume_capture = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Capture the positions volume name before rollback"
    )
    rollback_volume_readback = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Read back the journal collector positions volume after rollback"
    )
    rollback_readback = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Read back the collector after delivery rollback"
    )
    assert rollback_remove["ansible.builtin.command"]["argv"] == [
        "podman",
        "rm",
        "--force",
        "--time",
        "10",
        "o11y-journal-collector",
    ]
    assert "when" not in rollback_remove
    assert all(
        apply_block["rescue"].index(task) < apply_block["rescue"].index(rollback_volume_capture)
        for task in (rollback_state, rollback_logs, rollback_config, rollback_action)
    )
    assert apply_block["rescue"].index(rollback_volume_capture) < apply_block["rescue"].index(rollback_remove)
    assert apply_block["rescue"].index(rollback_remove) < apply_block["rescue"].index(rollback_volume_readback)
    assert ".Name" in " ".join(rollback_volume_capture["ansible.builtin.command"]["argv"])
    assert "_journal_rollback_volume_name.stdout" in rollback_volume_readback["ansible.builtin.command"]["argv"][3]
    assert rollback_volume_readback["when"][0] == "_journal_rollback_volume_name.rc == 0"
    rollback_verify = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Require the failed pilot container to be absent after rollback"
    )
    assert rollback_readback["ansible.builtin.command"]["argv"][:3] == ["podman", "ps", "--all"]
    assert "_journal_rollback_readback.stdout | trim == ''" in rollback_verify["ansible.builtin.assert"]["that"]
    assert rollback_verify["when"] == "not ansible_check_mode"

    stop_remove = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Force-remove only the pilot container while preserving positions"
    )
    stop_readback = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Read back the stopped pilot container"
    )
    assert stop_remove["ansible.builtin.command"]["argv"] == [
        "podman",
        "rm",
        "--force",
        "--time",
        "10",
        "o11y-journal-collector",
    ]
    assert "not ansible_check_mode" in stop_remove["when"]
    stop_volume_capture = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Read the positions volume state before removing collector"
    )
    volume_readback = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Read back the positions volume after stop"
    )
    assert ".Name" in " ".join(stop_volume_capture["ansible.builtin.command"]["argv"])
    assert stop_play["tasks"].index(stop_gate) < stop_play["tasks"].index(stop_volume_capture)
    assert stop_play["tasks"].index(stop_volume_capture) < stop_play["tasks"].index(stop_remove)
    assert stop_play["tasks"].index(stop_remove) < stop_play["tasks"].index(volume_readback)
    assert stop_readback["ansible.builtin.command"]["argv"][:3] == ["podman", "ps", "--all"]
    assert stop_readback["when"] == "not ansible_check_mode"
    stop_verify = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Require the stopped pilot container to be absent across host reboots"
    )
    assert "_journal_stop_readback.stdout | trim == ''" in stop_verify["ansible.builtin.assert"]["that"]

    volume_verify = next(
        task
        for task in stop_play["tasks"]
        if task.get("name") == "Require the existing positions volume to survive stop"
    )
    assert volume_readback["ansible.builtin.command"]["argv"][:3] == [
        "podman",
        "volume",
        "exists",
    ]
    assert "_journal_stop_volume_name.stdout" in volume_readback["ansible.builtin.command"]["argv"][3]
    assert volume_verify["ansible.builtin.assert"]["that"] == [
        "_journal_stop_volume_name.stdout | default('') | trim | length > 0",
        "_journal_stop_volume_after.rc | default(1) == 0",
    ]

    rollback_volume = next(
        task
        for task in apply_block["rescue"]
        if task.get("name") == "Require the positions volume to survive collector rollback"
    )
    assert rollback_volume["ansible.builtin.assert"]["that"] == [
        "_journal_rollback_volume_name.stdout | default('') | trim | length > 0",
        "_journal_rollback_volume_readback.rc | default(1) == 0",
    ]


def test_survey_check_mode_skips_container_probe_and_reports_unverified():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    survey_play = next(
        play
        for play in plays
        if play.get("name") == "Survey fixed standard journal directory candidates"
    )
    survey_command = next(
        task
        for task in survey_play["tasks"]
        if task.get("name") == "Survey rootless journal path and exact-name file readability"
    )
    check_mode_result = next(
        task
        for task in survey_play["tasks"]
        if task.get("name") == "Record check-mode survey as unverified without probing the container runtime"
    )

    assert survey_command["when"] == "not ansible_check_mode"
    assert "check_mode" not in survey_command
    assert survey_play["become"] is False
    assert check_mode_result["when"] == "ansible_check_mode"
    assert check_mode_result["ansible.builtin.set_fact"]["_journal_directory_survey"] == {
        "rc": 0,
        "stdout": '{"result":"check_mode_unverified","entry_count":0,"permission_denied_target":"unavailable"}',
    }
    assert "check_mode_unverified" in PLAYBOOK.read_text()

    report = next(
        task
        for task in survey_play["tasks"]
        if task.get("name") == "Report only the fixed journal source status"
    )
    template_environment = Environment()
    template_environment.filters["from_json"] = json.loads
    status_template = template_environment.from_string(report["ansible.builtin.debug"]["msg"])
    for result in (
        "/var/log/journal",
        "/run/log/journal",
        "ambiguous",
        "none",
        "probe_unavailable",
        "check_mode_unverified",
    ):
        rendered = status_template.render(
            _journal_directory_survey={"stdout": json.dumps({"result": result})}
        )
        assert "/var/log/journal" not in rendered
        assert "/run/log/journal" not in rendered

    diagnostic = next(
        task
        for task in survey_play["tasks"]
        if task.get("name") == "Report only the bounded collector journal diagnostic"
    )
    assert "entry_count=" in diagnostic["ansible.builtin.debug"]["msg"]
    assert "permission_denied_target=" in diagnostic["ansible.builtin.debug"]["msg"]
    assert ".MESSAGE" not in diagnostic["ansible.builtin.debug"]["msg"]


def test_positions_repair_check_mode_preserves_unverified_result_after_skipped_command():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    repair_play = next(
        play
        for play in plays
        if play.get("name") == "Repair only the journal positions volume root ownership"
    )
    tasks = repair_play["tasks"]
    repair_command = next(
        task for task in tasks
        if task.get("name") == "Run the separately selected guarded positions repair"
    )
    check_mode_result = next(
        task for task in tasks
        if task.get("name") == "Record positions repair as unverified in check mode"
    )
    assertion = next(
        task for task in tasks
        if task.get("name") == "Require repair completion or explicit check-mode unverified state"
    )

    assert tasks.index(repair_command) < tasks.index(check_mode_result) < tasks.index(assertion)
    assert repair_command["register"] == "_journal_positions_repair"
    assert repair_command["when"] == "not ansible_check_mode"
    assert check_mode_result["when"] == "ansible_check_mode"
    assert check_mode_result["ansible.builtin.set_fact"]["_journal_positions_repair"] == {
        "rc": 0,
        "stdout": '{"status":"check_mode_unverified","reason":"not_run"}',
    }
    assert assertion["ansible.builtin.assert"]["that"] == [
        "_journal_positions_repair.rc | default(1) == 0",
        "(_journal_positions_repair.stdout | default('{}') | from_json).status in ['repaired', 'already_correct', 'check_mode_unverified']",
    ]


def test_rollback_health_diagnostic_is_allowlisted_and_fixed_probe_is_gated():
    ansible_playbook = shutil.which("ansible-playbook")
    if not ansible_playbook:
        pytest.skip("ansible-playbook is unavailable")
    first_line = Path(ansible_playbook).read_text(errors="replace").splitlines()[0]
    ansible_python = first_line[2:].strip().split()[-1] if first_line.startswith("#!") else sys.executable
    state_cases = [
        {
            "rc": 125,
            "stderr": "Error: no container with name or ID found",
            "stdout": "",
            "expected": "container_missing",
        },
        {
            "rc": 125,
            "stderr": "permission denied while inspecting container",
            "stdout": "",
            "expected": "permission_denied",
        },
        {"rc": 1, "stderr": "unexpected inspect failure", "stdout": "", "expected": "state_probe_failed"},
        {"rc": 0, "stderr": "", "stdout": "", "expected": "empty_formatted_fields"},
        {
            "rc": 0,
            "stderr": "",
            "stdout": "state=running restart_count=0 health=starting failing_streak=0 log_count=0 exit_codes=",
            "expected": "fields_available",
        },
    ]
    config_cases = [
        {"rc": 125, "stdout": "", "expected": "config_probe_failed"},
        {"rc": 0, "stdout": "", "expected": "empty_formatted_fields"},
        {
            "rc": 0,
            "stdout": (
                "healthcheck_present=absent test_kind=none interval=unavailable timeout=unavailable "
                "retries=unavailable start_period=unavailable"
            ),
            "expected": "healthcheck_absent",
        },
        {
            "rc": 0,
            "stdout": (
                "healthcheck_present=present test_kind=CMD-SHELL interval=15s timeout=5s "
                "retries=8 start_period=20s"
            ),
            "expected": "fields_available",
        },
    ]
    action_cases = [
        {"rc": 125, "stdout": "", "expected": "action_probe_failed"},
        {"rc": 0, "stdout": "", "expected": "empty_formatted_fields"},
        {"rc": 0, "stdout": "on_failure=unknown", "expected": "action_unknown"},
        {"rc": 0, "stdout": "on_failure=none", "expected": "fields_available"},
    ]
    log_cases = [
        {"rc": 125, "stdout": "private stderr", "expected": "unavailable"},
        {"rc": 0, "stdout": "", "expected": "empty"},
        {"rc": 0, "stdout": "permission denied at /private/path token=secret", "expected": "permission_denied"},
        {"rc": 0, "stdout": "failed to load config /private/path token=secret", "expected": "config_error"},
        {"rc": 0, "stdout": "no such file or directory: /private/path", "expected": "missing_file"},
        {"rc": 0, "stdout": "connection refused to 203.0.113.7 header=Bearer secret", "expected": "connection_refused"},
        {"rc": 0, "stdout": "unrecognized startup issue /private/path token=secret", "expected": "other"},
        {
            "rc": 0,
            "stdout": "",
            "stderr": "failed to parse config /private/path token=secret",
            "expected": "config_error",
        },
    ]
    gate_cases = [
        {
            "state": {"rc": 0, "stdout": "state=running health=starting"},
            "config": {"rc": 0, "stdout": "healthcheck_present=present interval=15s"},
            "action": {"rc": 0, "stdout": "on_failure=none"},
            "expected": True,
        },
        {
            "state": {"rc": 0, "stdout": "state=running health=starting"},
            "config": {"rc": 0, "stdout": "healthcheck_present=present interval=15s"},
            "action": {"rc": 0, "stdout": "on_failure=restart"},
            "expected": False,
        },
        {
            "state": {"rc": 0, "stdout": "state=stopped health=starting"},
            "config": {"rc": 0, "stdout": "healthcheck_present=present interval=15s"},
            "action": {"rc": 0, "stdout": "on_failure=none"},
            "expected": False,
        },
        {
            "state": {"rc": 0, "stdout": "state=running health=starting"},
            "config": {"rc": 0, "stdout": "healthcheck_present=absent interval=unavailable"},
            "action": {"rc": 0, "stdout": "on_failure=none"},
            "expected": False,
        },
        {
            "state": {"rc": 0, "stdout": "state=running health=starting"},
            "config": {"rc": 125, "stdout": ""},
            "action": {"rc": 0, "stdout": "on_failure=none"},
            "expected": False,
        },
        {
            "state": {"rc": 0, "stdout": "state=running health=starting"},
            "config": {"rc": 0, "stdout": "healthcheck_present=present interval=15s"},
            "action": {"rc": 125, "stdout": ""},
            "expected": False,
        },
    ]
    readiness_cases = [
        {"allowed": True, "rc": 0, "expected": "passed"},
        {"allowed": True, "rc": 1, "expected": "failed"},
        {"allowed": True, "rc": 125, "expected": "probe_error"},
        {"allowed": True, "rc": 137, "expected": "timeout"},
        {"allowed": True, "rc": 42, "expected": "unexpected_error"},
        {"allowed": False, "expected": "not_run"},
    ]
    harness = r'''
import json, sys, yaml
from ansible.template import Templar, trust_as_template
from ansible.parsing.dataloader import DataLoader
plays = yaml.safe_load(open(sys.argv[1]))
tasks = next(play for play in plays if play.get("name") == "Deploy the bounded journal collector")["tasks"]
apply = next(task for task in tasks if task.get("name") == "Apply the collector and require exact-target Loki delivery")
state = next(
    task for task in apply["rescue"]
    if task.get("name") == "Capture collector runtime state and automatic health results before rollback"
)
config = next(
    task for task in apply["rescue"]
    if task.get("name") == "Capture collector effective health schedule before rollback"
)
action = next(
    task for task in apply["rescue"]
    if task.get("name") == "Capture collector health failure action before rollback"
)
logs = next(
    task for task in apply["rescue"]
    if task.get("name") == "Capture at most 80 collector log lines before rollback"
)
classifier = next(
    task for task in apply["rescue"]
    if task.get("name") == "Classify collector health probes without exposing stderr"
)
readiness_probe = next(
    task for task in apply["rescue"]
    if task.get("name") == "Run one fixed output-discarded readiness probe for bounded diagnosis"
)
readiness_classifier = next(
    task for task in apply["rescue"]
    if task.get("name") == "Classify the one-shot fixed readiness probe result"
)
postcheck = next(
    task for task in apply["rescue"]
    if task.get("name") == "Capture sanitized state and health log count after the readiness probe"
)
remove = next(
    task for task in apply["rescue"]
    if task.get("name") == "Force-remove only the failed collector while retaining positions"
)
render_format = lambda task: Templar(loader=DataLoader(), variables={}).template(
    trust_as_template(task["ansible.builtin.command"]["argv"][3])
)
rendered_formats = {
    "state": render_format(state),
    "config": render_format(config),
    "action": render_format(action),
}
rendered_logs = logs["ansible.builtin.command"]["argv"]
logs_no_log = logs.get("no_log")
expressions = classifier["ansible.builtin.set_fact"]
gate_expression = next(
    task for task in apply["rescue"]
    if task.get("name") == "Decide whether one fixed readiness probe is safe as diagnostic evidence"
)["ansible.builtin.set_fact"]["_journal_rollback_readiness_probe_allowed"]
readiness_expression = readiness_classifier["ansible.builtin.set_fact"]["_journal_readiness_probe_class"]
postcheck_format_expression = postcheck["ansible.builtin.command"]["argv"][3]
rendered_postcheck_format = Templar(loader=DataLoader(), variables={}).template(
    trust_as_template(postcheck_format_expression)
)
readiness_argv = readiness_probe["ansible.builtin.command"]["argv"]
readiness_settings = {
    "no_log": readiness_probe.get("no_log"),
    "failed_when": readiness_probe.get("failed_when"),
    "check_mode": readiness_probe.get("check_mode"),
    "when": readiness_probe.get("when"),
}
data = json.loads(sys.stdin.read())
classes = []
for case in data["classifier_cases"]:
    variables = {"_journal_rollback_state_evidence": {key: case[key] for key in ("rc", "stderr", "stdout")}}
    result = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_state_class"])
    ).strip()
    classes.append(result)
config_classes = []
for case in data["config_cases"]:
    variables = {"_journal_rollback_health_config": case}
    result = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_health_config_class"])
    ).strip()
    config_classes.append(result)
action_classes = []
for case in data["action_cases"]:
    variables = {"_journal_rollback_health_action": case}
    result = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_health_action_class"])
    ).strip()
    action_classes.append(result)
log_classes = []
log_counts = []
failure_outputs = []
for case in data["log_cases"]:
    variables = {
        "_journal_rollback_logs": case | {
            "stdout_lines": case["stdout"].splitlines(),
            "stderr_lines": case.get("stderr", "").splitlines(),
        },
    }
    result = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_log_class"])
    ).strip()
    log_classes.append(result)
    count = str(Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_log_count"])
    )).strip()
    log_counts.append(int(count))
    failure_message = next(
        task for task in apply["rescue"]
        if task.get("name") == "Fail closed after stopping the journal collector"
    )["ansible.builtin.fail"]["msg"]
    variables.update({"_journal_rollback_log_class": result, "_journal_rollback_log_count": int(count)})
    failure_outputs.append(Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(failure_message)
    ))
gate_results = []
for case in data["gate_cases"]:
    variables = {
        "_journal_rollback_state_evidence": case["state"],
        "_journal_rollback_health_config": case["config"],
        "_journal_rollback_health_action": case["action"],
    }
    result = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(gate_expression))
    gate_results.append(str(result).lower() == "true")
readiness_results = []
for case in data["readiness_cases"]:
    variables = {
        "_journal_rollback_readiness_probe_allowed": case["allowed"],
        "_journal_readiness_probe": {"rc": case.get("rc", 125)},
    }
    result = Templar(loader=DataLoader(), variables=variables).template(trust_as_template(readiness_expression)).strip()
    readiness_results.append(result)
print(json.dumps({
    "formats": rendered_formats,
    "logs": rendered_logs,
    "logs_no_log": logs_no_log,
    "classes": classes,
    "config_classes": config_classes,
    "action_classes": action_classes,
    "log_classes": log_classes,
    "log_counts": log_counts,
    "failure_outputs": failure_outputs,
    "gate_results": gate_results,
    "readiness_results": readiness_results,
    "readiness_argv": readiness_argv,
    "readiness_settings": readiness_settings,
    "postcheck_format": rendered_postcheck_format,
    "readiness_probe_precedes_removal": apply["rescue"].index(readiness_probe) < apply["rescue"].index(remove),
}))
'''
    temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else tempfile.gettempdir()
    with tempfile.TemporaryDirectory(prefix="o11y-journal-templar-", dir=temp_root) as ansible_tmp:
        env = os.environ.copy() | {
            "ANSIBLE_LOCAL_TEMP": ansible_tmp,
            "ANSIBLE_REMOTE_TEMP": ansible_tmp,
        }
        result = subprocess.run(
            [ansible_python, "-c", harness, str(PLAYBOOK)],
            cwd=ROOT,
            env=env,
            input=json.dumps(
                {
                    "classifier_cases": state_cases,
                    "config_cases": config_cases,
                    "action_cases": action_cases,
                    "log_cases": log_cases,
                    "gate_cases": gate_cases,
                    "readiness_cases": readiness_cases,
                }
            ),
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert rendered["formats"] == {
        "state": (
        '{{printf "state=%s restart_count=%d " .State.Status .RestartCount}}'
        "{{with .State.Health}}health={{.Status}} failing_streak={{.FailingStreak}} "
        "log_count={{len .Log}} exit_codes={{range .Log}}{{.ExitCode}},{{end}}"
        "{{else}}health=unavailable failing_streak=unavailable log_count=0 exit_codes=unavailable{{end}}"
        ),
        "config": (
        "{{with .Config.Healthcheck}}{{if .Test}}{{if eq (index .Test 0) \"NONE\"}}"
        "healthcheck_present=absent test_kind=none interval=unavailable timeout=unavailable "
        "retries=unavailable start_period=unavailable{{else}}healthcheck_present=present test_kind="
        "{{if eq (index .Test 0) \"CMD\"}}CMD{{else if eq (index .Test 0) \"CMD-SHELL\"}}CMD-SHELL"
        "{{else}}other{{end}} interval={{.Interval}} timeout={{.Timeout}} retries={{.Retries}} "
        "start_period={{.StartPeriod}}{{end}}{{else}}healthcheck_present=absent test_kind=none "
        "interval=unavailable timeout=unavailable retries=unavailable start_period=unavailable{{end}}"
        "{{else}}healthcheck_present=absent test_kind=none interval=unavailable timeout=unavailable "
        "retries=unavailable start_period=unavailable{{end}}"
        ),
        "action": (
        "{{if eq .Config.HealthcheckOnFailureAction \"none\"}}on_failure=none"
        "{{else if eq .Config.HealthcheckOnFailureAction \"restart\"}}on_failure=restart"
        "{{else if eq .Config.HealthcheckOnFailureAction \"stop\"}}on_failure=stop"
        "{{else if eq .Config.HealthcheckOnFailureAction \"kill\"}}on_failure=kill"
        "{{else}}on_failure=unknown{{end}}"
        ),
    }
    assert rendered["classes"] == [case["expected"] for case in state_cases]
    assert rendered["config_classes"] == [case["expected"] for case in config_cases]
    assert rendered["action_classes"] == [case["expected"] for case in action_cases]
    assert rendered["log_classes"] == [case["expected"] for case in log_cases]
    assert rendered["log_counts"] == [0, 0, 1, 1, 1, 1, 1, 1]
    assert "collector logs classification/count=config_error/1" in rendered["failure_outputs"][-1]
    assert rendered["logs"] == ["podman", "logs", "--tail", "80", "o11y-journal-collector"]
    assert rendered["logs_no_log"] is True
    assert not any(
        secret in json.dumps(rendered)
        for secret in ("/private/path", "203.0.113.7", "Bearer secret", "token=secret")
    )
    assert rendered["gate_results"] == [case["expected"] for case in gate_cases]
    assert rendered["readiness_results"] == [case["expected"] for case in readiness_cases]
    assert rendered["readiness_argv"] == [
        "/bin/bash",
        "-c",
        "/usr/bin/timeout --signal=KILL 3s podman exec o11y-journal-collector "
        "/bin/bash -c 'exec 2>/dev/null 3<>/dev/tcp/127.0.0.1/12345 >/dev/null' "
        ">/dev/null 2>&1",
    ]
    assert "podman healthcheck run" not in rendered["readiness_argv"]
    assert rendered["readiness_settings"] == {
        "no_log": None,
        "failed_when": False,
        "check_mode": False,
        "when": "_journal_rollback_readiness_probe_allowed | bool",
    }
    assert rendered["postcheck_format"] == (
        "{{.State.Status}} {{with .State.Health}}{{.Status}} {{len .Log}}"
        "{{else}}unavailable 0{{end}}"
    )
    assert rendered["readiness_probe_precedes_removal"] is True
    assert result.stderr == ""


def test_semaphore_template_is_dev_bound_and_requires_exact_sha():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    template = next(
        item for item in templates if item["name"] == "Deploy o11y Journal Collector (Dev)"
    )

    assert template["playbook"] == "platform/playbooks/deploy-o11y-journal-collector.yml"
    assert template["repository"] == "agent-cloud dev"
    variables = {item["name"]: item for item in template["survey_vars"]}
    assert variables["expected_repository_sha"]["required"] is True
    assert variables["service_branch"]["default_value"] == "dev"
    assert variables["journal_collector_action"]["default_value"] == "survey"
    assert variables["journal_collector_action"]["values"] == [
        {"name": "Survey journal paths", "value": "survey"},
        {"name": "Repair positions root ownership", "value": "repair-positions"},
        {"name": "Apply pilot", "value": "apply"},
        {"name": "Stop pilot", "value": "stop"},
    ]
