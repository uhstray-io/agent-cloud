"""A deploy must never delete a directory a running container bind-mounts.

compose_up_if_changed (platform/lib/common.sh) leaves containers running when their inputs
are unchanged. A bind mount pins the host directory's inode at container start, so deleting
and recreating the directory (file `state: absent` then `state: directory`, `rm -rf`) leaves
the running container looking at the deleted, empty inode while the host shows new files —
the 2026-10-05 authentik regression, where the worker saw an empty /blueprints/custom and
dropped every custom blueprint instance. Converge such directories in place instead: write
files into them and remove only the entries that left the set.

Scope (deliberately simple): every service whose deploy.sh calls compose_up_if_changed, its
`deploy-<service>.yml` playbook plus every task file under playbooks/tasks/ whose name
starts with the service name, against the relative (`./…`) bind-mount sources its compose
files declare. A task fails the guard when it deletes a source or one of its parents:
`ansible.builtin.file` with `state: absent`, or a shell/command running `rm -r` on it.
Removing a file INSIDE a mounted directory is fine and not flagged. An operator lever
(`deploy_force_recreate`) recreates containers already holding a stale mount.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SERVICES = REPO / "platform" / "services"
PLAYBOOKS = REPO / "platform" / "playbooks"
DEPLOY_DIR = "{{ _deploy_dir }}"


def _services() -> list[str]:
    return sorted(
        p.parent.parent.name
        for p in SERVICES.glob("*/deployment/deploy.sh")
        if "compose_up_if_changed" in p.read_text()
    )


def _bind_sources(service: str) -> set[str]:
    out: set[str] = set()
    for f in (SERVICES / service / "deployment").rglob("compose*.yml"):
        doc = yaml.safe_load(f.read_text()) or {}
        for svc in (doc.get("services") or {}).values():
            for vol in (svc or {}).get("volumes") or []:
                src = vol.get("source", "") if isinstance(vol, dict) else str(vol).split(":", 1)[0]
                if src.startswith("./"):
                    out.add(src[2:].rstrip("/"))
    return out


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        if any(k.startswith(("ansible.builtin.", "file", "shell", "command")) for k in node):
            yield node
        for key in ("tasks", "block", "rescue", "always", "pre_tasks", "post_tasks"):
            if key in node:
                yield from _tasks(node[key])


def _deleted_paths(task: dict) -> list[str]:
    paths = []
    for key in ("ansible.builtin.file", "file"):
        args = task.get(key)
        if isinstance(args, dict) and args.get("state") == "absent" and "path" in args:
            paths.append(str(args["path"]))
    for key in ("ansible.builtin.shell", "shell", "ansible.builtin.command", "command"):
        cmd = task.get(key)
        cmd = cmd.get("cmd", "") if isinstance(cmd, dict) else str(cmd or "")
        for m in re.finditer(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*\s+(\"[^\"]+\"|\S+)", cmd):
            paths.append(m.group(1).strip("\"'"))
    return paths


def _offences(service: str, sources: set[str], files: list[Path]) -> list[str]:
    hits = []
    for f in files:
        for task in _tasks(yaml.safe_load(f.read_text())):
            for path in _deleted_paths(task):
                if not path.startswith(DEPLOY_DIR):
                    continue
                rel = path[len(DEPLOY_DIR) :].strip("/")
                for src in sources:
                    if rel == "" or src == rel or src.startswith(rel + "/"):
                        hits.append(f"{f.name}: '{task.get('name')}' deletes {path} (bind source ./{src})")
    return hits


def _files_for(service: str) -> list[Path]:
    files = [PLAYBOOKS / f"deploy-{service}.yml"]
    files += sorted((PLAYBOOKS / "tasks").glob(f"{service}-*.yml"))
    return [f for f in files if f.exists()]


def test_scope_is_not_empty():
    services = _services()
    assert {"authentik", "o11y"} <= set(services), services
    assert "blueprints-active" in _bind_sources("authentik")
    assert "config/scrape.d" in _bind_sources("o11y")


def test_no_deploy_deletes_a_bind_mounted_directory():
    hits = []
    for service in _services():
        hits += _offences(service, _bind_sources(service), _files_for(service))
    assert not hits, "\n".join(hits)


def test_guard_catches_the_regression(tmp_path):
    pb = tmp_path / "deploy-authentik.yml"
    pb.write_text(
        "- hosts: x\n  tasks:\n"
        "    - name: Reset\n      ansible.builtin.file:\n"
        '        path: "{{ _deploy_dir }}/blueprints-active"\n        state: absent\n'
        '    - name: Wipe\n      ansible.builtin.shell: rm -rf "{{ _deploy_dir }}/blueprints-active"\n'
        "    - name: Prune one file (allowed)\n      ansible.builtin.file:\n"
        '        path: "{{ _deploy_dir }}/blueprints-active/{{ item }}"\n        state: absent\n'
    )
    hits = _offences("authentik", {"blueprints-active"}, [pb])
    assert len(hits) == 2, hits
