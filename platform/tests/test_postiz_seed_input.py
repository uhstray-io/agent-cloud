"""Offline trust-boundary and lifecycle checks; all credentials are synthetic."""

import copy
import importlib.util
import shlex
import subprocess
from pathlib import Path

import pytest
from jinja2 import Environment

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/postiz-seed-input.py"
SPEC = importlib.util.spec_from_file_location("postiz_seed_input", SCRIPT)
seed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed)


def test_parser_is_literal_and_allowlisted():
    fields = {"X_API_KEY": "SEED_X_API_KEY", "X_API_SECRET": "SEED_X_API_SECRET"}
    text = "export X_API_KEY='a$HOME$(exit 8)#b' # comment\nX_API_SECRET=quoted-value\nJWT_SECRET=ignored"
    assert seed.parse_inputs(text, fields) == {
        "SEED_X_API_KEY": "a$HOME$(exit 8)#b", "SEED_X_API_SECRET": "quoted-value",
    }
    for bad in ["X_API_KEY=x", "X_API_KEY=x\nX_API_KEY=y", "X_API_KEY='unfinished"]:
        with pytest.raises(seed.Refusal):
            seed.parse_inputs(bad, fields)


class FakeAPI:
    def __init__(self, status="success"):
        self.env = {"id": 2, "project_id": 1, "name": "dedicated-seed", "json": '{"keep":"unchanged"}',
                    "env": '{"keep":"unchanged"}', "secrets": [{"id": 4, "name": "KEEP", "type": "env"}]}
        self.template = {"id": 151, "name": "Seed Postiz Secrets", "playbook": seed.SEED_PLAYBOOK,
                         "environment_id": 2, "app": "ansible"}
        self.calls = []
        self.status = status

    def __call__(self, path, body=None):
        self.calls.append((path, copy.deepcopy(body)))
        if path == "/templates/151":
            return copy.deepcopy(self.template)
        if path == "/templates":
            return [copy.deepcopy(self.template)]
        if path == "/tasks/last":
            return []
        if path == "/environment/2":
            if body:
                assert body["json"] == self.env["json"]
                assert body["env"] == self.env["env"]
                for op in body["secrets"]:
                    if op["operation"] == "create":
                        self.env["secrets"].append({"id": 10, "name": op["name"], "type": "env"})
                    else:
                        self.env["secrets"] = [s for s in self.env["secrets"] if s["id"] != op["id"]]
            return copy.deepcopy(self.env)
        if path == "/tasks":
            assert "environment" not in body
            if self.status == "uncertain":
                raise seed.Refusal("uncertain submission")
            return {"id": 440, "status": self.status}
        raise AssertionError(path)


def test_seed_cleans_only_created_inputs(capsys):
    api = FakeAPI()
    seed.stage_and_seed(api, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"})
    assert api.env["secrets"] == [{"id": 4, "name": "KEEP", "type": "env"}]
    assert "synthetic-value" not in capsys.readouterr().out
    assert all("synthetic-value" not in str(body) for path, body in api.calls if path == "/tasks")


def test_uncertain_submission_retains_inputs_and_never_retries():
    api = FakeAPI("uncertain")
    with pytest.raises(seed.Refusal, match="uncertain"):
        seed.stage_and_seed(api, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"})
    assert len([path for path, body in api.calls if path == "/tasks"]) == 1
    assert any(s["name"] == "SEED_X_API_KEY" for s in api.env["secrets"])


def test_collision_refuses_before_any_write():
    api = FakeAPI()
    api.env["secrets"].append({"id": 5, "name": "SEED_DISCORD_CLIENT_ID", "type": "env"})
    with pytest.raises(seed.Refusal, match="Existing encrypted"):
        seed.stage_and_seed(api, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"})
    assert all(body is None for path, body in api.calls)


def test_shared_environment_refuses_before_any_write():
    api = FakeAPI()

    def shared(path, body=None):
        if path == "/templates":
            return [api.template, {"id": 999, "environment_id": 2}]
        return api(path, body)

    with pytest.raises(seed.Refusal, match="dedicated environment"):
        seed.stage_and_seed(shared, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"})
    assert all(body is None for path, body in api.calls)


def test_missing_secret_metadata_refuses_before_any_write():
    api = FakeAPI()
    del api.env["secrets"]
    with pytest.raises(seed.Refusal, match="no secrets array"):
        seed.stage_and_seed(api, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"})
    assert all(body is None for path, body in api.calls)


def test_empty_token_refused():
    with pytest.raises(seed.Refusal, match="token"):
        seed.API("https://semaphore.example.test", 1, "")


@pytest.mark.parametrize("url", ["http://example.test", "https://user@example.test", "https://example.test/path"])
def test_endpoint_refusal(url):
    with pytest.raises(seed.Refusal):
        seed.API(url, 1, "synthetic-token")


def test_declaration_excludes_stateful_fields():
    fields = seed.provider_fields()
    assert fields["X_API_KEY"] == "SEED_X_API_KEY"
    assert "JWT_SECRET" not in fields
    assert "POSTIZ_OAUTH_CLIENT_SECRET" not in fields


@pytest.mark.parametrize("value", ["", "a$(printf unintended) b#'\"$HOME"])
def test_provider_config_survives_shell_loading_literally(value):
    text = (seed.ROOT / "platform/services/postiz/deployment/templates/postiz.env.j2").read_text()
    env = Environment()
    env.filters["quote"] = shlex.quote
    for name in seed.provider_fields():
        line = next(line for line in text.splitlines() if line.startswith(name + "="))
        key = line.split("secrets.", 1)[1].split()[0]
        rendered = env.from_string(line).render(secrets={key: value})
        result = subprocess.run(["bash"], input=rendered + f'\nprintf "%s" "${name}"\n',
                                text=True, capture_output=True, check=True)
        assert result.stdout == value
