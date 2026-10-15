"""The deploy-time MCP guards (agentgateway-mcp tasks 3.2, 3.8): the allowlist, the schema read and the render check.

The real vars file and the real tasks of deploy-agentgateway.yml run on localhost through
harness_sandbox. What is pinned:

- the refusal is an ALLOWLIST (task 3.8): a target is `{name, schema}` and nothing else, so an `mcp`,
  `sse` or `stdio` target, a `host`, a schema outside the shipped directory and a hostile loopback port
  cannot be declared, and each refusal names the entry and runs before anything is rendered or restarted;
- the shipped schema is read from the placed monorepo and reaches the render as compact JSON;
- the deploy checks its own render: nothing MCP with the flag off, a strict key policy on any mcp block
  (without it /mcp answers every request, spike 1.1), and exactly the admin-derived identities enrolled on it.
"""

import base64
import json
import re
import sys
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

REPO = playbook_yaml.REPO
DEPLOY = REPO / "platform/playbooks/deploy-agentgateway.yml"
DECL = REPO / "platform/playbooks/vars/secret-declarations/agentgateway.yml"
DEPLOY_DIR = REPO / "platform/services/agentgateway/deployment"
CONFIG = DEPLOY_DIR / "templates/config.yaml.j2"
SCHEMA_FILE = "agentgateway-v1.json"
SCHEMA_TEXT = json.dumps(json.loads((DEPLOY_DIR / "mcp-schemas" / SCHEMA_FILE).read_text()), separators=(",", ":"))

PHASE1 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 1"))
NAMES = [t["name"] for t in PHASE1["tasks"]]
GUARD = "Refuse an MCP declaration that cannot render a safe target"
READ_SCHEMA = "Read the shipped OpenAPI schema of each declared MCP target"
CHECK_SCHEMA = "Refuse a target schema that is not an OpenAPI document with paths"
HOLD_SCHEMA = "Hold each schema as compact JSON for the render"
READ_RENDER = "Read the identity names and key-entry count the render wrote"
CHECK_SURFACE = "Require the rendered MCP surface to match agw_mcp_enabled"
CHECK_POLICY = "Require the rendered mcp block to carry its own strict key policy"
CHECK_ENROL = "Require the mcp key policy to enrol exactly the admin-derived identities"
RENDER = "Manage secrets and render env + config"


def _admin(name: str) -> dict:
    return {"key": name, "username": name, "name": name.title(), "email": f"{name}@example.invalid"}


# One service client, the internal identity, and two personal admins: /mcp admits the admins only.
OK = {
    "agw_mcp_enabled": True,
    "agw_clients": ["svc", "mcp-self"],
    "agw_admin_identities_enabled": True,
    "platform_admins": [_admin("alice"), _admin("bob")],
    "agw_client_policies": {"mcp-self": {"tokens_per_hour": 100}},
    "agw_mcp_openapi_targets": [{"name": "gateway", "schema": SCHEMA_FILE}],
}


def _task(name: str) -> dict:
    return next(t for t in PHASE1["tasks"] if t.get("name") == name)


def _play(tmp: Path, host_vars: dict, tasks: list, play_vars: dict | None = None, args: tuple = ()):
    inv = {
        "all": {
            "hosts": {"gw": {"ansible_connection": "local", "ansible_python_interpreter": sys.executable, **host_vars}}
        }
    }
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    (tmp / "play.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "all",
                    "gather_facts": False,
                    "vars_files": [str(DECL)],
                    "vars": play_vars or {},
                    "tasks": tasks,
                }
            ]
        )
    )
    return harness_sandbox.run(
        ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(tmp / "play.yml"), *args],
        tmp,
        cwd=REPO,
        env={**harness_sandbox.env_for(tmp), "ANSIBLE_STDOUT_CALLBACK": "default"},
    )


def _problems(tmp: Path, host_vars: dict) -> list[str]:
    out = tmp / "problems.json"
    expr = "{{ _agw_mcp_problems | to_json }}"
    r = _play(tmp, host_vars, [{"ansible.builtin.copy": {"content": expr, "dest": str(out)}}])
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(out.read_text())


def _guard(tmp: Path, host_vars: dict):
    return _play(tmp, host_vars, [_task(GUARD)])


# ── the allowlist (task 3.8) ──────────────────────────────────────────────────


def test_a_correct_declaration_has_no_problems(tmp_path):
    assert _problems(tmp_path, OK) == []
    r = _guard(tmp_path, OK)
    assert r.returncode == 0, r.stdout + r.stderr


