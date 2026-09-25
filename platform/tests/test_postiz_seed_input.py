"""Offline trust-boundary and lifecycle checks; all credentials are synthetic."""

import copy
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
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


ENDPOINT = "https://bao.example:8200"


class FakeAPI:
    def __init__(self, status="success"):
        # A provisioned dedicated environment: the two AppRole inputs and the approved endpoint.
        self.env = {"id": 2, "project_id": 1, "name": "dedicated-seed",
                    "json": '{"openbao_addr":"https://bao.example:8200"}', "env": "{}",
                    "secrets": [{"id": 3, "name": "BAO_ROLE_ID", "type": "env"},
                                {"id": 4, "name": "BAO_SECRET_ID", "type": "env"}]}
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
    seed.stage_and_seed(api, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"}, ENDPOINT)
    # Only the staged input is removed; the environment's own AppRole inputs stay.
    assert api.env["secrets"] == [{"id": 3, "name": "BAO_ROLE_ID", "type": "env"},
                                  {"id": 4, "name": "BAO_SECRET_ID", "type": "env"}]
    assert "synthetic-value" not in capsys.readouterr().out
    assert all("synthetic-value" not in str(body) for path, body in api.calls if path == "/tasks")


def test_uncertain_submission_retains_inputs_and_never_retries():
    api = FakeAPI("uncertain")
    with pytest.raises(seed.Refusal, match="uncertain"):
        seed.stage_and_seed(api, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"}, ENDPOINT)
    assert len([path for path, body in api.calls if path == "/tasks"]) == 1
    assert any(s["name"] == "SEED_X_API_KEY" for s in api.env["secrets"])


def test_collision_refuses_before_any_write():
    api = FakeAPI()
    api.env["secrets"].append({"id": 5, "name": "SEED_DISCORD_CLIENT_ID", "type": "env"})
    with pytest.raises(seed.Refusal, match="reconciliation.*leftover inputs: SEED_DISCORD_CLIENT_ID"):
        seed.stage_and_seed(api, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"}, ENDPOINT)
    assert all(body is None for path, body in api.calls)


def test_shared_environment_refuses_before_any_write():
    api = FakeAPI()

    def shared(path, body=None):
        if path == "/templates":
            return [api.template, {"id": 999, "environment_id": 2}]
        return api(path, body)

    with pytest.raises(seed.Refusal, match="dedicated environment"):
        seed.stage_and_seed(shared, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"}, ENDPOINT)
    assert all(body is None for path, body in api.calls)


def test_omitted_empty_secret_metadata_refuses_before_any_write():
    api = FakeAPI()
    del api.env["secrets"]
    with pytest.raises(seed.Refusal, match="Provision both AppRole inputs"):
        seed.stage_and_seed(api, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"}, ENDPOINT)
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


@pytest.mark.parametrize("value", [None, "", "a$(printf unintended) b#'\"$HOME"])
@pytest.mark.parametrize("newline", ["", "\n"])
def test_provider_config_survives_actual_loader(tmp_path, value, newline):
    text = (seed.ROOT / "platform/services/postiz/deployment/templates/postiz.env.j2").read_text()
    env = Environment()
    env.filters["quote"] = shlex.quote
    fields = seed.provider_fields()
    lines = []
    for name in fields:
        line = next(line for line in text.splitlines() if line.startswith(name + "="))
        key = line.split("secrets.", 1)[1].split()[0]
        lines.append(env.from_string(line).render(secrets={} if value is None else {key: value}))
    config = tmp_path / "postiz.env"
    config.write_text("# synthetic configuration\n\n" + "\n".join(lines) + newline)
    compose = yaml.safe_load((seed.ROOT / "platform/services/postiz/deployment/compose.yml").read_text())
    command = compose["services"]["postiz"]["command"]
    assert command[:2] == ["sh", "-c"]
    loader, suffix = command[2].rsplit("nginx && pnpm run pm2", 1)
    assert not suffix.strip()
    child = "import json, os; print(json.dumps(dict(os.environ)))"
    script = loader.replace("$$", "$").replace("/config/postiz.env", shlex.quote(str(config)))
    script += shlex.join([sys.executable, "-c", child])
    result = subprocess.run([*command[:2], script], text=True, capture_output=True, check=True)
    actual = json.loads(result.stdout)
    assert {name: actual[name] for name in fields} == dict.fromkeys(fields, value or "")


def test_a_changed_openbao_endpoint_refuses_before_any_write():
    # Review of PR #205: the seed task logs in and writes at the environment's address, so
    # an address changed after provisioning must stop the seed before anything is staged.
    for configured in ('{"openbao_addr":"https://elsewhere.example:8200"}', "{}", "not json"):
        api = FakeAPI()
        api.env["json"] = configured
        with pytest.raises(seed.Refusal, match="endpoint"):
            seed.stage_and_seed(api, 1, 151, 2, {"SEED_X_API_KEY": "synthetic-value"}, ENDPOINT)
        assert not any(body for path, body in api.calls if path in ("/environment/2", "/tasks"))
