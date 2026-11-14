"""The launch gate's template allowlist filter, and the two templates it is rendered beside.

The filter (platform/playbooks/filter_plugins/launch_gate.py) is tested on synthetic documents and on the
repository's own OPA catalog and templates.yml, so a template the catalog names but templates.yml does not
declare fails here, not at a deploy. The templates (platform/services/launch-gate/deployment/templates) are
rendered with Jinja2 under Ansible's own settings (trim_blocks, its bool filter and to_nice_json). The
rendered config is then read by the app's own load_config, so a template and the app cannot drift apart
without this failing.
"""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml
from ansible.errors import AnsibleFilterError

REPO = Path(__file__).resolve().parents[2]
CATALOG = REPO / "platform/services/opa/deployment/policies/agentcloud/data.json"
TEMPLATES = REPO / "platform/semaphore/templates.yml"
LOCAL_TEMPLATES = REPO / "platform/semaphore/templates-local.yml"
PLAYBOOK = REPO / "platform/playbooks/deploy-launch-gate.yml"
DEPLOY = REPO / "platform/services/launch-gate/deployment"
ROLE_AGENTS = ["infra-agent", "security-agent", "o11y-agent", "service-agent", "network-agent"]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


filt = _load("launch_gate_filter", REPO / "platform/playbooks/filter_plugins/launch_gate.py")
app = _load("launch_gate_app", DEPLOY / "app/launch_gate.py")


def catalog(**agents):
    """A minimal OPA data document: a destructive list, plus the agents given."""
    return {"catalog": {"semaphore": {"destructive_templates": ["Wipe and Rebuild"]}, **agents}}


def templates(*entries):
    """A minimal templates.yml document from (name, dev_variant) pairs."""
    return {"templates": [{"name": name, "dev_variant": dev} for name, dev in entries]}


def real_documents():
    return json.loads(CATALOG.read_text()), yaml.safe_load(TEMPLATES.read_text())


# ── the filter ──────────────────────────────────────────────────────────────────


def test_it_is_registered_under_its_name():
    assert filt.FilterModule().filters() == {"launch_gate_allowlist": filt.launch_gate_allowlist}


def test_each_named_template_and_its_dev_variant_is_listed_once():
    doc = catalog(**{"agent-one": {"allowed_templates": ["A", "B"], "service_deploy_templates": ["C"]}})
    tpl = templates(("A", True), ("B", False), ("C", True))
    assert filt.launch_gate_allowlist(doc, tpl, ["agent-one"]) == ["A", "A (Dev)", "B", "C", "C (Dev)"]


def test_a_template_without_dev_variant_gets_no_dev_name():
    doc = catalog(**{"agent-one": {"allowed_templates": ["B"]}})
    assert filt.launch_gate_allowlist(doc, templates(("B", False)), ["agent-one"]) == ["B"]


def test_agents_are_unioned_and_a_shared_template_is_listed_once():
    doc = catalog(
        **{
            "agent-one": {"allowed_templates": ["A"]},
            "agent-two": {"allowed_templates": ["A", "B"]},
        }
    )
    tpl = templates(("A", False), ("B", False))
    assert filt.launch_gate_allowlist(doc, tpl, ["agent-one", "agent-two"]) == ["A", "B"]


def test_a_destructive_template_and_its_dev_variant_are_never_listed():
    doc = catalog(**{"agent-one": {"allowed_templates": ["Wipe and Rebuild", "A"]}})
    tpl = templates(("Wipe and Rebuild", True), ("A", False))
    assert filt.launch_gate_allowlist(doc, tpl, ["agent-one"]) == ["A"]


def test_clean_deploy_is_refused_even_when_the_catalog_does_not_list_it_as_destructive():
    doc = catalog(**{"agent-one": {"allowed_templates": ["Clean Deploy Widget", "A"]}})
    tpl = templates(("Clean Deploy Widget", True), ("A", False))
    assert filt.launch_gate_allowlist(doc, tpl, ["agent-one"]) == ["A"]


def test_an_agent_not_in_the_catalog_is_refused():
    with pytest.raises(AnsibleFilterError, match="nobody"):
        filt.launch_gate_allowlist(catalog(), templates(), ["nobody"])


def test_an_agent_with_no_allowed_templates_list_is_refused_by_name():
    doc = catalog(skynet={"allowed_actions": {"semaphore": ["run_task"]}})
    with pytest.raises(AnsibleFilterError, match="skynet"):
        filt.launch_gate_allowlist(doc, templates(), ["skynet"])


def test_a_name_the_templates_file_does_not_declare_is_refused_as_drift():
    doc = catalog(**{"agent-one": {"allowed_templates": ["Gone"]}})
    with pytest.raises(AnsibleFilterError, match="Gone"):
        filt.launch_gate_allowlist(doc, templates(("A", False)), ["agent-one"])


