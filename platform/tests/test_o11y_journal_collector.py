"""Safety contract for the bounded receiver-host journald pilot."""

import ast
import importlib.util
import json
import os
import re
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


def _assert_positions_survey_diagnostics_schema(report, has_reason=False):
    top_level = {
        "status", "volume_use_count", "collector_present", "entry_count", "root_owner",
        "root_mode_access", "acl", "mount", "children", "journal_cursor", "free_space",
        "survey_diagnostics", "pending_repair_diagnostic", "live_diagnostic",
    }
    assert set(report) == top_level | ({"reason"} if has_reason else set())
    diagnostics = report["survey_diagnostics"]
    assert set(diagnostics) == {"inventory_fields", "named_volume", "podman_version"}
    assert set(diagnostics["inventory_fields"]) == {"NeedsChown", "NeedsCopyUp"}
    assert set(diagnostics["named_volume"]) == {
        "outcome", "identity", "inventory_identity", "fields", "template_fields", "template_identity",
    }
    assert set(diagnostics["named_volume"]["fields"]) == {"NeedsChown", "NeedsCopyUp"}
    assert set(diagnostics["named_volume"]["template_fields"]) == {"NeedsChown", "NeedsCopyUp"}
    assert set(diagnostics["podman_version"]) == {"client", "server"}
    pending = report["pending_repair_diagnostic"]
    assert set(pending) == {
        "owner_rwx", "group_other_write_or_special_bits", "group_write", "other_write",
        "setuid", "setgid", "sticky", "volume_free_bytes",
        "volume_free_inodes", "graphroot_free_bytes", "graphroot_free_inodes", "failed_checks",
    }
    assert pending["owner_rwx"] in {"complete", "incomplete", "unverified"}
    assert pending["group_other_write_or_special_bits"] in {"absent", "present", "unverified"}
    for key in ("group_write", "other_write", "setuid", "setgid", "sticky"):
        assert pending[key] in {"absent", "present", "unverified"}
    for key in ("volume_free_bytes", "volume_free_inodes", "graphroot_free_bytes", "graphroot_free_inodes"):
        assert pending[key] in {"sufficient", "low", "unverified"}
    assert len(pending["failed_checks"]) <= len(POSITIONS._PENDING_CHECK_NAMES)
    assert set(pending["failed_checks"]) <= set(POSITIONS._PENDING_CHECK_NAMES)
    for field in (
        *diagnostics["inventory_fields"].values(),
        *diagnostics["named_volume"]["fields"].values(),
        *diagnostics["named_volume"]["template_fields"].values(),
    ):
        assert set(field) == {"presence", "type", "value"}
    live = report["live_diagnostic"]
    assert set(live["role_metadata"]) == {"receiver", "exporter"}
    for role in live["role_metadata"].values():
        assert set(role) == {"owner", "access", "acl", "mount", "children"}


def _positions_state(
    owner=(88, 88), mode=0o700, children=(), acl=False,
    root_mount=False, child_mount=False, component_layout=True, journal_cursor_valid=True,
    journal_cursor_presence="present",
    free_bytes=64 * 1024 * 1024, free_inodes=256,
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
        "journal_cursor_presence": journal_cursor_presence,
        "cursor_checked": False,
        "journal_cursor_valid": journal_cursor_valid,
        "free_bytes": free_bytes,
        "free_inodes": free_inodes,
    }


def _positions_found(metadata=None, users=0):
    return {
        "status": "observed",
        "volume_name": "o11y_journal-collector-state",
        "mountpoint": "/private/podman/volume/_data",
        "volume_use_count": users,
        "mount_count": users,
        "collector_present": bool(users),
        "collector_mount_verified": bool(users),
        "project": "o11y",
        "needs_chown": False,
        "needs_copy_up": False,
        "metadata": metadata or _positions_state(),
    }


def _positions_with_space_counters(found, free_bytes, free_inodes):
    metadata = {**found["metadata"], "free_bytes": free_bytes, "free_inodes": free_inodes}
    result = {**found, "metadata": metadata}
    if "storage_space" in found:
        result["storage_space"] = {"free_bytes": free_bytes, "free_inodes": free_inodes}
    return result


def _positions_volume_reply(argv, volume):
    if argv[:3] != ["podman", "volume", "inspect"] or "--format" not in argv:
        return None
    labels = volume.get("Labels", {})
    values = [
        volume.get("Name", "<no value>"),
        labels.get("com.docker.compose.project", "<no value>"),
        labels.get("com.docker.compose.volume", ""),
    ]
    for field in ("NeedsChown", "NeedsCopyUp"):
        raw = volume.get(field, False)
        values.append(str(raw).lower() if type(raw) is bool else str(raw))
    return SimpleNamespace(returncode=0, stdout="\n".join(values))


def _flag_volume_template(volume, needs_chown="false", needs_copy_up="false"):
    labels = volume.get("Labels", {})
    return "\n".join((
        volume.get("Name", "<no value>"),
        labels.get("com.docker.compose.project", "<no value>"),
        labels.get("com.docker.compose.volume", ""),
        needs_chown,
        needs_copy_up,
    ))


def test_positions_template_flags_accept_omitted_json_false_and_preserve_presence():
    volume = {
        "Name": "o11y_journal-collector-state", "Driver": "local", "Scope": "local",
        "Options": {}, "Mountpoint": "/private/podman/volume/_data", "MountCount": 0,
        "NeedsCopyUp": True,
        "Labels": {"com.docker.compose.project": "o11y", "com.docker.compose.volume": "journal-collector-state"},
    }
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if "--format" in argv:
            return SimpleNamespace(returncode=0, stdout=_flag_volume_template(volume, "false", "true"))
        return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))

    result = POSITIONS._named_volume_flags(volume["Name"], "o11y", volume, run)
    assert result["outcome"] == "observed"
    assert result["flags"] == {"NeedsChown": False, "NeedsCopyUp": True}
    assert result["fields"]["NeedsChown"]["presence"] == "missing"
    assert result["template_fields"]["NeedsChown"]["value"] is False
    assert sum("--format" in argv for argv in calls) == 1
    template_call = next(argv for argv in calls if "--format" in argv)
    assert template_call[:4] == ["podman", "volume", "inspect", "--format"]
    assert template_call[-1] == volume["Name"]


@pytest.mark.parametrize("raw", ["<no value>", "False", "false ", "0", "", "unknown"])
def test_positions_template_flags_refuse_unknown_or_nonliteral_producer(raw):
    volume = {
        "Name": "o11y_journal-collector-state", "Labels": {
            "com.docker.compose.project": "o11y", "com.docker.compose.volume": "journal-collector-state"
        },
        "NeedsChown": False, "NeedsCopyUp": True,
    }

    def run(argv, **_kwargs):
        if "--format" in argv:
            return SimpleNamespace(returncode=0, stdout=_flag_volume_template(volume, raw, "true"))
        return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))

    assert POSITIONS._named_volume_flags(volume["Name"], "o11y", volume, run)["flags"] is None


def test_positions_template_flags_refuse_identity_mismatch_and_bracket_race():
    volume = {
        "Name": "o11y_journal-collector-state", "Labels": {
            "com.docker.compose.project": "o11y", "com.docker.compose.volume": "journal-collector-state"
        },
        "NeedsChown": False, "NeedsCopyUp": True,
    }
    calls = 0

    def run(argv, **_kwargs):
        nonlocal calls
        if "--format" in argv:
            return SimpleNamespace(returncode=0, stdout=_flag_volume_template(volume, "false", "true"))
        calls += 1
        changed = {**volume, "Name": "other"} if calls == 2 else volume
        return SimpleNamespace(returncode=0, stdout=json.dumps([changed]))

    result = POSITIONS._named_volume_flags(volume["Name"], "o11y", volume, run)
    assert result["outcome"] == "identity_mismatch"
    assert result["flags"] is None


def test_positions_metadata_parser_requires_expected_journal_cursor():
    nodes = ast.parse(POSITIONS._METADATA_SCRIPT).body
    parser_nodes = [
        node for node in nodes
        if isinstance(node, ast.FunctionDef) and node.name in {"scalar", "journal_cursor"}
    ]
    namespace = {}
    namespace = {"re": re, "json": json}
    exec(compile(ast.Module(body=parser_nodes, type_ignores=[]), "positions-metadata-parser", "exec"), namespace)
    cursor = "s=1a;i=2b;b=3c;m=4d;t=5e;x=6f"
    # Alloy v1.9.2 writePositionFile emits this yaml.v2 two-space mapping shape.
    good = (
        "positions:\n  ? path: cursor-loki.source.journal.o11y_alloy\n"
        "    labels: \"\"\n  : \"" + cursor + "\"\n"
    ).encode()
    assert namespace["journal_cursor"](good) is True
    for bad in (
        good.replace(b"  ? path", b"    ? path"),
        good.replace(b"cursor-loki.source.journal.o11y_alloy", b"cursor-other"),
        good.replace(b'labels: ""', b'labels: "unexpected"'),
        good.replace(cursor.encode(), b"not-a-cursor"),
        good + b"extra: true\n",
    ):
        assert namespace["journal_cursor"](bad) is False


@pytest.mark.parametrize(
    ("cursor_state", "expected_presence", "expected_checked", "expected_valid"),
    [
        ("absent", "absent", False, False),
        ("unsafe", "present", False, False),
        ("oversized", "present", False, False),
        ("read_error", "present", False, False),
        ("valid", "present", True, True),
    ],
)
def test_positions_cursor_checked_tracks_stable_content_examination(
    tmp_path, cursor_state, expected_presence, expected_checked, expected_valid
):
    root = tmp_path / "positions"
    component = root / "loki.source.journal.o11y_alloy"
    component.mkdir(mode=0o700, parents=True)
    os.chmod(root, 0o700)
    os.chmod(component, 0o700)
    positions = component / "positions.yml"
    cursor = "s=1a;i=2b;b=3c;m=4d;t=5e;x=6f"
    content = (
        "positions:\n  ? path: cursor-loki.source.journal.o11y_alloy\n"
        "    labels: \"\"\n  : \"" + cursor + "\"\n"
    )
    if cursor_state != "absent":
        positions.write_text("x" * 65537 if cursor_state == "oversized" else content)
        os.chmod(positions, 0o644 if cursor_state == "unsafe" else 0o600)

    prelude = f'''import builtins,io,os,sys,types
root={str(root)!r}
real_lstat=os.lstat
real_fstat=os.fstat
real_open=os.open
def root_owned(st):
    return types.SimpleNamespace(st_mode=st.st_mode,st_uid=0,st_gid=0,st_nlink=st.st_nlink,
        st_dev=st.st_dev,st_ino=st.st_ino,st_size=st.st_size,st_mtime_ns=st.st_mtime_ns)
os.lstat=lambda path,*args,**kwargs: root_owned(real_lstat(path,*args,**kwargs))
os.fstat=lambda fd: root_owned(real_fstat(fd))
def fake_open(path,flags,*args,**kwargs):
    if path=="positions.yml" and {cursor_state!r}=="read_error": raise PermissionError("private")
    return real_open(path,flags,*args,**kwargs)
os.open=fake_open
original_open=builtins.open
def fake_builtin_open(path,*args,**kwargs):
    if path=="/proc/self/mountinfo": return io.StringIO("1 0 0:1 / / rw - testfs /dev/test rw\\n")
    return original_open(path,*args,**kwargs)
builtins.open=fake_builtin_open
os.listxattr=lambda *_args,**_kwargs: []
sys.argv.append("true")
exec({POSITIONS._METADATA_SCRIPT!r})
'''
    result = subprocess.run(
        [sys.executable, "-c", prelude, str(root), "true"],
        check=False, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    assert metadata["journal_cursor_presence"] == expected_presence
    assert metadata["cursor_checked"] is expected_checked
    assert metadata["journal_cursor_valid"] is expected_valid
    diagnostic = POSITIONS._live_diagnostic(_positions_found(metadata=metadata))
    if expected_checked:
        assert "cursor_validity" not in diagnostic["unverified_checks"]
    else:
        assert "cursor_validity" in diagnostic["unverified_checks"]


def test_positions_verification_enables_cursor_reading(monkeypatch):
    observed = []

    def discover(**kwargs):
        observed.append(kwargs.get("read_cursor"))
        return {"status": "unavailable"}

    monkeypatch.setattr(POSITIONS, "discover", discover)
    POSITIONS.verify()
    assert observed == [True]


@pytest.mark.parametrize(("action", "function"), [("verify", "verify"), ("verify-live", "verify_live")])
def test_positions_cli_dispatches_prestart_and_live_actions(action, function, monkeypatch, capsys):
    result = {"status": "ready", "reason": function}
    monkeypatch.setattr(sys, "argv", [str(POSITIONS_HELPER), action])
    monkeypatch.setattr(POSITIONS, function, lambda: result)
    POSITIONS.main()
    assert json.loads(capsys.readouterr().out) == result


def test_positions_survey_returns_only_bounded_metadata_categories():
    found = _positions_found(_positions_state(owner=(9001, 9001), children=[
        {"kind": "file", "uid": 0, "gid": 0, "nlink": 1, "mode": 0o600}
    ]))
    report = POSITIONS.survey(lambda: found)

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
        "journal_cursor": "valid",
        "free_space": "sufficient",
        "live_diagnostic": {
            **POSITIONS._live_diagnostic(found),
            "host_uid_map": "not_run",
            "collector_uid_map": "not_run",
            "uid_maps": "not_run",
            "namespace_probe": "not_run",
        },
        "pending_repair_diagnostic": POSITIONS._pending_diagnostic(found),
    }
    assert "/private" not in json.dumps(report)
    assert "9001" not in json.dumps(report)
    assert set(report["live_diagnostic"]["mount"].values()) == {"unverified"}
    assert "cursor_validity" in report["live_diagnostic"]["unverified_checks"]
    assert {key: report["live_diagnostic"][key] for key in (
        "host_uid_map", "collector_uid_map", "uid_maps", "namespace_probe",
    )} == {
        "host_uid_map": "not_run", "collector_uid_map": "not_run",
        "uid_maps": "not_run", "namespace_probe": "not_run",
    }


def test_live_diagnostic_reports_only_fixed_mount_layout_and_role_categories():
    found = _positions_found(_positions_state(
        owner=(9001, 9001), component_layout=False,
        journal_cursor_presence="present", journal_cursor_valid=False,
    ))
    found["metadata"]["cursor_checked"] = True
    found["metadata"]["top_level"] = {
        "seed_file": 1, "journal_component": 1, "other_files": 1,
        "other_directories": 0, "other_symlinks": 1, "other_kinds": 0,
    }
    found["live_mount"] = {
        "destination": "match", "volume_name": "match", "source": "mismatch",
        "type": "match", "rw": "match", "user": "unverified",
    }

    diagnostic = POSITIONS._live_diagnostic(found)

    assert diagnostic["failed_checks"] == [
        "owner", "component_layout", "cursor_validity", "positions_access", "mount_source",
    ]
    assert diagnostic["unverified_checks"] == ["container_user"]
    assert diagnostic["mount"] == {
        "destination": "match", "volume_name": "match", "source": "mismatch",
        "type": "match", "rw": "match", "user": "unverified",
    }
    assert diagnostic["top_level"]["seed_file"] == 1
    assert diagnostic["top_level"]["other_symlinks"] == 1
    assert "9001" not in json.dumps(diagnostic)
    assert "/private" not in json.dumps(diagnostic)


