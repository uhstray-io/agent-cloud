"""Shared-key retirement is enforced by deploy-agentgateway.yml (gateway task 5.1).

Before inventory `legacy_shared_expires` the rendered config enrols the shared vLLM key as the
`legacy-shared` identity; on or after it the identity is gone, Phase 2 still rolls that config out,
and the last play fails while the vLLM key in OpenBao still matches the recorded pre-rotation
fingerprint. The deploy's own play vars, guard, credential task file and verdict play run for real
on localhost through harness_sandbox, against a synthetic KV-v2 OpenBao; the shared render step is
a stand-in rendering the real config.yaml.j2, and deploy.sh is a stand-in that leaves a marker.
`agw_today` pins the date.
"""

import hashlib
import subprocess
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import seed_harness
import yaml

REPO = playbook_yaml.REPO
DEPLOY = REPO / "platform/playbooks/deploy-agentgateway.yml"
CHECK = REPO / "platform/playbooks/tasks/agw-legacy-key-check.yml"
CONFIG = REPO / "platform/services/agentgateway/deployment/templates/config.yaml.j2"
DECL = REPO / "platform/playbooks/vars/secret-declarations/agentgateway.yml"
GUARD = "Refuse a malformed shared-key expiry or legacy-shared declared as a client"
INCLUDE = "Shared-key retirement: record the fingerprint, compare the vLLM key with it"

PLAYS = yaml.safe_load(DEPLOY.read_text())
PHASE1 = next(p for p in PLAYS if p.get("name", "").startswith("Phase 1"))
PHASE4 = next(p for p in PLAYS if p.get("name", "").startswith("Phase 4"))
TASKS = {t["name"]: t for t in PHASE1["tasks"]}
OLD = "shared-vllm-key-OLD-0000"
NEW = "rotated-vllm-key-NEW-1111"
OTHER_FP = "f" * 64
EXPIRES = "2026-10-20"


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


class Bao(seed_harness.FakeBao):
    """KV-v2 for secret/services/agentgateway: versioned, check-and-set honoured."""

    store: dict = {}
    version = 1

    def do_GET(self):
        self.record("GET")
        if self.path == "/v1/secret/data/services/agentgateway":
            return self.reply({"data": {"data": type(self).store, "metadata": {"version": type(self).version}}})
        self.reply({}, 404)

    def do_POST(self):
        self.record("POST")
        cls = type(self)
        if self.path == "/v1/secret/data/services/agentgateway":
            body = self.body()
            if body.get("options", {}).get("cas") != cls.version:
                return self.reply({"errors": ["check-and-set parameter did not match"]}, 400)
            cls.store = dict(body["data"])
            cls.version += 1
            return self.reply({"data": {"version": cls.version}})
        self.reply({}, 404)

    def do_PATCH(self):
        self.record("PATCH")
        self.reply({"errors": ["unguarded write"]}, 500)


@pytest.fixture
def bao():
    Bao.requests = []
    Bao.version = 1
    with seed_harness.serve(Bao) as url:
        yield url


