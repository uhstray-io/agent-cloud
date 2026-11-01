"""The opt-in pinned podman-compose install (tasks/install-podman-compose.yml).

Ubuntu 24.04's apt podman-compose (1.0.6) cannot read the `!override` / `!reset` merge tags
Plane's overlays use. A caller sets `podman_compose_version`; the task installs that exact wheel
by hash. These tests pin what makes that safe: the one PyPI artifact is hash-checked and pulls in
nothing else, the step never runs in local mode, and every version a playbook asks for is pinned.
"""

import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
TASKS = yaml.safe_load((REPO / "platform/playbooks/tasks/install-podman-compose.yml").read_text())
BLOCK = next(t for t in TASKS if t.get("name") == "Pinned podman-compose release")
STEPS = {t["name"]: t for t in BLOCK["block"]}


def test_the_block_is_opt_in_linux_only_and_never_local():
    when = BLOCK["when"]
    assert "podman_compose_version is defined" in when
    assert "ansible_system == 'Linux'" in when
    assert any("local_mode" in w and w.startswith("not") for w in when)


def test_the_wheel_is_installed_by_hash_alone():
    argv = STEPS["Install the release by hash, without dependencies"]["ansible.builtin.command"]["argv"]
    for flag in ("--no-deps", "--require-hashes", "--only-binary=:all:"):
        assert flag in argv
    content = STEPS["Write the hash-pinned requirement"]["ansible.builtin.copy"]["content"]
    assert "--hash=sha256:" in content and "==" in content


def test_its_dependencies_come_from_apt_through_the_venv():
    apt = STEPS["Install the venv tooling and podman-compose's runtime dependencies (apt)"]["ansible.builtin.apt"]
    assert {"python3-yaml", "python3-dotenv", "python3-venv"} <= set(apt["name"])
    argv = STEPS["Create the release's venv (sees apt's PyYAML and python-dotenv)"]["ansible.builtin.command"]["argv"]
    assert "--system-site-packages" in argv


def test_an_unpinned_version_is_refused_before_any_install():
    names = list(STEPS)
    assert names[0] == "Refuse a podman-compose release without a pinned wheel hash"


def test_every_version_a_playbook_asks_for_is_pinned():
    pinned = set(BLOCK["vars"]["_podman_compose_wheels"])
    asked = set()
    for path in (REPO / "platform/playbooks").glob("*.yml"):
        for play in yaml.safe_load(path.read_text()) or []:
            version = (play.get("vars") or {}).get("podman_compose_version") if isinstance(play, dict) else None
            if version:
                asked.add(str(version))
    assert asked, "no playbook asks for a podman-compose release"
    assert asked <= pinned, f"asked for but not pinned: {asked - pinned}"
