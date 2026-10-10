"""Source-contract tests for the direct Podman Alloy journal proof."""

import importlib.util
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/prove-o11y-journal-volume.py"
SPEC = importlib.util.spec_from_file_location("o11y_journal_direct_proof", HELPER)
PROOF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROOF)


def test_bounded_scan_records_safe_directories_symlinks_and_files():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "empty-dir").mkdir()
        (root / "nested").mkdir()
        (root / "nested" / "fixture").write_bytes(b"synthetic")
        (root / "link").symlink_to("nested/fixture")

        found = PROOF.scandir_manifest(root)

    assert found["empty-dir"][0] == "dir"
    assert found["link"][:2] == ("symlink", "nested/fixture")
    assert found["nested/fixture"][:3] == (
        "file", 9, PROOF.hashlib.sha256(b"synthetic").hexdigest()
    )


def test_target_state_scan_refuses_symlinks_and_special_files():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "outside").mkdir()
        (root / "alias").symlink_to("outside")
        with pytest.raises(PROOF.Refused, match="state_path_unsafe"):
            PROOF.scandir_manifest(root, target_state=True)


def test_bounded_scan_refuses_too_many_entries():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "one").touch()
        (root / "two").touch()
        with patch.object(PROOF, "MAX_SCAN_ENTRIES", 1), pytest.raises(PROOF.Refused, match="layout_unbounded"):
            PROOF.scandir_manifest(root)


def test_copy_positive_control_ignores_benign_directory_metadata_but_requires_file_bytes():
    expected = {
        "zone": ("dir", 0o755),
        "zone/utc": ("symlink", "Etc/UTC"),
        "zone/data": ("file", 4, "digest"),
    }
    copied = {
        "zone": ("dir", 0o700),
        "zone/utc": ("symlink", "Etc/UTC"),
        "zone/data": ("file", 4, "digest"),
        "zone/extra-empty": ("dir", 0o755),
    }
    assert PROOF.copied_manifest(expected, copied)
    assert not PROOF.copied_manifest(expected, {"zone/data": ("file", 4, "other")})
    assert not PROOF.copied_manifest(expected, {"zone": ("dir", 0o755)})


def test_direct_volume_mount_policy_is_explicit_and_rejects_unowned_names():
    name = "jproof-" + "a" * 32
    assert PROOF.volume_mount_arg(name, "/usr/share/zoneinfo", no_copy=False) == f"{name}:/usr/share/zoneinfo:rw"
    assert PROOF.volume_mount_arg(name, "/alloy-state", no_copy=True) == f"{name}:/alloy-state:rw,nocopy"
    with pytest.raises(PROOF.Refused, match="volume_identity_unverified"):
        PROOF.volume_mount_arg("receiver-state", "/alloy-state", no_copy=True)


@pytest.mark.parametrize(("no_copy", "expected_result", "after"), [
    (False, "copied", {"zone/data": ("file", 4, "digest")} ),
    (True, "no_copy", {}),
])
def test_direct_mount_behavior_uses_same_image_control_and_selected_copy_mode(
    no_copy, expected_result, after
):
    image_id = "sha256:" + "a" * 64
    inspect = {"Id": "b" * 64, "Mounts": []}
    with (patch.object(PROOF, "volume_create", return_value=("jproof-" + "c" * 32, {})),
          patch.object(PROOF, "volume_scan", side_effect=[{}, after]),
          patch.object(PROOF, "create_container", return_value=inspect) as create,
          patch.object(PROOF, "mount_check") as check_mount,
          patch.object(PROOF, "run", return_value=b"0"),
          patch.object(PROOF, "remove_owned_container") as remove):
        result = PROOF.prove_mount_behavior(
            "podman", "jproof-owner", image_id, "/usr/share/zoneinfo",
            {"zone/data": ("file", 4, "digest")}, no_copy=no_copy,
        )

    assert result == expected_result
    assert create.call_args.args[3:5] == ("copy" if not no_copy else "nocopy", image_id)
    assert create.call_args.args[5] == [
        PROOF.volume_mount_arg("jproof-" + "c" * 32, "/usr/share/zoneinfo", no_copy=no_copy)
    ]
    check_mount.assert_called_once()
    remove.assert_called_once()


