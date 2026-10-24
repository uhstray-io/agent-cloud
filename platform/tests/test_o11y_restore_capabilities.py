"""Fixed-path, read-only verification of the Semaphore restore-coordination role."""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import harness_sandbox
import pytest
import yaml
from fake_http import DrainingHandler
from playbook_yaml import REPO

PLAYBOOKS = REPO / "platform/playbooks"
TASK = PLAYBOOKS / "tasks/verify-o11y-restore-coordination-capabilities.yml"
ENTRYPOINT = PLAYBOOKS / "verify-o11y-restore-coordination-capabilities.yml"
POLICY = REPO / "platform/services/openbao/deployment/config/policies/semaphore-read.hcl"
DATA_PATH = "secret/data/coordination/o11y-restore-target"
METADATA_PATH = "secret/metadata/coordination/o11y-restore-target"
EXPECTED = {DATA_PATH: ["create", "read", "update"], METADATA_PATH: ["read"]}
REVIEWED_SHA = "a" * 40

_spec = importlib.util.spec_from_file_location(
    "verify_o11y_restore_capabilities", PLAYBOOKS / "files/verify_o11y_restore_capabilities.py"
)
helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(helper)


def _payload(*, capabilities=None, stored_policy=None, policy_fields=None):
    source = POLICY.read_text()
    policy = source if stored_policy is None else stored_policy
    policy_data = {"policy": policy} if policy_fields is None else policy_fields
    return {
        "reviewed_repository_sha": REVIEWED_SHA,
        "expected_policy": source,
        "policy_status": 200,
        "policy_response": {"data": policy_data},
        "capabilities_status": 200,
        "capabilities_response": {"data": capabilities or EXPECTED},
    }


def test_exact_policy_and_effective_capabilities_pass_with_sanitized_result():
    result = helper.verify(_payload())
    assert result == {
        "status": "verified",
        "reason": "verified",
        "reviewed_repository_sha": REVIEWED_SHA,
        "policy_matches_source": True,
        "data_capabilities_match": True,
        "metadata_capabilities_match": True,
    }


@pytest.mark.parametrize("grant", ["root", "sudo", "delete", "list", "patch", "deny", "unknown"])
def test_extra_or_unrecognized_data_capabilities_refuse(grant):
    caps = {**EXPECTED, DATA_PATH: [*EXPECTED[DATA_PATH], grant]}
    result = helper.verify(_payload(capabilities=caps))
    assert result["status"] == "refused"
    assert result["reason"] == "capabilities_mismatch"
    assert result["policy_matches_source"] is True
    assert result["data_capabilities_match"] is False
    assert result["metadata_capabilities_match"] is True


@pytest.mark.parametrize("caps", [
    {DATA_PATH: ["read"], METADATA_PATH: ["read"]},
    {DATA_PATH: ["create", "read", "update"], METADATA_PATH: ["read", "list"]},
    {DATA_PATH: ["create", "read", "update"], "secret/data/other": ["read"]},
    {DATA_PATH: ["create", "read", "update", "read"], METADATA_PATH: ["read"]},
])
def test_missing_extra_or_ambiguous_capability_sets_refuse(caps):
    result = helper.verify(_payload(capabilities=caps))
    assert result["status"] == "refused"


def test_policy_source_mismatch_refuses_before_capability_acceptance():
    result = helper.verify(_payload(stored_policy="path \"secret/data/*\" { capabilities = [\"root\"] }"))
    assert result["status"] == "refused"
    assert result["reason"] == "policy_mismatch"
    assert result["policy_matches_source"] is False
    assert result["data_capabilities_match"] is False


@pytest.mark.parametrize("fields", [
    {"rules": "path \"x\" { capabilities = [\"read\"] }"},
    {"policy": "same", "rules": "different"},
    {"policy": "same", "name": "another-policy"},
])
def test_missing_or_ambiguous_documented_policy_field_refuses(fields):
    result = helper.verify(_payload(policy_fields=fields))
    assert result["status"] == "refused"
    assert result["reason"] in {"policy_response_invalid", "policy_response_ambiguous"}


def test_helper_does_not_echo_credentials_urls_policy_or_capability_payloads():
    secret_markers = ("synthetic-role-secret", "synthetic-token", "https://private.example", "raw-policy-text")
    payload = _payload(stored_policy="raw-policy-text")
    payload["policy_response"]["data"]["policy"] = "raw-policy-text"
    output = json.dumps(helper.verify(payload))
    assert all(marker not in output for marker in secret_markers)
    assert "create" not in output and "update" not in output