def test_nothing_is_checked_while_the_flag_is_false(tmp_path):
    assert (
        _problems(tmp_path, {"agw_mcp_openapi_targets": "garbage", "agw_loopback_port": "x", "agw_clients": []}) == []
    )


@pytest.mark.parametrize(
    "entry, key",
    [
        pytest.param(
            {"name": "gw", "schema": SCHEMA_FILE, "mcp": {"host": "127.0.0.1", "port": 4000, "path": "/mcp"}},
            "mcp",
            id="an-mcp-kind-aimed-at-the-gateway",
        ),
        pytest.param({"name": "gw", "schema": SCHEMA_FILE, "sse": {"host": "127.0.0.1:4000"}}, "sse", id="an-sse-kind"),
        pytest.param({"name": "gw", "schema": SCHEMA_FILE, "stdio": {"cmd": "sh"}}, "stdio", id="a-stdio-kind"),
        pytest.param(
            {"name": "gw", "schema": SCHEMA_FILE, "host": "127.0.0.1:4000"},
            "host",
            id="a-host-aimed-at-the-external-listener",
        ),
        pytest.param(
            {"name": "gw", "schema": SCHEMA_FILE, "port": 4000, "path": "/mcp"}, "path, port", id="a-port-and-path"
        ),
        pytest.param({"name": "gw", "schema": SCHEMA_FILE, "kind": "openapi"}, "kind", id="a-kind-field"),
    ],
)
def test_a_target_is_only_a_name_and_a_schema(tmp_path, entry, key):
    hv = {**OK, "agw_mcp_openapi_targets": [entry]}
    (problem,) = _problems(tmp_path, hv)
    assert problem.startswith("agw_mcp_openapi_targets entry gw:") and f"the key(s) {key} are not accepted" in problem
    r = _guard(tmp_path, hv)
    assert (
        r.returncode != 0
        and "agw_mcp_enabled cannot be rendered" in r.stdout + r.stderr
        and "entry gw" in r.stdout + r.stderr
    )


@pytest.mark.parametrize(
    "schema", ["../secrets.json", "/etc/passwd.json", "sub/dir.json", "a.yaml", "", ".hidden.json", "x.json/", "x.JSON"]
)
def test_a_schema_outside_the_shipped_directory_is_refused(tmp_path, schema):
    hv = {**OK, "agw_mcp_openapi_targets": [{"name": "gw", "schema": schema}]}
    (problem,) = _problems(tmp_path, hv)
    assert (
        problem.startswith("agw_mcp_openapi_targets entry gw:") and "schema must be the name of a .json file" in problem
    )


@pytest.mark.parametrize(
    "targets, expect",
    [
        pytest.param([], "non-empty list", id="no-target"),
        pytest.param("gateway", "non-empty list", id="a-string"),
        pytest.param({"name": "gw", "schema": SCHEMA_FILE}, "non-empty list", id="a-mapping-instead-of-a-list"),
        pytest.param(["gw"], "entry #1: must be a mapping", id="an-entry-that-is-not-a-mapping"),
        pytest.param([{"schema": SCHEMA_FILE}], "entry #1: name must match", id="a-nameless-entry"),
        pytest.param(
            [{"name": "Bad Name", "schema": SCHEMA_FILE}],
            "entry Bad Name: name must match",
            id="a-name-outside-the-charset",
        ),
        pytest.param(
            [{"name": "gw", "schema": "a.json"}, {"name": "gw", "schema": "b.json"}],
            "entry gw: the target name is declared more than once",
            id="a-duplicate-name",
        ),
    ],
)
def test_a_malformed_declaration_is_refused_naming_the_entry(tmp_path, targets, expect):
    problems = _problems(tmp_path, {**OK, "agw_mcp_openapi_targets": targets})
    assert len(problems) == 1 and expect in problems[0], problems


@pytest.mark.parametrize("port", [4000, 4001, 15000, 19001, 19002, 80, 1023, 65536, "abc", "40 02"])
def test_a_loopback_port_the_gateway_already_binds_or_outside_the_range_is_refused(tmp_path, port):
    (problem,) = _problems(tmp_path, {**OK, "agw_loopback_port": port})
    assert problem.startswith("agw_loopback_port must be an integer from 1024 to 65535")


@pytest.mark.parametrize("port", [4002, "4010", 20000])
def test_a_free_loopback_port_is_accepted(tmp_path, port):
    assert _problems(tmp_path, {**OK, "agw_loopback_port": port}) == []