def test_live_positions_debug_rescues_malformed_nested_diagnostic_before_rendering():
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template

    plays = yaml.safe_load(PLAYBOOK.read_text())
    apply_play = next(play for play in plays if play.get("hosts") == "o11y_svc")
    apply_block = next(
        task for task in apply_play["tasks"]
        if task.get("name") == "Apply the collector and require exact-target Loki delivery"
    )
    live_boundary = next(
        task for task in apply_block["block"]
        if task.get("name") == "Parse live positions output through a protected boundary"
    )
    validation = next(
        task for task in live_boundary["block"]
        if task.get("name") == "Validate live positions diagnostic fields before summary"
    )
    rescue = live_boundary["rescue"][0]["ansible.builtin.set_fact"][
        "_journal_positions_live_summary"
    ]
    debug = next(
        task for task in apply_block["block"]
        if task.get("name") == "Report sanitized live positions diagnostic"
    )
    base = {
        "helper_status": "observed",
        "helper_reason": "positions_identity_verified",
        "failed_checks": ["owner"],
        "unverified_checks": ["cursor_validity"],
        "mount": {
            "destination": "match",
            "volume_name": "match",
            "source": "mismatch",
            "type": "match",
            "rw": "match",
            "user": "unverified",
        },
        "top_level": {
            "seed_file": 1,
            "journal_component": 0,
            "other_files": 2,
            "other_directories": 3,
            "other_symlinks": 4,
            "other_kinds": 5,
            "receiver_component": 1,
            "exporter_component": 0,
        },
        "role_metadata": {
            "receiver": {
                "owner": "matches_collector", "access": "read_write_execute",
                "acl": "absent", "mount": "clear", "children": "empty",
            },
            "exporter": {
                "owner": "unverified", "access": "unverified", "acl": "unverified",
                "mount": "unverified", "children": "unverified",
            },
        },
    }
    malformed = [
        ("helper_reason", {**base, "helper_reason": "PRIVATE_MARKER_REASON"}),
        ("mount_value", {
            **base,
            "mount": {**base["mount"], "source": "PRIVATE_MARKER_SOURCE"},
        }),
        ("extra_mount_key", {
            **base,
            "mount": {**base["mount"], "private": "PRIVATE_MARKER_MOUNT_KEY"},
        }),
        ("top_level_list", {
            **base,
            "top_level": ["PRIVATE_MARKER_TOP_LEVEL"],
        }),
        ("check_name", {
            **base,
            "failed_checks": ["owner", "PRIVATE_MARKER_CHECK"],
        }),
        ("role_value", {
            **base,
            "role_metadata": {
                **base["role_metadata"],
                "receiver": {**base["role_metadata"]["receiver"], "owner": "PRIVATE_MARKER_ROLE"},
            },
        }),
        ("role_count_bound", {
            **base,
            "top_level": {**base["top_level"], "receiver_component": 33},
        }),
        ("role_extra_key", {
            **base,
            "role_metadata": {
                **base["role_metadata"],
                "receiver": {**base["role_metadata"]["receiver"], "private": "PRIVATE_MARKER_ROLE"},
            },
        }),
    ]
    def evaluate(diagnostic):
        variables = {
            "_journal_positions_live_parsed": {
                "status": "ready",
                "live_diagnostic": diagnostic,
            },
        }
        templar = Templar(loader=DataLoader(), variables=variables)
        passed = all(
            templar.evaluate_conditional(trust_as_template(condition))
            for condition in validation["ansible.builtin.assert"]["that"]
        )
        variables["_journal_positions_live_summary"] = (
            {"status": "ready", "live_diagnostic": diagnostic}
            if passed else rescue
        )
        rendered = Templar(loader=DataLoader(), variables=variables).template(
            trust_as_template(debug["ansible.builtin.debug"]["msg"])
        )
        return passed, variables["_journal_positions_live_summary"], rendered

    valid_passed, valid_summary, valid_rendered = evaluate(base)
    assert valid_passed is True
    assert valid_summary["status"] == "ready"
    for expected in (
        "helper=observed",
        "reason=positions_identity_verified",
        "failed_checks=['owner']",
        "unverified_checks=['cursor_validity']",
        "seed_file=1",
        "journal_component=0",
        "other_entries=2/3/4/5",
        "receiver_role=",
        "exporter_role=",
    ):
        assert expected in valid_rendered

    for name, diagnostic in malformed:
        passed, summary, rendered = evaluate(diagnostic)
        assert passed is False, name
        assert summary == rescue, name
        assert "helper=unavailable" in rendered, name
        assert "PRIVATE_MARKER" not in rendered, name


@pytest.mark.parametrize("field", ["NeedsChown", "NeedsCopyUp"])
@pytest.mark.parametrize(
    ("value", "expected_type", "expected_value"),
    [
        ("missing", "missing", "unavailable"),
        (None, "null", "unavailable"),
        (False, "boolean", False),
        (True, "boolean", True),
        ("PRIVATE_MARKER_STRING", "string", "unavailable"),
        (9001, "number", "unavailable"),
        (["PRIVATE_MARKER_ARRAY"], "array", "unavailable"),
        ({"secret": "PRIVATE_MARKER_OBJECT"}, "object", "unavailable"),
    ],
)
def test_survey_initialization_diagnostics_expose_only_type_and_real_boolean(
    field, value, expected_type, expected_value, monkeypatch
):
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
    if value == "missing":
        volume.pop(field)
    else:
        volume[field] = value
    if expected_type == "boolean":
        volume["NeedsCopyUp" if field == "NeedsChown" else "NeedsChown"] = None
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:4] == ["podman", "inspect", "--type", "container"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {
                "com.docker.compose.project": "o11y", "com.docker.compose.service": "alloy"
            }}}]))
        if argv[:3] == ["podman", "volume", "inspect"] and "--all" in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        template_reply = _positions_volume_reply(argv, volume)
        if template_reply is not None:
            return template_reply
        if argv[:3] == ["podman", "volume", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "version"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "Client": {"Version": "5.4.2"}, "Server": {"Version": "5.4.2"}
            }))
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError(f"unexpected diagnostic command: {argv}")

    monkeypatch.setattr(POSITIONS, "_inspect_metadata", lambda *_args, **_kwargs: _positions_state(owner=(0, 0)))
    report = POSITIONS.survey(lambda: POSITIONS.discover(run=run, diagnostics=True))
    assert report["status"] == ("observed" if value == "missing" else "volume_initialization_unverified")
    if value is True:
        assert expected_value is True
    diagnostic = report["survey_diagnostics"]
    actual = diagnostic["inventory_fields"][field]
    assert actual == {
        "presence": "missing" if value == "missing" else "present",
        "type": expected_type,
        "value": expected_value,
    }
    assert diagnostic["named_volume"]["identity"] == "match"
    assert diagnostic["named_volume"]["fields"][field] == actual
    assert diagnostic["podman_version"] == {"client": "5.4.2", "server": "5.4.2"}
    assert [argv for argv in calls if argv[:2] == ["podman", "version"]] == [
        ["podman", "version", "--format", "json"]
    ]
    named_calls = [argv for argv in calls if argv[:3] == ["podman", "volume", "inspect"] and "--all" not in argv]
    assert len(named_calls) == 3
    assert named_calls[0] == named_calls[2] == ["podman", "volume", "inspect", "o11y_journal-collector-state"]
    assert named_calls[1][3] == "--format"
    assert "PRIVATE_MARKER" not in json.dumps(report)
    assert "/private/" not in json.dumps(report)


@pytest.mark.parametrize(
    ("named_result", "expected_outcome", "expected_identity"),
    [
        ("PRIVATE_MARKER_MALFORMED", "unavailable", "unverified"),
        (["duplicate", "duplicate"], "unavailable", "unverified"),
        (["mismatch"], "identity_mismatch", "mismatch"),
    ],
)
def test_survey_named_volume_diagnostic_is_categorical_and_redacted(
    named_result, expected_outcome, expected_identity, monkeypatch
):
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
        "NeedsChown": "PRIVATE_MARKER_FIELD",
        "NeedsCopyUp": False,
    }

    def run(argv, **_kwargs):
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:4] == ["podman", "inspect", "--type", "container"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {
                "com.docker.compose.project": "o11y", "com.docker.compose.service": "alloy"
            }}}]))
        if argv[:3] == ["podman", "volume", "inspect"] and "--all" in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        template_reply = _positions_volume_reply(argv, volume)
        if template_reply is not None:
            return template_reply
        if argv[:3] == ["podman", "volume", "inspect"]:
            if named_result == "PRIVATE_MARKER_MALFORMED":
                return SimpleNamespace(returncode=0, stdout='"PRIVATE_MARKER_MALFORMED"')
            if named_result[0] == "duplicate":
                return SimpleNamespace(returncode=0, stdout=json.dumps([volume, volume]))
            mismatch = {**volume, "Name": "PRIVATE_MARKER_NAME", "Labels": {
                "com.docker.compose.project": "PRIVATE_MARKER_PROJECT"
            }}
            return SimpleNamespace(returncode=0, stdout=json.dumps([mismatch]))
        if argv[:2] == ["podman", "version"]:
            return SimpleNamespace(returncode=0, stdout="{}")
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError(f"unexpected diagnostic command: {argv}")

    monkeypatch.setattr(POSITIONS, "_inspect_metadata", lambda *_args, **_kwargs: _positions_state(owner=(0, 0)))
    report = POSITIONS.survey(lambda: POSITIONS.discover(run=run, diagnostics=True))
    named = report["survey_diagnostics"]["named_volume"]
    assert named["outcome"] == expected_outcome
    assert named["identity"] == expected_identity
    assert named["inventory_identity"] == ("mismatch" if expected_identity == "mismatch" else "unverified")
    assert named["fields"]["NeedsChown"]["value"] == "unavailable"
    assert "PRIVATE_MARKER" not in json.dumps(report)
    assert report["survey_diagnostics"]["podman_version"] == {
        "client": "unavailable", "server": "unavailable"
    }


def test_survey_named_volume_version_queries_run_only_after_exact_identity(monkeypatch):
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
        "NeedsChown": "invalid",
        "NeedsCopyUp": False,
    }
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:4] == ["podman", "inspect", "--type", "container"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {
                "com.docker.compose.project": "o11y", "com.docker.compose.service": "alloy"
            }}}]))
        if argv[:3] == ["podman", "volume", "inspect"] and "--all" in argv:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        template_reply = _positions_volume_reply(argv, volume)
        if template_reply is not None:
            return template_reply
        if argv[:3] == ["podman", "volume", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "version"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "Client": {"Version": "5.4.2"}, "Server": {"Version": "PRIVATE_MARKER_VERSION"}
            }))
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError(f"unexpected diagnostic command: {argv}")

    monkeypatch.setattr(POSITIONS, "_inspect_metadata", lambda *_args, **_kwargs: _positions_state(owner=(0, 0)))
    report = POSITIONS.survey(lambda: POSITIONS.discover(run=run, diagnostics=True))
    query_names = [argv for argv in calls if argv[:3] == ["podman", "volume", "inspect"] and "--all" not in argv]
    assert len(query_names) == 3
    assert report["status"] == "volume_initialization_unverified"
    assert report["survey_diagnostics"]["podman_version"] == {
        "client": "5.4.2", "server": "unavailable"
    }
    assert "PRIVATE_MARKER" not in json.dumps(report)
    assert [argv for argv in calls if argv[:2] == ["podman", "version"]] == [
        ["podman", "version", "--format", "json"]
    ]


def test_survey_default_discovery_has_exact_diagnostic_schema(monkeypatch):
    volume = {
        "Name": "o11y_journal-collector-state", "Driver": "local", "Scope": "local",
        "Options": {}, "Mountpoint": "/private/podman/volume/_data", "MountCount": 0,
        "NeedsChown": False, "NeedsCopyUp": True,
        "Labels": {
            "com.docker.compose.project": "o11y",
            "com.docker.compose.volume": "journal-collector-state",
        },
    }
    calls = []

    def call(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:3] == ["podman", "inspect", "--type"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {
                "com.docker.compose.project": "o11y", "com.docker.compose.service": "alloy"
            }}}]))
        if argv[:4] == ["podman", "volume", "inspect", "--all"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        template_reply = _positions_volume_reply(argv, volume)
        if template_reply is not None:
            return template_reply
        if argv[:3] == ["podman", "volume", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "version"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "Client": {"Version": "5.4.2"}, "Server": {"Version": "5.4.2"}
            }))
        raise AssertionError(f"unexpected default survey command: {argv}")

    monkeypatch.setattr(POSITIONS, "_call", call)
    monkeypatch.setattr(POSITIONS, "_inspect_metadata", lambda *_args, **_kwargs: _positions_state(owner=(0, 0)))
    report = POSITIONS.survey()

    _assert_positions_survey_diagnostics_schema(report)
    assert report["status"] == "volume_initialization_pending"
    assert [argv for argv in calls if argv[:2] == ["podman", "version"]] == [
        ["podman", "version", "--format", "json"]
    ]


def test_survey_early_refusal_keeps_exact_unverified_diagnostic_schema(monkeypatch):
    calls = []

    def call(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="false")

    monkeypatch.setattr(POSITIONS, "_call", call)
    report = POSITIONS.survey()

    _assert_positions_survey_diagnostics_schema(report)
    assert report["status"] == "not_rootless"
    assert not any(argv[:3] == ["podman", "volume", "inspect"] for argv in calls)
    assert not any(argv[:2] == ["podman", "version"] for argv in calls)