def test_an_empty_agent_list_is_refused():
    with pytest.raises(AnsibleFilterError, match="at least one"):
        filt.launch_gate_allowlist(catalog(), templates(), [])


def test_agents_given_as_one_string_are_refused():
    doc = catalog(**{"agent-one": {"allowed_templates": ["A"]}})
    with pytest.raises(AnsibleFilterError, match="agents must be a list"):
        filt.launch_gate_allowlist(doc, templates(("A", False)), "agent-one")


def test_a_document_without_the_catalog_section_is_refused():
    with pytest.raises(AnsibleFilterError, match="catalog"):
        filt.launch_gate_allowlist({"templates": []}, templates(), ["agent-one"])


def test_a_catalog_without_its_destructive_list_is_refused():
    doc = {"catalog": {"agent-one": {"allowed_templates": ["A"]}}}
    with pytest.raises(AnsibleFilterError, match="semaphore"):
        filt.launch_gate_allowlist(doc, templates(("A", False)), ["agent-one"])


def test_a_templates_document_without_its_list_is_refused():
    doc = catalog(**{"agent-one": {"allowed_templates": ["A"]}})
    with pytest.raises(AnsibleFilterError, match="templates"):
        filt.launch_gate_allowlist(doc, {"other": []}, ["agent-one"])


def test_a_template_name_declared_twice_is_refused():
    doc = catalog(**{"agent-one": {"allowed_templates": ["A"]}})
    with pytest.raises(AnsibleFilterError, match="twice"):
        filt.launch_gate_allowlist(doc, templates(("A", False), ("A", True)), ["agent-one"])


def test_the_repository_allowlist_holds_no_destructive_or_clean_deploy_name():
    cat, tpl = real_documents()
    destructive = set(cat["catalog"]["semaphore"]["destructive_templates"])
    out = filt.launch_gate_allowlist(cat, tpl, ROLE_AGENTS)
    assert out, "the role agents named no templates at all"
    assert not [name for name in out if name in destructive or name.startswith("Clean Deploy")]


def test_the_repository_security_agent_gets_harden_ssh_and_its_dev_variant():
    cat, tpl = real_documents()
    out = filt.launch_gate_allowlist(cat, tpl, ["security-agent"])
    assert "Harden SSH" in out
    assert "Harden SSH (Dev)" in out


def test_every_repository_allowlist_name_is_declared_and_dev_only_where_templates_yml_says_so():
    cat, tpl = real_documents()
    declared = {entry["name"]: bool(entry.get("dev_variant", False)) for entry in tpl["templates"]}
    for name in filt.launch_gate_allowlist(cat, tpl, ROLE_AGENTS):
        base = name.removesuffix(filt.DEV_SUFFIX)
        assert base in declared, name
        if base != name:
            assert declared[base], name


def test_the_repository_skynet_agent_is_refused_because_it_names_no_templates():
    # skynet launches through the gate; it is not a template holder. A caller maps to the role agents.
    cat, tpl = real_documents()
    with pytest.raises(AnsibleFilterError, match="skynet"):
        filt.launch_gate_allowlist(cat, tpl, ["skynet"])


# ── (Local) variants, local-dev only ────────────────────────────────────────────


def local_templates(*names):
    """A minimal templates-local.yml document from template names."""
    return {"templates": [{"name": name} for name in names]}


def test_local_variants_are_added_only_where_templates_local_declares_them():
    doc = catalog(**{"agent-one": {"allowed_templates": ["A", "B", "Wipe and Rebuild"]}})
    tpl = templates(("A", True), ("B", False), ("Wipe and Rebuild", True))
    local = local_templates("A (Local)", "Wipe and Rebuild (Local)", "Unrelated (Local)")
    assert filt.launch_gate_allowlist(doc, tpl, ["agent-one"], local) == ["A", "A (Dev)", "A (Local)", "B"]


def test_without_the_local_document_no_local_name_is_listed():
    cat, tpl = real_documents()
    assert not [name for name in filt.launch_gate_allowlist(cat, tpl, ROLE_AGENTS) if name.endswith(" (Local)")]


def test_a_local_document_without_its_list_is_refused():
    doc = catalog(**{"agent-one": {"allowed_templates": ["A"]}})
    with pytest.raises(AnsibleFilterError, match="templates-local.yml"):
        filt.launch_gate_allowlist(doc, templates(("A", False)), ["agent-one"], {"other": []})


def test_a_local_template_name_declared_twice_is_refused():
    doc = catalog(**{"agent-one": {"allowed_templates": ["A"]}})
    with pytest.raises(AnsibleFilterError, match="declared twice in templates-local.yml"):
        local = local_templates("A (Local)", "A (Local)")
        filt.launch_gate_allowlist(doc, templates(("A", False)), ["agent-one"], local)


