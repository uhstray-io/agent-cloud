"""mount-caddy-certs.yml and files/compose_add_volume.py (production-internal-ca task 5.1).

The playbook runs on localhost against a stub container engine that keeps state: `compose
up` rebuilds the container's mounts from the compose file it finds in its working
directory, so the play's post-recreate check reads what the edit actually declared. Runs
through harness_sandbox, so no write can leave the test's directory.
"""

import json
import subprocess
import sys
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

PLAYBOOK = playbook_yaml.REPO / "platform/playbooks/mount-caddy-certs.yml"
EDITOR = playbook_yaml.REPO / "platform/playbooks/files/compose_add_volume.py"
# The monorepo's own Caddy compose file: the long-standing production layout.
COMPOSE = (playbook_yaml.REPO / "platform/services/caddy/deployment/compose.yml").read_text()

STUB = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
import yaml
state = Path(os.environ["STUB_STATE"])
s = json.loads(state.read_text())
a = sys.argv[1:]
log = state.with_name("calls")
with log.open("a") as f:
    f.write(" ".join(a) + "\n")
if a[0] == "inspect":
    if '"working_dir"' in a[2]:
        print(json.dumps({"working_dir": s["working_dir"], "service": s["service"], "mounts": s["mounts"]}))
    else:
        for m in s["mounts"]:
            print(f'{m["destination"]}|{str(m["rw"]).lower()}|{m["source"]}')
elif a[0] == "compose":
    vols = yaml.safe_load(Path("compose.yml").read_text())["services"][s["service"]]["volumes"]
    if s.get("fail_up_with") and any(s["fail_up_with"] in v for v in vols):
        sys.exit("up failed")
    mounts = []
    for v in vols:
        src, dst, *mode = v.split(":")
        mounts.append({"source": str(Path(src).resolve()) if src.startswith(".") else src,
                       "destination": dst, "rw": mode != ["ro"]})
    s["mounts"] = mounts
    state.write_text(json.dumps(s))
elif a[0] == "exec":
    print('{"apps": {"http": {"servers": {"srv0": {"routes": [{"match": [{"host": ["auth.example.test"]}]}]}}}}}')
