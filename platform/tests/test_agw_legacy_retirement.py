"""Shared-key retirement is enforced by deploy-agentgateway.yml (gateway task 5.1).

Before inventory `legacy_shared_expires` the rendered config enrols the shared vLLM key as the
`legacy-shared` identity; on or after it the identity is gone and the deploy fails while the vLLM
key in OpenBao still matches the recorded pre-rotation fingerprint. The deploy's own play vars,
its guard and the credential task file run for real on localhost through harness_sandbox; the
shared render step is a stand-in that renders the real config.yaml.j2. `agw_today` pins the date.
"""

import hashlib
import subprocess
from pathlib import Path

import harness_sandbox
import playbook_yaml
import yaml

REPO = playbook_yaml.REPO
DEPLOY = REPO / "platform/playbooks/deploy-agentgateway.yml"
CHECK = REPO / "platform/playbooks/tasks/agw-legacy-key-check.yml"
CONFIG = REPO / "platform/services/agentgateway/deployment/templates/config.yaml.j2"
GUARD = "Refuse a malformed shared-key expiry or legacy-shared declared as a client"
INCLUDE = "Shared-key retirement: record the fingerprint, refuse an unrotated key after expiry"

PHASE1 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 1"))
TASKS = {t["name"]: t for t in PHASE1["tasks"]}
OLD = "shared-vllm-key-OLD-0000"
NEW = "rotated-vllm-key-NEW-1111"
EXPIRES = "2026-10-20"


def _run(tmp: Path, vllm: str, *args: str, fp: str = "", **hv) -> subprocess.CompletedProcess:
    secrets = {"client_stray": "client-key-AAAA", "vllm_api_key": vllm, "agw_db_password": "p",
               "agw_oidc_cookie_seed": "s", "agentgateway_oidc_client_secret": "c",
               "legacy_shared_key_sha256": fp}
    host = {"ansible_connection": "local", "agw_clients": ["stray"], "agw_models": [{"name": "m"}],
            "agw_upstream_base_url": "http://u.invalid/v1", "service_name": "agentgateway",
            "secrets": secrets, "_resolved": secrets, **hv}
    play_vars = {k: v for k, v in PHASE1["vars"].items() if k.startswith(("_legacy", "_agw_today"))}
    include = dict(TASKS[INCLUDE])
    include["ansible.builtin.include_tasks"] = str(CHECK)
    tasks = [TASKS[GUARD],
             {"name": "render", "ansible.builtin.template": {"src": str(CONFIG), "dest": str(tmp / "config.yaml")}},
             include]
    (tmp / "inv.yml").write_text(yaml.safe_dump({"all": {"hosts": {"gw": host}}}))
    (tmp / "play.yml").write_text(yaml.safe_dump([{"hosts": "all", "gather_facts": False,
                                                   "vars": play_vars, "tasks": tasks}]))
    return harness_sandbox.run(["ansible-playbook", "-i", str(tmp / "inv.yml"), str(tmp / "play.yml"), *args],
                               tmp, cwd=REPO, env=harness_sandbox.env_for(tmp))


def _keys(tmp: Path) -> dict:
    keys = yaml.safe_load((tmp / "config.yaml").read_text())["llm"]["policies"]["apiKey"]["keys"]
    return {k["metadata"]["name"]: k for k in keys}


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _no_secret(r: subprocess.CompletedProcess) -> None:
    out = r.stdout + r.stderr
    for v in (OLD, NEW, _sha(OLD), _sha(NEW)):
        assert v not in out


def test_before_expiry_enrols_legacy_shared_pinned_to_the_recorded_fingerprint(tmp_path):
    r = _run(tmp_path, NEW, "-e", "agw_today=2026-10-19", fp=_sha(OLD),
             legacy_shared_expires=EXPIRES, agw_client_policies={"legacy-shared": {"tokens_per_hour": 1000}})
    assert r.returncode == 0, r.stdout + r.stderr
    keys = _keys(tmp_path)
    assert keys["legacy-shared"]["keyHash"] == "sha256:" + _sha(OLD)
    assert keys["legacy-shared"]["budgets"][0]["limit"]["amount"] == 1000
    assert "stray" in keys
    _no_secret(r)


