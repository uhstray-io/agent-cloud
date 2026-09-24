"""The API launcher: check mode is placed and gated BEFORE the task exists (MISTAKES 3.8)."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/semaphore-launch.py"
SPEC = importlib.util.spec_from_file_location("semaphore_launch", SCRIPT)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class FakeAPI:
    project = 1

    def __init__(self, version="v2.17.31-02309ba", records_params=True, busy=False):
        self.version, self.records_params, self.busy = version, records_params, busy
        self.template = {"id": 220, "name": "Deploy agentgateway (Dev)",
                         "survey_vars": [{"name": "service_branch", "type": "string"}]}
        self.calls, self.created = [], None

    def __call__(self, path, body=None, project_scoped=True):
        self.calls.append((path, copy.deepcopy(body)))
        if path == "/info" and not project_scoped:
            return {"version": self.version}
        if path == "/templates":
            return [{"id": 220, "name": self.template["name"]}, {"id": 5, "name": "Deploy n8n"}]
        if path == "/templates/220":
            return copy.deepcopy(self.template)
        if path == "/tasks/last":
            return [{"id": 9, "template_id": 220, "status": "running"}] if self.busy else []
        if path == "/tasks" and body:
            self.created = body
            return {"id": 1200, "status": "waiting"}
        if path == "/tasks/1200":
            params = (self.created or {}).get("params") if self.records_params else None
            return {"id": 1200, "status": "success", "params": params}
        if path == "/tasks/1200/stop":
            return None
        raise AssertionError(path)


def posts(api):
    return [path for path, body in api.calls if body is not None]


def test_check_mode_is_sent_inside_params_never_top_level():
    api = FakeAPI()
    launcher.launch(api, "Deploy agentgateway (Dev)", {"service_branch": "dev"}, dry_run=True)
    assert api.created["params"] == {"dry_run": True, "diff": True}
    assert "dry_run" not in api.created and "diff" not in api.created
    assert json.loads(api.created["environment"]) == {"service_branch": "dev"}


def test_a_real_run_carries_no_check_mode_flags():
    api = FakeAPI()
    launcher.launch(api, "Deploy agentgateway (Dev)", {"service_branch": "dev"}, dry_run=False)
    assert "params" not in api.created and "dry_run" not in api.created


@pytest.mark.parametrize("version", ["v2.18.0", "v3.0.0", "unknown"])
def test_check_mode_on_an_unverified_server_is_refused_before_any_task(version):
    api = FakeAPI(version=version)
    with pytest.raises(launcher.Refusal, match="unverified"):
        launcher.launch(api, "Deploy agentgateway (Dev)", {}, dry_run=True)
    assert posts(api) == []


def test_undeclared_survey_values_are_refused_before_any_task():
    api = FakeAPI()
    with pytest.raises(launcher.Refusal, match="Not survey fields"):
        launcher.launch(api, "Deploy agentgateway (Dev)", {"agw_ui_enabled": "true"}, dry_run=True)
    assert posts(api) == []


def test_a_running_task_on_the_template_refuses_a_second():
    api = FakeAPI(busy=True)
    with pytest.raises(launcher.Refusal, match="already has a running task"):
        launcher.launch(api, "Deploy agentgateway (Dev)", {}, dry_run=False)
    assert posts(api) == []


def test_tripwire_stops_a_task_whose_check_mode_the_server_did_not_record():
    api = FakeAPI(records_params=False)
    with pytest.raises(launcher.Refusal, match="did not record check mode"):
        launcher.launch(api, "Deploy agentgateway (Dev)", {}, dry_run=True)
    assert posts(api) == ["/tasks", "/tasks/1200/stop"]