def _run(tmp: Path, url: str, vllm: str, *args: str, fp: str = "", live_fp: str | None = None,
         **hv) -> subprocess.CompletedProcess:
    """fp: the fingerprint manage-secrets read; live_fp: what the store holds now (default fp)."""
    secrets = {"client_workstation": "client-key-AAAA", "vllm_api_key": vllm, "agw_db_password": "p",
               "agw_oidc_cookie_seed": "s", "agentgateway_oidc_client_secret": "c"}
    stored = dict(secrets)
    live = fp if live_fp is None else live_fp
    if live:
        stored["legacy_shared_key_sha256"] = live
    Bao.store = stored
    existing = dict(secrets, **({"legacy_shared_key_sha256": fp} if fp else {}))
    host = {"ansible_connection": "local", "agw_clients": ["workstation"], "agw_models": [{"name": "m"}],
            "agw_upstream_base_url": "http://u.invalid/v1", "service_name": "agentgateway",
            "secrets": secrets, "_resolved": secrets, "_existing": existing,
            "_bao_auth": {"json": {"auth": {"client_token": seed_harness.LOGIN}}}, **hv}
    play_vars = {k: v for k, v in PHASE1["vars"].items() if k.startswith(("_legacy", "_agw_today"))}
    play_vars["_bao_url"] = url
    include = dict(TASKS[INCLUDE], **{"ansible.builtin.include_tasks": str(CHECK)})
    render = {"name": "render", "ansible.builtin.template": {"src": str(CONFIG), "dest": str(tmp / "config.yaml")}}
    deploy = {"name": "deploy.sh stand-in", "ansible.builtin.copy": {
        "src": str(tmp / "config.yaml"), "dest": str(tmp / "deployed")}, "when": "not ansible_check_mode"}
    plays = [{"hosts": "all", "gather_facts": False, "vars": play_vars, "tasks": [TASKS[GUARD], render, include]},
             {"hosts": "all", "gather_facts": False, "tasks": [deploy]},
             dict(PHASE4, hosts="all")]
    (tmp / "inv.yml").write_text(yaml.safe_dump({"all": {"hosts": {"gw": host}}}))
    (tmp / "play.yml").write_text(yaml.safe_dump(plays))
    return harness_sandbox.run(["ansible-playbook", "-i", str(tmp / "inv.yml"), str(tmp / "play.yml"), *args],
                               tmp, cwd=REPO, env=harness_sandbox.env_for(tmp))


def _keys(path: Path) -> dict:
    keys = yaml.safe_load(path.read_text())["llm"]["policies"]["apiKey"]["keys"]
    return {k["metadata"]["name"]: k for k in keys}


def _writes() -> list:
    return [m for m, _ in Bao.requests if m in ("POST", "PATCH")]


def _no_secret(r: subprocess.CompletedProcess) -> None:
    out = r.stdout + r.stderr
    for v in (OLD, NEW, _sha(OLD), _sha(NEW), seed_harness.LOGIN):
        assert v not in out


def test_before_expiry_enrols_legacy_shared_pinned_to_the_recorded_fingerprint(tmp_path, bao):
    r = _run(tmp_path, bao, NEW, "-e", "agw_today=2026-10-19", fp=_sha(OLD),
             legacy_shared_expires=EXPIRES, agw_client_policies={"legacy-shared": {"tokens_per_hour": 1000}})
    assert r.returncode == 0, r.stdout + r.stderr
    keys = _keys(tmp_path / "config.yaml")
    assert keys["legacy-shared"]["keyHash"] == "sha256:" + _sha(OLD)
    assert keys["legacy-shared"]["budgets"][0]["limit"]["amount"] == 1000
    assert "workstation" in keys
    assert _writes() == []
    _no_secret(r)


def test_before_expiry_unrecorded_records_the_fingerprint_once_by_cas(tmp_path, bao):
    r = _run(tmp_path, bao, OLD, "-e", "agw_today=2026-10-01", legacy_shared_expires=EXPIRES)
    assert r.returncode == 0, r.stdout + r.stderr
    assert Bao.store["legacy_shared_key_sha256"] == _sha(OLD)
    assert Bao.store["vllm_api_key"] == OLD  # siblings preserved
    assert _writes() == ["POST"]
    assert _keys(tmp_path / "config.yaml")["legacy-shared"]["keyHash"] == "sha256:" + _sha(OLD)
    _no_secret(r)


def test_a_fingerprint_recorded_after_the_read_is_not_overwritten(tmp_path, bao):
    # Another run recorded a fingerprint after this run's manage-secrets read: the CAS on absence
    # refuses instead of overwriting it.
    r = _run(tmp_path, bao, OLD, "-e", "agw_today=2026-10-01", live_fp=OTHER_FP, legacy_shared_expires=EXPIRES)
    assert r.returncode != 0
    assert "guarded OpenBao key changed" in r.stdout
    assert Bao.store["legacy_shared_key_sha256"] == OTHER_FP and _writes() == []
    _no_secret(r)


