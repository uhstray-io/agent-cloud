"""A deploy must never delete or replace a directory a running container bind-mounts.

compose_up_if_changed (platform/lib/common.sh) leaves containers running when their inputs
are unchanged. A bind mount pins the host directory's inode at container start, so deleting
and recreating the directory (file `state: absent` then `state: directory`, `rm -rf`) leaves
the running container looking at the deleted, empty inode while the host shows new files —
the 2026-10-05 authentik regression, where the worker saw an empty /blueprints/custom and
dropped every custom blueprint instance. Converge such directories in place instead: write
files into them and remove only the entries that left the set.

Scope: every service whose deploy.sh calls compose_up_if_changed. Scanned: its
`deploy-<service>.yml`, every task file it includes or imports (followed transitively, so
shared tasks such as tasks/place-monorepo.yml are covered), every `tasks/<service>-*.yml`,
and the service's deploy.sh. Checked against the relative (`./...`) bind-mount sources its
compose files declare. A finding is any of these aimed at a source or one of its parents:

- `file` with `state: absent`;
- `rm` with a recursive flag (-r, -R, -rf, --recursive; `--` honoured) or `mv`, in a
  shell/command string, a command `argv`/`cmd` list, or deploy.sh;
- `copy` of a directory (src ending in `/`) or `unarchive` onto it;
- a mirroring sync — `synchronize` with `delete: true`, `rsync --delete`, or `git` with
  `force` — is allowed ONLY when every affected bind source is gitignored (rsync's
  `.gitignore` filter and git both leave it alone) or tracked with at least one file (rsync
  and git update an existing directory in place and never replace it). A mounted directory
  that is neither could be removed by the mirror and recreated later.

Removing a file INSIDE a mounted directory is fine and not flagged. Paths are resolved only
through `{{ _deploy_dir }}` and `{{ _monorepo_dir }}` (deploy.sh: relative to its own
directory); anything else is out of scope. An operator lever (`deploy_force_recreate`)
recreates containers already holding a stale mount.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SERVICES = REPO / "platform" / "services"
PLAYBOOKS = REPO / "platform" / "playbooks"


def _services() -> list[str]:
    return sorted(
        p.parent.parent.name
        for p in SERVICES.glob("*/deployment/deploy.sh")
        if "compose_up_if_changed" in p.read_text()
    )


def _deploy_rel(service: str) -> str:
    return f"platform/services/{service}/deployment"


def _bind_sources(service: str) -> set[str]:
    """Repo-relative bind-mount sources."""
    out: set[str] = set()
    for f in (SERVICES / service / "deployment").rglob("compose*.yml"):
        doc = yaml.safe_load(f.read_text()) or {}
        for svc in (doc.get("services") or {}).values():
            for vol in (svc or {}).get("volumes") or []:
                src = vol.get("source", "") if isinstance(vol, dict) else str(vol).split(":", 1)[0]
                if src.startswith("./"):
                    out.add(f"{_deploy_rel(service)}/{src[2:].rstrip('/')}")
    return out


def _norm(path, service: str) -> str | None:
    """Repo-relative form of a templated path, or None when it is out of scope."""
    p = str(path).strip().strip("\"'")
    for var, rel in (("{{ _deploy_dir }}", _deploy_rel(service)), ("{{ _monorepo_dir }}", "")):
        if p.startswith(var):
            return (rel + "/" + p[len(var) :].strip("/")).strip("/")
    return None


def _norm_sh(token: str, service: str) -> str:
    """deploy.sh runs from its deployment dir: strip $VAR/ and ./ prefixes."""
    t = re.sub(r"^(\$\{?\w+\}?/)+", "", token.strip("\"'"))
    t = t[2:] if t.startswith("./") else t
    return f"{_deploy_rel(service)}/{t.rstrip('/')}".rstrip("/")


def _hits(path: str | None, sources: set[str]) -> list[str]:
    if path is None:
        return []
    return [s for s in sources if path == "" or s == path or s.startswith(path + "/")]


def _rm_mv_targets(argv: list[str]) -> list[str]:
    out, i = [], 0
    while i < len(argv):
        cmd = argv[i].rsplit("/", 1)[-1]
        if cmd not in ("rm", "mv"):
            i += 1
            continue
        j, recursive, opts_done, targets = i + 1, cmd == "mv", False, []
        while j < len(argv) and argv[j] not in (";", "&&", "||", "|"):
            a = argv[j]
            if not opts_done and a == "--":
                opts_done = True
            elif not opts_done and a.startswith("--"):
                recursive |= a == "--recursive"
            elif not opts_done and a.startswith("-") and len(a) > 1:
                recursive |= "r" in a or "R" in a
            else:
                targets.append(a)
            j += 1
        if recursive:
            out += targets
        i = j
    return out


def _split(cmd) -> list[str]:
    if isinstance(cmd, list):
        return [str(c) for c in cmd]
    text = re.sub(r"(;|&&|\|\|)", r" \1 ", str(cmd))
    try:
        return shlex.split(text, comments=True)
    except ValueError:
        return text.split()


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("tasks", "block", "rescue", "always", "pre_tasks", "post_tasks", "handlers"):
            if key in node:
                yield from _tasks(node[key])


def _arg(task: dict, name: str):
    for key in (f"ansible.builtin.{name}", f"ansible.posix.{name}", name):
        if key in task:
            return task[key]
    return None


def _task_findings(task: dict, service: str, sources: set[str]):
    """Yield (kind, source): 'delete' always fails, 'sync' fails unless _sync_safe."""
    a = _arg(task, "file")
    if isinstance(a, dict) and a.get("state") == "absent":
        yield from (("delete", s) for s in _hits(_norm(a.get("path", ""), service), sources))
    for mod in ("shell", "command"):
        a = _arg(task, mod)
        if a is None:
            continue
        argv = _split((a.get("argv") or a.get("cmd", "")) if isinstance(a, dict) else a)
        for t in _rm_mv_targets(argv):
            yield from (("delete", s) for s in _hits(_norm(t, service), sources))
        if any(x.rsplit("/", 1)[-1] == "rsync" for x in argv) and any(x.startswith("--delete") for x in argv):
            dest = [x for x in argv if not x.startswith("-")][-1]
            yield from (("sync", s) for s in _hits(_norm(dest, service), sources))
    a = _arg(task, "copy")
    if isinstance(a, dict) and str(a.get("src", "")).endswith("/"):
        yield from (("delete", s) for s in _hits(_norm(a.get("dest", ""), service), sources))
    a = _arg(task, "unarchive")
    if isinstance(a, dict):
        yield from (("delete", s) for s in _hits(_norm(a.get("dest", ""), service), sources))
    a = _arg(task, "synchronize")
    if isinstance(a, dict) and a.get("delete") in (True, "true", "yes"):
        yield from (("sync", s) for s in _hits(_norm(a.get("dest", ""), service), sources))
    a = _arg(task, "git")
    if isinstance(a, dict) and a.get("force"):
        yield from (("sync", s) for s in _hits(_norm(a.get("dest", ""), service), sources))


def _sync_safe(source: str) -> bool:
    """Gitignored (the mirror leaves it alone) or tracked (the mirror updates it in place)."""
    ignored = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", source + "/x"], check=False)
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--", source], capture_output=True, text=True, check=True
    )
    return ignored.returncode == 0 or bool(tracked.stdout.strip())


def _included(f: Path) -> list[Path]:
    out = []
    for task in _tasks(yaml.safe_load(f.read_text())):
        for mod in ("include_tasks", "import_tasks"):
            a = _arg(task, mod)
            if a is None:
                continue
            ref = a.get("file", "") if isinstance(a, dict) else str(a)
            assert "{{" not in ref, f"{f.name}: templated include {ref!r} cannot be followed; extend the guard"
            for base in (f.parent, PLAYBOOKS):
                if (base / ref).exists():
                    out.append((base / ref).resolve())
                    break
            else:
                raise AssertionError(f"{f.name}: include {ref!r} not found")
    return out


def _files_for(service: str) -> list[Path]:
    todo = [PLAYBOOKS / f"deploy-{service}.yml", *sorted((PLAYBOOKS / "tasks").glob(f"{service}-*.yml"))]
    seen: list[Path] = []
    while todo:
        f = todo.pop().resolve()
        if f in seen or not f.exists():
            continue
        seen.append(f)
        todo += _included(f)
    return seen


def _offences(service: str, sources: set[str], files: list[Path], deploy_sh: Path | None = None) -> list[str]:
    hits = []
    for f in files:
        for task in _tasks(yaml.safe_load(f.read_text())):
            for kind, src in _task_findings(task, service, sources):
                if kind == "sync" and _sync_safe(src):
                    continue
                hits.append(f"{f.name}: '{task.get('name')}' ({kind}) hits bind source {src}")
    if deploy_sh is not None and deploy_sh.exists():
        for n, line in enumerate(deploy_sh.read_text().splitlines(), 1):
            for t in _rm_mv_targets(_split(line)):
                hits += [f"{deploy_sh.name}:{n} deletes bind source {s}" for s in _hits(_norm_sh(t, service), sources)]
    return hits


def test_scope_is_not_empty():
    assert {"authentik", "o11y"} <= set(_services())
    assert f"{_deploy_rel('authentik')}/blueprints-active" in _bind_sources("authentik")
    assert f"{_deploy_rel('o11y')}/config/scrape.d" in _bind_sources("o11y")
    for svc, extra in (("o11y", "o11y-alert-provision.yml"), ("authentik", "manage-secrets.yml")):
        names = {f.name for f in _files_for(svc)}
        assert {f"deploy-{svc}.yml", "place-monorepo.yml", extra} <= names, names


def test_no_deploy_deletes_a_bind_mounted_directory():
    hits = []
    for svc in _services():
        hits += _offences(svc, _bind_sources(svc), _files_for(svc), SERVICES / svc / "deployment" / "deploy.sh")
    assert not hits, "\n".join(hits)


def test_place_monorepo_mirrors_are_seen_and_excused_only_by_the_precondition():
    # place-monorepo mirrors the repo with rsync --delete (local) and git force (prod); both
    # must register as syncs over every bind source, and every source must pass _sync_safe.
    pm = PLAYBOOKS / "tasks" / "place-monorepo.yml"
    for svc in _services():
        kinds = [
            k for t in _tasks(yaml.safe_load(pm.read_text())) for k, _ in _task_findings(t, svc, _bind_sources(svc))
        ]
        assert kinds.count("sync") == 2 * len(_bind_sources(svc)), kinds
        assert all(_sync_safe(s) for s in _bind_sources(svc))


# ── each shape the guard recognises, proven caught ──────────────────────────
SRC = {f"{_deploy_rel('authentik')}/blueprints-active"}
UNSAFE = {f"{_deploy_rel('authentik')}/never-tracked-dir"}
BP = '"{{ _deploy_dir }}/blueprints-active"'
SHAPES = {
    "file_absent": f"ansible.builtin.file:\n        path: {BP}\n        state: absent",
    "file_absent_parent": 'ansible.builtin.file:\n        path: "{{ _deploy_dir }}"\n        state: absent',
    "rm_rf": f"ansible.builtin.shell: rm -rf {BP}",
    "rm_rf_dashdash": f"ansible.builtin.shell: rm -rf -- {BP}",
    "rm_R": f"ansible.builtin.shell: rm -R {BP}",
    "rm_recursive": f"ansible.builtin.shell: rm --recursive --force {BP}",
    "rm_chained": f"ansible.builtin.shell: cd /tmp && rm -rf {BP}; true",
    "mv_swap": f'ansible.builtin.shell: mv "{{{{ _deploy_dir }}}}/new" {BP}',
    "command_argv": "ansible.builtin.command:\n        argv: [rm, -rf, '{{ _deploy_dir }}/blueprints-active']",
    "command_cmd_list": "ansible.builtin.command:\n        cmd: [rm, -r, '{{ _deploy_dir }}/blueprints-active']",
    "copy_dir": f"ansible.builtin.copy:\n        src: files/blueprints/\n        dest: {BP}",
    "unarchive": f"ansible.builtin.unarchive:\n        src: x.tgz\n        dest: {BP}",
}
SYNC_SHAPES = {
    "synchronize_delete": (
        'ansible.posix.synchronize:\n        src: a/\n        dest: "{{ _deploy_dir }}/never-tracked-dir"\n'
        "        delete: true"
    ),
    "rsync_delete": 'ansible.builtin.shell: rsync -a --delete a/ "{{ _deploy_dir }}/never-tracked-dir/"',
    "git_force": 'ansible.builtin.git:\n        repo: r\n        dest: "{{ _monorepo_dir }}"\n        force: true',
}


def _one(tmp_path, body: str) -> Path:
    pb = tmp_path / "deploy-x.yml"
    pb.write_text(f"- hosts: x\n  tasks:\n    - name: shape\n      {body}\n")
    return pb


def test_every_delete_shape_is_caught(tmp_path):
    missed = [n for n, body in SHAPES.items() if not _offences("authentik", SRC, [_one(tmp_path, body)])]
    assert not missed, missed


def test_mirroring_sync_is_caught_unless_the_source_is_ignored_or_tracked(tmp_path):
    missed = [n for n, body in SYNC_SHAPES.items() if not _offences("authentik", UNSAFE, [_one(tmp_path, body)])]
    assert not missed, missed
    assert not _offences("authentik", SRC, [_one(tmp_path, SYNC_SHAPES["git_force"])])  # gitignored


def test_per_file_prune_is_allowed(tmp_path):
    body = (
        'ansible.builtin.file:\n        path: "{{ _deploy_dir }}/blueprints-active/{{ item }}"\n        state: absent'
    )
    assert not _offences("authentik", SRC, [_one(tmp_path, body)])


def test_deploy_sh_rm_is_caught(tmp_path):
    sh = tmp_path / "deploy.sh"
    sh.write_text('rm -rf -- "$SCRIPT_DIR/blueprints-active"\nrm -f blueprints-active/a.yaml\n')
    hits = _offences("authentik", SRC, [], sh)
    assert len(hits) == 1, hits


def test_include_following_is_transitive(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks" / "inner.yml").write_text(f"- name: wipe\n  ansible.builtin.shell: rm -rf {BP}\n")
    (tmp_path / "tasks" / "mid.yml").write_text("- ansible.builtin.include_tasks: inner.yml\n")
    pb = tmp_path / "deploy-x.yml"
    pb.write_text("- hosts: x\n  tasks:\n    - ansible.builtin.include_tasks: tasks/mid.yml\n")
    files = [pb.resolve()]
    i = 0
    while i < len(files):
        files += [f for f in _included(files[i]) if f not in files]
        i += 1
    assert _offences("authentik", SRC, files)
