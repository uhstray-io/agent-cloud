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
FIRST = "Read the current config.yaml and any copy an interrupted run staged"
LAST = "Drop the staged copy (still there when the render changed nothing, or it held a raw key)"

PHASE1 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 1"))
NAMES = [t["name"] for t in PHASE1["tasks"]]
KEY_A = "plain-client-key-AAAA-1111"
KEY_B = "plain-client-key-BBBB-2222"
KEY_C = "plain-client-key-CCCC-3333"
KEY_D = "plain-client-key-DDDD-4444"
PLAIN = {"local_mode": True, "agw_plaintext_keys": True}
HASHED_LOCAL = {"local_mode": True, "agw_plaintext_keys": False}


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


def _rendered(tmp: Path, key: str, **hv) -> bytes:
    """What the deploy renders for `key`, produced in a directory of its own."""
    own = tmp / f"render-{key}"
    own.mkdir()
    _ok(_deploy(own, key, **hv))
    return (own / "d" / "config.yaml").read_bytes()


def _interrupted(tmp: Path, staged: bytes, live: bytes) -> Path:
    """The deploy dir a run left when it stopped after staging `staged`, with `live` in place."""
    d = tmp / "d"
    d.mkdir()
    for name, data in (("config.yaml", live), ("config.yaml.replaced", staged)):
        (d / name).write_bytes(data)
        (d / name).chmod(0o644)
    return d


def test_a_run_interrupted_after_the_render_keeps_its_staged_copy_as_previous(tmp_path):
    old, new = _rendered(tmp_path, KEY_A), _rendered(tmp_path, KEY_B)
    d = _interrupted(tmp_path, staged=old, live=new)
    _ok(_deploy(tmp_path, KEY_B))  # the same render again: nothing new to keep
    assert (d / "config.yaml.previous").read_bytes() == old
    assert _mode(d / "config.yaml.previous") == 0o644
    assert not (d / "config.yaml.replaced").exists()


def test_a_run_interrupted_before_the_render_keeps_nothing(tmp_path):
    cur = _rendered(tmp_path, KEY_A)
    d = _interrupted(tmp_path, staged=cur, live=cur)
    _ok(_deploy(tmp_path, KEY_A))
    assert sorted(p.name for p in d.iterdir()) == ["config.yaml"]


def test_an_interrupted_runs_raw_key_copy_is_discarded(tmp_path):
    raw, new = _rendered(tmp_path, KEY_C, **PLAIN), _rendered(tmp_path, KEY_D, **HASHED_LOCAL)
    assert KEY_C.encode() in raw
    d = _interrupted(tmp_path, staged=raw, live=new)
    _ok(_deploy(tmp_path, KEY_D, **HASHED_LOCAL))
    assert sorted(p.name for p in d.iterdir()) == ["config.yaml"]


def test_a_staged_copy_no_config_backs_is_not_kept_as_previous(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "config.yaml.replaced").write_text("stale: staged by a run that never finished\n")
    _ok(_deploy(tmp_path, KEY_A))
    assert not (d / "config.yaml.previous").exists()
    assert not (d / "config.yaml.replaced").exists()


def test_turning_plaintext_keys_off_never_keeps_the_raw_key_config(tmp_path):
    d = tmp_path / "d"
    _ok(_deploy(tmp_path, KEY_C, **PLAIN))
    assert KEY_C in (d / "config.yaml").read_text()
    _ok(_deploy(tmp_path, KEY_D, **HASHED_LOCAL))
    assert sorted(p.name for p in d.iterdir()) == ["config.yaml"]
    assert KEY_D not in (d / "config.yaml").read_text()


def test_turning_plaintext_keys_off_leaves_the_last_hashed_copy(tmp_path):
    d = tmp_path / "d"
    _ok(_deploy(tmp_path, KEY_A, **HASHED_LOCAL))
    _ok(_deploy(tmp_path, KEY_B, **HASHED_LOCAL))
    hashed = (d / "config.yaml.previous").read_bytes()
    _ok(_deploy(tmp_path, KEY_C, **PLAIN))
    _ok(_deploy(tmp_path, KEY_D, **HASHED_LOCAL))
    assert (d / "config.yaml.previous").read_bytes() == hashed
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


MANAGE = REPO / "platform/playbooks/manage-agentgateway-client-key.yml"
DROP = "Drop the rollback copy that still enrols the old key"


def test_rotate_and_revoke_drop_the_rollback_copy_after_the_deploy(tmp_path):
    """The copy a rotate/revoke deploy keeps enrols the OLD key; the run ends by removing it."""
    plays = yaml.safe_load(MANAGE.read_text())
    names = [p.get("name") for p in plays]
    deploy = next(i for i, p in enumerate(plays) if p.get("import_playbook") == "deploy-agentgateway.yml")
    assert names.index(DROP) == deploy + 1 == len(plays) - 1
    drop = plays[names.index(DROP)]
    assert drop["hosts"] == "agentgateway_svc"
    d = tmp_path / "gw"
    d.mkdir()
    (d / "config.yaml").write_text("live\n")
    (d / "config.yaml.previous").write_text("old key hash\n")
    inv = {"all": {"hosts": {"g": {"ansible_connection": "local", "local_monorepo_dir": str(tmp_path),
                                   "monorepo_deploy_path": "gw"}},
                   "children": {"agentgateway_svc": {"hosts": {"g": {}}}}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    (tmp_path / "play.yml").write_text(yaml.safe_dump([drop]))

    def run(*args):
        return harness_sandbox.run(["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml"),
                                    *args], tmp_path, cwd=REPO, env=harness_sandbox.env_for(tmp_path))

    _ok(run("--check"))
    assert (d / "config.yaml.previous").exists()  # check mode writes nothing
    _ok(run())
    assert not (d / "config.yaml.previous").exists() and (d / "config.yaml").read_text() == "live\n"
    _ok(run())  # idempotent: nothing left to remove


def test_a_failed_deploy_leaves_the_rollback_copy_in_place(tmp_path):
    """The real playbook's tail — the deploy import replaced by a stand-in that fails or
    succeeds — so the drop runs only after a deploy that succeeded."""
    plays = yaml.safe_load(MANAGE.read_text())
    deploy = next(i for i, p in enumerate(plays) if p.get("import_playbook") == "deploy-agentgateway.yml")
    d = tmp_path / "gw"
    d.mkdir()
    inv = {"all": {"hosts": {"g": {"ansible_connection": "local", "local_monorepo_dir": str(tmp_path),
                                   "monorepo_deploy_path": "gw"}},
                   "children": {"agentgateway_svc": {"hosts": {"g": {}}}}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    for fails in (True, False):
        (d / "config.yaml.previous").write_text("old key hash\n")
        stand_in = {"name": "deploy stand-in", "hosts": "agentgateway_svc", "gather_facts": False,
                    "tasks": [{"ansible.builtin.fail": {"msg": "deploy failed"}, "when": fails}]}
        (tmp_path / "play.yml").write_text(yaml.safe_dump([stand_in, *plays[deploy + 1:]]))
        r = harness_sandbox.run(["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml")],
                                tmp_path, cwd=REPO, env=harness_sandbox.env_for(tmp_path))
        assert (r.returncode != 0) == fails, r.stdout + r.stderr
        assert (d / "config.yaml.previous").exists() == fails