def test_dev_entrypoint_and_catalog_bind_only_the_reviewed_sha_and_fixed_sources():
    plays = yaml.safe_load(ENTRYPOINT.read_text())
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    payload_paths = plays[1]["vars"]["_reviewed_checkout_payload_paths"]
    assert set(payload_paths) == {
        "platform/playbooks/verify-o11y-restore-coordination-capabilities.yml",
        "platform/playbooks/tasks/verify-o11y-restore-coordination-capabilities.yml",
        "platform/playbooks/files/verify_o11y_restore_capabilities.py",
        "platform/playbooks/require-reviewed-checkout.yml",
        "platform/playbooks/files/reviewed-checkout.py",
        "platform/playbooks/tasks/preflight-o11y-restore-target.yml",
        "platform/playbooks/tasks/assert-bao-transport.yml",
        "platform/semaphore/templates.yml",
        "platform/services/openbao/deployment/config/policies/semaphore-read.hcl",
    }
    assert len(payload_paths) <= 16

    catalog = yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]
    matches = [item for item in catalog if item.get("name") == "Verify o11y Restore Coordination Capabilities (Dev)"]
    assert len(matches) == 1
    assert matches[0]["playbook"] == "platform/playbooks/verify-o11y-restore-coordination-capabilities.yml"
    assert matches[0]["repository"] == "agent-cloud dev"
    assert [item["name"] for item in matches[0]["survey_vars"]] == ["expected_repository_sha"]
    assert matches[0]["survey_vars"][0]["required"] is True


def test_requests_are_fixed_read_only_and_check_mode_safe():
    tasks = yaml.safe_load(TASK.read_text())
    uri_tasks = [item for item in tasks if "ansible.builtin.uri" in item]
    assert [item["name"] for item in uri_tasks] == [
        "Authenticate with the protected Semaphore AppRole",
        "Read back the fixed Semaphore policy",
        "Read effective capabilities for the two fixed coordination paths",
    ]
    args = [item["ansible.builtin.uri"] for item in uri_tasks]
    assert [item["method"] for item in args] == ["POST", "GET", "POST"]
    assert args[0]["url"] == "{{ _orca_bao_url }}/v1/auth/approle/login"
    assert args[1]["url"] == "{{ _orca_bao_url }}/v1/sys/policies/acl/semaphore-read"
    assert args[2]["url"] == "{{ _orca_bao_url }}/v1/sys/capabilities-self"
    assert args[2]["body"]["paths"] == [DATA_PATH, METADATA_PATH]
    assert args[0]["headers"] == {}
    assert args[1]["headers"] == {"X-Vault-Token": "{{ _orca_login.json.auth.client_token }}"}
    assert args[2]["headers"] == {"X-Vault-Token": "{{ _orca_login.json.auth.client_token }}"}
    assert all(item["unredirected_headers"] == ["X-Vault-Token"] for item in args[1:])
    assert all("coordination/o11y-restore-target" not in item["url"] for item in args)
    for task, uri in zip(uri_tasks, args, strict=True):
        assert task["no_log"] is True
        assert task["changed_when"] is False
        assert task["check_mode"] is False
        assert uri["validate_certs"] is True
        assert uri["use_proxy"] is False
        assert uri["use_netrc"] is False
        assert uri["follow_redirects"] == "none"
        assert uri["url_username"] is None and uri["url_password"] is None
    assert not any(uri["method"] in {"PUT", "PATCH", "DELETE"} for uri in args)


