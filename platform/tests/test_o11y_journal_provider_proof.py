"""Source-level contract for the isolated journal provider/image proof action."""

import importlib.util
import io
import json
import tarfile
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/prove-o11y-journal-volume.py"
SPEC = importlib.util.spec_from_file_location("o11y_journal_provider_proof", HELPER)
PROOF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROOF)


def _image_archive(files, path):
    with path.open("wb") as stream, tarfile.open(fileobj=stream, mode="w") as archive:
        for name, contents in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(contents)
            archive.addfile(entry, io.BytesIO(contents))


def test_image_scan_uses_bounded_nonempty_same_image_control():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "image.tar"
        _image_archive({
            "alloy-state/seed": b"state",
            "etc/ssl/certs/test.pem": b"public certificate",
            "etc/ssl/certs/another.pem": b"another public certificate",
        }, archive)
        state, positive_path, positive = PROOF.image_files(archive)

    assert state == {"seed": b"state"}
    assert positive_path == "/etc/ssl/certs"
    assert positive == {"test.pem": b"public certificate", "another.pem": b"another public certificate"}


def test_image_scan_refuses_a_missing_positive_control():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "image.tar"
        _image_archive({"alloy-state/seed": b"state"}, archive)
        assert PROOF.image_files(archive) == ({"seed": b"state"}, None, {})


def test_image_scan_refuses_an_unbounded_archive():
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "oversized.tar"
        with archive.open("wb") as stream:
            stream.truncate(PROOF.MAX_BYTES + 1)
        try:
            PROOF.image_files(archive)
        except PROOF.Refused as exc:
            assert exc.reason == "image_layout_unbounded"
        else:
            raise AssertionError("an oversized image export must be refused")


def test_podman_label_formats_are_normalized_or_refused():
    assert PROOF.labels({"Labels": {"agent-cloud.proof-owner": "owned"}}) == {
        "agent-cloud.proof-owner": "owned",
    }
    assert PROOF.labels({"Labels": "agent-cloud.proof-owner=owned"}) == {
        "agent-cloud.proof-owner": "owned",
    }
    try:
        PROOF.labels({"Labels": "same=one,same=two"})
    except PROOF.Refused as exc:
        assert exc.reason == "provider_response_invalid"
    else:
        raise AssertionError("duplicate labels must be refused")


def test_test_compose_is_network_isolated_and_keeps_no_copy_explicit():
    rendered = PROOF.compose_yaml(
        "jpv0123456789a", "jpv0123456789a-c", "/alloy-state", True, Path("/private/scratch/config"),
    )
    compose = yaml.safe_load(rendered)

    assert "network_mode: \"none\"" in rendered
    assert "pull_policy: \"never\"" in rendered
    assert "volume: {nocopy: true}" in rendered
    assert 'user: "0:0"' in rendered
    assert "read_only: true" in rendered
    assert "cap_drop: [\"ALL\"]" in rendered
    assert "ports:" not in rendered
    assert "/var/run/podman" not in rendered
    assert "/var/log/journal" not in rendered
    assert compose["services"]["proof"]["network_mode"] == "none"
    assert compose["services"]["proof"]["volumes"][0]["volume"]["nocopy"] is True
    assert compose["services"]["proof"]["labels"]["agent-cloud.proof-owner"] == "jpv0123456789a"


def test_semaphore_action_is_dev_bound_and_does_not_unlock_apply():
    playbook = (ROOT / "platform/playbooks/deploy-o11y-journal-collector.yml").read_text()
    templates = (ROOT / "platform/semaphore/templates.yml").read_text()

    assert "prove-provider" in playbook
    assert "prove-provider" in templates
    assert "journal_collector_action | default('apply') != 'apply'" in playbook
    assert "journal_collector_action | default('apply') != 'repair-positions'" in playbook
    assert "cursor_resume == 'unverified'" in playbook


def test_emitted_tuple_contains_only_bounded_categories_and_digests():
    report = {
        "status": "provider_copy_behavior_proven",
        "copy_control": "copied",
        "positive_control_no_copy": "empty",
        "state_path_no_copy": "empty",
        "effective_mount": "rw",
        "image": PROOF.IMAGE,
        "image_id": "sha256:" + "a" * 64,
        "compose_version": "1.2.3",
        "compose_binary_sha256": "b" * 64,
        "podman_client": "5.0.0",
        "podman_server": "5.0.0",
        "state_destination": "alloy_state",
        "cursor_resume": "unverified",
    }
    output = io.StringIO()
    with patch.object(PROOF, "prove", return_value=report), redirect_stdout(output):
        assert PROOF.main() == 0

    result = json.loads(output.getvalue())
    assert set(result) == set(report)
    assert all("/" not in value for key, value in result.items() if isinstance(value, str) and key != "image")