def test_survey_flag_assertions_require_literal_booleans():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    assertion = next(
        task["ansible.builtin.assert"]
        for play in plays
        for task in play.get("tasks", [])
        if task.get("name") == "Require bounded positions-volume survey fields"
    )
    conditions = [condition for condition in assertion["that"] if ".value " in condition]

    assert len(conditions) == 6
    assert all("is sameas true or" in condition for condition in conditions)
    assert all("is sameas false or" in condition for condition in conditions)
    environment = Environment()
    environment.filters["from_json"] = json.loads
    inputs = (0, 1, True, False, "unavailable")
    for condition in conditions:
        field_path = condition.split("survey_diagnostics.", 1)[1].split(".value", 1)[0]
        for value, expected in zip(inputs, ("False", "False", "True", "True", "True"), strict=True):
            diagnostics = {
                "inventory_fields": {
                    "NeedsChown": {"value": "unavailable"},
                    "NeedsCopyUp": {"value": "unavailable"},
                },
                "named_volume": {"fields": {
                    "NeedsChown": {"value": "unavailable"},
                    "NeedsCopyUp": {"value": "unavailable"},
                }, "template_fields": {
                    "NeedsChown": {"value": "unavailable"},
                    "NeedsCopyUp": {"value": "unavailable"},
                }},
            }
            target = diagnostics
            for part in field_path.split("."):
                target = target[part]
            target["value"] = value
            result = environment.from_string("{{ " + condition + " }}").render(
                _journal_positions_survey={"stdout": json.dumps({"survey_diagnostics": diagnostics})}
            )
            assert result == expected


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
        template_reply = _positions_volume_reply(argv, volume)
        if template_reply is not None:
            return template_reply
        if argv[:3] == ["podman", "volume", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError("unexpected discovery command")

    cursor_reads = []
    monkeypatch.setattr(POSITIONS, "_inspect_metadata", lambda path, read_cursor, run: (
        cursor_reads.append(read_cursor) or _positions_state(owner=(0, 0))
    ))
    found = POSITIONS.discover(run=run)
    assert found["status"] == "observed"
    assert found["volume_use_count"] == 0
    assert all(argv[0:2] != ["podman", "run"] for argv in calls)
    assert all("volume create" not in " ".join(argv) and "chown" not in " ".join(argv) for argv in calls)
    assert not any(argv[:2] == ["podman", "version"] for argv in calls)
    assert cursor_reads == [False]
    assert [argv for argv in calls if argv[:3] == ["podman", "volume", "inspect"]][0] == [
        "podman", "volume", "inspect", "--all"
    ]

    volume["Labels"].pop("com.docker.compose.volume")
    assert POSITIONS.discover(run=run)["status"] == "observed"


@pytest.mark.parametrize(
    ("volumes", "containers", "expected"),
    [
        ([], [], "bootstrap_allowed"),
        ([{"Name": "o11y_journal-collector-state", "Labels": {"com.docker.compose.project": "other"}}], [], "refused"),
        ([{"Name": "old_volume", "Labels": {
            "com.docker.compose.project": "o11y", "com.docker.compose.volume": "journal-collector-state"
        }}], [], "refused"),
        ([], [{"Names": [POSITIONS.COLLECTOR]}], "refused"),
        ([], None, "refused"),
    ],
)
def test_positions_missing_volume_requires_complete_inventory_no_collision_and_no_stopped_collector(
    volumes, containers, expected, monkeypatch
):
    monkeypatch.setattr(POSITIONS, "_store_space", lambda _run: {
        "status": "observed", "free_bytes": 64 * 1024 * 1024, "free_inodes": 256,
    })
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
            return SimpleNamespace(returncode=0, stdout=json.dumps(volumes))
        if argv[:2] == ["podman", "ps"]:
            output = "invalid json" if containers is None else json.dumps(containers)
            return SimpleNamespace(returncode=0, stdout=output)
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(
        POSITIONS,
        "_inspect_metadata",
        lambda *_args, **_kwargs: pytest.fail("missing volume has no metadata"),
    )
    result = POSITIONS.verify(lambda: POSITIONS.discover(run=run))
    assert result["status"] == expected
    assert ["podman", "ps", "--all", "--format", "json"] in calls


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("NeedsChown", True, "volume_initialization_pending"),
        ("NeedsCopyUp", True, "volume_initialization_pending"),
        ("NeedsChown", None, "observed"),
        ("NeedsCopyUp", None, "observed"),
        ("NeedsChown", "true", "volume_initialization_unverified"),
        ("NeedsCopyUp", 0, "volume_initialization_unverified"),
    ],
)
def test_positions_discovery_requires_boolean_initialization_flags(field, value, expected, monkeypatch):
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
        template_reply = _positions_volume_reply(argv, volume)
        if template_reply is not None:
            return template_reply
        if argv[:3] == ["podman", "volume", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError("initialization refusal must happen before a container mount")

    monkeypatch.setattr(
        POSITIONS, "_inspect_metadata",
        lambda path, read_cursor=False, run=None: _positions_state(owner=(0, 0)),
    )
    assert POSITIONS.discover(run=run)["status"] == expected


def test_positions_discovery_allows_repair_of_initialized_empty_volume(monkeypatch):
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
    owner = {"uid": 88, "gid": 88}
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:3] == ["podman", "inspect", "--type"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {
                "com.docker.compose.project": "o11y", "com.docker.compose.service": "alloy"
            }}}]))
        template_reply = _positions_volume_reply(argv, volume)
        if template_reply is not None:
            return template_reply
        if argv[:3] == ["podman", "volume", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError(f"unexpected discovery command: {argv}")

    monkeypatch.setattr(
        POSITIONS,
        "_inspect_metadata",
        lambda _path, read_cursor=False, run=None: _positions_state(
            owner=(owner["uid"], owner["gid"]), mode=0o700, children=[]
        ),
    )

    def change_root(found):
        assert found["status"] == "observed"
        owner.update(uid=0, gid=0)
        return True

    result = POSITIONS.repair_positions(
        discover_fn=lambda: POSITIONS.discover(run=run),
        change_fn=change_root,
        access_test_fn=lambda _name: True,
        image_available_fn=lambda: True,
    )

    assert result == {"status": "repaired", "reason": "verified"}
    assert len([argv for argv in calls if argv[:3] == ["podman", "volume", "inspect"] and "--all" not in argv]) == 12


def test_positions_survey_allowlist_accepts_helper_initialization_status(monkeypatch):
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
        "NeedsChown": True,
        "NeedsCopyUp": False,
    }

    def run(argv, **_kwargs):
        if argv[:2] == ["podman", "info"]:
            return SimpleNamespace(returncode=0, stdout="true")
        if argv[:3] == ["podman", "inspect", "--type"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"Config": {"Labels": {
                "com.docker.compose.project": "o11y", "com.docker.compose.service": "alloy"
            }}}]))
        template_reply = _positions_volume_reply(argv, volume)
        if template_reply is not None:
            return template_reply
        if argv[:3] == ["podman", "volume", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([volume]))
        if argv[:2] == ["podman", "ps"]:
            return SimpleNamespace(returncode=0, stdout="[]")
        raise AssertionError(f"unexpected discovery command: {argv}")

    monkeypatch.setattr(POSITIONS, "_inspect_metadata", lambda _path, read_cursor=False, run=None: _positions_state())
    report = POSITIONS.discover(run=run)
    assert report["status"] == "volume_initialization_pending"

    plays = yaml.safe_load(PLAYBOOK.read_text())
    assertion = next(
        task["ansible.builtin.assert"]
        for play in plays
        for task in play.get("tasks", [])
        if task.get("name") == "Require bounded positions-volume survey fields"
    )
    accepted = next(condition for condition in assertion["that"] if ".status in " in condition)
    accepted_statuses = yaml.safe_load(accepted.split(" in ", 1)[1])
    assert report["status"] in accepted_statuses


def _missing_volume_found(**overrides):
    return {
        "status": "volume_missing",
        "project": "o11y",
        "volume_name": "o11y_journal-collector-state",
        "volume_inventory_complete": True,
        "name_collision": False,
        "collector_present": False,
        "storage_space": {"free_bytes": 64 * 1024 * 1024, "free_inodes": 256},
        **overrides,
    }


def _empty_pending_volume_found(needs_chown=False, needs_copy_up=False, **overrides):
    return {
        "status": "volume_initialization_pending",
        "project": "o11y",
        "volume_name": "o11y_journal-collector-state",
        "mountpoint": "/private/podman/volume/_data",
        "volume_inventory_complete": True,
        "name_collision": False,
        "collector_present": False,
        "storage_space": {"free_bytes": 64 * 1024 * 1024, "free_inodes": 256},
        "volume_use_count": 0,
        "mount_count": 0,
        "needs_chown": needs_chown,
        "needs_copy_up": needs_copy_up,
        "volume": {
            "Name": "o11y_journal-collector-state",
            "CreatedAt": "2026-10-09T12:00:00Z",
            "Labels": {
                "com.docker.compose.project": "o11y",
                "com.docker.compose.volume": "journal-collector-state",
            },
            "NeedsChown": needs_chown,
            "NeedsCopyUp": needs_copy_up,
        },
        "metadata": _positions_state(owner=(0, 0), mode=0o700, children=[], journal_cursor_valid=False),
        **overrides,
    }


def _pending_case(**changes):
    found = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[], journal_cursor_valid=False),
    )
    for path, value in changes.items():
        target = found
        parts = path.split(".")
        if parts[:2] == ["volume", "Labels"] and len(parts) > 2:
            target = found["volume"]["Labels"]
            target[".".join(parts[2:])] = value
            continue
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return found


@pytest.mark.parametrize(
    "found",
    [
        _pending_case(status="observed"),
        _pending_case(volume_inventory_complete=False),
        _pending_case(name_collision=True),
        _pending_case(project=None),
        _pending_case(volume=None),
        _pending_case(**{"volume.Name": "other"}),
        _pending_case(mountpoint="relative"),
        _pending_case(**{"volume.Labels": None}),
        _pending_case(**{"volume.Labels.com.docker.compose.project": "other"}),
        _pending_case(**{"volume.Labels.com.docker.compose.volume": "other"}),
        _pending_case(needs_chown=True),
        _pending_case(needs_copy_up=False),
        _pending_case(volume_use_count=1),
        _pending_case(mount_count=1),
        _pending_case(collector_present=True),
        _pending_case(**{"metadata.status": "unavailable"}),
        _pending_case(**{"metadata.children": [{"kind": "file"}]}),
        _pending_case(**{"metadata.child_count": 1}),
        _pending_case(**{"metadata.acl": True}),
        _pending_case(**{"metadata.root_is_mount": True}),
        _pending_case(**{"metadata.child_mount": True}),
        _pending_case(**{"metadata.root": None}),
        _pending_case(**{"metadata.root.mode": 0o600}),
        _pending_case(**{"metadata.root.mode": 0o722}),
        _pending_case(**{"metadata.root.uid": 0, "metadata.root.gid": 0}),
        _pending_case(**{"metadata.root.uid": 0, "metadata.root.gid": 88}),
        _pending_case(**{"metadata.root.mode": "unavailable"}),
        _pending_case(**{"metadata.free_bytes": POSITIONS.MIN_FREE_BYTES - 1}),
        _pending_case(**{"metadata.free_inodes": POSITIONS.MIN_FREE_INODES - 1}),
        _pending_case(**{"storage_space.free_bytes": POSITIONS.MIN_FREE_BYTES - 1}),
        _pending_case(**{"storage_space.free_inodes": POSITIONS.MIN_FREE_INODES - 1}),
    ],
)
def test_pending_diagnostic_matches_existing_gate(found):
    diagnostic = POSITIONS._pending_diagnostic(found)
    gate_passes = POSITIONS._pending_empty(found, owner_mismatch=True) and POSITIONS._space_is_sufficient(
        found, require_storage=True
    )
    assert (diagnostic["failed_checks"] == []) is gate_passes
    assert set(diagnostic["failed_checks"]) <= set(POSITIONS._PENDING_CHECK_NAMES)


# Frozen from the daeed133 base predicate, before diagnostics were extracted.
@pytest.mark.parametrize(
    ("found", "base_results"),
    [
        (_pending_case(), (True, True)),
        (_pending_case(**{"metadata.root.uid": 0, "metadata.root.gid": 0}), (False, True)),
        (_pending_case(**{"metadata.root.mode": 0o600}), (False, False)),
        (_pending_case(**{"metadata.status": "unavailable"}), (False, False)),
        (_pending_case(**{"metadata.free_bytes": None}), (False, False)),
        (_pending_case(**{"storage_space.free_inodes": None}), (False, False)),
    ],
)
def test_pending_gate_matches_frozen_base_acceptance(found, base_results):
    actual = tuple(
        POSITIONS._pending_empty(found, owner_mismatch=owner_mismatch)
        and POSITIONS._space_is_sufficient(found, require_storage=True)
        for owner_mismatch in (True, False)
    )
    assert actual == base_results


@pytest.mark.parametrize(
    ("mode", "owner_rwx", "unsafe_bits"),
    [
        (0o700, "complete", "absent"),
        (0o600, "incomplete", "absent"),
        (0o720, "complete", "present"),
        (0o702, "complete", "present"),
        (0o4700, "complete", "present"),
        (0o2700, "complete", "present"),
        (0o1700, "complete", "present"),
        ("unknown", "unverified", "unverified"),
    ],
)
def test_pending_diagnostic_distinguishes_mode_failures(mode, owner_rwx, unsafe_bits):
    diagnostic = POSITIONS._pending_diagnostic(_pending_case(**{"metadata.root.mode": mode}))
    assert diagnostic["owner_rwx"] == owner_rwx
    assert diagnostic["group_other_write_or_special_bits"] == unsafe_bits


@pytest.mark.parametrize(
    ("field", "mask"),
    [("group_write", 0o020), ("other_write", 0o002), ("setuid", 0o4000), ("setgid", 0o2000), ("sticky", 0o1000)],
)
def test_pending_diagnostic_classifies_each_mode_bit(field, mask):
    clear = POSITIONS._pending_diagnostic(_pending_case())
    present = POSITIONS._pending_diagnostic(_pending_case(**{"metadata.root.mode": 0o700 | mask}))

    assert clear[field] == "absent"
    assert present[field] == "present"


def test_individual_mode_bits_preserve_the_combined_failed_check():
    diagnostic = POSITIONS._pending_diagnostic(_pending_case(**{"metadata.root.mode": 0o722}))

    assert diagnostic["group_write"] == "present"
    assert diagnostic["other_write"] == "present"
    assert diagnostic["setuid"] == "absent"
    assert diagnostic["setgid"] == "absent"
    assert diagnostic["sticky"] == "absent"
    assert diagnostic["failed_checks"] == ["group_other_write_or_special_absent"]


@pytest.mark.parametrize("mode", [False, True, -1, 4096])
def test_pending_diagnostic_marks_malformed_individual_mode_bits_unverified(mode):
    found = _pending_case(**{"metadata.root.mode": mode})
    legacy_found = _pending_case(**{"metadata.root.mode": int(mode)})
    diagnostic = POSITIONS._pending_diagnostic(found)
    legacy_diagnostic = POSITIONS._pending_diagnostic(legacy_found)

    assert all(diagnostic[key] == "unverified" for key in (
        "group_write", "other_write", "setuid", "setgid", "sticky",
    ))
    assert diagnostic["owner_rwx"] == legacy_diagnostic["owner_rwx"]
    assert diagnostic["group_other_write_or_special_bits"] == legacy_diagnostic[
        "group_other_write_or_special_bits"
    ]
    assert diagnostic["failed_checks"] == legacy_diagnostic["failed_checks"]
    assert POSITIONS._pending_checks(found) == POSITIONS._pending_checks(legacy_found)
    assert POSITIONS._pending_empty(found, owner_mismatch=True) == POSITIONS._pending_empty(
        legacy_found, owner_mismatch=True
    )


