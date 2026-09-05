"""Request A security boundary, exercised without a network connection."""

import copy
import io
import json
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "semaphore" / "inspect-discovery-metadata.py"
MODULE = runpy.run_path(str(SCRIPT))
INSPECT = MODULE["inspect"]
ENV = {"SEMAPHORE_URL": "https://semaphore.example.com", "SEMAPHORE_TOKEN": "synthetic-secret",
       "SEMAPHORE_PROJECT_ID": "7"}
RECORDS = {
    "templates": [{"id": 11, "name": "Check Discovery Pipeline", "repository_id": 12,
                   "inventory_id": 13, "environment_id": 14, "playbook": "platform/playbooks/check-discovery.yml"}],
    "repositories": [{"id": 12, "git_branch": "dev", "ssh_key_id": 15, "name": "synthetic-secret"}],
    "inventory": [{"id": 13, "type": "static-yaml", "ssh_key_id": 16,
                   "inventory": "all:\n  children:\n    netbox_svc:\n      vars:\n        secret: synthetic-secret"}],
    "keys": [{"id": 15, "type": "none"}, {"id": 16, "type": "ssh", "secret": "synthetic-secret"}],
}


class Client:
    def __init__(self, records):
        self.records = records
        self.requests = []

    def open(self, request, timeout):
        assert timeout == 10
        assert request.get_method() == "GET"
        assert request.data is None
        assert request.get_header("Authorization") == "Bearer synthetic-secret"
        self.requests.append(request.full_url)
        response = io.BytesIO(json.dumps(self.records[request.full_url.rsplit("/", 1)[1]]).encode())
        response.status = 200
        return response


def test_only_allowlisted_reads_and_redacted_evidence():
    client = Client(RECORDS)
    with patch.dict(INSPECT.__globals__, build_opener=lambda *args: client):
        result = INSPECT(ENV)
    assert client.requests == [f"https://semaphore.example.com/api/project/7/{name}" for name in RECORDS]
    assert result["netbox_group_present"] is True
    assert result["repository_branch"] == "dev"
    assert result["key_references"]["inventory"] == {"id": 16, "type": "ssh"}
    assert result["executed_revision"] == result["key_provenance"] == "unverified"
    assert "synthetic-secret" not in json.dumps(result)
    assert "example.com" not in json.dumps(result)


@pytest.mark.parametrize("change", [
    {"SEMAPHORE_TOKEN": ""}, {"SEMAPHORE_PROJECT_ID": ""}, {"SEMAPHORE_PROJECT_ID": "../1"},
    {"SEMAPHORE_URL": "http://semaphore.example.com"},
    {"SEMAPHORE_URL": "https://" + "user:" + ENV["SEMAPHORE_TOKEN"] + "@semaphore.example.com"},
    {"SEMAPHORE_URL": "https://semaphore.example.com/?token=synthetic-secret"},
    {"SEMAPHORE_TOKEN": "bad\nheader"},
])
def test_invalid_inputs_refuse_before_network(change):
    with (
        patch.dict(INSPECT.__globals__, build_opener=lambda *args: pytest.fail("network attempted")),
        pytest.raises(MODULE["Refusal"]),
    ):
        INSPECT(ENV | change)


def test_duplicate_template_and_absent_target_do_not_prove_access():
    records = copy.deepcopy(RECORDS)
    records["templates"] *= 2
    with (
        patch.dict(INSPECT.__globals__, build_opener=lambda *args: Client(records)),
        pytest.raises(MODULE["Refusal"], match="missing_or_ambiguous_record"),
    ):
        INSPECT(ENV)
    records = copy.deepcopy(RECORDS)
    records["inventory"][0]["inventory"] = "other_svc: {}"
    with patch.dict(INSPECT.__globals__, build_opener=lambda *args: Client(records)):
        assert INSPECT(ENV)["netbox_group_present"] is False


def test_redirects_and_exception_payloads_never_escape(capsys):
    with pytest.raises(MODULE["Refusal"], match="redirect_refused"):
        MODULE["NoRedirect"]().redirect_request(None, None, 302, "", {}, "https://elsewhere.example.com")
    main = MODULE["main"]

    def fail(_env):
        raise ValueError("synthetic-secret server payload")

    with patch.dict(main.__globals__, inspect=fail), patch("sys.argv", [str(SCRIPT)]):
        assert main() == 1
    assert json.loads(capsys.readouterr().out) == {"status": "blocked", "reason": "metadata_read_failed"}


@pytest.mark.parametrize("body,reason", [
    (b"synthetic-secret", "metadata_read_failed"),
    (b"x" * (MODULE["LIMIT"] + 1), "response_too_large"),
    (b'{"secret":"synthetic-secret"}', "invalid_record_list"),
])
def test_bad_responses_are_bounded_and_redacted(body, reason, capsys):
    class BadResponseClient:
        def open(self, request, timeout):
            response = io.BytesIO(body)
            response.status = 200
            return response

    main = MODULE["main"]
    with (
        patch.dict(INSPECT.__globals__, build_opener=lambda *args: BadResponseClient()),
        patch.dict("os.environ", ENV, clear=True),
        patch("sys.argv", [str(SCRIPT)]),
    ):
        assert main() == 1
    assert json.loads(capsys.readouterr().out) == {"status": "blocked", "reason": reason}
