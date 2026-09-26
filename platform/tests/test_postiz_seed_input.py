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
PLAYBOOK = seed.declaration(seed.TEMPLATE)["playbook"]
import semaphore_seed as core  # noqa: E402  (on sys.path once the CLI is loaded)


def test_parser_is_literal_and_allowlisted():
    fields = {"X_API_KEY": "SEED_X_API_KEY", "X_API_SECRET": "SEED_X_API_SECRET"}
    text = "export X_API_KEY='a$HOME$(exit 8)#b' # comment\nX_API_SECRET=quoted-value\nJWT_SECRET=ignored"
    assert seed.parse_inputs(text, fields) == {
        "SEED_X_API_KEY": "a$HOME$(exit 8)#b", "SEED_X_API_SECRET": "quoted-value",
    }
    for bad in ["X_API_KEY=x", "X_API_KEY=x\nX_API_KEY=y", "X_API_KEY='unfinished"]:
        with pytest.raises(seed.Refusal):
            seed.parse_inputs(bad, fields)


def stage(api, values):
    """The shared lifecycle core, called the way postiz-seed-input.py's --apply calls it."""
    target = core.Target(151, 2, "Seed Postiz Secrets", "dedicated-seed", PLAYBOOK, 5, 2)
    return seed.stage_and_seed(api, 1, target, values)


class FakeAPI:
    def __init__(self, status="success"):
        # A provisioned dedicated environment: the two AppRole inputs and no extra vars.
        self.env = {"id": 2, "project_id": 1, "name": "dedicated-seed",
                    "json": "{}", "env": "{}",
                    "secrets": [{"id": 3, "name": "BAO_ROLE_ID", "type": "env"},
                                {"id": 4, "name": "BAO_SECRET_ID", "type": "env"}]}
        self.template = {"id": 151, "name": "Seed Postiz Secrets", "playbook": PLAYBOOK,
                         "environment_id": 2, "app": "ansible", "repository_id": 5, "inventory_id": 2}
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
    stage(api, {"SEED_X_API_KEY": "synthetic-value"})
    # Only the staged input is removed; the environment's own AppRole inputs stay.
    assert api.env["secrets"] == [{"id": 3, "name": "BAO_ROLE_ID", "type": "env"},
                                  {"id": 4, "name": "BAO_SECRET_ID", "type": "env"}]
    assert "synthetic-value" not in capsys.readouterr().out
    assert all("synthetic-value" not in str(body) for path, body in api.calls if path == "/tasks")


def test_uncertain_submission_retains_inputs_and_never_retries():
    api = FakeAPI("uncertain")
    with pytest.raises(seed.Refusal, match="uncertain"):
        stage(api, {"SEED_X_API_KEY": "synthetic-value"})
    assert len([path for path, body in api.calls if path == "/tasks"]) == 1
    assert any(s["name"] == "SEED_X_API_KEY" for s in api.env["secrets"])


def test_collision_refuses_before_any_write():
    api = FakeAPI()
    api.env["secrets"].append({"id": 5, "name": "SEED_DISCORD_CLIENT_ID", "type": "env"})
    with pytest.raises(seed.Refusal, match="reconciliation.*leftover inputs: SEED_DISCORD_CLIENT_ID"):
        stage(api, {"SEED_X_API_KEY": "synthetic-value"})
    assert all(body is None for path, body in api.calls)


def test_shared_environment_refuses_before_any_write():
    api = FakeAPI()

    def shared(path, body=None):
        if path == "/templates":
            return [api.template, {"id": 999, "environment_id": 2}]
        return api(path, body)

    with pytest.raises(seed.Refusal, match="dedicated environment"):
        stage(shared, {"SEED_X_API_KEY": "synthetic-value"})
    assert all(body is None for path, body in api.calls)


def test_omitted_empty_secret_metadata_refuses_before_any_write():
    api = FakeAPI()
    del api.env["secrets"]
    with pytest.raises(seed.Refusal, match="Provision both AppRole inputs"):
        stage(api, {"SEED_X_API_KEY": "synthetic-value"})
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


def test_an_address_pin_or_extra_var_refuses_before_any_write():
    # The seed task takes its OpenBao address from the inventory; any extra var in the
    # environment (an address pin included) would override it, so staging stops first.
    for configured in ('{"openbao_addr":"https://elsewhere.example:8200"}', '{"other":1}', "not json"):
        api = FakeAPI()
        api.env["json"] = configured
        with pytest.raises(seed.Refusal, match="reconciliation"):
            stage(api, {"SEED_X_API_KEY": "synthetic-value"})
        assert not any(body for path, body in api.calls if path in ("/environment/2", "/tasks"))


class NamedAPI(FakeAPI):
    """The dev variant as --apply finds it: by name, with its repository and inventory."""

    def __init__(self, repository_id=5, inventory_id=2):
        super().__init__()
        self.template.update(name="Seed Postiz Secrets (Dev)", repository_id=repository_id,
                             inventory_id=inventory_id, arguments=None)
        self.env["name"] = "Postiz seed inputs (Dev)"
        declared = yaml.safe_load((seed.ROOT / "platform/semaphore/repositories.yml").read_text())["repositories"]
        self.repos = [dict(r, id=5 if r["name"] == "agent-cloud dev" else 4) for r in declared]

    def __call__(self, path, body=None):
        if path == "/environment":
            self.calls.append((path, None))
            return [copy.deepcopy(self.env)]
        if path == "/repositories":
            self.calls.append((path, None))
            return copy.deepcopy(self.repos)
        return super().__call__(path, body)


def run_apply(monkeypatch, tmp_path, api):
    env_file = tmp_path / "providers.env"
    field = next(iter(seed.provider_fields()))
    group = [f for f in seed.provider_fields() if f.split("_")[0] == field.split("_")[0]]
    env_file.write_text("".join(f"{f}=synthetic-{i}\n" for i, f in enumerate(group)))
    monkeypatch.setattr(seed, "API", lambda url, project, token: api)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("synthetic-token"))
    monkeypatch.setattr("sys.argv", ["postiz-seed-input.py", "--env-file", str(env_file), "--apply",
                                     "--inventory", "2",
                                     "--url", "https://semaphore.example"])
    return seed.main()


def test_apply_finds_the_template_by_name_and_seeds_once(monkeypatch, tmp_path):
    api = NamedAPI()
    assert run_apply(monkeypatch, tmp_path, api) == 0
    assert [p for p, b in api.calls if b is not None and p == "/tasks"] == ["/tasks"]


@pytest.mark.parametrize("rebound", [{"repository_id": 4}, {"inventory_id": 3}])
def test_apply_refuses_a_rebound_template_before_any_write(monkeypatch, tmp_path, capsys, rebound):
    # The Postiz CLI used to take raw ids and pin neither (security review, 2026-09-25).
    api = NamedAPI(**rebound)
    assert run_apply(monkeypatch, tmp_path, api) == 1
    assert "approved binding" in capsys.readouterr().err
    assert not any(b for _, b in api.calls)
