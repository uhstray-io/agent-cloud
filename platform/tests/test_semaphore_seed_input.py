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
OPENBAO_SEED = {"playbook": "platform/playbooks/seed-openbao-key.yml", "template_names": {"Seed OpenBao Key (Dev)"}}


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
        self.env = {"id": 9, "project_id": 1, "name": "OpenBao key seed inputs (Dev)", "json": "{}",
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


def test_locate_resolves_names_and_preflight_requires_the_isolated_binding():
    assert cli.locate(FakeAPI(), "Seed OpenBao Key (Dev)", "OpenBao key seed inputs (Dev)") == (301, 9)
    # The binding is preflight's to check, once, before any write (not a second copy in locate).
    api = FakeAPI(bound_env=2)
    with pytest.raises(cli.Refusal, match="Provision Seed Environment"):
        cli.preflight(api, 1, 301, 9, playbook="platform/playbooks/seed-openbao-key.yml",
                      template_names={"Seed OpenBao Key (Dev)"})
    assert not any(body for _, body in api.calls)


def test_seed_stages_runs_once_with_settings_and_removes_only_its_input(capsys):
    api = FakeAPI()
    cli.stage_and_seed(api, 1, 301, 9, {"BAO_VALUE": SECRET}, **OPENBAO_SEED,
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
        cli.stage_and_seed(api, 1, 301, 9, {"BAO_VALUE": SECRET}, **OPENBAO_SEED)
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
                                     "--input", f"BAO_VALUE={value}", "--inventory", "2",
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
    if "_env_json" in drift:  # an address pin added to the environment after provisioning
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
        cli.stage_and_seed(api, 1, 301, 9, {"BAO_VALUE": SECRET}, **OPENBAO_SEED)
    assert not any(body for path, body in api.calls if path in ("/environment/9", "/tasks"))


def test_the_clean_environment_rule_refuses_any_address_pin():
    # The seed run takes its address from the inventory; a pin would override it.
    rule = cli.stage_and_seed.__globals__["seed_environment_problems"]
    env = {"json": '{"openbao_addr":"https://bao.example:8200"}', "env": "{}", "secrets": []}
    assert any("OpenBao address pinned" in p for p in rule(env))
    assert rule(env, legacy_pin_ok=True) == []  # the provisioner, which removes it
    assert rule(dict(env, json="{}")) == []
    assert rule({"json": "{}", "env": "{}"}) == []  # upstream omits an empty secrets list
    assert "no secrets list; contents cannot be established" in rule({"json": "{}", "env": "{}", "secrets": None})


def test_seed_refuses_an_empty_isolated_environment_before_writes():
    api = FakeAPI()
    del api.env["secrets"]  # v2.18.12 single GET omits an empty loaded list
    with pytest.raises(cli.Refusal, match="Provision both AppRole inputs"):
        cli.stage_and_seed(api, 1, 301, 9, {"BAO_VALUE": SECRET},
                           **OPENBAO_SEED)
    assert not any(body for path, body in api.calls if path in ("/environment/9", "/tasks"))


@pytest.mark.parametrize("mode", [(), ("--verify-only", "--apply"), ("--apply",)])
def test_every_mode_runs_the_preflight_exactly_once(monkeypatch, tmp_path, mode):
    # Preflight reads the template list once per run; a second preflight doubles it.
    api = FakeAPI()
    calls = []
    real = cli.preflight
    monkeypatch.setattr(cli, "preflight", lambda *a, **k: calls.append(1) or real(*a, **k))
    import semaphore_seed
    monkeypatch.setattr(semaphore_seed, "preflight", cli.preflight)
    assert run_cli(monkeypatch, tmp_path, api, *mode) == 0
    assert len(calls) == 1


def test_an_environment_changed_after_preflight_is_refused_before_staging(monkeypatch):
    api = FakeAPI()
    reads = []
    real = FakeAPI.__call__

    def changing(self, path, body=None):
        if path == "/environment/9" and body is None:
            reads.append(1)
            if len(reads) == 2:  # the re-read before staging sees another writer's change
                self.env["json"] = '{"openbao_addr":"https://elsewhere.example:8200"}'
        return real(self, path, body)
    monkeypatch.setattr(FakeAPI, "__call__", changing)
    with pytest.raises(cli.Refusal, match="changed during preflight"):
        cli.stage_and_seed(api, 1, 301, 9, {"BAO_VALUE": SECRET}, **OPENBAO_SEED)
    assert not any(body for path, body in api.calls if path in ("/environment/9", "/tasks"))


@pytest.mark.parametrize("reply", [None, [], "running"])
def test_an_unreadable_poll_names_the_task(monkeypatch, reply):
    import semaphore_seed
    monkeypatch.setattr(semaphore_seed.time, "sleep", lambda _s: None)
    with pytest.raises(cli.Refusal, match="task 900 status unreadable"):
        semaphore_seed.wait(lambda path, body=None: reply, {"id": 900, "status": "waiting"}, "Access check")
