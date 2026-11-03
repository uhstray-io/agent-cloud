"""The Dispatch Console's view-only directory identity (plan 17 D28, task 8.2a).

The console reads the Authentik user and group directory with a token owned by a service
account that holds exactly two permissions, view_user and view_group, and sits in no admin group.
These tests hold that shape in three places:

- the blueprint (console-oidc.yaml): the role, the group, the account and the token, parsed;
- env.j2 and the secret declaration: the token reaches the worker env from its own OpenBao field;
- verify-users.py.j2 (section 5): the deploy's live proof. The REAL template is rendered with
  ansible and the rendered script runs in a subprocess, with http.client pointed at a local fake
  that answers as Authentik does: a directory read is 200, a write the view grant cannot make is 403.

Requires ansible-playbook for the two render tests (the real `template` module, as the deploy uses).
"""

import json
import os
import re
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from ansible_python import needs_ansible
from fake_http import DrainingHandler

REPO = Path(__file__).resolve().parents[2]
DEPLOYMENT = REPO / "platform/services/authentik/deployment"
BLUEPRINT = DEPLOYMENT / "blueprints/console-oidc.yaml"
ENV_J2 = DEPLOYMENT / "templates/env.j2"
VERIFY = DEPLOYMENT / "templates/verify-users.py.j2"
SECRET_DECL = REPO / "platform/playbooks/vars/secret-declarations/authentik.yml"

# Synthetic values only. Never a real credential.
DIR_TOKEN = "synthetic-directory-token-0001"
BOOT_TOKEN = "synthetic-bootstrap-token-0002"
PROBE_PK = "pk-write-probe-0003"