def test_pending_diagnostic_keeps_unobserved_metadata_unverified():
    found = _pending_case(**{"metadata.status": "unavailable"})
    diagnostic = POSITIONS._pending_diagnostic(found)
    assert diagnostic["owner_rwx"] == "unverified"
    assert diagnostic["group_other_write_or_special_bits"] == "unverified"
    assert all(diagnostic[key] == "unverified" for key in ("group_write", "other_write", "setuid", "setgid", "sticky"))
    assert diagnostic["volume_free_bytes"] == "unverified"
    assert diagnostic["volume_free_inodes"] == "unverified"
    assert "metadata_observed" in diagnostic["failed_checks"]


def test_pending_diagnostic_marks_each_mode_bit_unverified_when_mode_is_missing():
    found = _pending_case()
    found["metadata"]["root"].pop("mode")

    diagnostic = POSITIONS._pending_diagnostic(found)

    assert all(diagnostic[key] == "unverified" for key in (
        "group_write", "other_write", "setuid", "setgid", "sticky",
    ))


@pytest.mark.parametrize("filesystem", ["volume", "graphroot"])
@pytest.mark.parametrize("counter,threshold", [
    ("free_bytes", POSITIONS.MIN_FREE_BYTES),
    ("free_inodes", POSITIONS.MIN_FREE_INODES),
])
@pytest.mark.parametrize("value,expected", [
    (None, "unverified"),
    ("bad", "unverified"),
    (0, "low"),
    ("threshold", "sufficient"),
])
def test_pending_diagnostic_separates_capacity_checks(filesystem, counter, threshold, value, expected):
    value = threshold if value == "threshold" else value
    found = _pending_case()
    if filesystem == "volume":
        found["metadata"][counter] = value
    else:
        found["storage_space"][counter] = value
    diagnostic = POSITIONS._pending_diagnostic(found)
    assert diagnostic[f"{filesystem}_{counter}"] == expected


def test_pending_diagnostic_is_read_only_and_sanitized(monkeypatch):
    found = _pending_case()
    monkeypatch.setattr(POSITIONS.subprocess, "run", lambda *_a, **_kw: pytest.fail("unexpected Podman call"))
    report = POSITIONS.survey(lambda: found)
    diagnostic = report["pending_repair_diagnostic"]
    assert set(diagnostic) == {
        "owner_rwx", "group_other_write_or_special_bits", "group_write", "other_write",
        "setuid", "setgid", "sticky", "volume_free_bytes",
        "volume_free_inodes", "graphroot_free_bytes", "graphroot_free_inodes", "failed_checks",
    }
    rendered = json.dumps(diagnostic)
    assert not any(marker in rendered for marker in ("private", "o11y", "88", "700", "Mountpoint"))
    found["storage_space"]["free_bytes"] = POSITIONS.MIN_FREE_BYTES - 1
    result = POSITIONS.repair_positions(
        lambda: found,
        lambda _found: pytest.fail("refusal diagnostics must not mutate ownership"),
    )
    assert result["status"] == "refused"
    assert result["pending_repair_diagnostic"] == POSITIONS._pending_diagnostic(found)


def test_positions_bootstrap_allows_only_proven_missing_volume_without_collision_or_collector():
    assert POSITIONS.verify(lambda: _missing_volume_found())["status"] == "bootstrap_allowed"
    for found in (
        _missing_volume_found(volume_inventory_complete=False),
        _missing_volume_found(name_collision=True),
        _missing_volume_found(collector_present=True),
    ):
        assert POSITIONS.verify(lambda found=found: found)["status"] == "refused"


@pytest.mark.parametrize("needs_chown,needs_copy_up", [(False, False), (True, False), (False, True), (True, True)])
def test_positions_bootstrap_allows_empty_unique_volume_with_boolean_flags(needs_chown, needs_copy_up):
    found = _empty_pending_volume_found(needs_chown, needs_copy_up)
    assert POSITIONS.verify(lambda: found)["status"] == (
        "bootstrap_allowed" if (needs_chown, needs_copy_up) == (False, True) else "refused"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"volume_use_count": 1},
        {"mount_count": 1},
        {"collector_present": True},
        {"metadata": _positions_state(owner=(88, 0))},
        {"metadata": _positions_state(mode=0o722)},
        {"metadata": _positions_state(mode=0o2700)},
        {"metadata": _positions_state(acl=True)},
        {"metadata": _positions_state(root_mount=True)},
        {"metadata": _positions_state(child_mount=True)},
        {"metadata": _positions_state(children=[{"kind": "file"}])},
        {"volume": {"NeedsChown": "false", "NeedsCopyUp": False}},
    ],
)
def test_positions_bootstrap_refuses_shared_changed_or_ambiguous_empty_volume(change):
    found = _empty_pending_volume_found(**change)
    assert POSITIONS.verify(lambda: found)["status"] == "refused"


@pytest.mark.parametrize("space", [
    {"free_bytes": POSITIONS.MIN_FREE_BYTES - 1, "free_inodes": 256},
    {"free_bytes": POSITIONS.MIN_FREE_BYTES, "free_inodes": POSITIONS.MIN_FREE_INODES - 1},
    {"free_bytes": "unavailable", "free_inodes": 256},
])
def test_positions_bootstrap_refuses_low_or_unverified_first_mount_space(space):
    assert POSITIONS.verify(lambda: _missing_volume_found(storage_space=space))["status"] == "refused"
    pending = _empty_pending_volume_found(storage_space=space)
    assert POSITIONS.verify(lambda: pending)["status"] == "refused"


def test_positions_pending_volume_requires_graphroot_and_volume_headroom():
    pending = _empty_pending_volume_found(needs_copy_up=True)
    pending["storage_space"] = {"free_bytes": POSITIONS.MIN_FREE_BYTES - 1,
                                "free_inodes": POSITIONS.MIN_FREE_INODES}
    assert POSITIONS.verify(lambda: pending)["status"] == "refused"


@pytest.mark.parametrize("storage_space", [None, {"status": "unavailable"}])
def test_positions_pending_bootstrap_refuses_missing_or_failed_graphroot_capacity(storage_space):
    pending = _empty_pending_volume_found(needs_copy_up=True, storage_space=storage_space)
    assert POSITIONS.verify(lambda: pending) == {
        "status": "refused", "reason": "volume_initialization_pending"
    }


def test_initialized_volume_repair_does_not_require_graphroot_capacity():
    found = _positions_found(_positions_state(owner=(88, 88)))
    found["storage_space"] = {"status": "unavailable"}
    assert POSITIONS._space_is_sufficient(found)
    assert not POSITIONS._space_is_sufficient(found, require_storage=True)


def test_positions_pending_repair_refuses_changed_volume_creation_identity():
    original = _empty_pending_volume_found(needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[]))
    replaced = _empty_pending_volume_found(needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[]))
    replaced["volume"] = {**original["volume"], "CreatedAt": "2026-10-09T12:01:00Z"}
    observations = iter((original, replaced))
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda _found: pytest.fail("changed creation identity must refuse before mutation"),
    )
    assert result == {"status": "refused", "reason": "evidence_changed"}


@pytest.mark.parametrize("malformed_stage", ["first", "fresh"])
def test_positions_pending_repair_refuses_missing_metadata_before_mutation(malformed_stage):
    original = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[], journal_cursor_valid=False),
    )
    malformed = {**original, "metadata": None}
    observations = iter((malformed,)) if malformed_stage == "first" else iter((original, malformed))
    mutations = []

    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda _found: mutations.append("chown") or True,
        mode_change_fn=lambda *_args: mutations.append("chmod") or "changed",
    )

    assert result == {"status": "refused", "reason": "evidence_changed"}
    assert mutations == []


@pytest.mark.parametrize("created_at", [
    None,
    "not-a-timestamp",
    "0001-01-01T00:00:00Z",
    "2026-10-09T12:00:00+00:60",
    "2026-10-09T12:00:00-01:99",
])
def test_positions_pending_repair_refuses_unknown_creation_time_before_chown(created_at):
    found = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[]),
    )
    if created_at is None:
        found["volume"].pop("CreatedAt")
    else:
        found["volume"]["CreatedAt"] = created_at
    result = POSITIONS.repair_positions(
        lambda: found,
        lambda _found: pytest.fail("unknown creation time must refuse before chown"),
    )
    assert result == {"status": "refused", "reason": "volume_identity_unverified"}


@pytest.mark.parametrize("created_at", [
    "2026-10-09T12:00:00+23:59",
    "2026-10-09T12:00:00-00:00",
])
def test_positions_pending_repair_changes_only_empty_volume_root_without_container_probe(created_at):
    original = _empty_pending_volume_found(needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[], journal_cursor_valid=False)
    )
    corrected = _empty_pending_volume_found(needs_copy_up=True,
        metadata=_positions_state(owner=(0, 0), mode=0o700, children=[], journal_cursor_valid=False)
    )
    original["volume"]["CreatedAt"] = created_at
    corrected["volume"]["CreatedAt"] = created_at
    observations = iter((original, original, corrected, corrected))
    calls = []
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda found: calls.append(("root-only-change", found["metadata"]["children"])) or True,
        access_test_fn=lambda *_args: pytest.fail("pending repair must not mount a probe container"),
        image_available_fn=lambda: pytest.fail("pending repair must not inspect or pull an image"),
    )
    assert result == {"status": "repaired", "reason": "pending_initialization_owner_verified"}
    assert calls == [("root-only-change", [])]


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o722])
def test_pending_mode_repair_reduces_only_write_bits_before_owner_change(mode):
    target_mode = mode & ~0o022
    original = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=mode, children=[], component_layout=False,
                                  journal_cursor_valid=False),
    )
    reduced = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=target_mode, children=[], journal_cursor_valid=False),
    )
    corrected = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(0, 0), mode=target_mode, children=[], journal_cursor_valid=False),
    )
    observations = iter((original, original, reduced, corrected, corrected))
    calls = []

    def reduce_mode(found, target):
        assert found["metadata"]["root"]["mode"] == mode
        calls.append(("mode", target))
        return "changed"

    def change_owner(found):
        assert found["metadata"]["root"]["mode"] == target_mode
        calls.append(("owner", found["metadata"]["root"]["uid"], target_mode))
        return True

    result = POSITIONS.repair_positions(
        iter(observations).__next__, change_owner, mode_change_fn=reduce_mode,
        access_test_fn=lambda *_args: pytest.fail("pending repair must not mount a probe container"),
        image_available_fn=lambda: pytest.fail("pending repair must not inspect or pull an image"),
    )

    assert result == {"status": "repaired", "reason": "pending_initialization_owner_verified"}
    assert calls == [("mode", target_mode), ("owner", 88, target_mode)]


@pytest.mark.parametrize(
    ("mode", "change"),
    [
        (0o4700, {}),
        (0o2700, {}),
        (0o1700, {}),
        (0o620, {}),
        (0o720, {"volume_inventory_complete": False}),
        (0o720, {"name_collision": True}),
        (0o720, {"project": None}),
        (0o720, {"volume": None}),
        (0o720, {"volume.Name": "other"}),
        (0o720, {"mountpoint": "relative"}),
        (0o720, {"volume.Labels": None}),
        (0o720, {"volume.Labels.com.docker.compose.project": "other"}),
        (0o720, {"volume.Labels.com.docker.compose.volume": "other"}),
        (0o720, {"needs_chown": True}),
        (0o720, {"needs_copy_up": False}),
        (0o720, {"metadata.acl": True}),
        (0o720, {"volume_use_count": 1}),
        (0o720, {"mount_count": 1}),
        (0o720, {"collector_present": True}),
        (0o720, {"metadata.status": "unavailable"}),
        (0o720, {"metadata.children": [{"kind": "file"}]}),
        (0o720, {"metadata.child_count": 1}),
        (0o720, {"metadata.root_is_mount": True}),
        (0o720, {"metadata.child_mount": True}),
        (0o720, {"metadata.root": None}),
        (0o720, {"metadata.root.uid": 0, "metadata.root.gid": 0}),
        (0o720, {"metadata.root.uid": 0, "metadata.root.gid": 88}),
        (0o720, {"metadata.free_bytes": POSITIONS.MIN_FREE_BYTES - 1}),
        (0o720, {"metadata.free_inodes": POSITIONS.MIN_FREE_INODES - 1}),
        (0o720, {"storage_space.free_inodes": POSITIONS.MIN_FREE_INODES - 1}),
        (0o720, {"storage_space.free_bytes": POSITIONS.MIN_FREE_BYTES - 1}),
    ],
)
def test_pending_mode_exception_refuses_special_bits_and_unrelated_failures(mode, change):
    found = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=mode, children=[], journal_cursor_valid=False),
    )
    for path, value in change.items():
        target = found
        parts = path.split(".")
        if parts[:2] == ["volume", "Labels"] and len(parts) > 2:
            found["volume"]["Labels"][".".join(parts[2:])] = value
            continue
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value

    result = POSITIONS.repair_positions(
        lambda: found,
        lambda _found: pytest.fail("unsafe pending mode must refuse before owner mutation"),
        mode_change_fn=lambda *_args: pytest.fail("unsafe pending mode must refuse before mode mutation"),
    )

    assert result["status"] == "refused"
    assert result["reason"] == "pending_volume_unsupported"


@pytest.mark.parametrize("mode", ["720", True, -1, 0o10000, 0o600])
def test_pending_mode_exception_refuses_unknown_or_incomplete_owner_mode(mode):
    found = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=mode, children=[], journal_cursor_valid=False),
    )

    result = POSITIONS.repair_positions(
        lambda: found,
        lambda _found: pytest.fail("invalid owner mode must refuse before owner mutation"),
        mode_change_fn=lambda *_args: pytest.fail("invalid owner mode must refuse before mode mutation"),
    )

    assert result["status"] == "refused"
    assert result["reason"] == "pending_volume_unsupported"


def test_pending_mode_repair_returns_uncertain_after_failed_mode_or_owner_mutation():
    original = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o720, children=[], journal_cursor_valid=False),
    )
    reduced = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[], journal_cursor_valid=False),
    )
    for mode_result, owner_result, expected_calls in (
        ("uncertain", True, []),
        ("changed", False, ["owner"]),
    ):
        observations = iter((original, original, reduced))
        calls = []

        def change_owner(_found, result=owner_result, recorded=calls):
            recorded.append("owner")
            return result

        def reduce_mode(*_args, result=mode_result):
            return result

        result = POSITIONS.repair_positions(
            iter(observations).__next__, change_owner,
            mode_change_fn=reduce_mode,
        )

        assert result == {"status": "uncertain", "reason": "first_mount_history_unproven"}
        assert calls == expected_calls


