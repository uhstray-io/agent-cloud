"""apply-cloudflare-tofu's dry run refuses to plan against a missing or stale backend.hcl.

PR 203 Codex review: under --check the backend write is only simulated, so `tofu init` read
whatever file the workspace already held, or none. Runs the real write and dry-run tasks under
--check against a scratch directory. Requires ansible-playbook.
"""

import shutil
from pathlib import Path

import harness_sandbox
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/apply-cloudflare-tofu.yml"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")

CONFIGURED = ('bucket = "b"\nkey    = "cloudflare/terraform.tfstate"\n'
              'endpoints = { s3 = "https://r2.example.invalid" }\n')


def _dry_run(tmp_path, existing):
    tasks = [t for p in yaml.safe_load(PLAYBOOK.read_text()) for t in p.get("tasks", [])]
    wanted = [t for t in tasks if str(t.get("name", "")).startswith(("Write the R2 backend config", "Dry run with a"))]
    tf_dir = tmp_path / "tf"
    tf_dir.mkdir()
    if existing is not None:
        (tf_dir / "backend.hcl").write_text(existing)
        (tf_dir / "backend.hcl").chmod(0o600)  # as the real write leaves it
    marker = {"name": "Reached init", "ansible.builtin.debug": {"msg": "REACHED-INIT"}}
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_tf_dir": str(tf_dir), "_bucket": "b", "_r2_endpoint": "https://r2.example.invalid"},
        "tasks": [*wanted, marker],
    }]
    path = tmp_path / "dry.yml"
    path.write_text(yaml.safe_dump(harness))
    return harness_sandbox.run_playbook(path, tmp_path, "--check").stdout


def test_a_stale_backend_stops_the_dry_run(tmp_path):
    out = _dry_run(tmp_path, CONFIGURED.replace('"b"', '"old-bucket"'))
    assert "missing or differs" in out and "REACHED-INIT" not in out


def test_a_missing_backend_stops_the_dry_run(tmp_path):
    out = _dry_run(tmp_path, None)
    assert "missing or differs" in out and "REACHED-INIT" not in out


def test_a_current_backend_lets_the_dry_run_plan(tmp_path):
    out = _dry_run(tmp_path, CONFIGURED)
    assert "REACHED-INIT" in out and "missing or differs" not in out