def test_the_repository_local_allowlist_adds_only_declared_local_names_to_the_production_list():
    cat, tpl = real_documents()
    local = yaml.safe_load(LOCAL_TEMPLATES.read_text())
    declared_local = {entry["name"] for entry in local["templates"]}
    prod = filt.launch_gate_allowlist(cat, tpl, ROLE_AGENTS)
    out = filt.launch_gate_allowlist(cat, tpl, ROLE_AGENTS, local)
    added = sorted(set(out) - set(prod))
    assert set(prod) <= set(out)
    assert added, "no role agent template has a (Local) variant"
    assert all(name.endswith(" (Local)") and name in declared_local for name in added)
    assert all(name.removesuffix(" (Local)") in prod for name in added)
    assert "Snapshot Firewall (Local)" in out  # skynet's first local service-onboarding launch


def test_the_deploy_reads_templates_local_only_under_local_mode():
    text = PLAYBOOK.read_text()
    start = text.index("_launch_gate_allowlist: >-")
    call = text[start : text.index("\n\n", start)]
    assert "templates-local.yml" in call
    assert "if (_launch_gate_local_mode | bool) else none" in call


# ── the templates, rendered as Ansible renders them ─────────────────────────────


def _jinja_env():
    """Ansible's template settings: trim_blocks, keep_trailing_newline, and its own to_nice_json, bool and
    urlsplit filters."""
    jinja2 = pytest.importorskip("jinja2")
    core = pytest.importorskip("ansible.plugins.filter.core")
    urls = pytest.importorskip("ansible.plugins.filter.urlsplit")
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters["to_nice_json"] = core.to_nice_json
    env.filters["bool"] = core.to_bool
    env.filters["urlsplit"] = urls.split_url
    return env


def _render(name, **variables):
    env = _jinja_env()
    return env.from_string((DEPLOY / "templates" / name).read_text()).render(**variables)


def _env_values(text):
    values = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep, f"not a KEY=VALUE line: {line!r}"
        assert key not in values, f"{key} is set twice"
        values[key] = value
    return values


SEMAPHORE_TOKEN = "S" * 40
IMAGE = "registry.invalid/launch-gate@sha256:" + "0" * 64


def test_env_template_renders_the_fixed_keys_and_one_token_per_caller():
    secrets = {
        "semaphore_api_token": SEMAPHORE_TOKEN,
        "caller_skynet_token": "A" * 48,
        "caller_security-agent_token": "B" * 48,
    }
    # Local mode: the OPA name map is production-only (test_env_template_maps_the_opa_name_outside_local_mode_only).
    out = _render(
        "env.j2",
        _launch_gate_image=IMAGE,
        _launch_gate_bind="127.0.0.1",
        _launch_gate_port=8443,
        _launch_gate_tls_name="launch-gate.local.agent-cloud.test",
        _launch_gate_callers={"skynet": ["security-agent"], "security-agent": ["security-agent"]},
        _launch_gate_local_mode=True,
        secrets=secrets,
    )
    assert _env_values(out) == {
        "LAUNCH_GATE_IMAGE": IMAGE,
        "LAUNCH_GATE_BIND": "127.0.0.1",
        "LAUNCH_GATE_PORT": "8443",
        "LAUNCH_GATE_TLS_NAME": "launch-gate.local.agent-cloud.test",
        "LAUNCH_GATE_SEMAPHORE_TOKEN": SEMAPHORE_TOKEN,
        "LAUNCH_GATE_CALLER_TOKEN_SKYNET": "A" * 48,
        "LAUNCH_GATE_CALLER_TOKEN_SECURITY_AGENT": "B" * 48,
    }
    assert "{{" not in out and "{%" not in out


def test_env_template_with_no_callers_renders_only_the_fixed_keys():
    out = _render(
        "env.j2",
        _launch_gate_image=IMAGE,
        _launch_gate_bind="127.0.0.1",
        _launch_gate_port=8443,
        _launch_gate_tls_name="launch-gate.local.agent-cloud.test",
        _launch_gate_callers={},
        _launch_gate_local_mode=True,
        secrets={"semaphore_api_token": SEMAPHORE_TOKEN},
    )
    assert sorted(_env_values(out)) == [
        "LAUNCH_GATE_BIND",
        "LAUNCH_GATE_IMAGE",
        "LAUNCH_GATE_PORT",
        "LAUNCH_GATE_SEMAPHORE_TOKEN",
        "LAUNCH_GATE_TLS_NAME",
    ]


