"""deploy-agentgateway.yml keeps the config.yaml a changed render replaced, as config.yaml.previous.

rollback-inference-route.yml (gateway-config mode, gateway task 4.6) puts that file back. The
deploy's own tasks run on localhost through harness_sandbox, around a stand-in for the shared
render step that renders the real config.yaml.j2 the way manage-secrets does (same template,
mode 0644), so the kept copy is checked for what it actually carries: key hashes, never a key.
"""

import hashlib
import os
import subprocess
from pathlib import Path

import harness_sandbox
import playbook_yaml
import yaml

REPO = playbook_yaml.REPO
DEPLOY = REPO / "platform/playbooks/deploy-agentgateway.yml"
DEPLOY_DIR = REPO / "platform/services/agentgateway/deployment"
CONFIG = DEPLOY_DIR / "templates/config.yaml.j2"
RENDER = "Manage secrets and render env + config"
FIRST = "Clear a staged copy an interrupted run left behind"
LAST = "Drop the staged copy (still there only when the render left config.yaml unchanged)"

PHASE1 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 1"))
NAMES = [t["name"] for t in PHASE1["tasks"]]
KEY_A = "plain-client-key-AAAA-1111"
KEY_B = "plain-client-key-BBBB-2222"


def _hosts(key: str, **hv) -> dict:
    return {"agw_clients": ["stray"], "agw_models": [{"name": "m"}], "agw_upstream_base_url": "http://u.invalid/v1",
            "secrets": {"client_stray": key, "vllm_api_key": "v", "agw_db_password": "p",
                        "agw_oidc_cookie_seed": "s", "agentgateway_oidc_client_secret": "c"}, **hv}


def _deploy(tmp: Path, key: str, *args: str, **hv) -> subprocess.CompletedProcess:
    """The deploy's tasks from FIRST to LAST, with the shared render replaced by a stand-in."""
    d = tmp / "d"
    d.mkdir(exist_ok=True)
    render = {"name": RENDER, "ansible.builtin.template": {
        "src": str(CONFIG), "dest": str(d / "config.yaml"), "mode": "0644"}}
    tasks = [render if t["name"] == RENDER else t for t in PHASE1["tasks"][NAMES.index(FIRST):NAMES.index(LAST) + 1]]
    inv = {"all": {"hosts": {"gw": {"ansible_connection": "local", **_hosts(key, **hv)}}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    (tmp / "play.yml").write_text(yaml.safe_dump([{
        "hosts": "all", "gather_facts": False,
        "vars": {"_deploy_dir": str(d), "_keep_previous": PHASE1["vars"]["_keep_previous"]}, "tasks": tasks}]))
    return harness_sandbox.run(["ansible-playbook", "-i", str(tmp / "inv.yml"), str(tmp / "play.yml"), *args],
                               tmp, cwd=REPO, env=harness_sandbox.env_for(tmp))


def _ok(r: subprocess.CompletedProcess) -> None:
    assert r.returncode == 0, r.stdout + r.stderr


def _mode(p: Path) -> int:
    return os.stat(p).st_mode & 0o777


def test_the_shared_render_is_bracketed_by_the_keep_tasks():
    assert NAMES.index(FIRST) < NAMES.index(RENDER) < NAMES.index(LAST)
    assert NAMES.index("Place the monorepo + ensure podman/compose") < NAMES.index(FIRST)


def test_a_first_deploy_keeps_nothing(tmp_path):
    _ok(_deploy(tmp_path, KEY_A))
    d = tmp_path / "d"
    assert (d / "config.yaml").exists()
    assert not (d / "config.yaml.previous").exists()
    assert not (d / "config.yaml.replaced").exists()


def test_a_changed_render_keeps_the_replaced_config_with_its_mode_and_no_key(tmp_path):
    _ok(_deploy(tmp_path, KEY_A))
    d = tmp_path / "d"
    before = (d / "config.yaml").read_bytes()
    _ok(_deploy(tmp_path, KEY_B))
    prev = d / "config.yaml.previous"
    assert prev.read_bytes() == before
    assert (d / "config.yaml").read_bytes() != before
    assert _mode(prev) == _mode(d / "config.yaml") == 0o644
    assert os.stat(prev).st_uid == os.stat(d / "config.yaml").st_uid
    text = prev.read_text()
    assert f"sha256:{hashlib.sha256(KEY_A.encode()).hexdigest()}" in text
    assert KEY_A not in text and KEY_B not in text
    assert not (d / "config.yaml.replaced").exists()


def test_an_unchanged_render_leaves_the_last_good_copy_alone(tmp_path):
    _ok(_deploy(tmp_path, KEY_A))
    _ok(_deploy(tmp_path, KEY_B))
    d = tmp_path / "d"
    last_good = (d / "config.yaml.previous").read_bytes()
    _ok(_deploy(tmp_path, KEY_B))
    assert (d / "config.yaml.previous").read_bytes() == last_good
    assert not (d / "config.yaml.replaced").exists()


def test_check_mode_writes_nothing(tmp_path):
    _ok(_deploy(tmp_path, KEY_A))
    d = tmp_path / "d"
    before = (d / "config.yaml").read_bytes()
    _ok(_deploy(tmp_path, KEY_B, "--check"))
    assert (d / "config.yaml").read_bytes() == before
    assert sorted(p.name for p in d.iterdir()) == ["config.yaml"]


def test_raw_local_dev_keys_are_never_kept(tmp_path):
    hv = {"local_mode": True, "agw_plaintext_keys": True}
    _ok(_deploy(tmp_path, KEY_A, **hv))
    d = tmp_path / "d"
    assert KEY_A in (d / "config.yaml").read_text()  # the stand-in really rendered raw keys
    _ok(_deploy(tmp_path, KEY_B, **hv))
    assert sorted(p.name for p in d.iterdir()) == ["config.yaml"]


def test_a_copy_staged_by_an_interrupted_run_is_not_kept_as_previous(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "config.yaml.replaced").write_text("stale: staged by a run that never finished\n")
    _ok(_deploy(tmp_path, KEY_A))
    assert not (d / "config.yaml.previous").exists()
    assert not (d / "config.yaml.replaced").exists()


def test_both_kept_names_are_gitignored_so_local_rsync_delete_spares_them():
    ignored = (DEPLOY_DIR / ".gitignore").read_text().splitlines()
    assert "config.yaml.previous" in ignored and "config.yaml.replaced" in ignored


def test_the_change_aware_digest_does_not_hash_the_kept_copies():
    # deploy.sh hashes .env, config.yaml, the compose files and ./certs; the kept copies sit
    # beside config.yaml, so a kept copy appearing must not read as an input change.
    script = (DEPLOY_DIR / "deploy.sh").read_text()
    assert "files+=(.env config.yaml)" in script
    assert "config.yaml.previous" not in script and "config.yaml.replaced" not in script