def test_the_internal_identity_must_be_declared(tmp_path):
    (problem,) = _problems(tmp_path, {**OK, "agw_clients": ["svc"]})
    assert "mcp-self must be declared in agw_clients" in problem


@pytest.mark.parametrize(
    "hv",
    [
        pytest.param({"agw_admin_identities_enabled": False}, id="the-gate-is-off"),
        pytest.param({"platform_admins": []}, id="no-admin-is-declared"),
    ],
)
def test_mcp_needs_admin_identities_and_the_refusal_names_both_knobs(tmp_path, hv):
    (problem,) = _problems(tmp_path, {**OK, **hv})
    assert "agw_admin_identities_enabled" in problem and "platform_admins" in problem
    r = _guard(tmp_path, {**OK, **hv})
    assert r.returncode != 0 and "agw_admin_identities_enabled" in r.stdout + r.stderr


def test_an_admin_identity_with_a_model_restriction_is_refused_naming_it(tmp_path):
    """MCP would widen it: the chat tool runs as mcp-self, which is not model-restricted."""
    policies = {**OK["agw_client_policies"], "bob": {"allowed_models": ["m"]}}
    (problem,) = _problems(tmp_path, {**OK, "agw_client_policies": policies})
    assert "admin identity bob" in problem and "allowed_models" in problem


def test_a_service_client_with_a_model_restriction_is_not_a_problem(tmp_path):
    """It cannot reach /mcp, so there is nothing to widen."""
    policies = {**OK["agw_client_policies"], "svc": {"allowed_models": ["m"]}}
    assert _problems(tmp_path, {**OK, "agw_client_policies": policies}) == []


@pytest.mark.parametrize("policies", [{}, {"mcp-self": {}}, {"mcp-self": {"allowed_models": ["m"]}}])
def test_the_internal_identity_must_carry_its_own_budget(tmp_path, policies):
    """Its budget is the aggregate cap on every model request made through MCP."""
    (problem,) = _problems(tmp_path, {**OK, "agw_client_policies": policies})
    assert "agw_client_policies.mcp-self.tokens_per_hour is required" in problem


def test_the_guard_runs_before_the_monorepo_is_placed_and_before_anything_renders():
    assert NAMES.index(GUARD) < NAMES.index("Place the monorepo + ensure podman/compose") < NAMES.index(RENDER)
    assert NAMES.index(GUARD) < NAMES.index(READ_SCHEMA) < NAMES.index(RENDER)


def test_a_person_cannot_be_declared_as_the_internal_identity(tmp_path):
    """Task 4.2: `mcp-self` is reserved for a derived identity, by the admin-list derivation the deploy already runs."""
    hv = {
        "agw_admin_identities_enabled": True,
        "agw_clients": ["alice"],
        "platform_admins": [{"key": "x", "username": "mcp-self", "name": "X", "email": "x@example.invalid"}],
    }
    out = tmp_path / "problems.json"
    expr = "{{ _agw_identity_problems | to_json }}"
    r = _play(tmp_path, hv, [{"ansible.builtin.copy": {"content": expr, "dest": str(out)}}])
    assert r.returncode == 0, r.stdout + r.stderr
    (problem,) = json.loads(out.read_text())
    assert "mcp-self" in problem and "reserved" in problem


# ── the schema read ───────────────────────────────────────────────────────────

SCHEMA_TASKS = [READ_SCHEMA, CHECK_SCHEMA, HOLD_SCHEMA]


def _read_schemas(tmp: Path, schema_dir: Path, hv: dict, args: tuple = (), deploy_dir: Path | None = None):
    """The schema tasks with the controller-side schema directory `schema_dir`; `deploy_dir` is the target's."""
    out = tmp / "schemas.json"
    expr = "{{ (_agw_mcp_schemas | default({})) | to_json }}"
    tasks = [_task(n) for n in SCHEMA_TASKS]
    tasks.append({"ansible.builtin.copy": {"content": expr, "dest": str(out)}})
    tasks.append(
        {"ansible.builtin.debug": {"msg": "SCHEMAS_B64={{ (_agw_mcp_schemas | default({})) | to_json | b64encode }}"}}
    )
    play_vars = {"_agw_mcp_schema_dir": str(schema_dir), "_deploy_dir": str(deploy_dir or tmp / "no-such-target")}
    r = _play(tmp, hv, tasks, play_vars, args)
    return r, (json.loads(out.read_text()) if out.exists() else None)