def test_env_template_maps_the_opa_name_outside_local_mode_only():
    # Production: the OPA host name and the declared address reach compose.prod.yml's extra_hosts through .env.
    common = {
        "_launch_gate_image": IMAGE,
        "_launch_gate_bind": "192.0.2.10",
        "_launch_gate_port": 8443,
        "_launch_gate_tls_name": "launch-gate.dc1.example.internal",
        "_launch_gate_callers": {},
        "_launch_gate_opa_url": "https://opa.dc1.example.internal:8181",
        "_launch_gate_opa_ip": "192.0.2.20",
        "secrets": {"semaphore_api_token": SEMAPHORE_TOKEN},
    }
    prod = _env_values(_render("env.j2", _launch_gate_local_mode=False, **common))
    assert prod["LAUNCH_GATE_OPA_HOSTNAME"] == "opa.dc1.example.internal"
    assert prod["LAUNCH_GATE_OPA_IP"] == "192.0.2.20"
    # Port and userinfo are not part of the host name the mapping needs.
    alt = dict(common, _launch_gate_opa_url="https://user@opa.dc1.example.internal:8181/base")
    assert _env_values(_render("env.j2", _launch_gate_local_mode=False, **alt))["LAUNCH_GATE_OPA_HOSTNAME"] == (
        "opa.dc1.example.internal"
    )
    # Local mode: neither key is rendered, and the URL and address are not read at all.
    local = _env_values(_render("env.j2", _launch_gate_local_mode=True, **common))
    assert not [key for key in local if key.startswith("LAUNCH_GATE_OPA_")], local


def test_config_template_renders_a_config_the_app_loads_and_accepts(tmp_path):
    out = _render(
        "config.json.j2",
        _launch_gate_semaphore_url="https://semaphore.invalid",
        _launch_gate_semaphore_project="2",
        _launch_gate_opa_url="https://opa.invalid:8181",
        _launch_gate_allowlist=["Harden SSH", "Harden SSH (Dev)"],
        _launch_gate_callers={"skynet": ["security-agent"]},
        _launch_gate_server_leaf="launch-gate-server",
        _launch_gate_local_mode=False,
    )
    path = tmp_path / "config.json"
    path.write_text(out)
    environ = {
        "LAUNCH_GATE_SEMAPHORE_TOKEN": "x" * 40,
        "LAUNCH_GATE_CALLER_TOKEN_SKYNET": "y" * 40,
    }
    config = app.load_config(str(path), environ)
    assert config.semaphore_project == 2
    assert config.allowlist == frozenset({"Harden SSH", "Harden SSH (Dev)"})
    assert config.callers == {"skynet": frozenset({"security-agent"})}
    assert config.listen_host == "0.0.0.0"
    assert config.listen_port == 8443
    assert config.tls_cert == "/certs/launch-gate-server/current/cert.pem"
    assert config.tls_key == "/certs/launch-gate-server/current/key.pem"
    assert config.opa_ca_bundle == "/certs/step-ca-bundle.crt"
    assert config.local_mode is False


def test_config_template_holds_exactly_the_keys_load_config_accepts():
    out = _render(
        "config.json.j2",
        _launch_gate_semaphore_url="https://semaphore.invalid",
        _launch_gate_semaphore_project=2,
        _launch_gate_opa_url="https://opa.invalid:8181",
        _launch_gate_allowlist=["A"],
        _launch_gate_callers={"skynet": ["security-agent"]},
        _launch_gate_server_leaf="launch-gate-server",
        _launch_gate_local_mode=True,
    )
    assert sorted(json.loads(out)) == sorted(
        [
            "semaphore_url",
            "semaphore_project",
            "opa_url",
            "opa_ca_bundle",
            "allowlist",
            "callers",
            "listen_host",
            "listen_port",
            "tls_cert",
            "tls_key",
            "local_mode",
        ]
    )
    assert json.loads(out)["local_mode"] is True


def test_the_templates_draw_only_on_underscore_or_secrets_variables():
    # test_credential_leaks.bats scans *.j2 for approved namespaces, but its regex `\{\{[\s]*` does not match
    # `{{ name` written with a space: in a POSIX bracket `[\s]` is a backslash or an `s`, not whitespace. CI's
    # GNU grep agrees: run 38731550214 (head 66c18f5b, Bash Tests) reports `ok 200 credential leak: Jinja2
    # templates use approved variable namespaces` while a template at that head carries `{{ <variable> |`. So a
    # bare variable written with a space passes that scan. Jinja's own parser lists the free variables instead.
    meta = pytest.importorskip("jinja2.meta")
    env = _jinja_env()
    for name in ("env.j2", "config.json.j2"):
        free = meta.find_undeclared_variables(env.parse((DEPLOY / "templates" / name).read_text()))
        assert free, f"{name}: no variables found, so the check would prove nothing"
        bad = sorted(var for var in free if var != "secrets" and not var.startswith("_"))
        assert not bad, f"{name} draws on variables outside the approved namespaces: {bad}"
        assert ("secrets" in free) == (name == "env.j2"), name