def test_pending_mode_repair_returns_uncertain_when_fresh_readback_changes():
    original = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o720, children=[], journal_cursor_valid=False),
    )
    changed = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, acl=True, children=[], journal_cursor_valid=False),
    )
    observations = iter((original, original, changed))

    result = POSITIONS.repair_positions(
        iter(observations).__next__,
        lambda _found: pytest.fail("failed mode readback must prevent owner mutation"),
        mode_change_fn=lambda *_args: "changed",
    )

    assert result == {"status": "uncertain", "reason": "first_mount_history_unproven"}


def test_pending_mode_repair_refuses_pre_mutation_identity_or_mode_race():
    original = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o720, children=[], journal_cursor_valid=False),
    )
    changed = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o730, children=[], journal_cursor_valid=False),
    )
    observations = iter((original, changed))

    result = POSITIONS.repair_positions(
        iter(observations).__next__,
        lambda _found: pytest.fail("pre-mutation race must prevent owner change"),
        mode_change_fn=lambda *_args: pytest.fail("pre-mutation race must prevent mode change"),
    )

    assert result == {"status": "refused", "reason": "evidence_changed"}


def test_pending_mode_repair_rerun_returns_already_correct_without_mutation():
    found = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(0, 0), mode=0o700, children=[], journal_cursor_valid=False),
    )

    result = POSITIONS.repair_positions(
        lambda: found,
        lambda *_args: pytest.fail("converged pending repair must not mutate ownership"),
        mode_change_fn=lambda *_args: pytest.fail("converged pending repair must not mutate mode"),
    )

    assert result == {"status": "already_correct", "reason": "owner_matches"}


def test_pending_mode_helper_pins_directory_and_verifies_exact_reduction(tmp_path):
    root = tmp_path / "positions"
    root.mkdir()
    root.chmod(0o720)
    before = root.stat()
    expected = {"root": {
        "uid": before.st_uid, "gid": before.st_gid, "mode": 0o720,
        "dev": before.st_dev, "ino": before.st_ino,
    }}
    prelude = r'''import builtins,io,os
original_open=builtins.open
def fake_open(path,*args,**kwargs):
    if path.startswith("/proc/self/fdinfo/"):
        return io.StringIO("mnt_id:\t1\n")
    if path=="/proc/self/mountinfo":
        return io.StringIO("1 0 0:1 / / rw - testfs /dev/test rw\n")
    return original_open(path,*args,**kwargs)
builtins.open=fake_open
os.listxattr=lambda *_args,**_kwargs: []
'''
    script = prelude + "exec(" + repr(POSITIONS._CHMOD_PENDING_SCRIPT) + ")"
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), json.dumps(expected), str(0o700)],
        check=False, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "changed"}
    assert root.stat().st_ino == before.st_ino
    assert root.stat().st_mode & 0o7777 == 0o700


def test_pending_mode_helper_reduces_untrusted_command_output_to_uncertain():
    result = POSITIONS._reduce_pending_root_mode(
        {"metadata": _positions_state(owner=(88, 88), mode=0o720), "mountpoint": "/private/path"},
        0o700,
        run=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="secret path /private/path", stderr="private detail",
        ),
    )

    assert result == "uncertain"
    assert "private" not in result


def test_pending_mode_helper_reports_uncertain_after_partial_fchmod_failure(tmp_path):
    root = tmp_path / "positions"
    root.mkdir()
    root.chmod(0o720)
    before = root.stat()
    expected = {"root": {
        "uid": before.st_uid, "gid": before.st_gid, "mode": 0o720,
        "dev": before.st_dev, "ino": before.st_ino,
    }}
    prelude = r'''import builtins,io,os
original_open=builtins.open
original_fchmod=os.fchmod
def fake_open(path,*args,**kwargs):
    if path.startswith("/proc/self/fdinfo/"):
        return io.StringIO("mnt_id:\t1\n")
    if path=="/proc/self/mountinfo":
        return io.StringIO("1 0 0:1 / / rw - testfs /dev/test rw\n")
    return original_open(path,*args,**kwargs)
def partial_fchmod(fd,mode):
    original_fchmod(fd,mode)
    raise OSError("private failure")
builtins.open=fake_open
os.listxattr=lambda *_args,**_kwargs: []
os.fchmod=partial_fchmod
'''
    script = prelude + "exec(" + repr(POSITIONS._CHMOD_PENDING_SCRIPT) + ")"
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), json.dumps(expected), str(0o700)],
        check=False, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "uncertain"}
    assert root.stat().st_mode & 0o7777 == 0o700
    assert "private failure" not in result.stdout


@pytest.mark.parametrize(
    ("script_name", "mode", "arguments", "mutator"),
    [
        ("_CHMOD_PENDING_SCRIPT", 0o720, [str(0o700)], "fchmod"),
        ("_CHOWN_SCRIPT", 0o700, ["0", "0"], "chown"),
    ],
)
@pytest.mark.parametrize("race", ["replacement", "replacement_after_open"])
def test_pinned_directory_helpers_refuse_replacement_before_mutation(
    tmp_path, script_name, mode, arguments, mutator, race,
):
    root = tmp_path / "positions"
    root.mkdir()
    root.chmod(mode)
    before = root.stat()
    expected = {"root": {
        "uid": before.st_uid, "gid": before.st_gid, "mode": mode,
        "dev": before.st_dev, "ino": before.st_ino,
    }, "children": []}
    marker = tmp_path / "mutator-called"
    prelude = r'''import builtins,io,os,sys
root=sys.argv[1]
marker=sys.argv[-1]
original_open=os.open
original_fchmod=os.fchmod
def fake_open(path,*args,**kwargs):
    if path.startswith("/proc/self/fdinfo/"):
        return io.StringIO("mnt_id:\t1\n")
    if path=="/proc/self/mountinfo":
        return io.StringIO("1 0 0:1 / / rw - testfs /dev/test rw\n")
    return builtins_open(path,*args,**kwargs)
builtins_open=builtins.open
builtins.open=fake_open
os.listxattr=lambda *_args,**_kwargs: []
def replaced_open(path,*args,**kwargs):
    if path==root and RACE=="replacement":
        os.replace(root,root+".saved")
        os.mkdir(root)
        os.chmod(root,MODE)
        return original_open(path,*args,**kwargs)
    fd=original_open(path,*args,**kwargs)
    if path==root and RACE=="replacement_after_open":
        os.replace(root,root+".saved")
        os.mkdir(root)
        os.chmod(root,MODE)
    return fd
def record_mutation(*_args,**_kwargs):
    with builtins_open(marker,"w",encoding="utf-8") as stream: stream.write("called")
'''.replace("MODE", str(mode)).replace("RACE", repr(race))
    prelude += "os.open=replaced_open\n"
    if mutator == "fchmod":
        prelude += "os.fchmod=record_mutation\n"
    else:
        prelude += "os.chown=record_mutation\n"
    script = prelude + "exec(" + repr(getattr(POSITIONS, script_name)) + ")"
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), json.dumps(expected), *arguments, str(marker)],
        check=False, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "refused"}
    assert not marker.exists()


@pytest.mark.parametrize(
    ("script_name", "mode", "arguments", "mutator"),
    [
        ("_CHMOD_PENDING_SCRIPT", 0o720, [str(0o700)], "fchmod"),
        ("_CHOWN_SCRIPT", 0o700, ["0", "0"], "chown"),
    ],
)
def test_pinned_directory_helpers_refuse_exact_overlay_mount_before_mutation(
    tmp_path, script_name, mode, arguments, mutator,
):
    root = tmp_path / "positions"
    root.mkdir()
    root.chmod(mode)
    before = root.stat()
    expected = {"root": {
        "uid": before.st_uid, "gid": before.st_gid, "mode": mode,
        "dev": before.st_dev, "ino": before.st_ino,
    }, "children": []}
    marker = tmp_path / "mutator-called"
    prelude = r'''import builtins,io,os,sys
root=sys.argv[1]
marker=sys.argv[-1]
original_open=builtins.open
def fake_open(path,*args,**kwargs):
    if path.startswith("/proc/self/fdinfo/"):
        return io.StringIO("mnt_id:\t1\n")
    if path=="/proc/self/mountinfo":
        return io.StringIO("99 1 0:1 / " + root + " rw - overlay overlay rw\n")
    return original_open(path,*args,**kwargs)
builtins.open=fake_open
os.listxattr=lambda *_args,**_kwargs: []
def record_mutation(*_args,**_kwargs):
    with original_open(marker,"w",encoding="utf-8") as stream: stream.write("called")
'''
    prelude += f"os.{mutator}=record_mutation\n"
    script = prelude + "exec(" + repr(getattr(POSITIONS, script_name)) + ")"
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), json.dumps(expected), *arguments, str(marker)],
        check=False, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "refused"}
    assert not marker.exists()


@pytest.mark.parametrize(
    ("script_name", "mode", "arguments", "mutator", "field", "observed"),
    [
        ("_CHMOD_PENDING_SCRIPT", 0o720, [str(0o700)], "fchmod", "mode", 0o500),
        ("_CHMOD_PENDING_SCRIPT", 0o720, [str(0o700)], "fchmod", "uid", 1234),
        ("_CHOWN_SCRIPT", 0o700, ["0", "0"], "chown", "mode", 0o777),
        ("_CHOWN_SCRIPT", 0o700, ["0", "0"], "chown", "gid", 1234),
    ],
)
def test_pinned_directory_helpers_refuse_fresh_mode_or_owner_drift(
    tmp_path, script_name, mode, arguments, mutator, field, observed,
):
    root = tmp_path / "positions"
    root.mkdir()
    root.chmod(mode)
    before = root.stat()
    expected = {"root": {
        "uid": before.st_uid, "gid": before.st_gid, "mode": mode,
        "dev": before.st_dev, "ino": before.st_ino,
    }, "children": []}
    marker = tmp_path / "mutator-called"
    prelude = r'''import builtins,io,os,sys
from types import SimpleNamespace
root=sys.argv[1]
marker=sys.argv[-1]
original_open=builtins.open
original_lstat=os.lstat
original_fstat=os.fstat
original_listdir=os.listdir
drift={"active":False}
def fake_open(path,*args,**kwargs):
    if path.startswith("/proc/self/fdinfo/"):
        return io.StringIO("mnt_id:\t1\n")
    if path=="/proc/self/mountinfo":
        return io.StringIO("1 0 0:1 / / rw - testfs /dev/test rw\n")
    return original_open(path,*args,**kwargs)
def drifted(st):
    values={"st_uid":st.st_uid,"st_gid":st.st_gid,"st_mode":st.st_mode,
            "st_dev":st.st_dev,"st_ino":st.st_ino}
    if FIELD=="mode": values["st_mode"]=(st.st_mode & ~0o7777) | OBSERVED
    else: values["st_"+FIELD]=OBSERVED
    return SimpleNamespace(**values)
def fake_lstat(path):
    st=original_lstat(path)
    return drifted(st) if path==root and drift["active"] else st
def fake_fstat(fd):
    st=original_fstat(fd)
    return drifted(st) if drift["active"] else st
def fake_listdir(fd):
    names=original_listdir(fd)
    drift["active"]=True
    return names
builtins.open=fake_open
os.lstat=fake_lstat
os.fstat=fake_fstat
os.listdir=fake_listdir
os.listxattr=lambda *_args,**_kwargs: []
def record_mutation(*_args,**_kwargs):
    with original_open(marker,"w",encoding="utf-8") as stream: stream.write("called")
'''.replace("FIELD", repr(field)).replace("OBSERVED", str(observed))
    prelude += f"os.{mutator}=record_mutation\n"
    script = prelude + "exec(" + repr(getattr(POSITIONS, script_name)) + ")"
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), json.dumps(expected), *arguments, str(marker)],
        check=False, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "refused"}
    assert not marker.exists()


@pytest.mark.parametrize("change_stage", ["before_chown", "after_chown"])
def test_positions_pending_repair_allows_above_threshold_counter_changes(change_stage):
    original = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[], journal_cursor_valid=False),
    )
    corrected = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(0, 0), mode=0o700, children=[], journal_cursor_valid=False),
    )
    changed = _positions_with_space_counters(original, 96 * 1024 * 1024, 512)
    verified = _positions_with_space_counters(corrected, 112 * 1024 * 1024, 768)
    mount_gate = _positions_with_space_counters(corrected, 128 * 1024 * 1024, 1024)
    observations = (
        (original, changed, verified, mount_gate)
        if change_stage == "before_chown"
        else (original, original, verified, mount_gate)
    )
    result = POSITIONS.repair_positions(
        iter(observations).__next__,
        lambda _found: True,
        access_test_fn=lambda *_args: pytest.fail("pending repair must not mount a probe container"),
        image_available_fn=lambda: pytest.fail("pending repair must not inspect or pull an image"),
    )
    assert result == {"status": "repaired", "reason": "pending_initialization_owner_verified"}


@pytest.mark.parametrize(
    ("counter_source", "space"),
    [
        ("storage_space", {"free_bytes": POSITIONS.MIN_FREE_BYTES - 1, "free_inodes": 256}),
        ("storage_space", {"free_bytes": POSITIONS.MIN_FREE_BYTES, "free_inodes": POSITIONS.MIN_FREE_INODES - 1}),
        ("metadata", {"free_bytes": POSITIONS.MIN_FREE_BYTES - 1, "free_inodes": 256}),
        ("metadata", {"free_bytes": POSITIONS.MIN_FREE_BYTES, "free_inodes": POSITIONS.MIN_FREE_INODES - 1}),
    ],
)
def test_positions_pending_repair_refuses_below_threshold_space(counter_source, space):
    overrides = {counter_source: space} if counter_source == "storage_space" else {
        "metadata": _positions_state(
            owner=(88, 88), mode=0o700, children=[], journal_cursor_valid=False, **space
        )
    }
    found = _empty_pending_volume_found(needs_copy_up=True, **overrides)
    result = POSITIONS.repair_positions(
        lambda: found,
        lambda _found: pytest.fail("low capacity must refuse before chown"),
    )
    assert result["status"] == "refused"
    assert result["reason"] == "pending_volume_unsupported"
    assert result["pending_repair_diagnostic"]["failed_checks"]


def test_positions_pending_repair_rechecks_races_before_mutation():
    original = _empty_pending_volume_found(needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[], journal_cursor_valid=False)
    )
    changed = _empty_pending_volume_found(needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o710, children=[], journal_cursor_valid=False)
    )
    observations = iter((original, changed))
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda _found: pytest.fail("fresh metadata race must refuse before owner change"),
    )
    assert result == {"status": "refused", "reason": "evidence_changed"}