def test_before_expiry_unrecorded_check_mode_skips_the_fingerprint_write(tmp_path):
    r = _run(tmp_path, OLD, "--check", "-e", "agw_today=2026-10-01", legacy_shared_expires=EXPIRES)
    assert r.returncode == 0, r.stdout + r.stderr
    after = r.stdout.split("Record the shared key's fingerprint", 1)[1].split("\n", 1)[1]
    assert after.startswith("skipping:")
    _no_secret(r)


def test_before_expiry_unrecorded_renders_the_current_vllm_key_then_records_it(tmp_path):
    # No OpenBao here, so the record step (bao-merge-keys) fails after the render; reaching it
    # proves the write is attempted, and the render enrols the current key's hash meanwhile.
    r = _run(tmp_path, OLD, "-e", "agw_today=2026-10-01", legacy_shared_expires=EXPIRES)
    assert r.returncode != 0
    assert "Require the merge inputs" in r.stdout
    assert _keys(tmp_path)["legacy-shared"]["keyHash"] == "sha256:" + _sha(OLD)
    _no_secret(r)


def test_on_expiry_unrotated_key_drops_the_identity_and_fails_without_the_value(tmp_path):
    r = _run(tmp_path, OLD, "-e", f"agw_today={EXPIRES}", fp=_sha(OLD), legacy_shared_expires=EXPIRES)
    assert r.returncode != 0
    assert "still matches the pre-rotation shared key" in r.stdout
    assert "legacy-shared" not in _keys(tmp_path)
    _no_secret(r)


def test_after_expiry_unrotated_key_fails_in_check_mode_too(tmp_path):
    r = _run(tmp_path, OLD, "--check", "-e", "agw_today=2026-11-01", fp=_sha(OLD), legacy_shared_expires=EXPIRES)
    assert r.returncode != 0
    assert "still matches the pre-rotation shared key" in r.stdout
    _no_secret(r)


def test_after_expiry_rotated_key_passes_without_the_identity(tmp_path):
    r = _run(tmp_path, NEW, "-e", "agw_today=2026-11-01", fp=_sha(OLD), legacy_shared_expires=EXPIRES)
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(_keys(tmp_path)) == {"stray"}
    _no_secret(r)


def test_after_expiry_with_no_recorded_fingerprint_fails(tmp_path):
    r = _run(tmp_path, NEW, "-e", "agw_today=2026-11-01", legacy_shared_expires=EXPIRES)
    assert r.returncode != 0
    assert "holds no" in r.stdout and "legacy_shared_key_sha256" in r.stdout
    _no_secret(r)


def test_absent_expiry_renders_no_identity_and_skips_the_check(tmp_path):
    r = _run(tmp_path, OLD, "-e", "agw_today=2030-01-01")
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(_keys(tmp_path)) == {"stray"}
    assert "Compare the vLLM key" not in r.stdout


def test_malformed_expiry_is_refused(tmp_path):
    r = _run(tmp_path, OLD, legacy_shared_expires="20 Oct 2026")
    assert r.returncode != 0 and "must be an ISO date" in r.stdout


def test_legacy_shared_as_a_client_is_refused(tmp_path):
    r = _run(tmp_path, OLD, legacy_shared_expires=EXPIRES, agw_clients=["stray", "legacy-shared"])
    assert r.returncode != 0 and "not an" in r.stdout


def test_declaration_adds_the_fingerprint_only_with_an_expiry():
    text = (REPO / "platform/playbooks/vars/secret-declarations/agentgateway.yml").read_text()
    assert "{'name': 'legacy_shared_key_sha256', 'type': 'existing'}" in text
    assert "legacy_shared_expires | default('', true)" in text
