"""Generic isolated-environment seeding: allowlists, binding checks, lifecycle.

All values are synthetic. The shared lifecycle core is also covered through the
Postiz CLI in test_postiz_seed_input.py; these tests cover what the generic CLI adds.
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/semaphore-seed-input.py"
SPEC = importlib.util.spec_from_file_location("semaphore_seed_input", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)

SECRET = "synthetic-value-never-printed"
ENDPOINT = "https://bao.example:8200"


def test_catalog_declares_the_openbao_seed_as_isolated():
    decl = cli.declaration("Seed OpenBao Key")
    assert decl["seed_inputs"] == ["BAO_VALUE"]
    assert decl["isolated_environment"] == "OpenBao key seed inputs"
    assert decl["seed_access_check"] in {v["name"] for v in decl["survey_vars"]}
    assert cli.resolve_names(decl, "dev") == ("Seed OpenBao Key (Dev)", "OpenBao key seed inputs (Dev)")


def test_a_template_without_isolation_is_refused():
    with pytest.raises(cli.Refusal):
        cli.declaration("Deploy agentgateway")


def test_inputs_are_allowlisted_single_lines(tmp_path):
    good = tmp_path / "v"
    good.write_text(SECRET + "\n")
    assert cli.read_inputs([f"BAO_VALUE={good}"], {"BAO_VALUE"}) == {"BAO_VALUE": SECRET}
    two = tmp_path / "two"
    two.write_text("a\nb\n")
    empty = tmp_path / "empty"
    empty.write_text("")
    for bad in [f"OTHER={good}", f"BAO_VALUE={two}", f"BAO_VALUE={empty}", "BAO_VALUE", f"BAO_VALUE={good}"]:
        pairs = [bad, bad] if bad == f"BAO_VALUE={good}" else [bad]
        with pytest.raises(cli.Refusal):
            cli.read_inputs(pairs, {"BAO_VALUE"})


def test_settings_exclude_the_access_switch_and_require_declared_fields():
    decl = cli.declaration("Seed OpenBao Key")
    assert cli.read_settings(["bao_path=services/x", "bao_key=k"], decl) == {"bao_path": "services/x", "bao_key": "k"}
    for bad in (["bao_path=services/x"], ["bao_path=a", "bao_key=b", "bao_verify_access_only=true"],
                ["bao_path=a", "bao_key=b", "unknown=1"]):
        with pytest.raises(cli.Refusal):
            cli.read_settings(bad, decl)


class FakeAPI:
    """Semaphore with one seed template bound to its isolated environment."""

    def __init__(self, bound_env=9, status="success"):
        self.template = {"id": 301, "name": "Seed OpenBao Key (Dev)",
                         "playbook": "platform/playbooks/seed-openbao-key.yml",
                         "environment_id": bound_env, "app": "ansible", "arguments": None,
                         "repository_id": 5, "inventory_id": 2}
        self.env = {"id": 9, "project_id": 1, "name": "OpenBao key seed inputs (Dev)", "json": '{"openbao_addr":"https://bao.example:8200"}',
                    "env": "{}", "secrets": [{"id": 1, "name": "BAO_ROLE_ID", "type": "env"},
                                             {"id": 2, "name": "BAO_SECRET_ID", "type": "env"}]}
        self.shared = {"id": 2, "project_id": 1, "name": "local-dev"}
        self.repos = [{"id": 4, "name": "agent-cloud", "git_url": "https://github.com/uhstray-io/agent-cloud.git",
                       "git_branch": "main"},
                      {"id": 5, "name": "agent-cloud dev", "git_url": "https://github.com/uhstray-io/agent-cloud.git",
                       "git_branch": "dev"}]
        self.status = status
        self.calls = []

    def __call__(self, path, body=None):
        self.calls.append((path, copy.deepcopy(body)))
        if path == "/templates":
            return [copy.deepcopy(self.template), {"id": 5, "name": "Deploy n8n", "environment_id": 2}]
        if path == "/templates/301":
            return copy.deepcopy(self.template)
        if path == "/environment":
            return [copy.deepcopy(self.env), copy.deepcopy(self.shared)]
        if path == "/repositories":
            return copy.deepcopy(self.repos)
        if path == "/environment/9":
            if body:
                for op in body["secrets"]:
                    if op["operation"] == "create":
                        self.env["secrets"].append({"id": 50, "name": op["name"], "type": "env"})
                    else:
                        self.env["secrets"] = [s for s in self.env["secrets"] if s["id"] != op["id"]]
            return copy.deepcopy(self.env)
        if path == "/tasks/last":
            return []
        if path == "/tasks":
            return {"id": 900, "status": self.status}
        if path == "/tasks/900":
            return {"id": 900, "status": self.status}
        raise AssertionError(path)


def test_locate_requires_the_isolated_binding():
    assert cli.locate(FakeAPI(), "Seed OpenBao Key (Dev)", "OpenBao key seed inputs (Dev)",
                      "platform/playbooks/seed-openbao-key.yml") == (301, 9)
    with pytest.raises(cli.Refusal, match="Provision Seed Environment"):
        cli.locate(FakeAPI(bound_env=2), "Seed OpenBao Key (Dev)", "OpenBao key seed inputs (Dev)",
                   "platform/playbooks/seed-openbao-key.yml")


def test_seed_stages_runs_once_with_settings_and_removes_only_its_input(capsys):
    api = FakeAPI()
    cli.stage_and_seed(api, 1, 301, 9, {"BAO_VALUE": SECRET}, playbook="platform/playbooks/seed-openbao-key.yml",
                       endpoint=ENDPOINT, template_names={"Seed OpenBao Key (Dev)"},
                       extra={"bao_path": "services/x", "bao_key": "k"})
    submissions = [body for path, body in api.calls if path == "/tasks" and body]
    assert len(submissions) == 1
    assert json.loads(submissions[0]["environment"]) == {"bao_path": "services/x", "bao_key": "k"}
    assert SECRET not in submissions[0]["environment"]
    assert [s["name"] for s in api.env["secrets"]] == ["BAO_ROLE_ID", "BAO_SECRET_ID"]
    assert SECRET not in capsys.readouterr().out


def test_seed_refuses_a_leftover_staged_input_before_any_write():
    api = FakeAPI()
    api.env["secrets"].append({"id": 7, "name": "BAO_VALUE", "type": "env"})
    with pytest.raises(cli.Refusal):
        cli.stage_and_seed(api, 1, 301, 9, {"BAO_VALUE": SECRET}, playbook="platform/playbooks/seed-openbao-key.yml",
                           endpoint=ENDPOINT, template_names={"Seed OpenBao Key (Dev)"})
    assert not any(body for path, body in api.calls if path in ("/environment/9", "/tasks"))


def test_verify_only_stages_nothing_and_sets_only_the_access_switch():
    api = FakeAPI()
    cli.verify_access(api, 1, 301, "bao_verify_access_only", {"bao_path": "services/x", "bao_key": "k"},
                      check=lambda: None)
    writes = [(path, body) for path, body in api.calls if body]
    assert [path for path, _ in writes] == ["/tasks"]
    assert json.loads(writes[0][1]["environment"])["bao_verify_access_only"] == "true"
    with pytest.raises(cli.Refusal):
        cli.verify_access(FakeAPI(status="error"), 1, 301, "bao_verify_access_only", {}, check=lambda: None)


# ── the CLI end to end: every mode runs the same read-only preflight first ──────

def run_cli(monkeypatch, tmp_path, api, *mode):
    value = tmp_path / "value"
    value.write_text(SECRET + "\n")
    monkeypatch.setattr(cli, "API", lambda url, project, token: api)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("synthetic-token"))
    monkeypatch.setattr("sys.argv", ["semaphore-seed-input.py", "--template", "Seed OpenBao Key",
                                     "--set", "bao_path=services/x", "--set", "bao_key=k",
                                     "--input", f"BAO_VALUE={value}", "--inventory", "2", "--openbao-addr", ENDPOINT,
                                     "--url", "https://semaphore.example", *mode])
    return cli.main()


def writes_of(api):
    return [path for path, body in api.calls if body is not None]


@pytest.mark.parametrize("mode", [(), ("--verify-only", "--apply"), ("--apply",)])
def test_a_consistent_seed_passes_every_mode(monkeypatch, tmp_path, capsys, mode):
    api = FakeAPI()
    assert run_cli(monkeypatch, tmp_path, api, *mode) == 0, capsys.readouterr().err
    assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize("mode", [(), ("--verify-only", "--apply"), ("--apply",)])
@pytest.mark.parametrize("drift", [{"repository_id": 4}, {"inventory_id": 7}, {"arguments": '["-e","x=1"]'},
                                   {"app": "terraform"}, {"_env_json": '{"openbao_addr":"https://elsewhere.example"}'}])
def test_a_rebound_or_reshaped_template_is_refused_before_any_write(monkeypatch, tmp_path, capsys, mode, drift):
    # Review of PR #205: a template rebound to another repository or inventory after
    # provisioning, or given arguments, must get no staged input and no task, in any mode.
    api = FakeAPI()
    if "_env_json" in drift:  # the environment's endpoint moved after provisioning
        api.env["json"] = drift["_env_json"]
    else:
        api.template.update(drift)
    assert run_cli(monkeypatch, tmp_path, api, *mode) == 1
    assert writes_of(api) == []
    assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize("mode", [(), ("--verify-only", "--apply")])
def test_a_leftover_input_fails_dry_run_and_verify_not_only_apply(monkeypatch, tmp_path, capsys, mode):
    api = FakeAPI()
    api.env["secrets"].append({"id": 7, "name": "BAO_VALUE", "type": "env"})
    assert run_cli(monkeypatch, tmp_path, api, *mode) == 1
    assert "reconciliation" in capsys.readouterr().err
    assert writes_of(api) == []


def test_a_repository_record_that_drifted_from_its_declaration_is_refused(monkeypatch, tmp_path):
    api = FakeAPI()
    api.repos[1]["git_branch"] = "feature/unreviewed"
    assert run_cli(monkeypatch, tmp_path, api, "--apply") == 1
    assert writes_of(api) == []


def test_a_leftover_input_of_another_seed_refuses_the_seed_before_any_write():
    # Review of PR #205: the preflight checked only this seed's input names, so an unrelated
    # leftover (another seed's SEED_* value) let the seed launch with it present.
    api = FakeAPI()
    api.env["secrets"].append({"id": 8, "name": "SEED_X_API_KEY", "type": "env"})
    with pytest.raises(cli.Refusal, match="leftover inputs: SEED_X_API_KEY"):
        cli.stage_and_seed(api, 1, 301, 9, {"BAO_VALUE": SECRET}, playbook="platform/playbooks/seed-openbao-key.yml",
                           endpoint=ENDPOINT, template_names={"Seed OpenBao Key (Dev)"})
    assert not any(body for path, body in api.calls if path in ("/environment/9", "/tasks"))


def test_the_clean_environment_rule_checks_the_endpoint():
    rule = cli.stage_and_seed.__globals__["seed_environment_problems"]
    env = {"json": '{"openbao_addr":"https://elsewhere.example"}', "env": "{}", "secrets": []}
    assert "OpenBao endpoint differs from the approved endpoint" in rule(env, ENDPOINT)
    assert "OpenBao endpoint set but no approved endpoint to check it against" in rule(env)
    assert rule(dict(env, json=f'{{"openbao_addr":"{ENDPOINT}"}}'), ENDPOINT) == []
    assert rule(dict(env, json="{}")) == []