def _run_fixture(tmp_path: Path, *, capabilities=None, stored_policy=None, check=False, extra=None, publish=False):
    class FakeBao(DrainingHandler):
        requests = []
        published_policy = None

        def log_message(self, *_args):
            pass

        def _reply(self, body, status=200):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            type(self).requests.append(("POST", self.path, body, self.headers.get("X-Vault-Token")))
            if self.path == "/v1/auth/approle/login":
                return self._reply({"auth": {"client_token": "synthetic-token"}})
            if self.path == "/v1/sys/capabilities-self":
                return self._reply({"data": capabilities or EXPECTED})
            return self._reply({"errors": ["unexpected path"]}, 404)

        def do_PUT(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            type(self).requests.append(("PUT", self.path, body, self.headers.get("X-Vault-Token")))
            if self.path != "/v1/sys/policies/acl/semaphore-read":
                return self._reply({"errors": ["unexpected path"]}, 404)
            type(self).published_policy = body["policy"]
            return self._reply({"data": {}})

        def do_GET(self):  # noqa: N802
            type(self).requests.append(("GET", self.path, {}, self.headers.get("X-Vault-Token")))
            if self.path == "/v1/sys/policies/acl/semaphore-read":
                value = type(self).published_policy
                if value is None:
                    value = POLICY.read_text() if stored_policy is None else stored_policy
                return self._reply({"data": {"name": "semaphore-read", "policy": value, "rules": value}})
            return self._reply({"errors": ["unexpected path"]}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBao)
    server.daemon_threads = True
    address = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    files = tmp_path / "files"
    files.mkdir(exist_ok=True)
    shutil.copy2(PLAYBOOKS / "files/verify_o11y_restore_capabilities.py", files / "verify_o11y_restore_capabilities.py")
    inventory = tmp_path / "inventory.yml"
    inventory.write_text(yaml.safe_dump({
        "all": {
            "hosts": {"localhost": {"ansible_connection": "local"}},
            "vars": {"openbao_addr": address},
            "children": {"openbao_svc": {"hosts": {"openbao-test": {"service_url": address}}}},
        }
    }))
    play = tmp_path / "play.yml"
    payload = base64.b64encode(POLICY.read_bytes()).decode("ascii")
    play.write_text(yaml.safe_dump([{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": {
            "expected_repository_sha": REVIEWED_SHA,
            "_reviewed_checkout_payloads": {
                "platform/services/openbao/deployment/config/policies/semaphore-read.hcl": payload,
            },
        },
        "tasks": [{"name": "Run the exact shared verification", "ansible.builtin.include_tasks": str(TASK)}],
    }], sort_keys=False))
    env = harness_sandbox.env_for(tmp_path)
    env.update(BAO_ROLE_ID="synthetic-role-id", BAO_SECRET_ID="synthetic-role-secret")
    publisher_done = None
    try:
        if publish:
            publisher_play = tmp_path / "publisher.yml"
            publisher_play.write_text(yaml.safe_dump([{
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "vars": {
                    "_bao_url": address,
                    "_bao_role_id": "synthetic-role-id",
                    "_bao_secret_id": "synthetic-role-secret",
                    "_policy_name": "semaphore-read",
                    "_policy_file": str(POLICY),
                },
                "tasks": [{
                    "name": "Run the real shared policy publisher",
                    "ansible.builtin.include_tasks": str(PLAYBOOKS / "tasks/apply-openbao-policy.yml"),
                }],
            }], sort_keys=False))
            publisher_done = harness_sandbox.run(
                ["ansible-playbook", "-i", str(inventory), str(publisher_play)],
                tmp_path,
                cwd=REPO,
                env=env,
            )
        done = harness_sandbox.run(
            ["ansible-playbook", "-i", str(inventory), str(play), "-e", json.dumps(extra or {}),
             *( ["--check"] if check else [])],
            tmp_path,
            cwd=REPO,
            env=env,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return done, FakeBao.requests, publisher_done


@pytest.mark.parametrize("check", [False, True])
def test_synthetic_http_readback_runs_only_fixed_reads_even_in_check_mode(tmp_path, check):
    done, requests, _publisher_done = _run_fixture(tmp_path, check=check)
    assert done.returncode == 0, done.stdout + done.stderr
    assert [item[:2] for item in requests] == [
        ("POST", "/v1/auth/approle/login"),
        ("GET", "/v1/sys/policies/acl/semaphore-read"),
        ("POST", "/v1/sys/capabilities-self"),
    ]
    assert requests[-1][2] == {"paths": [DATA_PATH, METADATA_PATH]}
    assert all(item[3] in {None, "synthetic-token"} for item in requests)
    assert "verified" in done.stdout
    assert "synthetic-role-secret" not in done.stdout + done.stderr
    assert "synthetic-token" not in done.stdout + done.stderr
    assert POLICY.read_text() not in done.stdout + done.stderr


def test_synthetic_http_extra_effective_grant_refuses_without_record_access(tmp_path):
    broad = {**EXPECTED, DATA_PATH: [*EXPECTED[DATA_PATH], "delete"]}
    done, requests, _publisher_done = _run_fixture(tmp_path, capabilities=broad)
    assert done.returncode != 0
    assert "capabilities_mismatch" in done.stdout
    assert [item[:2] for item in requests] == [
        ("POST", "/v1/auth/approle/login"),
        ("GET", "/v1/sys/policies/acl/semaphore-read"),
        ("POST", "/v1/sys/capabilities-self"),
    ]
    assert "secret/data/coordination/o11y-restore-target" not in done.stdout + done.stderr
    assert "synthetic-role-secret" not in done.stdout + done.stderr
    assert "synthetic-token" not in done.stdout + done.stderr


@pytest.mark.parametrize("name", ["openbao_addr", "bao_role_id", "BAO_ROLE_ID"])
def test_launch_endpoint_or_credential_override_refuses_before_network(tmp_path, name):
    done, requests, _publisher_done = _run_fixture(tmp_path, extra={name: "synthetic-override"})
    assert done.returncode != 0
    assert requests == []
    assert "synthetic-override" not in done.stdout + done.stderr


def test_shared_policy_publisher_preserves_bytes_for_exact_verifier_readback(tmp_path):
    done, requests, publisher_done = _run_fixture(tmp_path, publish=True)
    assert publisher_done is not None and publisher_done.returncode == 0
    assert done.returncode == 0, done.stdout + done.stderr
    assert requests[1][:2] == ("PUT", "/v1/sys/policies/acl/semaphore-read")
    assert requests[1][2] == {"policy": POLICY.read_text()}
    assert [item[:2] for item in requests] == [
        ("POST", "/v1/auth/approle/login"),
        ("PUT", "/v1/sys/policies/acl/semaphore-read"),
        ("POST", "/v1/auth/approle/login"),
        ("GET", "/v1/sys/policies/acl/semaphore-read"),
        ("POST", "/v1/sys/capabilities-self"),
    ]
    assert "verified" in done.stdout
    assert POLICY.read_text() not in done.stdout + done.stderr + publisher_done.stdout + publisher_done.stderr