def test_positions_pending_repair_preserves_uncertain_volume_after_failed_mutation():
    original = _empty_pending_volume_found(needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[], journal_cursor_valid=False)
    )
    observations = iter((original, original))
    restored = []
    result = POSITIONS.repair_positions(
        lambda: next(observations),
        lambda _found: False,
        restore_fn=lambda _found, uid, gid: restored.append((uid, gid)) or True,
        access_test_fn=lambda *_args: pytest.fail("pending repair must not mount a probe container"),
        image_available_fn=lambda: pytest.fail("pending repair must not inspect or pull an image"),
    )
    assert result == {"status": "uncertain", "reason": "first_mount_history_unproven"}
    assert restored == []


def test_positions_pending_repair_never_restores_owner_after_volume_mount():
    original = _empty_pending_volume_found(
        needs_copy_up=True,
        metadata=_positions_state(owner=(88, 88), mode=0o700, children=[], journal_cursor_valid=False),
    )
    observations = iter((original, original))
    mounted_and_removed = []

    def race_during_mutation(_found):
        mounted_and_removed.append("mount_then_remove")
        return False

    result = POSITIONS.repair_positions(
        lambda: next(observations),
        race_during_mutation,
        restore_fn=lambda *_args: pytest.fail("owner rollback is forbidden when first-mount history is unknown"),
    )
    assert result == {"status": "uncertain", "reason": "first_mount_history_unproven"}
    assert mounted_and_removed == ["mount_then_remove"]


def test_positions_post_start_empty_volume_with_initialized_flags_stays_strict_ready():
    found = _empty_pending_volume_found(
        needs_chown=False,
        needs_copy_up=True,
        collector_present=True,
        volume_use_count=1,
        mount_count=1,
        collector_mount_verified=True,
        metadata=_positions_state(owner=(0, 0), children=[{
            "name": "positions-file", "kind": "file", "uid": 0, "gid": 0,
            "nlink": 1, "mode": 0o600,
        }]),
    )
    assert POSITIONS.verify(lambda: found)["status"] == "ready"
    assert POSITIONS.verify_live(lambda: found)["status"] == "ready"


@pytest.mark.parametrize("change", [
    {"collector_present": False},
    {"volume_use_count": 0},
    {"volume_use_count": 2},
    {"mount_count": 0},
    {"mount_count": 2},
    {"collector_mount_verified": False},
])
def test_positions_live_verifier_requires_one_collector_and_exact_rw_mount(change):
    found = _positions_found(users=1)
    found.update(change)
    result = POSITIONS.verify_live(lambda: found)
    assert result["status"] == "refused"
    assert result["reason"] == "collector_mount_unverified"
    assert result["live_diagnostic"]["helper_reason"] == "collector_mount_unverified"


@pytest.mark.parametrize("metadata", [
    _positions_state(owner=(0, 0), journal_cursor_valid=False),
    _positions_state(owner=(0, 0), component_layout=False),
    _positions_state(owner=(0, 0), children=[{"kind": "other"}], component_layout=False),
])
def test_positions_post_start_refuses_partial_copy_hidden_entry_or_missing_cursor(metadata):
    found = _positions_found(metadata, users=1)
    found["needs_copy_up"] = True
    found["volume"] = {
        "Name": "o11y_journal-collector-state",
        "Labels": {"com.docker.compose.project": "o11y", "com.docker.compose.volume": "journal-collector-state"},
        "NeedsChown": False,
        "NeedsCopyUp": True,
    }
    assert POSITIONS.verify(lambda: found)["status"] == "refused"
    assert POSITIONS.verify_live(lambda: found)["status"] == "refused"


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
        template_reply = _positions_volume_reply(argv, volume)
        if template_reply is not None:
            return template_reply
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

    monkeypatch.setattr(
        POSITIONS, "_inspect_metadata",
        lambda _path, read_cursor=False, run=None: _positions_state(owner=(0, 0)),
    )
    report = POSITIONS.discover(run=run)
    assert report["status"] == (
        "observed" if source_matches else "collector_mount_unverified"
    )
    if source_matches:
        assert report["collector_mount_verified"] is True
    else:
        assert report["live_mount"]["source"] == "mismatch"


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


@pytest.mark.parametrize("change_stage", ["before_chown", "after_chown"])
def test_positions_repair_allows_above_threshold_counter_changes(change_stage):
    original = _positions_found(_positions_state(owner=(88, 88), mode=0o700))
    corrected = _positions_found(_positions_state(owner=(0, 0), mode=0o700))
    fresh = _positions_with_space_counters(original, 96 * 1024 * 1024, 512)
    verified = _positions_with_space_counters(corrected, 112 * 1024 * 1024, 768)
    mount_gate = _positions_with_space_counters(corrected, 128 * 1024 * 1024, 1024)
    observations = (
        (original, fresh, verified, mount_gate)
        if change_stage == "before_chown"
        else (original, original, verified, mount_gate)
    )
    result = POSITIONS.repair_positions(
        iter(observations).__next__,
        lambda _found: True,
        lambda _name: True,
        image_available_fn=lambda: True,
    )
    assert result == {"status": "repaired", "reason": "verified"}


@pytest.mark.parametrize(
    "metadata",
    [
        _positions_state(owner=(88, 88), free_bytes=POSITIONS.MIN_FREE_BYTES - 1),
        _positions_state(owner=(88, 88), free_inodes=POSITIONS.MIN_FREE_INODES - 1),
    ],
)
def test_positions_repair_refuses_below_threshold_space(metadata):
    assert POSITIONS.repair_positions(
        lambda: _positions_found(metadata),
        lambda _found: pytest.fail("low capacity must refuse before chown"),
        image_available_fn=lambda: True,
    ) == {"status": "refused", "reason": "insufficient_space"}


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
    assert "_journal_positions_gate_summary.status in ['ready', 'bootstrap_allowed']" in source
    assert "_journal_positions_pre_mount_status in ['ready', 'bootstrap_allowed']" in source
    plays = yaml.safe_load(source)
    apply_play = next(play for play in plays if play.get("hosts") == "o11y_svc")
    def walk(tasks):
        for task in tasks or []:
            yield task
            for key in ("block", "rescue", "always"):
                yield from walk(task.get(key))

    live_gate = next(
        task for task in walk(apply_play["tasks"])
        if task.get("name") == "Verify the live collector positions mount and access"
    )
    assert live_gate["ansible.builtin.command"]["argv"] == ["python3", "-", "verify-live"]
    assert '"journal_cursor_presence":cursor_presence' in POSITIONS_HELPER.read_text()
    assert '"children": "alloy_component_layout"' in POSITIONS_HELPER.read_text()


def _cursorless_prestart_found(**overrides):
    found = _positions_found(_positions_state(
        owner=(0, 0),
        children=[],
        journal_cursor_valid=False,
        journal_cursor_presence="absent",
    ))
    found.update({
        "project": "o11y",
        "volume": {
            "Name": "o11y_journal-collector-state",
            "Driver": "local",
            "Scope": "local",
            "Options": {},
            "Mountpoint": "/private/podman/volume/_data",
            "MountCount": 0,
            "Labels": {
                "com.docker.compose.project": "o11y",
                "com.docker.compose.volume": "journal-collector-state",
            },
        },
        "needs_chown": False,
        "needs_copy_up": False,
        "collector_present": False,
        "volume_use_count": 0,
        "mount_count": 0,
        "storage_space": {"free_bytes": 64 * 1024 * 1024, "free_inodes": 256},
        **overrides,
    })
    return found


@pytest.mark.parametrize("children", [[], [{"kind": "file", "uid": 0, "gid": 0, "nlink": 1}],
    [{"kind": "dir", "uid": 0, "gid": 0, "nlink": 2}]])
def test_positions_prestart_allows_only_observed_absent_cursor_safe_layout(children):
    found = _cursorless_prestart_found(metadata=_positions_state(
        owner=(0, 0), children=children, journal_cursor_valid=False,
        journal_cursor_presence="absent",
    ))
    result = POSITIONS.verify(lambda: found)
    assert result["status"] == "ready"
    assert result["reason"] == "positions_cursor_absent_prestart_retry"
    result = POSITIONS.verify_live(lambda: found)
    assert result["status"] == "refused"
    assert result["reason"] == "collector_mount_unverified"


@pytest.mark.parametrize("metadata", [
    _positions_state(owner=(0, 0), journal_cursor_valid=False, journal_cursor_presence="present"),
    _positions_state(owner=(0, 0), journal_cursor_valid=False, journal_cursor_presence="unknown"),
])
def test_positions_prestart_refuses_invalid_unreadable_or_unknown_cursor(metadata):
    assert POSITIONS.verify(lambda: _cursorless_prestart_found(metadata=metadata))["status"] == "refused"


@pytest.mark.parametrize("change", [
    {"collector_present": True},
    {"volume_use_count": 1},
    {"mount_count": 1},
    {"needs_chown": True},
    {"needs_copy_up": True},
    {"storage_space": None},
    {"storage_space": {"status": "unavailable"}},
    {"storage_space": {"free_bytes": POSITIONS.MIN_FREE_BYTES, "free_inodes": POSITIONS.MIN_FREE_INODES - 1}},
])
def test_positions_prestart_cursorless_retry_refuses_live_unknown_or_low_evidence(change):
    assert POSITIONS.verify(lambda: _cursorless_prestart_found(**change))["status"] == "refused"


def test_positions_live_verifier_never_returns_bootstrap_for_missing_or_pending_volume():
    assert POSITIONS.verify_live(lambda: _missing_volume_found())["status"] == "refused"
    assert POSITIONS.verify_live(lambda: _empty_pending_volume_found(needs_copy_up=True))["status"] == "refused"


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


def test_extra_component_role_diagnostics_do_not_change_prestart_or_live_refusal():
    metadata = _positions_state(
        owner=(0, 0), children=[{
            "name": "unreported-role-hash", "kind": "dir", "uid": 0, "gid": 0,
            "mode": 0o700, "nlink": 2, "dev": 7, "ino": 13,
        }],
        component_layout=False, journal_cursor_valid=False, journal_cursor_presence="absent",
    )
    metadata["top_level"] = {
        "seed_file": 0, "journal_component": 0, "other_files": 0,
        "other_directories": 1, "other_symlinks": 0, "other_kinds": 0,
        "receiver_component": 1, "exporter_component": 0,
    }
    metadata["role_metadata"] = {
        "receiver": {
            "owner": "matches_collector", "access": "read_write_execute",
            "acl": "absent", "mount": "clear", "children": "empty",
        },
        "exporter": {
            "owner": "unverified", "access": "unverified", "acl": "unverified",
            "mount": "unverified", "children": "unverified",
        },
    }
    found = _cursorless_prestart_found(metadata=metadata)
    assert POSITIONS.verify(lambda: found)["status"] == "refused"
    assert POSITIONS.verify_live(lambda: found)["status"] == "refused"


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
        prelude = f'''import builtins,io,os,sys,types
sys.argv.append("false")
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
            st_dev=st.st_dev,st_ino=st.st_ino,st_size=st.st_size,st_mtime_ns=st.st_mtime_ns)
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
        assert accepted["journal_cursor_presence"] == "present"
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
    assert refused["journal_cursor_presence"] == "absent"
    assert refused["component_layout"] is False

    component = make_component("multiple")
    for suffix in ("123", "456"):
        temp = component / f".positions.yml{suffix}"
        temp.write_text("pending")
        os.chmod(temp, 0o600)
    multiple = inspect(component)
    assert multiple["status"] == "observed"
    assert multiple["journal_cursor_presence"] == "absent"
    assert multiple["component_layout"] is False


def test_embedded_positions_metadata_marks_absence_only_after_successful_listing(tmp_path):
    root = tmp_path / "empty"
    root.mkdir(mode=0o700)
    mountpoint = root.parent
    prelude = f'''import builtins,io,os,sys
