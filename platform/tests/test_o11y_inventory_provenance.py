"""Strict source-binding and disclosure tests for the o11y inventory survey."""

import copy
import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "platform/playbooks/files/survey-o11y-inventory-provenance.py"
PLAYBOOK = ROOT / "platform/playbooks/survey-o11y-inventory-provenance.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
SPEC = importlib.util.spec_from_file_location("o11y_inventory_provenance", HELPER)
SURVEY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SURVEY)

PUBLIC_SHA = "a" * 40
PRIVATE_SHA = "b" * 40
PRIVATE_INVENTORY = "all:\n  children:\n    o11y_svc:\n      hosts:\n        receiver: {}\n"


def payload():
    return {
        "public_sha": PUBLIC_SHA,
        "private_sha": PRIVATE_SHA,
        "private_tip_sha": PRIVATE_SHA,
        "private_tree_entry": "100644 blob " + "c" * 40 + "\tinventory/production.yml",
        "project_id": 7,
        "repository_id": 12,
        "semaphore_statuses": [200, 200, 200],
        "inventories": [{
            "id": 31, "project_id": 7, "name": "production",
            "type": "static-yaml", "inventory": PRIVATE_INVENTORY,
        }],
        "templates": [{
            "id": 44, "project_id": 7, "name": SURVEY.EXPECTED_TEMPLATE,
            "inventory_id": 31, "repository_id": 12,
            "playbook": "platform/playbooks/survey-o11y-inventory-provenance.yml",
        }],
        "repositories": [{
            "id": 12, "project_id": 7, "name": SURVEY.EXPECTED_REPOSITORY,
            "git_url": SURVEY.EXPECTED_REPOSITORY_URL, "git_branch": "dev",
        }],
        "private_inventory": PRIVATE_INVENTORY,
    }


def test_exact_match_returns_only_safe_commit_receipt():
    report = SURVEY.evaluate(payload())

    assert report == {
        "schema_version": 1,
        "status": "observed",
        "reason": "exact_bytes_match",
        "public_sha": PUBLIC_SHA,
        "private_sha": PRIVATE_SHA,
    }
    encoded = json.dumps(report)
    for private_value in ("receiver", "static-yaml", "production", "http", "31", "44"):
        assert private_value not in encoded


def test_inventory_byte_difference_is_not_promoted_to_match():
    candidate = payload()
    candidate["private_inventory"] += "# reviewed change\n"

    report = SURVEY.evaluate(candidate)

    assert report["status"] == "mismatch"
    assert report["reason"] == "inventory_bytes_differ"


def test_public_receipt_keeps_the_exact_fixed_schema():
    report = SURVEY.evaluate(payload())

    assert set(report) == {"schema_version", "status", "reason", "public_sha", "private_sha"}


def test_stale_private_branch_tip_refuses_comparison():
    candidate = payload()
    candidate["private_tip_sha"] = "c" * 40

    report = SURVEY.evaluate(candidate)

    assert report["status"] == "unavailable"
    assert report["reason"] == "branch_tip_mismatch"


def test_duplicate_or_malformed_yaml_is_not_compared():
    for source in ("all: {}\nall: {}\n", "all: [unterminated\n"):
        candidate = payload()
        candidate["private_inventory"] = source

        report = SURVEY.evaluate(candidate)

        assert report["status"] == "unavailable"
        assert report["reason"] == "malformed_yaml"


def test_unknown_or_ambiguous_inventory_source_refuses():
    unknown = payload()
    unknown["inventories"][0]["type"] = "script"
    ambiguous = payload()
    ambiguous["inventories"].append(copy.deepcopy(ambiguous["inventories"][0]))

    assert SURVEY.evaluate(unknown)["reason"] == "inventory_source_unknown"
    assert SURVEY.evaluate(ambiguous)["reason"] == "identity_ambiguous"

    symlink = payload()
    symlink["private_tree_entry"] = "120000 blob " + "c" * 40 + "\tinventory/production.yml"
    assert SURVEY.evaluate(symlink)["reason"] == "inventory_source_unknown"


def test_template_must_bind_the_named_inventory_and_dev_repository():
    candidate = payload()
    candidate["templates"][0]["inventory_id"] = 99

    report = SURVEY.evaluate(candidate)

    assert report["status"] == "unavailable"
    assert report["reason"] == "identity_ambiguous"