def test_before_expiry_unrecorded_check_mode_skips_the_fingerprint_write(tmp_path, bao):
    r = _run(tmp_path, bao, OLD, "--check", "-e", "agw_today=2026-10-01", legacy_shared_expires=EXPIRES)
    assert r.returncode == 0, r.stdout + r.stderr
    after = r.stdout.split("Record the shared key's fingerprint", 1)[1].split("\n", 1)[1]
    assert after.startswith("skipping:")
    assert "legacy_shared_key_sha256" not in Bao.store and _writes() == []
    _no_secret(r)


def test_on_expiry_unrotated_key_rolls_out_without_the_identity_then_fails(tmp_path, bao):
    r = _run(tmp_path, bao, OLD, "-e", f"agw_today={EXPIRES}", fp=_sha(OLD), legacy_shared_expires=EXPIRES)
    assert r.returncode != 0
    assert "still matches the pre-rotation shared key" in r.stdout
    # The deploy step ran BEFORE the verdict, with a config that no longer enrols the shared key.
    assert set(_keys(tmp_path / "deployed")) == {"workstation"}
    assert r.stdout.index("deploy.sh stand-in") < r.stdout.index("still matches the pre-rotation shared key")
    _no_secret(r)


def test_after_expiry_unrotated_key_fails_in_check_mode_too(tmp_path, bao):
    r = _run(tmp_path, bao, OLD, "--check", "-e", "agw_today=2026-11-01", fp=_sha(OLD), legacy_shared_expires=EXPIRES)
    assert r.returncode != 0
    assert "still matches the pre-rotation shared key" in r.stdout
    _no_secret(r)


def test_after_expiry_rotated_key_passes_without_the_identity(tmp_path, bao):
    r = _run(tmp_path, bao, NEW, "-e", "agw_today=2026-11-01", fp=_sha(OLD), legacy_shared_expires=EXPIRES)
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(_keys(tmp_path / "deployed")) == {"workstation"}
    assert _writes() == []
    _no_secret(r)


def test_after_expiry_with_no_recorded_fingerprint_rolls_out_then_fails(tmp_path, bao):
    r = _run(tmp_path, bao, NEW, "-e", "agw_today=2026-11-01", legacy_shared_expires=EXPIRES)
    assert r.returncode != 0
    assert "holds no legacy_shared_key_sha256" in " ".join(r.stdout.split())
    assert set(_keys(tmp_path / "deployed")) == {"workstation"}
    assert _writes() == []
    _no_secret(r)


def test_absent_expiry_renders_no_identity_and_skips_the_check(tmp_path, bao):
    r = _run(tmp_path, bao, OLD, "-e", "agw_today=2030-01-01")
    assert r.returncode == 0, r.stdout + r.stderr
    assert set(_keys(tmp_path / "config.yaml")) == {"workstation"}
    assert "Compare the vLLM key" not in r.stdout


@pytest.mark.parametrize("bad", ["20 Oct 2026", "2026-13-01", "2026-02-30", "2026-1-5"])
def test_an_expiry_that_is_not_a_calendar_date_is_refused(tmp_path, bao, bad):
    r = _run(tmp_path, bao, OLD, legacy_shared_expires=bad)
    assert r.returncode != 0 and "must be a real calendar date" in r.stdout
    assert not (tmp_path / "config.yaml").exists()


def test_legacy_shared_as_a_client_is_refused(tmp_path, bao):
    r = _run(tmp_path, bao, OLD, legacy_shared_expires=EXPIRES, agw_clients=["workstation", "legacy-shared"])
    assert r.returncode != 0 and "must be a real calendar date" in r.stdout
    assert not (tmp_path / "config.yaml").exists()


def test_the_fingerprint_is_not_declared_through_manage_secrets():
    # A declared `existing` field absent from the store is written back as '' by manage-secrets.
    assert "'legacy_shared_key_sha256'" not in DECL.read_text()


def test_the_verdict_is_the_last_play_after_the_deploy_and_phase1_never_fails_on_it():
    names = [p.get("name", "") for p in PLAYS]
    assert names[-1].startswith("Phase 4")
    assert any(n.startswith("Phase 2") for n in names[:-1])
    check = yaml.safe_load(CHECK.read_text())
    assert not [t for t in check if "ansible.builtin.assert" in t or "ansible.builtin.fail" in t]
