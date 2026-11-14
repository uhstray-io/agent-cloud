"""tasks/verify-cosign.yml, run for real against a fake container engine.

The task is the only signature check in agent-cloud, so its refusals are the property: a tag (which
can move after the check), a missing certificate identity (which would accept any signer) and an
issuer that is not https are each refused before cosign runs at all. `podman` is a script that logs
its arguments, so "cosign ran" is a line in the log.
"""

import json
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible
from harness_sandbox import fake_bin, run_playbook

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "platform/playbooks/tasks/verify-cosign.yml"
DIGEST_REF = "ghcr.io/example/app@sha256:" + "a" * 64
IDENTITY = "https://github.com/example/app/.github/workflows/images.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"

pytestmark = [needs_ansible]


def _verify(tmp_path: Path, **inputs):
    log = tmp_path / "engine.log"
    env = fake_bin(tmp_path, {"podman": f'echo "$*" >> "{log}"\nexit 0\n'})
    env["ANSIBLE_NOCOLOR"] = "1"
    play = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"_cosign_refs": [DIGEST_REF], "_cosign_identity": IDENTITY, "_cosign_issuer": ISSUER, **inputs},
        "tasks": [{"ansible.builtin.include_tasks": str(TASK)}],
    }]
    playbook = tmp_path / "verify.yml"
    playbook.write_text(yaml.safe_dump(play, width=10**6))
    proc = run_playbook(playbook, tmp_path, "-e", json.dumps({"ansible_become": False}), env=env)
    return proc, (log.read_text() if log.exists() else "")


def test_a_digest_pinned_reference_with_an_exact_signer_is_verified(tmp_path):
    proc, log = _verify(tmp_path)
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert " verify " in log and f"--certificate-identity {IDENTITY}" in log and DIGEST_REF in log, log


@pytest.mark.parametrize("inputs,why", [
    ({"_cosign_refs": ["ghcr.io/example/app:v1.2.3"]}, "a tag can be moved after the check"),
    ({"_cosign_refs": [DIGEST_REF, "ghcr.io/example/app:latest"]}, "one tag among digests is still a tag"),
    ({"_cosign_identity": ""}, "with no identity, any signer would pass"),
    ({"_cosign_issuer": "http://token.actions.githubusercontent.com"}, "an issuer must be https"),
    ({"_cosign_refs": []}, "nothing to verify is not a verification"),
])
def test_a_reference_or_signer_that_proves_nothing_is_refused_before_cosign_runs(tmp_path, inputs, why):
    proc, log = _verify(tmp_path, **inputs)
    assert proc.returncode != 0, f"{why}:\n" + proc.stdout[-3000:]
    assert " verify " not in log, f"cosign ran although {why}:\n" + log