def test_image_positive_control_comes_from_same_image_tree_and_requires_regular_data():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "alloy-state").mkdir()
        (root / "etc" / "ssl" / "certs").mkdir(parents=True)
        (root / "etc" / "ssl" / "certs" / "proof.pem").write_bytes(b"public fixture")
        (root / "etc" / "ssl" / "certs" / "alias.pem").symlink_to("proof.pem")
        state, positive_path, positive = PROOF.image_manifest(root)

    assert state == {}
    assert positive_path == "etc/ssl/certs"
    assert positive["proof.pem"][0] == "file"
    assert positive["alias.pem"][:2] == ("symlink", "proof.pem")


def test_cursor_parser_accepts_expected_journal_cursor_only():
    cursor = "s=1;i=2;b=3;m=4;t=5;x=6"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "positions.yml").write_text(
            "positions:\n  ? path: cursor-loki.source.journal.o11y_alloy\n    labels: \"\"\n"
            f"  : {cursor}\n",
            encoding="utf-8",
        )
        component = root / "loki.source.journal.o11y_alloy"
        component.mkdir()
        (root / "positions.yml").rename(component / "positions.yml")
        assert PROOF.cursor_from_manifest(root) == cursor
        (component / "positions.yml").write_text("positions: {}\n", encoding="utf-8")
        with pytest.raises(PROOF.Refused, match="cursor_unavailable"):
            PROOF.cursor_from_manifest(root)


def test_cleanup_identity_must_match_exact_container_and_volume_receipts():
    owner = "jproof-owner"
    container = {
        "Id": "a" * 64,
        "Image": "sha256:" + "b" * 64,
        "Created": "created-once",
        "Config": {"Labels": {PROOF.PROOF_OWNER: owner, PROOF.PROOF_ROLE: "cursor"}},
    }
    expected_container = {"Id": "a" * 64, "Image": "sha256:" + "b" * 64,
                          "Created": "created-once", "role": "cursor"}
    assert PROOF.container_identity_matches(container, expected_container, owner)
    drifted_container = {**container, "Created": "changed"}
    assert not PROOF.container_identity_matches(drifted_container, expected_container, owner)

    volume = {
        "Name": "jproof-volume",
        "CreatedAt": "created-once",
        "Driver": "local",
        "Scope": "local",
        "Options": None,
        "Mountpoint": "/private/internal/path",
        "Labels": {PROOF.PROOF_OWNER: owner, PROOF.PROOF_ROLE: "cursor"},
        "MountCount": 0,
    }
    expected_volume = {"identity": PROOF.volume_identity(volume)}
    assert PROOF.volume_identity_matches(volume, expected_volume, owner, "cursor")
    assert not PROOF.volume_identity_matches({**volume, "MountCount": 1}, expected_volume, owner, "cursor")
    assert not PROOF.volume_identity_matches({**volume, "CreatedAt": "changed"}, expected_volume, owner, "cursor")


def test_cleanup_removes_only_the_exact_owned_container_and_refuses_drift():
    owner = "jproof-owner"
    container_id = "a" * 64
    expected = {"Id": container_id, "Image": "sha256:" + "b" * 64,
                "Created": "created-once", "owner": owner, "role": "cursor"}
    inspect = {
        "Id": container_id,
        "Image": expected["Image"],
        "Created": expected["Created"],
        "Config": {"Labels": {PROOF.PROOF_OWNER: owner, PROOF.PROOF_ROLE: "cursor"}},
        "State": {"Running": False},
        "Mounts": [],
    }
    PROOF.OWNED_CONTAINERS[container_id] = expected
    with (patch.object(PROOF, "inspect_container", return_value=inspect),
          patch.object(PROOF, "run") as command,
          patch.object(PROOF, "verify_no_foreign_volume_users")):
        PROOF.remove_owned_container("podman", container_id, owner)
    command.assert_called_once_with(["podman", "rm", container_id])
    assert container_id not in PROOF.OWNED_CONTAINERS

    PROOF.OWNED_CONTAINERS[container_id] = expected
    with (patch.object(PROOF, "inspect_container", return_value={**inspect, "Created": "changed"}),
          patch.object(PROOF, "run") as command,
          pytest.raises(PROOF.Refused, match="cleanup_identity_uncertain")):
        PROOF.remove_owned_container("podman", container_id, owner)
    command.assert_not_called()
    PROOF.OWNED_CONTAINERS.pop(container_id)