def test_the_shipped_schema_reaches_the_render_as_compact_json(tmp_path):
    r, schemas = _read_schemas(tmp_path, DEPLOY_DIR / "mcp-schemas", OK)
    assert r.returncode == 0, r.stdout + r.stderr
    assert schemas == {SCHEMA_FILE: SCHEMA_TEXT}


def test_a_dry_run_reads_the_schema_from_the_controller_when_the_target_has_none(tmp_path):
    """--check places no monorepo, so the target has no mcp-schemas/: the read must not depend on it."""
    r, _ = _read_schemas(tmp_path, DEPLOY_DIR / "mcp-schemas", OK, args=("--check",))
    assert r.returncode == 0, r.stdout + r.stderr
    encoded = re.search(r"SCHEMAS_B64=([A-Za-z0-9+/=]+)", r.stdout).group(1)
    assert json.loads(base64.b64decode(encoded)) == {SCHEMA_FILE: SCHEMA_TEXT}


def test_the_schema_directory_is_the_controllers_checkout_beside_the_template():
    var = PHASE1["vars"]["_agw_mcp_schema_dir"]
    assert var == "{{ playbook_dir ~ '/../services/' ~ service_name ~ '/deployment/mcp-schemas' }}"


def test_with_the_flag_false_nothing_is_read(tmp_path):
    r, schemas = _read_schemas(tmp_path, DEPLOY_DIR / "mcp-schemas", {**OK, "agw_mcp_enabled": False})
    assert r.returncode == 0, r.stdout + r.stderr
    assert schemas == {}


def test_without_any_declaration_and_the_flag_false_the_read_does_not_need_one(tmp_path):
    r, schemas = _read_schemas(tmp_path, DEPLOY_DIR / "mcp-schemas", {"agw_clients": ["a"]})
    assert r.returncode == 0, r.stdout + r.stderr
    assert schemas == {}


@pytest.mark.parametrize("content", ['{"info": {}}', '{"openapi": "3.0.3", "paths": {}}'])
def test_a_schema_that_is_not_an_openapi_document_with_paths_is_refused(tmp_path, content):
    d = tmp_path / "deploy"
    (d / "mcp-schemas").mkdir(parents=True)
    (d / "mcp-schemas" / SCHEMA_FILE).write_text(content)
    r, _ = _read_schemas(tmp_path, d / "mcp-schemas", OK)
    assert r.returncode != 0 and "is not an OpenAPI document with paths" in r.stdout + r.stderr


def test_a_declared_schema_that_is_not_shipped_fails_before_the_render(tmp_path):
    d = tmp_path / "deploy"
    (d / "mcp-schemas").mkdir(parents=True)
    r, _ = _read_schemas(tmp_path, d / "mcp-schemas", OK)
    assert r.returncode != 0


# ── the render check ──────────────────────────────────────────────────────────

CHECKS = [READ_RENDER, CHECK_SURFACE, CHECK_POLICY, CHECK_ENROL]


