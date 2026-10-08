"""The seed capability rule (filter seed_access_missing), tested directly.

test_postiz_access_only.py keeps one pass and one refusal per seed playbook to prove each
wires the rule in; the variants live here, as function calls instead of playbook runs.
"""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "seed_access", Path(__file__).resolve().parents[1] / "playbooks/filter_plugins/seed_access.py")
rule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rule)


@pytest.mark.parametrize("exists,capabilities", [
    (False, ["read", "create"]), (True, ["read", "patch"]),
    (False, ["root"]), (True, ["root"]),
    (False, ["read", "create", "update", "patch", "delete"]),
])
def test_the_capability_the_seed_uses_is_enough(exists, capabilities):
    assert rule.seed_access_missing(capabilities, exists) == ""


@pytest.mark.parametrize("exists,capabilities,missing", [
    (False, ["deny"], "read and create"), (False, [], "read and create"), (False, None, "read and create"),
    (False, ["read"], "create"),
    (False, ["read", "update"], "create"), (False, ["read", "patch"], "create"),  # a new path is POSTed
    (True, ["read", "create"], "patch"), (True, ["read", "update"], "patch"),     # an existing one is PATCHed
    (False, ["create"], "read"),                                                   # the seed reads first
])
def test_a_token_the_real_seed_would_be_denied_is_named(exists, capabilities, missing):
    assert rule.seed_access_missing(capabilities, exists) == missing