# ── reading the blueprint ───────────────────────────────────────────────────────
class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that keeps every Authentik tag (!Env, !KeyOf, !Find, !Context) as a one-key dict."""


def _tagged(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return {"!" + suffix: loader.construct_sequence(node, deep=True)}
    return {"!" + suffix: loader.construct_scalar(node)}


_BlueprintLoader.add_multi_constructor("!", _tagged)


def _entries() -> list:
    loader = _BlueprintLoader(BLUEPRINT.read_text(encoding="utf-8"))
    try:
        return loader.get_single_data()["entries"]
    finally:
        loader.dispose()


def _entry(model: str, **identifiers) -> dict:
    found = [
        e for e in _entries()
        if e["model"] == model and all(e.get("identifiers", {}).get(k) == v for k, v in identifiers.items())
    ]
    assert len(found) == 1, f"expected exactly one {model} {identifiers}, found {len(found)}"
    return found[0]


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


# ── structure: the blueprint declares a view-only identity ─────────────────────
def test_the_role_grants_exactly_the_two_view_permissions():
    role = _entry("authentik_rbac.role", name="console-directory-reader")
    assert role["id"] == "console-directory-reader-role"
    perms = role["attrs"]["permissions"]
    assert sorted(perms) == ["authentik_core.view_group", "authentik_core.view_user"], perms
    assert not [p for p in perms if p.rsplit(".", 1)[-1].startswith(("add_", "change_", "delete_"))], perms


def test_the_group_is_not_superuser_and_carries_only_the_reader_role():
    grp = _entry("authentik_core.group", name="console-directory-readers")
    assert grp["id"] == "console-directory-readers-group"
    assert grp["attrs"]["is_superuser"] is False
    assert grp["attrs"]["roles"] == [{"!KeyOf": "console-directory-reader-role"}]


def test_the_group_declares_parent_null_so_a_hand_set_parent_is_reset_on_apply():
    # A re-apply writes only the declared attributes. Without `parent: null` a parent set by hand
    # in the UI survives, and is_superuser resolves over every ancestor, so a superuser parent
    # would make the reader a superuser. The key must be PRESENT and null, not merely absent.
    grp = _entry("authentik_core.group", name="console-directory-readers")
    assert "parent" in grp["attrs"], grp["attrs"]
    assert grp["attrs"]["parent"] is None, grp["attrs"]["parent"]


def test_the_account_is_a_service_account_whose_only_group_is_the_reader_group():
    user = _entry("authentik_core.user", username="svc-console-directory")
    assert user["id"] == "console-directory-user"
    attrs = user["attrs"]
    assert attrs["type"] == "service_account"
    assert attrs["path"] == "users/service-accounts"
    assert attrs["is_active"] is True
    assert attrs["groups"] == [{"!KeyOf": "console-directory-readers-group"}]
    assert "password" not in attrs
    assert "is_superuser" not in attrs


def test_nothing_in_the_console_blueprint_reaches_platform_admins():
    # The admin tier is a !Find on platform-admins. The directory identity must never reach it,
    # by a group reference, a role or a !Find anywhere in the file.
    refs = [s for e in _entries() for s in _strings(e)]
    assert "platform-admins" not in refs


def test_the_token_is_an_api_token_that_does_not_expire_and_reads_the_worker_env():
    tok = _entry("authentik_core.token", identifier="console-directory-reader-api-token")
    attrs = tok["attrs"]
    assert attrs["intent"] == "api"
    assert attrs["expiring"] is False
    assert attrs["key"] == {"!Env": "CONSOLE_DIRECTORY_TOKEN"}
    assert attrs["user"] == {"!KeyOf": "console-directory-user"}


# ── the worker env and the OpenBao field ───────────────────────────────────────
def test_the_secret_is_a_generated_random_value_of_its_own():
    decl = yaml.safe_load(SECRET_DECL.read_text(encoding="utf-8"))
    matches = [d for d in decl["_static_secret_definitions"] if d.get("name") == "console_directory_token"]
    assert matches == [{"name": "console_directory_token", "type": "random", "length": 60}], matches


@needs_ansible
def test_env_j2_renders_the_directory_token_beside_the_service_account_token(tmp_path):
    names = sorted(set(re.findall(r"secrets\.([a-z0-9_]+)", ENV_J2.read_text(encoding="utf-8"))))
    secrets = {name: f"synthetic-{name}" for name in names}
    secrets["console_directory_token"] = "dir-sentinel-0004"
    secrets["service_account_token"] = "synthetic-svc-0005"
    text = _render(tmp_path, ENV_J2, {"secrets": secrets, "_platform_admins": []})
    lines = [ln for ln in text.splitlines() if ln.startswith("CONSOLE_DIRECTORY_TOKEN=")]
    assert lines == ["CONSOLE_DIRECTORY_TOKEN=dir-sentinel-0004"], lines
    assert "SERVICE_ACCOUNT_TOKEN=synthetic-svc-0005" in text.splitlines()


# ── the deploy's live proof (verify-users.py.j2, section 5) ────────────────────
def _render(tmp: Path, src: Path, variables: dict) -> str:
    dest = tmp / "rendered.out"
    play = [{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": variables,
        "tasks": [{"ansible.builtin.template": {"src": str(src), "dest": str(dest), "mode": "0644"}}],
    }]
    playbook = tmp / "render.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(playbook)],
                          cwd=tmp, env=env, text=True, capture_output=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    return dest.read_text(encoding="utf-8")


def _verify_vars(enabled_apps: list, settle: int = 0) -> dict:
    return {
        "_shared_blueprints": ["platform-groups.yaml"],
        "_enabled_apps": enabled_apps,
        "authentik_app_catalog": {"console": {"file": "console-oidc.yaml", "type": "oidc"}},
        "_active_usernames": [],
        "_retired_usernames": [],
        "_retired_blueprints": [],
        "_platform_admins": [{"username": "stray"}],
        "_platform_admins_exceptions": [],
        "authentik_blueprint_settle_seconds": settle,
    }


@pytest.fixture(scope="module")
def rendered_console(tmp_path_factory):
    if not _ansible_available():
        pytest.skip("ansible-playbook not installed")
    return _render(tmp_path_factory.mktemp("console"), VERIFY, _verify_vars(["console"]))


@pytest.fixture(scope="module")
def rendered_no_console(tmp_path_factory):
    if not _ansible_available():
        pytest.skip("ansible-playbook not installed")
    return _render(tmp_path_factory.mktemp("plain"), VERIFY, _verify_vars([]))


def _ansible_available() -> bool:
    import shutil
    return shutil.which("ansible-playbook") is not None


def _scenario(**over) -> dict:
    sc = {
        "blueprints": [
            {"path": "custom/platform-groups.yaml", "status": "successful"},
            {"path": "custom/console-oidc.yaml", "status": "successful"},
            {"path": "custom/zz-sso-bindings.yaml", "status": "successful"},
            {"path": "custom/platform-admins.yaml", "status": "successful"},
        ],
        "users": [
            {"username": "stray", "is_superuser": True, "is_active": True,
             "groups_obj": [{"name": "platform-admins"}]},
            {"username": "svc-console-directory", "is_superuser": False, "is_active": True,
             "groups_obj": [{"name": "console-directory-readers"}]},
        ],
        "groups": [{"name": "platform-admins", "users_obj": [{"username": "stray"}]}],
        "dir_reads": [],       # statuses handed to successive directory reads before the default 200
        "write_status": 403,   # what POST /api/v3/core/groups/ answers the directory token
        "calls": [],           # (method, path, who) for every request the script makes
        "deleted": [],
    }
    sc.update(over)
    return sc


def _handler(sc: dict):
    class Handler(DrainingHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, body=None):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _who(self):
            auth = self.headers.get("Authorization", "")
            if auth == "Bearer " + BOOT_TOKEN:
                return "bootstrap"
            if auth == "Bearer " + DIR_TOKEN:
                return "directory"
            return None

        def _directory_read(self, who, results):
            # The view grant reads the list. A token without it is refused, as Authentik refuses it.
            if who == "bootstrap":
                return self._reply(200, {"results": results})
            status = sc["dir_reads"].pop(0) if sc["dir_reads"] else 200
            if status != 200:
                return self._reply(status, {"detail": "refused"})
            return self._reply(200, {"results": results})

        def _route(self, method):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            who = self._who()
            sc["calls"].append((method, url.path, who))
            if who is None:
                return self._reply(401, {"detail": "Invalid token"})
            if method == "GET" and url.path == "/api/v3/managed/blueprints/":
                return self._reply(200, {"results": sc["blueprints"]})
            if method == "GET" and url.path == "/api/v3/core/users/":
                if "username" in query:
                    return self._reply(200, {"results": [u for u in sc["users"] if u["username"] in query["username"]]})
                return self._directory_read(who, sc["users"][:1])
            if method == "GET" and url.path == "/api/v3/core/groups/":
                if "name" in query:
                    return self._reply(200, {"results": [g for g in sc["groups"] if g["name"] in query["name"]]})
                return self._directory_read(who, sc["groups"][:1])
            if method == "POST" and url.path == "/api/v3/core/groups/":
                if who == "directory" and sc["write_status"] == 201:
                    return self._reply(201, {"pk": PROBE_PK, "name": "console-directory-write-probe"})
                return self._reply(sc["write_status"], {"detail": "You do not have permission"})
            if method == "DELETE" and url.path == "/api/v3/core/groups/%s/" % PROBE_PK:
                if who != "bootstrap":
                    return self._reply(403, {"detail": "You do not have permission"})
                sc["deleted"].append(url.path)
                return self._reply(204)
            return self._reply(404, {"detail": "not faked"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    return Handler


@pytest.fixture
def fake_api():
    servers = []

    def start(sc: dict) -> int:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(sc))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv.server_address[1]

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


# Runs the rendered script with http.client aimed at the fake. The script itself is unchanged:
# its "localhost:9000" is redirected here, and nothing else is.
LAUNCHER = """
import http.client, runpy, sys
port, script = int(sys.argv[1]), sys.argv[2]
class _FakeAuthentik(http.client.HTTPConnection):
    def __init__(self, host, _port=None, *args, **kwargs):
        super().__init__("127.0.0.1", port, *args, **kwargs)
