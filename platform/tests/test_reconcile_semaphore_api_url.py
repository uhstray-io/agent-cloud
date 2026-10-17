"""Fail-closed contract for the Dev-bound Semaphore API URL reconciliation."""

import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import harness_sandbox
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/reconcile-semaphore-api-url.yml"
CATALOG = REPO / "platform/semaphore/templates.yml"
MERGE_HELPER = REPO / "platform/playbooks/tasks/bao-merge-keys.yml"


def _tasks():
    play = yaml.safe_load(PLAYBOOK.read_text())[-1]
    return play["tasks"]


def test_template_is_dev_bound_and_only_surveys_the_reviewed_sha():
    templates = yaml.safe_load(CATALOG.read_text())["templates"]
    template = next(t for t in templates if t["name"] == "Reconcile Semaphore API URL (Dev)")
    assert template["repository"] == "agent-cloud dev"
    assert [field["name"] for field in template["survey_vars"]] == ["expected_repository_sha"]


def test_review_and_extra_var_guards_run_before_the_play():
    document = yaml.safe_load(PLAYBOOK.read_text())
    assert document[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert document[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    assert "expected_repository_sha" not in str(document[-1].get("vars", {}))
    assert "extra_var_names" in str(document[-1]["vars"])


def test_semaphore_credentials_are_allowed_but_private_target_override_is_refused():
    vars_ = yaml.safe_load(PLAYBOOK.read_text())[-1]["vars"]
    forbidden = set(vars_["_reconcile_forbidden_inputs"])
    injected_credentials = {"openbao_addr", "bao_role_id", "bao_secret_id"}

    assert injected_credentials.isdisjoint(forbidden)
    assert {"semaphore_https_url", "OPENBAO_ADDR", "BAO_ROLE_ID", "BAO_SECRET_ID"} <= forbidden


def test_target_url_comes_from_exactly_one_semaphore_inventory_host():
    vars_ = yaml.safe_load(PLAYBOOK.read_text())[-1]["vars"]
    assert vars_["_reconcile_semaphore_hosts"] == "{{ groups.get('semaphore_svc', []) }}"
    assert vars_["_reconcile_semaphore_host"] == "{{ _reconcile_semaphore_hosts | first | default('') }}"
    assert vars_["_reconcile_target_url"] == (
        "{{ hostvars.get(_reconcile_semaphore_host, {}).get('semaphore_https_url', '') }}"
    )
    required = next(t for t in _tasks() if t["name"] == "Require the AppRole, OpenBao address, and private target URL")
    assert "_reconcile_semaphore_hosts | length == 1" in required["ansible.builtin.assert"]["that"]
    assert required["no_log"] is True


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize(
    ("semaphore_hosts", "include_target_url", "expected_success"),
    [
        (["semaphore"], True, True),
        (["semaphore"], False, False),
        ([], False, False),
        (["semaphore", "semaphore_backup"], True, False),
    ],
)
def test_implicit_localhost_reads_only_one_semaphore_host_url(
    tmp_path, semaphore_hosts, include_target_url, expected_success
):
    source_vars = yaml.safe_load(PLAYBOOK.read_text())[-1]["vars"]
    play = [{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": {
            key: source_vars[key]
            for key in (
                "_reconcile_semaphore_hosts",
                "_reconcile_semaphore_host",
                "_reconcile_target_url",
            )
        },
        "tasks": [{
            "name": "Require the unique private target host",
            "ansible.builtin.assert": {
                "that": [
                    "_reconcile_semaphore_hosts | length == 1",
                    "semaphore_https_url is not defined",
                    "_reconcile_target_url == 'https://semaphore.example/'",
                ],
                "fail_msg": "Semaphore inventory target is missing or ambiguous.",
                "quiet": True,
            },
            "no_log": True,
        }],
    }]
    inventory_lines = ["[semaphore_svc]"]
    inventory_lines.extend(
        f"{host} ansible_connection=local"
        + (" semaphore_https_url=https://semaphore.example/" if include_target_url else "")
        for host in semaphore_hosts
    )
    inventory = tmp_path / "inventory.ini"
    inventory.write_text("\n".join(inventory_lines) + "\n")
    playbook = tmp_path / "inventory-lookup.yml"
    playbook.write_text(yaml.safe_dump(play))

    result = harness_sandbox.run(
        ["ansible-playbook", "-i", str(inventory), str(playbook)],
        tmp_path,
        cwd=REPO,
        env=harness_sandbox.env_for(tmp_path),
    )
    if "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("host policy does not allow the sandbox-exec harness")
    if "Local RPC server did not start" in result.stderr:
        pytest.skip("installed Ansible local executor cannot start its RPC server in this environment")
    assert (result.returncode == 0) is expected_success, (result.stdout + result.stderr)[-1200:]
    assert "https://semaphore.example/" not in result.stdout + result.stderr


def test_desired_endpoint_is_root_https_and_preflight_is_read_only_and_private():
    tasks = _tasks()
    validation = next(t for t in tasks if "canonical HTTPS root URL" in t["name"])
    assertions = validation["ansible.builtin.assert"]["that"]
    assert any("^https://" in condition for condition in assertions)
    assert any("urlsplit('scheme') == 'https'" in condition for condition in assertions)
    assert any("urlsplit('path')" in condition for condition in assertions)
    assert any("urlsplit('query')" in condition for condition in assertions)
    assert any("urlsplit('fragment')" in condition for condition in assertions)
    assert any("urlsplit('username')" in condition for condition in assertions)
    assert any("urlsplit('password')" in condition for condition in assertions)

    preflight = next(t for t in tasks if "exact desired Semaphore endpoint" in t["name"])
    module = preflight["ansible.builtin.uri"]
    assert module["method"] == "GET"
    assert module["url"] == "{{ _reconcile_target_url.rstrip('/') }}/api/user"
    assert module["status_code"] == [200]
    assert module["follow_redirects"] == "none"
    assert module["use_netrc"] is False
    assert module["use_proxy"] is False
    assert preflight["check_mode"] is False
    assert preflight["no_log"] is True


def test_endpoint_pattern_accepts_https_roots_and_rejects_unsafe_shapes():
    tasks = _tasks()
    validation = next(t for t in tasks if "canonical HTTPS root URL" in t["name"])
    condition = validation["ansible.builtin.assert"]["that"][0]
    pattern = condition.split("match('", 1)[1].rsplit("')", 1)[0]
    https_root = "https:" + "/" + "/"
    http_root = "http:" + "/" + "/"
    safe = (
        https_root + "semaphore.example/",
        https_root + "semaphore.example:8443",
        https_root + "[2001:db8::1]/",
    )
    unsafe = (
        http_root + "semaphore.example/",
        https_root + "user:pass@semaphore.example/",
        https_root + "semaphore.example/api",
        https_root + "semaphore.example/?next=/",
        https_root + "semaphore.example/#fragment",
        https_root + "semaphore.example /",
    )
    assert all(re.fullmatch(pattern, value) for value in safe)
    for value in unsafe:
        assert not re.fullmatch(pattern, value), value
    assert all(urlsplit(value).scheme == "https" for value in safe)
    assert any("<= 65535" in check for check in validation["ansible.builtin.assert"]["that"])


def test_write_guards_preflight_url_and_token_with_version_cas_and_preserves_siblings():
    tasks = _tasks()
    merge = next(t for t in tasks if "compare-and-set" in t["name"])
    assert merge["ansible.builtin.include_tasks"] == "tasks/bao-merge-keys.yml"
    vars_ = merge["vars"]
    assert vars_["_bm_path"] == "services/semaphore"
    assert vars_["_bm_data"].keys() == {"url"}
    assert vars_["_bm_guard_values"] == {
        "url": "{{ _reconcile_original_data.url }}",
        "api_token": "{{ _reconcile_original_data.api_token }}",
    }
    assert vars_["_bm_on_missing"] == "fail"
    assert merge["no_log"] is True

    helper = yaml.safe_load((REPO / "platform/playbooks/tasks/bao-merge-keys.yml").read_text())
    guard = next(t for t in helper if t["name"] == "Require the guarded key to be unchanged")
    guard_expr = str(guard["ansible.builtin.assert"]["that"])
    assert "_bm_guard_values" in guard_expr
    assert "items2dict" in guard_expr
    assert guard["no_log"] is True
    cas_write = next(t for t in helper if t["name"] == "Write a guarded update at the version just read")
    assert "_bm_guard_values is defined or _bm_guard_key is defined" in cas_write["when"]
    assert cas_write["ansible.builtin.uri"]["body"]["options"]["cas"] == (
        "{{ _bm_existing.json.data.metadata.version }}"
    )
    assert cas_write["no_log"] is True

    readback = next(t for t in tasks if "Read back all Semaphore secret keys" in t["name"])
    assert readback["when"] == "_bm_changed | bool"
    assert readback["no_log"] is True
    verify = next(t for t in tasks if "Require the exact URL" in t["name"])
    assert "_reconcile_expected_data" in verify["ansible.builtin.assert"]["that"]
    assert verify["no_log"] is True


def test_verify_tag_runs_only_the_read_only_preflight_chain():
    tasks = _tasks()
    for task in tasks:
        if task["name"] in {
            "Merge only the URL using OpenBao version compare-and-set",
            "Read back all Semaphore secret keys after a write",
            "Require the exact URL and preserved sibling keys",
        }:
            assert "verify" not in task.get("tags", [])
        else:
            assert "verify" in task.get("tags", []), task["name"]


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
def test_helper_refuses_api_token_rotation_after_endpoint_preflight(tmp_path):
    guard = next(t for t in yaml.safe_load(MERGE_HELPER.read_text())
                 if t["name"] == "Require the guarded key to be unchanged")
    matching_guard = {**guard, "register": "_matching_guard"}
    rotated_guard = {
        **guard,
        "name": "Refuse an API token changed after preflight",
        "register": "_rotated_guard",
        "ignore_errors": True,
    }
    play = [{
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": {
            "_bm_current": {"url": "https://semaphore.example/", "api_token": "preflight-token"},
            "_bm_guard_values": {"url": "https://semaphore.example/", "api_token": "preflight-token"},
        },
        "tasks": [
            matching_guard,
            {
                "name": "Simulate token rotation after the successful endpoint preflight",
                "ansible.builtin.set_fact": {
                    "_bm_current": "{{ _bm_current | combine({'api_token': 'rotated-token'}) }}",
                },
                "no_log": True,
            },
            rotated_guard,
            {
                "name": "Require the matching control and changed-token refusal",
                "ansible.builtin.assert": {
                    "that": ["not _matching_guard.failed", "_rotated_guard.failed"],
                    "fail_msg": "The multi-key guard did not distinguish matching from rotated values.",
                    "quiet": True,
                },
            },
        ],
    }]
    path = tmp_path / "guard-race.yml"
    path.write_text(yaml.safe_dump(play))
    result = harness_sandbox.run(
        ["ansible-playbook", "-i", "localhost,", str(path)],
        tmp_path,
        cwd=REPO,
        env=harness_sandbox.env_for(tmp_path),
    )
    output = result.stdout + result.stderr
    if "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("host policy does not allow the sandbox-exec harness")
    if "Local RPC server did not start" in result.stderr:
        pytest.skip("installed Ansible local executor cannot start its RPC server in this environment")
    assert result.returncode == 0, output[-1200:]
    assert "preflight-token" not in output
    assert "rotated-token" not in output


def test_all_secret_or_endpoint_response_tasks_are_redacted():
    for task in _tasks():
        module = task.get("ansible.builtin.uri")
        if module and ("Authorization" in str(module.get("headers", {}))
                       or "X-Vault-Token" in str(module.get("headers", {}))
                       or "/auth/approle/login" in str(module.get("url", ""))):
            assert task["no_log"] is True, task["name"]

    messages = [task["ansible.builtin.debug"]["msg"] for task in _tasks()
                if "ansible.builtin.debug" in task]
    assert len(messages) == 1
    assert "{{ _reconcile_target_url }}" not in messages[0]
    assert "api_token" not in messages[0]
