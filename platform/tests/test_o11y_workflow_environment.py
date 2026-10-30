"""Dev-bound isolation wrapper coverage against a clean checkout and fake APIs."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness_sandbox
import pytest
import yaml
from fake_http import DrainingHandler
from playbook_yaml import REPO

WRAPPER = REPO / "platform/playbooks/provision-o11y-workflow-environment.yml"
CATALOG = REPO / "platform/semaphore/templates.yml"
TARGET = "Verify o11y Restore Coordination Capabilities (Dev)"
SHA = "expected_repository_sha"
PROJECT = "semaphore_project_id"
INVENTORY = "semaphore_inventory_id"
SOURCE = "semaphore_source_environment_id"
ACCESS = "fixture-semaphore-access"
ROLE = "fixture-role-id"
SECRET = "fixture-role-secret"
TOKEN = "fixture-openbao-token"


def _copy_file(source: Path, checkout: Path, relative: str) -> None:
    target = checkout / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _fixture_checkout(tmp_path: Path, *, api_origin=None, catalog_change=None) -> tuple[Path, str]:
    checkout = tmp_path / "checkout"
    paths = [
        "platform/playbooks/provision-o11y-workflow-environment.yml",
        "platform/playbooks/refuse-internal-extra-vars.yml",
        "platform/playbooks/require-reviewed-checkout.yml",
        "platform/playbooks/files/reviewed-checkout.py",
        "platform/playbooks/tasks/assert-bao-transport.yml",
        "platform/playbooks/tasks/require-parsed-inventory.yml",
        "platform/semaphore/provision-seed-environment.yml",
        "platform/semaphore/templates.yml",
        "platform/semaphore/repositories.yml",
        "platform/semaphore/filter_plugins/publication.py",
        "platform/semaphore/tasks/runtime-access.yml",
        "platform/semaphore/tasks/isolated-environments.yml",
    ]
    for relative in paths:
        _copy_file(REPO / relative, checkout, relative)

    if api_origin:
        wrapper_path = checkout / "platform/playbooks/provision-o11y-workflow-environment.yml"
        production_wrapper = WRAPPER.read_text()
        fixed_origin = '"http://127.0.0.1:3000"'
        assert production_wrapper.count(fixed_origin) == 1
        fixture_wrapper = production_wrapper.replace(fixed_origin, json.dumps(api_origin), 1)
        wrapper_path.write_text(fixture_wrapper)
        assert fixture_wrapper == wrapper_path.read_text()
        assert fixture_wrapper.replace(json.dumps(api_origin), fixed_origin, 1) == production_wrapper

    if catalog_change:
        catalog_path = checkout / "platform/semaphore/templates.yml"
        catalog = yaml.safe_load(catalog_path.read_text())
        catalog_change(catalog["templates"])
        catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False))

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(["git", "init", "-q", str(checkout)], env=git_env, check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], env=git_env, check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "fixture"], env=git_env, check=True)
    revision = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        env=git_env, text=True, capture_output=True, check=True,
    ).stdout.strip()
    check = subprocess.run(
        [sys.executable, str(checkout / "platform/playbooks/files/reviewed-checkout.py"),
         "--checkout", str(checkout), "--expected-sha", revision],
        text=True, capture_output=True, check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    return checkout, revision


class _SemaphoreAndBao(DrainingHandler):
    requests: list[tuple[str, str]] = []
    writes: list[tuple[str, str]] = []
    repositories: list[dict] = []
    records: list[dict] = []
    environments: list[dict] = []
    auth_values: dict[str, str] = {}

    def log_message(self, *_args):
        pass

    def reply(self, body, status=200):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self):
        return self.headers.get("Authorization") == f"Bearer {ACCESS}"

    def do_GET(self):  # noqa: N802
        type(self).requests.append(("GET", self.path))
        if self.path == "/api/project/1/repositories":
            return self.reply(type(self).repositories if self._authorized() else {}, 200 if self._authorized() else 403)
        if self.path == "/api/project/1/templates":
            rows = [{k: v for k, v in row.items() if k not in {"description", "survey_vars"}}
                    for row in type(self).records]
            return self.reply(rows if self._authorized() else {}, 200 if self._authorized() else 403)
        if self.path == "/api/project/1/environment":
            return self.reply(type(self).environments if self._authorized() else {}, 200 if self._authorized() else 403)
        for row in type(self).environments:
            if self.path == f"/api/project/1/environment/{row['id']}":
                value = {k: v for k, v in row.items() if k != "secrets" or v != []}
                return self.reply(value if self._authorized() else {}, 200 if self._authorized() else 403)
        for row in type(self).records:
            if self.path == f"/api/project/1/templates/{row['id']}":
                return self.reply(row if self._authorized() else {}, 200 if self._authorized() else 403)
        if self.path.endswith("/tasks/last"):
            return self.reply([] if self._authorized() else {}, 200 if self._authorized() else 403)
        return self.reply({}, 404)

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).requests.append(("POST", self.path))
        if self.path == "/v1/auth/approle/login":
            if json.loads(body) != {"role_id": ROLE, "secret_id": SECRET}:
                return self.reply({"errors": [SECRET]}, 403)
            return self.reply({"auth": {"client_token": TOKEN}})
        if not self._authorized():
            return self.reply({}, 403)
        type(self).writes.append(("POST", self.path))
        if self.path == "/api/project/1/environment":
            value = json.loads(body)
            value["id"] = 501
            type(self).environments.append(value)
            return self.reply(value, 201)
        return self.reply({}, 404)

    def do_PUT(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        type(self).requests.append(("PUT", self.path))
        if not self._authorized():
            return self.reply({}, 403)
        type(self).writes.append(("PUT", self.path))
        value = json.loads(body)
        for index, row in enumerate(type(self).environments):
            if self.path == f"/api/project/1/environment/{row['id']}":
                for operation in value.get("secrets", []):
                    if operation["operation"] != "create":
                        return self.reply({}, 400)
                    type(self).auth_values[operation["name"]] = operation["secret"]
                type(self).environments[index] = value | {
                    "secrets": [{"id": 701 + i, "name": item["name"], "type": "env"}
                                for i, item in enumerate(value.get("secrets", []))]
                }
                return self.reply({})
        for index, row in enumerate(type(self).records):
            if self.path == f"/api/project/1/templates/{row['id']}":
                type(self).records[index] = value
                return self.reply({})
        return self.reply({}, 404)


def _reset_api():
    _SemaphoreAndBao.requests = []
    _SemaphoreAndBao.writes = []
    _SemaphoreAndBao.repositories = [
        {"id": 1, "name": "agent-cloud", "git_branch": "main",
         "git_url": "https://github.com/uhstray-io/agent-cloud.git"},
        {"id": 5, "name": "agent-cloud dev", "git_branch": "dev",
         "git_url": "https://github.com/uhstray-io/agent-cloud.git"},
    ]
    declaration = next(
        row for row in yaml.safe_load(CATALOG.read_text())["templates"] if row["name"] == TARGET
    )
    _SemaphoreAndBao.records = [{
        "id": 206, "name": TARGET, "project_id": 1, "repository_id": 5,
        "inventory_id": 37, "environment_id": 42,
        "playbook": declaration["playbook"], "app": "ansible", "arguments": "[]",
        "autorun": False, "view_id": 9, "survey_vars": [],
    }, {
        "id": 99, "name": "Unrelated", "project_id": 1, "environment_id": 77,
        "survey_vars": [{"name": "preserve"}],
    }]
    _SemaphoreAndBao.environments = [{
        "id": 42, "project_id": 1, "name": "shared", "json": '{"shared":"keep"}',
        "env": "{}", "secrets": [{"id": 70, "name": "SHARED", "type": "env"}],
    }]
    _SemaphoreAndBao.auth_values = {}


def _run_wrapper(tmp_path: Path, checkout: Path, revision: str, *, extra=None, bao_url=None, check=False):
    if bao_url is None:
        bao_url = "http://127.0.0.1:3000"
    inventory = tmp_path / "inventory.yml"
    inventory.write_text(yaml.safe_dump({
        "all": {
            "hosts": {"localhost": {"ansible_connection": "local"}},
            "vars": {"openbao_addr": bao_url},
            "children": {"openbao_svc": {"hosts": {"openbao-test": {"service_url": bao_url}}}},
        }
    }))
    values = {
        SHA: revision,
        "o11y_workflow_target": TARGET,
        PROJECT: 1,
        INVENTORY: 37,
        SOURCE: 42,
    }
    values.update(extra or {})
    command = [
        shutil.which("ansible-playbook") or "ansible-playbook",
        "-i", str(inventory), str(checkout / "platform/playbooks/provision-o11y-workflow-environment.yml"),
        "-e", json.dumps(values),
    ]
    if check:
        command.append("--check")
    env = harness_sandbox.env_for(tmp_path)
    env.update(
        SEMAPHORE_TOKEN=ACCESS,
        BAO_ROLE_ID=ROLE,
        BAO_SECRET_ID=SECRET,
        PYTHONDONTWRITEBYTECODE="1",
        ANSIBLE_STDOUT_CALLBACK="default",
    )
    return harness_sandbox.run(command, tmp_path, cwd=REPO, env=env)


def test_catalog_declares_only_the_five_explicit_dev_rows_and_wrapper_survey():
    rows = yaml.safe_load(CATALOG.read_text())["templates"]
    names = [
        "Verify o11y Restore Coordination Capabilities (Dev)",
        "Preflight o11y Restore Target (Dev)",
        "Provision PBS Debian Cloud Template (Dev)",
        "Attach PBS Data Disk (Dev)",
        "Provision PBS VM (Dev)",
    ]
    targets = [row for row in rows if row["name"] in names]
    assert len(targets) == len(names)
    assert len({row["isolated_environment"] for row in targets}) == len(names)
    assert all(row["repository"] == "agent-cloud dev" for row in targets)
    assert all(row.get("dev_variant") is False for row in targets)

    wrapper = next(row for row in rows if row["name"] == "Provision o11y Workflow Environment (Dev)")
    assert wrapper["repository"] == "agent-cloud dev"
    assert wrapper.get("dev_variant") is False
    assert [field["name"] for field in wrapper["survey_vars"]] == [
        SHA, "o11y_workflow_target", PROJECT, INVENTORY, SOURCE,
    ]
    assert wrapper["survey_vars"][1]["values"] == names
    assert not any("role" in field["name"].lower() or "secret" in field["name"].lower()
                   for field in wrapper["survey_vars"])
    entrypoint = yaml.safe_load(WRAPPER.read_text())
    provision = next(
        row for row in entrypoint
        if row.get("name") == "Provision one declared protected Dev workflow environment"
    )
    assert provision["vars"]["_semaphore_url"] == "http://127.0.0.1:3000"


def test_real_wrapper_and_shared_provisioner_converge_one_direct_dev_template(tmp_path):
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SemaphoreAndBao)
    except OSError:
        pytest.skip("could not bind the synthetic loopback API")
    server.daemon_threads = True
    _reset_api()
    api_origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    checkout, revision = _fixture_checkout(tmp_path, api_origin=api_origin)
    try:
        before_shared = copy.deepcopy(_SemaphoreAndBao.environments[0])
        before_unrelated = copy.deepcopy(_SemaphoreAndBao.records[1])
        done = _run_wrapper(tmp_path, checkout, revision, bao_url=api_origin)
        if "Local RPC server did not start" in done.stdout + done.stderr:
            pytest.skip("installed Ansible runtime could not start its local RPC server in the harness sandbox")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _SemaphoreAndBao.environments[0] == before_shared
        assert _SemaphoreAndBao.records[1] == before_unrelated
        assert _SemaphoreAndBao.records[0]["name"] == TARGET
        assert _SemaphoreAndBao.records[0]["repository_id"] == 5
        assert _SemaphoreAndBao.records[0]["environment_id"] == 501
        assert _SemaphoreAndBao.environments[1]["name"] == "o11y restore-capability inputs (Dev)"
        assert json.loads(_SemaphoreAndBao.environments[1]["json"] or "{}") == {}
        assert _SemaphoreAndBao.environments[1]["env"] == "{}"
        assert {item["name"] for item in _SemaphoreAndBao.environments[1]["secrets"]} == {
            "BAO_ROLE_ID", "BAO_SECRET_ID",
        }
        assert _SemaphoreAndBao.auth_values == {"BAO_ROLE_ID": ROLE, "BAO_SECRET_ID": SECRET}
        assert len(_SemaphoreAndBao.writes) == 3
        assert SECRET not in done.stdout + done.stderr
        assert TOKEN not in done.stdout + done.stderr
        writes = copy.deepcopy(_SemaphoreAndBao.writes)

        rerun = _run_wrapper(tmp_path, checkout, revision, bao_url=api_origin)
        assert rerun.returncode == 0, rerun.stdout + rerun.stderr
        assert _SemaphoreAndBao.writes == writes
        assert SECRET not in rerun.stdout + rerun.stderr
        assert TOKEN not in rerun.stdout + rerun.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("extra,bao_url,catalog_change", [
    ({"o11y_workflow_target": "Provision VM (Dev)"}, None, None),
    ({"seed_template": TARGET}, None, None),
    ({"seed_variant": "dev"}, None, None),
    ({"seed_replace_auth": True}, None, None),
    ({PROJECT: 0}, None, None),
    ({INVENTORY: 0}, None, None),
    ({SOURCE: 0}, None, None),
    ({PROJECT: 2}, None, None),
    ({INVENTORY: 99}, None, None),
    ({SOURCE: 99}, None, None),
    ({SHA: "0" * 40}, None, None),
    ({"semaphore_url": "http://127.0.0.1:3000"}, None, None),
    ({"openbao_addr": "http://127.0.0.1:9"}, None, None),
    ({}, "https://elsewhere.example.invalid", None),
    ({}, None, lambda rows: next(row for row in rows if row["name"] == TARGET).update(repository="agent-cloud")),
    ({}, None, lambda rows: next(row for row in rows if row["name"] == TARGET).update(dev_variant=True)),
])
def test_invalid_scope_revision_endpoint_or_context_refuses_before_writes(tmp_path, extra, bao_url, catalog_change):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SemaphoreAndBao)
    server.daemon_threads = True
    _reset_api()
    api_origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    checkout, revision = _fixture_checkout(
        tmp_path, api_origin=api_origin, catalog_change=catalog_change,
    )
    try:
        result = _run_wrapper(tmp_path, checkout, revision, extra=extra,
                              bao_url=bao_url or api_origin)
        assert result.returncode != 0
        assert _SemaphoreAndBao.writes == []
        assert ROLE not in result.stdout + result.stderr
        assert SECRET not in result.stdout + result.stderr
        assert TOKEN not in result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