def test_api_failure_and_unexpected_repository_are_unavailable():
    api_failure = payload()
    api_failure["repositories"] = None
    wrong_repository = payload()
    wrong_repository["repositories"][0]["git_url"] = "https://private.invalid/repo.git"

    assert SURVEY.evaluate(api_failure)["reason"] == "semaphore_api_error"
    assert SURVEY.evaluate(wrong_repository)["reason"] == "identity_ambiguous"


def test_non_200_semaphore_response_is_unavailable_even_with_valid_rows():
    candidate = payload()
    candidate["semaphore_statuses"] = [200, 503, 200]

    report = SURVEY.evaluate(candidate)

    assert report["status"] == "unavailable"
    assert report["reason"] == "semaphore_api_error"


def test_missing_or_malformed_semaphore_statuses_are_unavailable():
    candidates = []
    missing = payload()
    missing.pop("semaphore_statuses")
    candidates.append(missing)
    candidates.extend(
        {**payload(), "semaphore_statuses": statuses}
        for statuses in (None, [200, 200], [200, "200", 200], [200, True, 200])
    )

    for candidate in candidates:
        report = SURVEY.evaluate(candidate)
        assert report["status"] == "unavailable"
        assert report["reason"] == "semaphore_api_error"


def test_playbook_uses_only_get_api_reads_and_cleans_private_checkout():
    plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    assert plays[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"
    assert plays[1]["ansible.builtin.import_playbook"] == "require-reviewed-checkout.yml"
    body = plays[2]
    tasks = body["tasks"]
    uri_tasks = [task for task in tasks if "ansible.builtin.uri" in task]
    assert len(uri_tasks) == 5
    assert sum(task["ansible.builtin.uri"].get("method", "GET") == "POST" for task in uri_tasks) == 1
    api_tasks = [task for task in uri_tasks if task.get("register") in {
        "_survey_inventories", "_survey_templates", "_survey_repositories",
    }]
    assert len(api_tasks) == 3
    assert all(task["ansible.builtin.uri"].get("method", "GET") == "GET" for task in api_tasks)
    assert all(task.get("no_log") is True for task in api_tasks)
    compare = next(task for task in tasks if "block" in task and "always" in task)
    comparator = next(
        item for item in compare["block"]
        if item.get("name") == "Run the strict fixed-schema inventory comparison"
    )
    comparator_input = comparator["ansible.builtin.command"]["stdin"]
    assert "'semaphore_statuses':" in comparator_input
    assert all(f"_survey_{name}.status | default(none)" in comparator_input
               for name in ("inventories", "templates", "repositories"))
    assert any("site-config-clone.yml" in str(item) for item in compare["block"])
    assert any("ls-remote" in str(item) for item in compare["block"])
    assert any("ls-tree" in str(item) for item in compare["block"])
    assert any("inventory/production.yml" in str(item) for item in compare["block"])
    assert "git push" not in PLAYBOOK.read_text(encoding="utf-8")
    assert all("api/project" in task["ansible.builtin.uri"]["url"] for task in api_tasks)
    assert compare["always"][0]["ansible.builtin.file"]["state"] == "absent"


def test_check_mode_is_refused_before_credentials_or_private_clone():
    plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    tasks = plays[2]["tasks"]
    first_validation = tasks[0]

    assert first_validation["name"] == "Validate exact public and private revisions"
    assert "not ansible_check_mode" in first_validation["ansible.builtin.assert"]["that"]
    credential_or_clone_tasks = [
        index for index, task in enumerate(tasks)
        if task.get("name") in {
            "Authenticate to OpenBao",
            "Read Semaphore API credentials and site-config deploy key from OpenBao",
            "Compare the exact reviewed private integration snapshot inside a cleanup block",
        }
    ]
    assert credential_or_clone_tasks
    assert all(index > 0 for index in credential_or_clone_tasks)


def test_template_requires_exact_public_and_private_revisions_and_is_dev_bound():
    document = yaml.safe_load(TEMPLATES.read_text(encoding="utf-8"))
    template = next(item for item in document["templates"] if item["name"] == SURVEY.EXPECTED_TEMPLATE)

    assert template["playbook"] == "platform/playbooks/survey-o11y-inventory-provenance.yml"
    assert template["repository"] == "agent-cloud dev"
    assert {item["name"] for item in template["survey_vars"]} == {
        "expected_repository_sha", "site_config_base_branch", "site_config_base_sha",
    }
