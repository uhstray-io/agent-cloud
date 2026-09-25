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


@pytest.fixture(autouse=True)
def no_poll_delay(monkeypatch):
    # The fake answers a terminal status on the first poll; the real 5 s interval is dead time.
    monkeypatch.setattr(launcher.wait.__globals__["time"], "sleep", lambda _s: None)


class FakeAPI:
    project = 1

    def __init__(self, version="v2.17.31-02309ba-1774450250", records_params=True, busy=False,
                 readback_fails=False, submit_fails=False, readback=None):
        self.version, self.records_params, self.busy = version, records_params, busy
        self.readback_fails = readback_fails
        self.submit_fails = submit_fails
        self.readback = readback
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
            if self.submit_fails:
                raise launcher.Refusal("Semaphore request outcome unavailable on /tasks")
            self.created = body
            return {"id": 1200, "status": "waiting"}
        if path == "/tasks/1200":
            if self.readback_fails:
                raise launcher.Refusal("Semaphore HTTP 502 on /tasks/1200; body suppressed")
            if self.readback is not None:
                return self.readback()
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


@pytest.mark.parametrize("version", ["v2.17.30-abc", "v2.17.32", "v2.18.0", "v2.19.10", "v3.0.0", "unknown"])
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
    with pytest.raises(launcher.Refusal, match="did not record check mode.*stop requested"):
        launcher.launch(api, "Deploy agentgateway (Dev)", {}, dry_run=True)
    assert posts(api) == ["/tasks", "/tasks/1200/stop"]


def test_a_failed_read_back_reports_the_task_id_as_possibly_running(capsys):
    api = FakeAPI(readback_fails=True)
    with pytest.raises(launcher.Refusal, match="Task 1200: launched, but its check mode could not be read"):
        launcher.launch(api, "Deploy agentgateway (Dev)", {}, dry_run=True)
    assert "Task 1200 launched" in capsys.readouterr().out
    assert posts(api) == ["/tasks", "/tasks/1200/stop"]


def test_the_client_never_follows_a_redirect_with_the_token():
    # urllib copies Authorization to a redirect target; the handler must refuse to follow.
    handler = launcher.NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example/") is None
    api = launcher.API("https://semaphore.example", 1, "synthetic-token")
    assert any(isinstance(h, launcher.NoRedirect) for h in api.open.__self__.handlers)


def test_a_lost_submission_response_is_reported_as_uncertain_and_never_retried():
    api = FakeAPI(submit_fails=True)
    with pytest.raises(launcher.Refusal, match="outcome uncertain.*may be running"):
        launcher.launch(api, "Deploy agentgateway (Dev)", {}, dry_run=False)
    assert posts(api) == ["/tasks"]


def test_a_network_error_becomes_a_refusal_not_a_traceback(monkeypatch):
    api = launcher.API("https://semaphore.example", 1, "synthetic-token")

    def unreachable(*_args, **_kwargs):
        raise OSError("connection reset")
    monkeypatch.setattr(api, "open", unreachable)
    with pytest.raises(launcher.Refusal, match="outcome unavailable"):
        api("/templates")


@pytest.mark.parametrize("version", ["v2.17.31-02309ba-1774450250", "v2.18.12", "v2.19.11-ansible2.16.5"])
def test_the_source_verified_releases_may_launch_check_mode(version):
    api = FakeAPI(version=version)
    launcher.launch(api, "Deploy agentgateway (Dev)", {}, dry_run=True)
    assert api.created["params"] == {"dry_run": True, "diff": True}


@pytest.mark.parametrize("shape", [lambda: None, lambda: {"id": 1200, "params": "dry_run"},
                                   lambda: {"id": 1200, "params": ["dry_run"]}, list])
def test_an_unexpected_read_back_shape_still_requests_a_stop(shape):
    api = FakeAPI(readback=shape)
    with pytest.raises(launcher.Refusal, match="Task 1200: launched, but"):
        launcher.launch(api, "Deploy agentgateway (Dev)", {}, dry_run=True)
    assert posts(api) == ["/tasks", "/tasks/1200/stop"]


@pytest.mark.parametrize("url", ["https://user:pw@semaphore.example", "https://semaphore.example/?x=1",
                                 "https://semaphore.example/#f", "http://semaphore.example"])
def test_the_launcher_refuses_any_url_but_a_plain_https_origin(url):
    # One client with the seed CLIs: the launcher used to accept credentials in the URL.
    with pytest.raises(launcher.Refusal, match="plain HTTPS origin"):
        launcher.API(url, 1, "synthetic-token")


def test_the_launcher_needs_only_the_standard_library():
    # It shares the seed core's client; the core reads YAML only inside the seed helpers
    # (Codex review of PR #249).
    import subprocess
    import sys
    code = ("import sys; sys.modules['yaml'] = None; sys.argv = ['semaphore-launch.py', '--help']; "
            f"import runpy; runpy.run_path({str(SCRIPT)!r}, run_name='__main__')")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--template" in result.stdout
