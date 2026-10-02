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
        print(json.dumps({k: s[k] for k in ("working_dir", "service", "config_files", "mounts")}))
    else:
        for m in s["mounts"]:
            print(f'{m["destination"]}|{str(m["rw"]).lower()}|{m["source"]}')
elif a[0] == "compose":
    assert a[1] == "-f", a  # the play always names the file it edited
    # ...and recreates only Caddy's own service, never the whole project
    assert a[-1] == s["service"] and "--no-deps" in a, a
    vols = yaml.safe_load(Path(a[2]).read_text())["services"][s["service"]]["volumes"]
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
    if s.get("fail_exec_with") and any(m["destination"] == s["fail_exec_with"] for m in s["mounts"]):
        sys.exit("admin API down")
    print('{"apps": {"http": {"servers": {"srv0": {"routes": [{"match": [{"host": ["auth.example.test"]}]}]}}}}}')
'''


def _setup(tmp: Path, compose: str = COMPOSE, working_dir: str | None = None, name: str = "compose.yml",
           **state) -> Path:
    cdir = tmp / "caddy"
    cdir.mkdir()
    (cdir / name).write_text(compose)
    (cdir / name).chmod(0o640)
    state.setdefault("config_files", str(cdir / name))
    stub = tmp / "engine"
    stub.write_text(f"#!{sys.executable}\n" + STUB.split("\n", 1)[1])
    stub.chmod(0o755)
    (tmp / "state.json").write_text(json.dumps({
        "working_dir": working_dir or str(cdir), "service": "caddy",
        "mounts": [{"source": str(cdir / "Caddyfile"), "destination": "/etc/caddy/Caddyfile", "rw": True}],
        **state}))
    return cdir


def _run(tmp: Path, check: bool = False, groups: dict | None = None, **hostvars) -> subprocess.CompletedProcess:
    host = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
            "container_engine": str(tmp / "engine"), "caddy_compose_dir": str(tmp / "caddy"),
            "caddy_container": "caddy", "caddy_probe_host": "auth.example.test",
            "caddy_verify_retries": 1, "caddy_verify_delay": 0, **hostvars}
    children = {"caddy_svc": {"hosts": {"c": host}}} if groups is None else groups
    (tmp / "inv.yml").write_text(yaml.safe_dump({"all": {"children": children}}))
    env = {**harness_sandbox.env_for(tmp), "STUB_STATE": str(tmp / "state.json")}
    cmd = ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(PLAYBOOK), *(["--check"] if check else [])]
    return harness_sandbox.run(cmd, tmp, cwd=playbook_yaml.REPO, env=env)


def _ups(tmp: Path) -> int:
    calls = tmp / "calls"
    lines = calls.read_text().splitlines() if calls.exists() else []
    return sum(line.startswith("compose ") and " up " in line for line in lines)


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
    assert r.returncode != 0 and "not one compose file in caddy_compose_dir" in r.stdout, r.stdout
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
    assert r.returncode != 0 and "The previous compose file is restored" in r.stdout, r.stdout
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



@pytest.mark.parametrize("groups", [{}, {"caddy_svc": {"hosts": {}}}], ids=["absent", "empty"])
def test_a_caddy_group_that_matches_no_hosts_fails(tmp_path, groups):
    # Review of fa34c265 (MISTAKES 2.21): a play over no hosts exits 0 and changes nothing.
    _setup(tmp_path)
    r = _run(tmp_path, groups=groups)
    assert r.returncode != 0 and "caddy_svc" in r.stdout, r.stdout
    assert _ups(tmp_path) == 0


def test_a_check_failing_after_the_recreate_rolls_back_too(tmp_path):
    # Review of 5d7f27c7: the rollback covered the recreate but not the checks after it.
    cdir = _setup(tmp_path, fail_exec_with="/etc/caddy/certs")
    r = _run(tmp_path)
    assert r.returncode != 0 and "The previous compose file is restored" in r.stdout, r.stdout
    assert (cdir / "compose.yml").read_text() == COMPOSE and _ups(tmp_path) == 2


def test_the_file_the_container_was_created_from_is_the_one_edited_and_recreated(tmp_path):
    # Review of 5d7f27c7: the edit and the recreate must name the same file.
    cdir = _setup(tmp_path, name="docker-compose.yml")
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/etc/caddy/certs:ro" in (cdir / "docker-compose.yml").read_text()
    assert not (cdir / "compose.yml").exists()
    assert f"compose -f {cdir}/docker-compose.yml up" in (tmp_path / "calls").read_text()



@pytest.mark.parametrize("label", ["compose.yml", "./compose.yml"])
def test_a_relative_compose_file_label_is_resolved_against_the_working_directory(tmp_path, label):
    # Review of b1ee8d50: Python podman-compose keeps the -f argument as given.
    cdir = _setup(tmp_path, config_files=label)
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "/etc/caddy/certs:ro" in (cdir / "compose.yml").read_text()
    assert f"compose -f {cdir}/compose.yml up" in (tmp_path / "calls").read_text()

def test_without_a_compose_file_label_compose_yml_is_assumed_and_said(tmp_path):
    _setup(tmp_path, config_files="")
    r = _run(tmp_path, check=True)
    assert r.returncode == 0 and "(no compose file label; assumed)" in r.stdout, r.stdout


@pytest.mark.parametrize("files", ["/opt/other/compose.yml", "{d}/compose.yml,{d}/override.yml",
                                   "../other/compose.yml"],
                         ids=["elsewhere", "two-files", "relative-elsewhere"])
def test_a_container_from_another_or_more_than_one_file_is_refused(tmp_path, files):
    cdir = _setup(tmp_path)
    s = json.loads((tmp_path / "state.json").read_text())
    s["config_files"] = files.format(d=cdir)
    (tmp_path / "state.json").write_text(json.dumps(s))
    r = _run(tmp_path)
    assert r.returncode != 0 and "not one compose file in caddy_compose_dir" in r.stdout, r.stdout
    assert (cdir / "compose.yml").read_text() == COMPOSE and _ups(tmp_path) == 0

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


def test_a_colon_inside_an_interpolated_source_is_not_a_separator():
    # Review of 5d7f27c7: `${X:-./certs}:/etc/caddy/certs` was read as target "-./certs}".
    text = "services:\n  caddy:\n    volumes:\n      - ${CERTS:-./certs}:/etc/caddy/certs:ro\n"
    r = _edit(text)
    assert r.returncode != 0 and "already mounts" in r.stderr, r.stderr