def test_synthetic_alloy_config_uses_real_journal_component_and_local_sink():
    rendered = PROOF.alloy_config().decode()
    assert 'loki.source.journal "o11y_alloy"' in rendered
    assert 'loki.echo "synthetic" {}' in rendered
    assert 'path = "/synthetic-journal"' in rendered
    assert "loki.write" not in rendered
    assert "/var/log/journal" not in rendered


def test_receiver_baseline_is_read_only_and_returns_only_digest():
    container_rows = [{"Id": "receiver-id", "Names": ["receiver"]}]
    volume_rows = [{"Name": "positions"}]
    receiver = {"id": "receiver-id", "state": "running", "labels": {}}
    inspected_container = {"Id": "receiver-id", "Image": "sha256:" + "c" * 64,
                           "State": {"Status": "running", "StartedAt": "same"},
                           "Config": {"Labels": {PROOF.PROOF_OWNER: "prior-owner"}}}
    inspected_volume = {"Name": "positions", "CreatedAt": "same", "Driver": "local", "Scope": "local",
                        "Options": None, "MountCount": 0,
                        "Labels": {PROOF.PROOF_OWNER: "prior-owner"}}
    with (patch.object(PROOF, "inventories", return_value=(container_rows, volume_rows)),
          patch.object(PROOF, "receiver_context", return_value=(receiver, "network-digest")),
          patch.object(PROOF, "inspect_container", return_value=inspected_container),
          patch.object(PROOF, "inspect_volume", return_value=inspected_volume),
          patch.object(PROOF, "run") as command):
        result = PROOF.baseline("podman", "current-owner")
        repeated = PROOF.baseline("podman", "current-owner")

    assert len(result) == 64
    assert repeated == result
    command.assert_not_called()


def test_dev_action_is_direct_only_and_apply_and_repair_remain_refused():
    playbook = (ROOT / "platform/playbooks/deploy-o11y-journal-collector.yml").read_text()
    template = (ROOT / "platform/semaphore/templates.yml").read_text()
    docs = ((ROOT / "platform/services/o11y/deployment/README.md").read_text()
            + (ROOT / "platform/services/o11y/deployment/CLAUDE.md").read_text())

    assert "prove-direct" in playbook
    assert "prove-direct" in template
    assert "prove-provider" not in playbook + template + docs
    assert "podman-compose" not in PROOF.__doc__
    assert "journal_collector_action | default('apply') != 'apply'" in playbook
    assert "journal_collector_action | default('apply') != 'repair-positions'" in playbook
    assert "check_mode_unverified" in playbook


def test_proof_output_contains_binding_digests_and_no_paths_or_fixture_content():
    report = {
        "status": "direct_podman_journal_proven",
        "copy_control": "copied",
        "positive_no_copy": "no_copy",
        "state_no_copy": "no_copy",
        "repeat_state_no_copy": "no_copy",
        "state_layout": "explicit_component_only",
        "cursor": "created_resumed_advanced",
        "image_id": "sha256:" + "a" * 64,
        "state_layout_sha256": "2" * 64,
        "positive_layout_sha256": "3" * 64,
        "podman_client": "5.0.0",
        "podman_server": "5.0.0",
        "podman_executable_sha256": "b" * 64,
        "runtime_context_sha256": "c" * 64,
        "project_network_sha256": "d" * 64,
        "reviewed_sha": "e" * 40,
        "inventory_revision": "f" * 64,
        "config_sha256": "1" * 64,
    }
    output = io.StringIO()
    with patch.object(PROOF, "prove", return_value=report), redirect_stdout(output):
        assert PROOF.main() == 0
    encoded = output.getvalue()
    decoded = json.loads(encoded)
    assert decoded == report
    assert "/" not in encoded
    assert "fixture-content" not in encoded