'''


def _setup(tmp: Path, compose: str = COMPOSE, working_dir: str | None = None, **state) -> Path:
    cdir = tmp / "caddy"
    cdir.mkdir()
    (cdir / "compose.yml").write_text(compose)
    (cdir / "compose.yml").chmod(0o640)
    stub = tmp / "engine"
    stub.write_text(f"#!{sys.executable}\n" + STUB.split("\n", 1)[1])
    stub.chmod(0o755)
    (tmp / "state.json").write_text(json.dumps({
        "working_dir": working_dir or str(cdir), "service": "caddy",
        "mounts": [{"source": str(cdir / "Caddyfile"), "destination": "/etc/caddy/Caddyfile", "rw": True}],
        **state}))
    return cdir


def _run(tmp: Path, check: bool = False, **hostvars) -> subprocess.CompletedProcess:
    host = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
            "container_engine": str(tmp / "engine"), "caddy_compose_dir": str(tmp / "caddy"),
            "caddy_container": "caddy", "caddy_probe_host": "auth.example.test", **hostvars}
    (tmp / "inv.yml").write_text(yaml.safe_dump({"all": {"children": {"caddy_svc": {"hosts": {"c": host}}}}}))
    env = {**harness_sandbox.env_for(tmp), "STUB_STATE": str(tmp / "state.json")}
    cmd = ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(PLAYBOOK), *(["--check"] if check else [])]
    return harness_sandbox.run(cmd, tmp, cwd=playbook_yaml.REPO, env=env)


def _ups(tmp: Path) -> int:
    calls = tmp / "calls"
    return sum(line.startswith("compose up") for line in calls.read_text().splitlines()) if calls.exists() else 0


def test_the_mount_is_added_once_and_caddy_recreated_once(tmp_path):
    cdir = _setup(tmp_path)
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    text = (cdir / "compose.yml").read_text()
    assert f"      - {cdir}/certs:/etc/caddy/certs:ro\n" in text
    # Only the one line was added: the operator's comments and the rest survive.
    assert text.replace(f"      - {cdir}/certs:/etc/caddy/certs:ro\n", "") == COMPOSE
    assert oct((cdir / "compose.yml").stat().st_mode & 0o777) == "0o640"
    assert (cdir / "certs").is_dir() and _ups(tmp_path) == 1

    again = _run(tmp_path)
    assert again.returncode == 0 and "changed=0" in again.stdout, again.stdout
    assert "already declared" in again.stdout and _ups(tmp_path) == 1


def test_a_dry_run_reports_the_plan_and_changes_nothing(tmp_path):
    cdir = _setup(tmp_path)
    r = _run(tmp_path, check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would be added; Caddy would be recreated" in r.stdout
    assert (cdir / "compose.yml").read_text() == COMPOSE
    assert not (cdir / "certs").exists() and _ups(tmp_path) == 0


def test_a_caddy_started_from_another_directory_is_refused(tmp_path):
    cdir = _setup(tmp_path, working_dir="/opt/elsewhere")
    r = _run(tmp_path)
    assert r.returncode != 0 and "not caddy_compose_dir" in r.stdout, r.stdout
    assert (cdir / "compose.yml").read_text() == COMPOSE and _ups(tmp_path) == 0


def test_another_mount_at_the_certificate_path_is_refused(tmp_path):
    compose = COMPOSE.replace("      - ./sites:/etc/caddy/sites:ro\n",
                              "      - ./sites:/etc/caddy/sites:ro\n      - ./old:/etc/caddy/certs:ro\n")
    cdir = _setup(tmp_path, compose=compose)
    r = _run(tmp_path)
    assert r.returncode != 0 and "already mounts './old:/etc/caddy/certs:ro'" in r.stdout + r.stderr, r.stdout
    assert (cdir / "compose.yml").read_text() == compose and _ups(tmp_path) == 0


def test_a_failed_recreate_restores_the_previous_file_and_brings_caddy_back(tmp_path):
    cdir = _setup(tmp_path, fail_up_with="/etc/caddy/certs")
    r = _run(tmp_path)
    assert r.returncode != 0 and "previous compose file is restored" in r.stdout, r.stdout
    assert (cdir / "compose.yml").read_text() == COMPOSE
    assert _ups(tmp_path) == 2  # the failed one, then the one on the restored file


def test_a_recreate_that_does_not_show_the_mount_fails(tmp_path):
    # The compose file already declares it, but the running container does not have it.
    _setup(tmp_path)
    vol = f"{tmp_path}/caddy/certs:/etc/caddy/certs:ro"
    edited = subprocess.run([sys.executable, str(EDITOR), "caddy", vol], input=COMPOSE, capture_output=True,
                            text=True, check=True)
    text = json.loads(edited.stdout)["text"]
    (tmp_path / "caddy" / "compose.yml").write_text(text)
    r = _run(tmp_path)
    assert r.returncode != 0 and "does not mount" in r.stdout, r.stdout


# ── The editor on its own ─────────────────────────────────────────────────────

def _edit(text: str, service: str = "caddy", volume: str = "/c:/etc/caddy/certs:ro") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(EDITOR), service, volume], input=text, capture_output=True, text=True)


@pytest.mark.parametrize("text,reason", [
    ("services:\n  caddy:\n    image: x\n", "no short-syntax volumes list"),
    ("services:\n  caddy:\n    image: x\n    volumes: [./a:/a]\n", "no block volumes list"),
    ("services:\n  caddy:\n    volumes:\n      - type: bind\n        source: ./a\n        target: /a\n",
     "no short-syntax volumes list"),
    ("services:\n  web:\n    volumes:\n      - ./a:/a\n", "no service 'caddy'"),
], ids=["no-volumes", "flow-list", "long-syntax", "no-service"])
def test_the_editor_refuses_a_layout_it_cannot_extend_safely(text, reason):
    r = _edit(text)
    assert r.returncode != 0 and reason in r.stderr, r.stderr


def test_the_editor_adds_to_the_named_service_only():
    text = ("services:\n  web:\n    volumes:\n      - ./w:/w\n  caddy:\n    volumes:\n      - ./a:/a\n"
            "    # trailing comment\n    restart: always\n")
    out = json.loads(_edit(text).stdout)
    want = text.replace("      - ./a:/a\n", "      - ./a:/a\n      - /c:/etc/caddy/certs:ro\n")
    assert out["changed"] and out["text"] == want