def _rendered(tmp: Path, **hv) -> Path:
    """The real template, rendered the way manage-secrets does, into <tmp>/d/config.yaml."""
    d = tmp / "d"
    d.mkdir(exist_ok=True)
    clients = hv.get("agw_clients", OK["agw_clients"])
    admins = hv.get("admins", ["alice", "bob"])
    secrets = {f"client_{c}": f"key-{c}" for c in [*clients, *admins]} | {
        "vllm_api_key": "v",
        "agw_oidc_cookie_seed": "s",
        "agentgateway_oidc_client_secret": "c",
    }
    vars_ = {
        **OK,
        **hv,
        "secrets": secrets,
        "agw_models": [{"name": "m"}],
        "agw_upstream_base_url": "http://u.invalid/v1",
        "agw_identities": [*clients, *admins],
        "platform_admins": [_admin(a) for a in admins],  # the vars file derives the identities from it
        "_agw_mcp_schemas": {SCHEMA_FILE: SCHEMA_TEXT},
    }
    r = _play(
        tmp,
        {},
        [{"ansible.builtin.template": {"src": str(CONFIG), "dest": str(d / "config.yaml"), "mode": "0644"}}],
        vars_,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return d / "config.yaml"


def _check(tmp: Path, cfg: Path, **hv):
    vars_ = {"_deploy_dir": str(cfg.parent), "_legacy_shared_active": False}
    return _play(tmp, {**OK, **hv}, [_task(n) for n in CHECKS], vars_)


def test_a_correct_render_passes_every_check(tmp_path):
    cfg = _rendered(tmp_path)
    r = _check(tmp_path, cfg)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_correct_flag_false_render_passes_and_has_nothing_mcp(tmp_path):
    cfg = _rendered(tmp_path, agw_mcp_enabled=False)
    assert not re.search(r"^mcp:$|^  loopback:$", cfg.read_text(), re.M)
    r = _check(tmp_path, cfg, agw_mcp_enabled=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_an_mcp_block_rendered_while_the_flag_is_false_fails(tmp_path):
    cfg = _rendered(tmp_path)  # a flag-true render, checked against a flag-false deploy
    r = _check(tmp_path, cfg, agw_mcp_enabled=False)
    assert r.returncode != 0 and "has an mcp block: True" in r.stdout + r.stderr, r.stdout


def test_a_missing_mcp_block_while_the_flag_is_true_fails(tmp_path):
    cfg = _rendered(tmp_path, agw_mcp_enabled=False)
    r = _check(tmp_path, cfg)
    assert r.returncode != 0 and "has an mcp block: False" in r.stdout + r.stderr, r.stdout


def test_a_loopback_gateway_rendered_while_the_flag_is_false_fails(tmp_path):
    cfg = _rendered(tmp_path, agw_mcp_enabled=False)
    text = cfg.read_text()
    marker = "\ngateways:\n  default:\n    port: 4000\n"
    assert marker in text
    cfg.write_text(text.replace(marker, marker + "  loopback:\n    port: 4002\n", 1))
    r = _check(tmp_path, cfg, agw_mcp_enabled=False)
    assert r.returncode != 0 and "a loopback gateway: True" in r.stdout + r.stderr, r.stdout


def test_a_missing_loopback_gateway_while_the_flag_is_true_fails(tmp_path):
    cfg = _rendered(tmp_path)
    text = cfg.read_text()
    stripped = re.sub(r"^  loopback:\n(?:    .*\n)+", "", text, flags=re.M)
    assert stripped != text
    cfg.write_text(stripped)
    r = _check(tmp_path, cfg)
    assert r.returncode != 0 and "a loopback gateway: False" in r.stdout + r.stderr, r.stdout


def test_an_mcp_block_with_no_key_policy_fails_closed(tmp_path):
    cfg = _rendered(tmp_path)
    text = cfg.read_text()
    head, mcp = text.split("\nmcp:\n", 1)
    mcp = re.split(r"^  policies:\n", mcp, maxsplit=1, flags=re.M)[0]  # drop mcp.policies and the key list with it
    cfg.write_text(head + "\nmcp:\n" + mcp)
    r = _check(tmp_path, cfg)
    assert r.returncode != 0 and "no strict apiKey policy" in r.stdout + r.stderr, r.stdout


@pytest.mark.parametrize(
    "edit, expect",
    [
        pytest.param(
            lambda t: t.replace("mode: strict", "mode: optional"),
            "no strict apiKey policy",
            id="a-policy-that-is-not-strict",
        ),
    ],
)
def test_a_key_policy_that_is_not_strict_fails(tmp_path, edit, expect):
    cfg = _rendered(tmp_path)
    head, mcp = cfg.read_text().split("\nmcp:\n", 1)
    cfg.write_text(head + "\nmcp:\n" + edit(mcp))
    r = _check(tmp_path, cfg)
    assert r.returncode != 0 and expect in r.stdout + r.stderr, r.stdout


def test_the_internal_identity_enrolled_on_mcp_fails_naming_it(tmp_path):
    cfg = _rendered(tmp_path)
    head, mcp = cfg.read_text().split("\nmcp:\n", 1)
    entry = "      - keyHash: sha256:" + "0" * 64 + "\n        metadata:\n          name: mcp-self\n"
    cfg.write_text(head + "\nmcp:\n" + mcp.rstrip("\n") + "\n" + entry)
    r = _check(tmp_path, cfg)
    assert r.returncode != 0 and "not allowed" in r.stdout + r.stderr and "mcp-self" in r.stdout + r.stderr, r.stdout


def test_a_client_missing_from_the_mcp_policy_fails_naming_it(tmp_path):
    cfg = _rendered(tmp_path, admins=["alice"])  # rendered without bob
    r = _check(tmp_path, cfg)
    assert r.returncode != 0 and "missing bob" in r.stdout + r.stderr, r.stdout


def test_the_check_output_carries_no_hash_or_key(tmp_path):
    cfg = _rendered(tmp_path, admins=["alice"])
    r = _check(tmp_path, cfg)
    assert r.returncode != 0
    assert "sha256:" not in r.stdout + r.stderr and "key-alice" not in r.stdout + r.stderr
