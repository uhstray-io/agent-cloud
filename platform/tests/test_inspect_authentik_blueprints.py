"""The Authentik blueprint inspector is read-only and never prints a token."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/inspect-authentik-blueprints.yml"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
READ_MODULES = {"ansible.builtin.assert", "ansible.builtin.debug", "ansible.builtin.command",
                "ansible.builtin.slurp", "ansible.builtin.include_tasks"}


def _tasks():
    return [t for play in yaml.safe_load(PLAYBOOK.read_text()) for t in play["tasks"]]


def test_only_read_modules():
    for t in _tasks():
        assert len(READ_MODULES.intersection(t)) == 1, t["name"]
        assert not {k for k in t if k.startswith("ansible.") or "." in k} - READ_MODULES, t["name"]


def test_every_command_is_unchanged_and_runs_in_check_mode():
    cmds = [t for t in _tasks() if "ansible.builtin.command" in t]
    assert cmds
    for t in cmds:
        assert t.get("changed_when") is False, t["name"]
        assert t.get("check_mode") is False, t["name"]


def test_nothing_hidden_by_no_log():
    assert all("no_log" not in t for t in _tasks())


def test_token_never_printed():
    api = next(t for t in _tasks() if t["name"].startswith("Read blueprint instances"))
    script = api["ansible.builtin.command"]["argv"][-1]
    for line in script.splitlines():
        if "print(" in line:
            assert not re.search(r"\btok\b", line.split("print(", 1)[1]), line
    for t in _tasks():
        if "ansible.builtin.debug" in t:
            assert "TOKEN" not in str(t["ansible.builtin.debug"]).upper(), t["name"]


def test_dev_template_declared():
    tpl = [t for t in yaml.safe_load(TEMPLATES.read_text())["templates"]
           if t["name"] == "Inspect Authentik Blueprints (Dev)"]
    assert tpl and tpl[0]["playbook"] == "platform/playbooks/inspect-authentik-blueprints.yml"
    assert tpl[0]["repository"] == "agent-cloud dev"


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
def test_check_mode_run_reports_each_section(tmp_path):
    deploy = tmp_path / "deploy"
    (deploy / "blueprints-active").mkdir(parents=True)
    (deploy / "blueprints-active" / "a.yaml").write_text("x")
    (deploy / "compose.yml").write_text(
        "services:\n  server:\n    volumes: ['./blueprints-active:/blueprints/custom:ro']\n"
        "  worker:\n    volumes: ['./blueprints-active:/blueprints/custom:ro']\n")
    engine = tmp_path / "engine"
    engine.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  ps) printf 'authentik-server Up\\nauthentik-worker Up\\n' ;;\n"
        "  inspect) case \"$3\" in *'range .Mounts'*'io.agent-cloud.inputs-sha256'*) ;;"
        " *'io.agent-cloud.inputs-sha256'*'range .Mounts'*) ;; *) exit 44 ;; esac;"
        " echo \"inspected $4\" ;;\n"
        "  exec) if [ \"$2\" = authentik-worker ]; then echo /blueprints/custom/a.yaml;"
        " else echo 'total 1 by_prefix {}'; fi ;;\n"
        "  *) exit 99 ;;\n"
        "esac\n")
    engine.chmod(0o755)
    inv = tmp_path / "inv.ini"
    inv.write_text(f"[authentik_svc]\nak ansible_connection=local\n\n[authentik_svc:vars]\n"
                   f"container_engine={engine}\ncompose_working_dir={deploy}\n")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_NOCOLOR="1")
    done = subprocess.run(["ansible-playbook", "--check", "-i", str(inv), str(PLAYBOOK)],
                          cwd=REPO, env=env, text=True, capture_output=True,
                          stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stdout[-3000:]
    for want in ("inspected authentik-server", "inspected authentik-worker",
                 "/blueprints/custom:ro", "a.yaml", "/blueprints/custom/a.yaml", "total 1"):
        assert want in done.stdout, want