http.client.HTTPConnection = _FakeAuthentik
sys.argv = [script]
runpy.run_path(script, run_name="__main__")
"""


def _run(tmp: Path, rendered: str, port: int, dir_token=DIR_TOKEN):
    script = tmp / "verify-users.py"
    script.write_text(rendered, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "AUTHENTIK_BOOTSTRAP_TOKEN": BOOT_TOKEN}
    if dir_token is not None:
        env["CONSOLE_DIRECTORY_TOKEN"] = dir_token
    return subprocess.run([sys.executable, "-I", "-c", LAUNCHER, str(port), str(script)],
                          env=env, capture_output=True, text=True, timeout=60, check=False)


def test_the_console_verify_passes_read_200_write_403_and_no_probe_is_left(tmp_path, rendered_console, fake_api):
    sc = _scenario()
    done = _run(tmp_path, rendered_console, fake_api(sc))
    out = done.stdout
    assert done.returncode == 0, out + done.stderr
    assert "GET /api/v3/core/users/ -> 200" in out
    assert "GET /api/v3/core/groups/ -> 200" in out
    assert "POST /api/v3/core/groups/ -> 403 (write refused)" in out
    assert "superuser=false" in out
    assert "VERIFY OK" in out
    # The write was attempted with the directory token, and nothing was created or removed.
    assert ("POST", "/api/v3/core/groups/", "directory") in sc["calls"]
    assert sc["deleted"] == []


def test_a_token_that_can_write_fails_the_deploy_and_its_probe_group_is_removed(tmp_path, rendered_console, fake_api):
    sc = _scenario(write_status=201)
    done = _run(tmp_path, rendered_console, fake_api(sc))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "must be 403: the token is not read-only" in done.stdout
    assert "VERIFY FAILED" in done.stdout
    # The probe group the token created is removed with the bootstrap token before the failure.
    assert sc["deleted"] == ["/api/v3/core/groups/%s/" % PROBE_PK]


def test_a_directory_read_the_token_cannot_make_fails_the_deploy(tmp_path, rendered_console, fake_api):
    sc = _scenario(dir_reads=[403])
    done = _run(tmp_path, rendered_console, fake_api(sc))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "GET /api/v3/core/users/ -> 403 (the directory read must be 200)" in done.stdout


@needs_ansible
def test_a_directory_read_that_is_not_yet_applied_is_polled_within_the_settle_window(tmp_path_factory, fake_api):
    rendered = _render(tmp_path_factory.mktemp("settle"), VERIFY, _verify_vars(["console"], settle=5))
    sc = _scenario(dir_reads=[403])
    done = _run(tmp_path_factory.mktemp("run"), rendered, fake_api(sc))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "GET /api/v3/core/users/ -> 200" in done.stdout


def test_a_superuser_directory_account_fails_the_deploy(tmp_path, rendered_console, fake_api):
    users = _scenario()["users"]
    users[1]["is_superuser"] = True
    done = _run(tmp_path, rendered_console, fake_api(_scenario(users=users)))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "is_superuser is not false; the directory reader must not be a superuser" in done.stdout
    # The OK line says superuser=false; a run that refused the account must not print it.
    assert "OK   user" not in done.stdout, done.stdout


def test_a_directory_account_in_platform_admins_fails_the_deploy(tmp_path, rendered_console, fake_api):
    users = _scenario()["users"]
    users[1]["groups_obj"] = [{"name": "console-directory-readers"}, {"name": "platform-admins"}]
    done = _run(tmp_path, rendered_console, fake_api(_scenario(users=users)))
    assert done.returncode == 2, done.stdout + done.stderr
    assert "must not hold the admin tier" in done.stdout
    assert "OK   user" not in done.stdout, done.stdout


def test_a_missing_directory_token_in_the_server_env_fails_the_deploy(tmp_path, rendered_console, fake_api):
    done = _run(tmp_path, rendered_console, fake_api(_scenario()), dir_token=None)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "directory token missing from the server env" in done.stdout


def test_with_the_console_app_disabled_no_directory_probe_is_rendered_or_run(tmp_path, rendered_no_console, fake_api):
    # Rendered without the console app: the probe is absent from the script, and a deploy that
    # declares no console account must not need the directory token at all.
    assert 'os.environ.get("CONSOLE_DIRECTORY_TOKEN")' not in rendered_no_console
    assert "console-directory-write-probe" not in rendered_no_console
    sc = _scenario(
        blueprints=[b for b in _scenario()["blueprints"] if b["path"] != "custom/console-oidc.yaml"],
        users=[_scenario()["users"][0]],
    )
    done = _run(tmp_path, rendered_no_console, fake_api(sc), dir_token=None)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "VERIFY OK" in done.stdout
    assert not [c for c in sc["calls"] if c[2] == "directory"]