sys.argv.append("true")
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
    metadata = json.loads(result.stdout)
    assert metadata["status"] == "observed"
    assert metadata["journal_cursor_presence"] == "absent"
    assert metadata["journal_cursor_valid"] is False
    assert metadata["top_level"] == {
        "seed_file": 0, "journal_component": 0, "other_files": 0,
        "other_directories": 0, "other_symlinks": 0, "other_kinds": 0,
        "receiver_component": 0, "exporter_component": 0,
    }

    component = root / "loki.source.journal.o11y_alloy"
    component.mkdir(mode=0o700)
    positions = component / "positions.yml"
    positions.write_text("invalid positions")
    os.chmod(positions, 0o600)
    result = subprocess.run(
        [sys.executable, "-c", prelude, str(root)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    assert metadata["journal_cursor_presence"] == "present"
    assert metadata["journal_cursor_valid"] is False
    assert metadata["top_level"]["journal_component"] == 1

    unreadable_prelude = prelude.replace(
        f"exec({POSITIONS._METADATA_SCRIPT!r})",
        f'''original_os_open=os.open
def fake_os_open(path,*args,**kwargs):
    if path=={str(positions)!r}: raise PermissionError(path)
    return original_os_open(path,*args,**kwargs)
os.open=fake_os_open
exec({POSITIONS._METADATA_SCRIPT!r})''',
    )
    result = subprocess.run(
        [sys.executable, "-c", unreadable_prelude, str(root)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    assert metadata["journal_cursor_presence"] == "present"
    assert metadata["journal_cursor_valid"] is False


def test_embedded_positions_metadata_aggregates_roles_without_emitting_names_or_contents(tmp_path):
    root = tmp_path / "bounded-layout"
    component = root / "loki.source.journal.o11y_alloy"
    component.mkdir(mode=0o700, parents=True)
    (root / "alloy_seed.json").write_text("PRIVATE_CONTENT_MARKER")
    (root / "private-name-marker").write_text("PRIVATE_CONTENT_MARKER")
    (root / "private-directory-marker").mkdir()
    (root / "private-link-marker").symlink_to("private-name-marker")
    os.chmod(root, 0o700)
    os.chmod(component, 0o700)
    mountinfo = f"1 0 0:1 / {root.parent} rw - testfs /dev/test rw\n"
    prelude = (
        "import builtins,io,os,sys; sys.argv.append('false'); "
        "real_open=builtins.open; "
        f"builtins.open=lambda path,*args,**kwargs: io.StringIO({mountinfo!r}) "
        "if path=='/proc/self/mountinfo' else real_open(path,*args,**kwargs); "
        "os.listxattr=lambda *_args,**_kwargs: []; "
        f"exec({POSITIONS._METADATA_SCRIPT!r})"
    )
    result = subprocess.run(
        [sys.executable, "-c", prelude, str(root)],
        check=False, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    assert metadata["top_level"] == {
        "seed_file": 1, "journal_component": 1, "other_files": 1,
        "other_directories": 1, "other_symlinks": 1, "other_kinds": 0,
        "receiver_component": 0, "exporter_component": 0,
    }
    assert all(value <= POSITIONS.MAX_CHILDREN for value in metadata["top_level"].values())
    assert "private-name-marker" not in result.stdout
    assert "PRIVATE_CONTENT_MARKER" not in result.stdout


def test_embedded_metadata_classifies_only_exact_component_directories_without_disclosing_names(tmp_path):
    receiver_name = "otelcol.receiver.loki.journal"
    exporter_name = "otelcol.exporter.otlp.journal"
    unknown_name = "private-directory-marker"

    def inspect(name, kind="directory", *, acl=False, mounted=False, race=False, child_count=0):
        root = tmp_path / f"{name.replace('.', '-')}-{kind}-{acl}-{mounted}-{race}-{child_count}"
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        role_path = root / name
        if kind == "directory":
            role_path.mkdir(mode=0o700)
            os.chmod(role_path, 0o700)
            for index in range(child_count):
                (role_path / f"child-{index}").touch()
        elif kind == "file":
            role_path.write_text("PRIVATE_CONTENT_MARKER")
        else:
            role_path.symlink_to(root)
        mount_lines = [f"1 0 0:1 / {root.parent} rw - testfs /dev/test rw\n"]
        if mounted:
            mount_lines.append(f"2 1 0:2 / {role_path} rw - testfs /dev/test rw\n")
        prelude = f'''import builtins,io,os,sys,types
sys.argv.append("false")
root={str(root)!r}
role_path={str(role_path)!r}
role_name={name!r}
real_stat=os.stat
real_open=builtins.open
real_listxattr=getattr(os,"listxattr",lambda *_args,**_kwargs: [])
def fake_open(path,*args,**kwargs):
    if path=="/proc/self/mountinfo": return io.StringIO({''.join(mount_lines)!r})
    return real_open(path,*args,**kwargs)
builtins.open=fake_open
def fake_listxattr(path,*args,**kwargs):
    if {acl!r} and isinstance(path,int):
        return ["system.posix_acl_access"]
    return real_listxattr(path,*args,**kwargs)
os.listxattr=fake_listxattr
stat_calls={{"value":0}}
def fake_stat(path,*args,**kwargs):
    value=real_stat(path,*args,**kwargs)
    if {race!r} and path==role_name and kwargs.get("dir_fd") is not None:
        stat_calls["value"]+=1
        if stat_calls["value"]==2:
            return types.SimpleNamespace(
                st_mode=value.st_mode,st_uid=value.st_uid,st_gid=value.st_gid,
                st_dev=value.st_dev,st_ino=value.st_ino+1,st_nlink=value.st_nlink,
                st_mtime_ns=value.st_mtime_ns,
            )
    return value
os.stat=fake_stat
exec({POSITIONS._METADATA_SCRIPT!r})
'''
        result = subprocess.run(
            [sys.executable, "-c", prelude, str(root)],
            check=False, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout), result.stdout

    for name, role in ((receiver_name, "receiver"), (exporter_name, "exporter")):
        metadata, _ = inspect(name)
        assert metadata["top_level"][f"{role}_component"] == 1
        assert metadata["top_level"]["other_directories"] == 1
        assert metadata["component_layout"] is False
        assert metadata["role_metadata"][role]["children"] == "empty"
        assert metadata["role_metadata"][role]["acl"] == "absent"
        assert metadata["role_metadata"][role]["mount"] == "clear"

    metadata, _ = inspect(unknown_name)
    assert metadata["top_level"]["other_directories"] == 1
    assert metadata["top_level"]["receiver_component"] == 0
    assert metadata["top_level"]["exporter_component"] == 0
    assert set(metadata["role_metadata"]["receiver"].values()) == {"unverified"}

    for kind, count_key in (("file", "other_files"), ("symlink", "other_symlinks")):
        metadata, _ = inspect(receiver_name, kind)
        assert metadata["top_level"]["receiver_component"] == 0
        assert metadata["top_level"][count_key] == 1
        assert metadata["component_layout"] is False
        assert set(metadata["role_metadata"]["receiver"].values()) == {"unverified"}

    for kwargs, field, expected in (
        ({"acl": True}, "acl", "present"),
        ({"mounted": True}, "mount", "ambiguous"),
        ({"race": True}, "children", "unverified"),
        ({"child_count": 33}, "children", "unverified"),
    ):
        metadata, _ = inspect(receiver_name, **kwargs)
        assert metadata["role_metadata"]["receiver"][field] == expected
        if kwargs.get("race") or kwargs.get("child_count"):
            assert set(metadata["role_metadata"]["receiver"].values()) == {"unverified"}

    visible = json.dumps(POSITIONS._live_diagnostic({"status": "observed", "metadata": metadata}))
    assert receiver_name not in visible and exporter_name not in visible and unknown_name not in visible
    assert "PRIVATE_CONTENT_MARKER" not in visible
    assert re.search(r"[0-9a-f]{64}", visible) is None


def test_embedded_positions_metadata_retries_rename_that_finishes_during_observation(tmp_path):
    root = tmp_path / "rename-during-observation"
    component = root / "loki.source.journal.o11y_alloy"
    component.mkdir(mode=0o700, parents=True)
    os.chmod(root, 0o700)
    os.chmod(component, 0o700)
    temp = component / ".positions.yml123456789"
    positions = component / "positions.yml"
    temp.write_text("invalid positions")
    os.chmod(temp, 0o600)
    prelude = f'''import builtins,io,os,sys,time
sys.argv.append("true")
original_open=builtins.open
def fake_open(path,*args,**kwargs):
    if path=="/proc/self/mountinfo":
        return io.StringIO("1 0 0:1 / {root.parent} rw - testfs /dev/test rw\\n")
    return original_open(path,*args,**kwargs)
builtins.open=fake_open
os.listxattr=lambda *_args,**_kwargs: []
time.sleep=lambda _seconds: os.rename({str(temp)!r},{str(positions)!r})
exec({POSITIONS._METADATA_SCRIPT!r})
'''
    result = subprocess.run(
        [sys.executable, "-c", prelude, str(root)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    metadata = json.loads(result.stdout)
    assert metadata["status"] == "observed"
    assert metadata["journal_cursor_presence"] == "present"
    assert metadata["journal_cursor_valid"] is False
    found = _cursorless_prestart_found(metadata=_positions_state(
        owner=(0, 0), children=[{"kind": "dir", "uid": 0, "gid": 0, "nlink": 2}],
        journal_cursor_valid=metadata["journal_cursor_valid"],
        journal_cursor_presence=metadata["journal_cursor_presence"],
    ))
    assert POSITIONS.verify(lambda: found)["status"] == "refused"


def test_embedded_positions_metadata_refuses_public_files_and_writable_directories(tmp_path):
    def inspect(component):
        root = component.parent
        mountpoint = root.parent
        prelude = f'''import builtins,io,os,sys
sys.argv.append("false")
original_open=builtins.open
original_os_open=os.open
def fake_open(path,*args,**kwargs):
    if str(path).endswith("/positions.yml"): raise AssertionError("survey read positions content")
    if path=="/proc/self/mountinfo":
        return io.StringIO("1 0 0:1 / {mountpoint} rw - testfs /dev/test rw\\n")
    return original_open(path,*args,**kwargs)
builtins.open=fake_open
def fake_os_open(path,*args,**kwargs):
    if str(path).endswith("/positions.yml"): raise AssertionError("survey opened positions content")
    return original_os_open(path,*args,**kwargs)
os.open=fake_os_open
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
        ("hidden-entry", "hidden", 0o600),
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
            filename = (
                ".unexpected" if target == "hidden"
                else "positions.yml" if target == "positions"
                else ".positions.yml123"
            )
            file = component / filename
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
    assert all(step in script for step in ("printf x", "mv -n --", "rm --", "trap"))
    assert "--pull=always" not in probe


def test_positions_helper_mutates_only_root_and_fails_closed_on_inner_recheck():
    assert "os.chown(root_fd,target_uid,target_gid)" in POSITIONS_HELPER.read_text()
    assert "os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)" in POSITIONS_HELPER.read_text()
    assert "os.fstat(root_fd)" in POSITIONS_HELPER.read_text()
    assert "str(target_uid), str(target_gid)" in POSITIONS_HELPER.read_text()
    assert "chown -R" not in POSITIONS_HELPER.read_text()
    assert "os.chmod(" not in POSITIONS_HELPER.read_text()
    assert 'subprocess.run(["chmod"' not in POSITIONS_HELPER.read_text()
    assert "listxattr" in POSITIONS_HELPER.read_text()
    assert "os.listdir(root_fd)" in POSITIONS_HELPER.read_text()
    assert "open(os.path.join" not in POSITIONS_HELPER.read_text()


def test_positions_chown_refuses_when_mountpoint_path_is_replaced_before_mutation(tmp_path):
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
    marker = tmp_path / "chown-called"
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
os.chown=lambda *_args,**_kwargs: original_builtin_open({str(marker)!r},"w").write("called")
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
        [sys.executable, "-c", script, str(root), expected, str(stat.st_uid), str(stat.st_gid), str(marker)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "refused"}
    assert not marker.exists()
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
        ("permission denied writing /alloy-state/private", "positions"),
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
    assert "--storage.path=/alloy-state" in service["command"]
    assert "journal-collector-state:/alloy-state" in service["volumes"]
    assert "journal-collector-state" in compose["volumes"]
    assert compose["volumes"]["journal-collector-state"]["labels"] == {
        "com.docker.compose.volume": "journal-collector-state"
    }
    assert POSITIONS.DATA_PATH == "/alloy-state"
    playbook_source = PLAYBOOK.read_text()
    assert "/alloy-state/.positions-live-gate." in playbook_source
    assert playbook_source.count('eq .Destination \\"/alloy-state\\"') == 2
    assert "select('search', '/var/lib/alloy|/alloy-state')" in playbook_source


def test_positions_survey_exception_keeps_bounded_receipt_schema(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(POSITIONS_HELPER), "survey"])
    monkeypatch.setattr(POSITIONS, "survey", lambda: (_ for _ in ()).throw(RuntimeError("private detail")))

    POSITIONS.main()

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "status": "unavailable",
        "reason": "survey_failed",
        "volume_use_count": 0,
        "collector_present": False,
        "entry_count": 0,
        "root_owner": "unavailable",
        "root_mode_access": "unavailable",
        "acl": "unavailable",
        "mount": "unavailable",
        "children": "unavailable",
        "journal_cursor": "unavailable",
        "free_space": "unavailable",
        "survey_diagnostics": POSITIONS._unverified_diagnostics(),
        "pending_repair_diagnostic": POSITIONS._pending_diagnostic({}),
        "live_diagnostic": {
            **POSITIONS._live_diagnostic({"status": "unavailable"}),
            "helper_reason": "survey_failed",
            "host_uid_map": "not_run",
            "collector_uid_map": "not_run",
            "uid_maps": "not_run",
            "namespace_probe": "not_run",
        },
    }
    _assert_positions_survey_diagnostics_schema(report, has_reason=True)
    assert "private detail" not in json.dumps(report)


def test_positions_check_mode_receipt_keeps_new_mode_fields_unverified():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    survey_play = next(
        play for play in plays
        if play.get("name") == "Survey fixed standard journal directory candidates"
    )
    task = next(
        task for task in survey_play["tasks"]
        if task.get("name") == "Record positions-volume survey as unverified in check mode"
    )
    report = json.loads(task["ansible.builtin.set_fact"]["_journal_positions_survey"]["stdout"])

    assert report["status"] == "check_mode_unverified"
    assert all(report["pending_repair_diagnostic"][key] == "unverified" for key in (
        "group_write", "other_write", "setuid", "setgid", "sticky",
    ))


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
    assert "container_inventory_unavailable" in source
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
    assert positions_survey.get("no_log") is not True
    assert positions_survey["when"] == "not ansible_check_mode"
    assert positions_survey["ansible.builtin.command"]["argv"] == ["python3", "-", "survey"]
    assert "/alloy-state/.positions-live-gate." not in str(positions_survey)
    assert not any("volume create" in str(task) for task in survey_play["tasks"])
    assert not any(
        "chown" in " ".join(task.get("ansible.builtin.command", {}).get("argv", []))
        for task in survey_play["tasks"]
    )

    repair_play = next(
        play for play in plays
        if play.get("name") == "Repair only the journal positions volume root ownership"
    )
    repair_task = next(
        task for task in repair_play["tasks"]
        if task.get("name") == "Run the separately selected guarded positions repair"
    )
    assert repair_task.get("no_log") is not True
    assert repair_task["when"] == "not ansible_check_mode"
    assert repair_task["ansible.builtin.command"]["argv"] == ["python3", "-", "repair-positions"]

    pre_mount = next(
        task for task in apply_play["tasks"]
        if task.get("name") == "Recheck positions volume initialization immediately before mount"
    )
    apply_block = next(
        task
        for task in apply_play["tasks"]
        if task.get("name") == "Apply the collector and require exact-target Loki delivery"
    )
    assert apply_block["when"] == "not ansible_check_mode"
    apply_task_names = [task.get("name") for task in apply_block["block"]]
    assert apply_play["tasks"].index(pre_mount) + 3 == apply_play["tasks"].index(apply_block)
    assert apply_play["tasks"][apply_play["tasks"].index(pre_mount) + 1]["name"] == (
        "Parse immediate pre-mount positions output through a protected boundary"
    )
    assert pre_mount["register"] == "_journal_positions_pre_mount"
    assert pre_mount.get("no_log") is not True
    assert apply_task_names.index("Start only the journal collector service") < apply_task_names.index(
        "Verify the live collector positions mount and access"
    ) < apply_task_names.index("Read the Podman rootless UID mapping") < apply_task_names.index(
        "Observe the live collector UID mapping for diagnostics"
    ) < apply_task_names.index(
        "Verify effective write and rename access in the live collector namespace"
    ) < apply_task_names.index("Report sanitized live positions diagnostic")
    live_write_probe = next(
        task for task in apply_block["block"]
        if task.get("name") == "Verify effective write and rename access in the live collector namespace"
    )
    live_mount = next(
        task for task in apply_block["block"]
        if task.get("name") == "Verify the live collector positions mount and access"
    )
    host_uid_map = next(
        task for task in apply_block["block"]
        if task.get("name") == "Read the Podman rootless UID mapping"
    )
    collector_uid_map = next(
        task for task in apply_block["block"]
        if task.get("name") == "Observe the live collector UID mapping for diagnostics"
    )
    assert live_mount.get("no_log") is not True
    assert host_uid_map["no_log"] is True
    assert collector_uid_map["no_log"] is True
    assert collector_uid_map["ansible.builtin.command"]["argv"] == [
        "podman", "exec", "o11y-journal-collector", "cat", "/proc/self/uid_map",
    ]
    assert live_write_probe["no_log"] is True
    live_probe_script = " ".join(live_write_probe["ansible.builtin.command"]["argv"])
    assert "uid_map" in live_probe_script and "mv -n" in live_probe_script and "printf x" in live_probe_script
    assert all(stage in live_probe_script for stage in ("identity", "create", "write", "rename", "cleanup"))
    assert "_journal_namespace_probe" not in source
    assert "_journal_collector_uid_map.stdout | trim == _journal_host_uid_map.stdout | trim" in source
    assert "created=0" in live_probe_script
    assert "if ! a=$(mktemp /alloy-state/.positions-live-gate.XXXXXX); then fail; fi;" in live_probe_script
    assert 'case "$a" in /alloy-state/.positions-live-gate.*)' in live_probe_script
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
    assert rollback_classifier["no_log"] is True
    assert "permission-denied mount targets=" in failure_message
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


def test_positions_visibility_is_limited_to_bounded_helper_and_uid_probe_results():
    plays = yaml.safe_load(PLAYBOOK.read_text())

    def walk(tasks):
        for task in tasks or []:
            yield task
            for key in ("block", "rescue", "always"):
                yield from walk(task.get(key))

    tasks = [task for play in plays for task in walk(play.get("tasks"))]
    by_name = {task.get("name"): task for task in tasks}
    visible = {
        "Verify positions volume identity and access before apply",
        "Recheck positions volume initialization immediately before mount",
        "Verify the live collector positions mount and access",
        "Survey existing positions volume metadata without mounting it",
        "Run the separately selected guarded positions repair",
    }
    protected = {
        "Read the Podman rootless UID mapping",
        "Verify effective write and rename access in the live collector namespace",
        "Require a recent exact-name entry in the selected journal",
        "Require the pinned rootless Alloy process to read the journal bind",
        "Survey rootless journal path and exact-name file readability",
        "Render a candidate journal config with the private receiver endpoint",
        "Validate the candidate config with the pinned Alloy image before any collector recreation",
        "Read the validated candidate config digest",
        "Promote the validated candidate config for the collector",
    }

    assert visible | protected <= by_name.keys()
    assert all(by_name[name].get("no_log") is not True for name in visible)
    assert all(by_name[name].get("no_log") is True for name in protected)
    helper_actions = {
        "Verify positions volume identity and access before apply": "verify",
        "Recheck positions volume initialization immediately before mount": "verify",
        "Verify the live collector positions mount and access": "verify-live",
        "Survey existing positions volume metadata without mounting it": "survey",
        "Run the separately selected guarded positions repair": "repair-positions",
    }
    for name, action in helper_actions.items():
        assert by_name[name]["ansible.builtin.command"]["argv"] == ["python3", "-", action]

    found = _positions_found(_positions_state(owner=(0, 0)))
    survey = POSITIONS.survey(lambda: found)
    verified = POSITIONS.verify(lambda: found)
    repair = POSITIONS.repair_positions(lambda: found)
    assert set(survey) == {
        "status", "volume_use_count", "collector_present", "entry_count", "root_owner",
        "root_mode_access", "acl", "mount", "children",
        "journal_cursor", "free_space", "pending_repair_diagnostic", "live_diagnostic",
    }
    assert set(verified) == (
        set(survey) - {"pending_repair_diagnostic", "live_diagnostic"}
    ) | {"reason"}
    assert repair == {"status": "already_correct", "reason": "owner_matches"}
    assert not any(value in str((survey, verified, repair)) for value in ("mountpoint", "positions.yml", "/private/"))


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
    positions_command = next(
        task
        for task in survey_play["tasks"]
        if task.get("name") == "Survey existing positions volume metadata without mounting it"
    )
    positions_check_mode = next(
        task
        for task in survey_play["tasks"]
        if task.get("name") == "Record positions-volume survey as unverified in check mode"
    )
    check_mode_result = next(
        task
        for task in survey_play["tasks"]
        if task.get("name") == "Record check-mode survey as unverified without probing the container runtime"
    )

    assert survey_command["when"] == "not ansible_check_mode"
    assert "check_mode" not in survey_command
    assert positions_command["when"] == "not ansible_check_mode"
    assert "check_mode" not in positions_command
    assert positions_check_mode["when"] == "ansible_check_mode"
    positions_result = json.loads(
        positions_check_mode["ansible.builtin.set_fact"]["_journal_positions_survey"]["stdout"]
    )
    assert positions_result["status"] == "check_mode_unverified"
    assert positions_result["pending_repair_diagnostic"] == POSITIONS._pending_diagnostic({})
    assert positions_result["journal_cursor"] == "unavailable"
    assert positions_result["free_space"] == "unavailable"
    assert positions_result["survey_diagnostics"]["named_volume"]["identity"] == "unverified"
    assert positions_result["survey_diagnostics"]["named_volume"]["template_identity"] == "unverified"
    assert (
        positions_result["survey_diagnostics"]["named_volume"]["template_fields"]["NeedsCopyUp"]["value"]
        == "unavailable"
    )
    assert positions_result["survey_diagnostics"]["podman_version"] == {
        "client": "unavailable", "server": "unavailable"
    }
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


def test_stale_exact_loki_receipt_fails_and_rollback_preserves_positions_volume():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    apply = next(play for play in plays if play.get("hosts") == "o11y_svc")
    apply_task = next(
        task for task in apply["tasks"]
        if task.get("name") == "Apply the collector and require exact-target Loki delivery"
    )
    delivery = next(
        task for task in apply_task["block"]
        if task.get("name") == "Require an exact service and signal stream in Loki"
    )
    assert any(
        "values[0][0] | int >= _journal_verification_start.stdout" in item
        for item in delivery["ansible.builtin.assert"]["that"]
    )
    rollback = next(
        task for task in apply_task["rescue"]
        if task.get("name") == "Force-remove only the failed collector while retaining positions"
    )
    assert rollback["ansible.builtin.command"]["argv"] == [
        "podman", "rm", "--force", "--time", "10", "o11y-journal-collector",
    ]
    assert "-v" not in rollback["ansible.builtin.command"]["argv"]


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
        (
            "(_journal_positions_repair.stdout | default('{}') | from_json).status in "
            "['repaired', 'already_correct', 'check_mode_unverified']"
        ),
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
        {"rc": 125, "stdout": "private stderr", "expected": "unavailable", "targets": []},
        {"rc": 0, "stdout": "", "expected": "empty", "targets": []},
        {
            "rc": 0,
            "stdout": "permission denied at /private/path token=secret",
            "expected": "permission_denied",
            "targets": [],
        },
        {
            "rc": 0,
            "stdout": "open /var/log/journal: permission denied token=secret",
            "expected": "permission_denied",
            "targets": ["journal"],
        },
        {
            "rc": 0,
            "stdout": "permission denied /alloy-state/private\npermission denied /etc/alloy/journal.alloy",
            "expected": "permission_denied",
            "targets": ["positions", "config"],
        },
        {
            "rc": 0,
            "stdout": "permission denied /var/lib/alloy/data/private",
            "expected": "permission_denied",
            "targets": ["positions"],
        },
        {
            "rc": 0,
            "stdout": "failed to load config /private/path token=secret",
            "expected": "config_error",
            "targets": [],
        },
        {"rc": 0, "stdout": "no such file or directory: /private/path", "expected": "missing_file", "targets": []},
        {
            "rc": 0,
            "stdout": "connection refused to 203.0.113.7 header=Bearer secret",
            "expected": "connection_refused",
            "targets": [],
        },
        {
            "rc": 0,
            "stdout": "unrecognized startup issue /private/path token=secret",
            "expected": "other",
            "targets": [],
        },
        {
            "rc": 0,
            "stdout": "",
            "stderr": "failed to parse config /private/path token=secret",
            "expected": "config_error",
            "targets": [],
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
permission_targets = []
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
    targets = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_permission_targets"])
    )
    permission_targets.append(targets)
    count = str(Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expressions["_journal_rollback_log_count"])
    )).strip()
    log_counts.append(int(count))
    failure_message = next(
        task for task in apply["rescue"]
        if task.get("name") == "Fail closed after stopping the journal collector"
    )["ansible.builtin.fail"]["msg"]
    variables.update({
        "_journal_rollback_log_class": result,
        "_journal_rollback_log_count": int(count),
        "_journal_rollback_permission_targets": targets,
    })
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
    "permission_targets": permission_targets,
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
    assert rendered["log_counts"] == [0, 0, 1, 1, 2, 1, 1, 1, 1, 1, 1]
    assert rendered["permission_targets"] == [case["targets"] for case in log_cases]
    assert "collector logs classification/count=config_error/1" in rendered["failure_outputs"][-1]
    assert "permission-denied mount targets=['positions', 'config']" in rendered["failure_outputs"][4]
    assert "permission-denied mount targets=['positions']" in rendered["failure_outputs"][5]
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


def test_live_namespace_create_failure_reports_create_without_relative_cleanup(tmp_path):
    plays = yaml.safe_load(PLAYBOOK.read_text())
    apply_play = next(play for play in plays if play.get("hosts") == "o11y_svc")
    apply_block = next(
        task for task in apply_play["tasks"]
        if task.get("name") == "Apply the collector and require exact-target Loki delivery"
    )
    probe = next(
        task for task in apply_block["block"]
        if task.get("name") == "Verify effective write and rename access in the live collector namespace"
    )
    script = probe["ansible.builtin.command"]["argv"][-1]
    identity_checks = (
        'exec 2>/dev/null; test "$EUID" -eq 0; mapping=$(</proc/self/uid_map); '
        'read -r inside outside length <<< "$mapping"; test "$inside" = 0; test "$length" -gt 0;'
    )
    assert identity_checks in script
    script = script.replace(identity_checks, "exec 2>/dev/null; mapping='0 1 1';")
    unrelated = tmp_path / "create"
    unrelated.write_text("preserve")
    result = subprocess.run(
        ["bash", "-c", "mktemp() { return 1; };\n" + script],
        cwd=tmp_path, check=False, capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert result.stdout == "create\n"
    assert result.stderr == ""
    assert unrelated.read_text() == "preserve"


@pytest.mark.parametrize(
    "stdout",
    ["", "not-json", "[]", "null", '"ready"', '{"status":"running"}'],
)
def test_positions_gate_malformed_or_unexpected_json_is_unavailable(stdout):
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        parsed = None
    status = parsed.get("status") if isinstance(parsed, dict) else None
    normalized_status = status if status in {"ready", "bootstrap_allowed"} else "unavailable"

    plays = yaml.safe_load(PLAYBOOK.read_text())
    apply_play = next(play for play in plays if play.get("hosts") == "o11y_svc")
    tasks = apply_play["tasks"]
    boundary = next(
        task for task in tasks
        if task.get("name") == "Parse positions apply-gate output through a protected boundary"
    )
    parse_task, object_assert, normalize_task = boundary["block"]
    rescue = boundary["rescue"][0]
    gate = next(
        task for task in tasks
        if task.get("name") == "Require positions access and mount identity before apply"
    )
    debug = next(
        task for task in tasks
        if task.get("name") == "Report the bounded positions apply-gate result"
    )
    pre_mount_boundary = next(
        task for task in tasks
        if task.get("name") == "Parse immediate pre-mount positions output through a protected boundary"
    )
    pre_mount_gate = next(
        task for task in tasks
        if task.get("name") == "Require positions volume remains safe to mount"
    )
    apply_block = next(
        task for task in tasks
        if task.get("name") == "Apply the collector and require exact-target Loki delivery"
    )
    live_boundary = next(
        task for task in apply_block["block"]
        if task.get("name") == "Parse live positions output through a protected boundary"
    )
    live_debug = next(
        task for task in apply_block["block"]
        if task.get("name") == "Report sanitized live positions diagnostic"
    )
    live_gate = next(
        task for task in apply_block["block"]
        if task.get("name") == "Require the actual positions volume mount and namespace access"
    )

    assert normalized_status == "unavailable"
    assert parse_task["ansible.builtin.set_fact"]["_journal_positions_gate_parsed"] == (
        "{{ _journal_positions_gate.stdout | from_json }}"
    )
    assert parse_task["no_log"] is True
    assert object_assert["ansible.builtin.assert"]["that"] == "_journal_positions_gate_parsed is mapping"
    assert object_assert["no_log"] is True
    assert normalize_task["no_log"] is True
    assert rescue["ansible.builtin.set_fact"]["_journal_positions_gate_summary"] == {
        "status": "unavailable", "root_owner": "unavailable",
        "root_mode_access": "unavailable", "entry_count": 0,
    }
    assert rescue["no_log"] is True
    assert gate["ansible.builtin.assert"]["that"] == [
        "_journal_positions_gate.rc | default(1) == 0",
        "_journal_positions_gate_summary.status in ['ready', 'bootstrap_allowed']",
    ]
    assert pre_mount_boundary["block"][0]["no_log"] is True
    assert pre_mount_boundary["block"][1]["no_log"] is True
    assert pre_mount_boundary["block"][2]["no_log"] is True
    assert pre_mount_boundary["rescue"][0]["ansible.builtin.set_fact"] == {
        "_journal_positions_pre_mount_status": "unavailable",
    }
    assert pre_mount_boundary["rescue"][0]["no_log"] is True
    assert pre_mount_gate["ansible.builtin.assert"]["that"] == [
        "_journal_positions_pre_mount.rc | default(1) == 0",
        "_journal_positions_pre_mount_status in ['ready', 'bootstrap_allowed']",
    ]
    assert live_boundary["block"][0]["no_log"] is True
    assert live_boundary["block"][1]["no_log"] is True
    assert live_boundary["block"][2]["no_log"] is True
    assert live_boundary["rescue"][0]["ansible.builtin.set_fact"] == {
        "_journal_positions_live_summary": {
            "status": "unavailable",
            "live_diagnostic": {
                "role_metadata": {
                    "receiver": {
                        "owner": "unverified", "access": "unverified", "acl": "unverified",
                        "mount": "unverified", "children": "unverified",
                    },
                    "exporter": {
                        "owner": "unverified", "access": "unverified", "acl": "unverified",
                        "mount": "unverified", "children": "unverified",
                    },
                },
            },
        },
    }
    assert live_boundary["rescue"][0]["no_log"] is True
    assert "_journal_positions_live_mount.stdout" not in live_debug["ansible.builtin.debug"]["msg"]
    assert "from_json" not in live_debug["ansible.builtin.debug"]["msg"]
    assert live_gate["ansible.builtin.assert"]["that"][1] == (
        "_journal_positions_live_summary.status == 'ready'"
    )
    assert "stdout" not in gate["ansible.builtin.assert"]["fail_msg"]
    assert "stdout" not in pre_mount_gate["ansible.builtin.assert"]["fail_msg"]
    assert "stdout" not in debug["ansible.builtin.debug"]["msg"]
    assert "from_json" not in debug["ansible.builtin.debug"]["msg"]
