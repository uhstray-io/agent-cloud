"""Unit tests for the launch gate's validation core. No network: the core does none, and so do these tests."""

import hmac
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "platform/services/launch-gate/deployment/app/launch_gate.py"


def _load_core():
    spec = importlib.util.spec_from_file_location("launch_gate_core", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclasses can resolve the module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lg = _load_core()

SEMA_TOKEN = "sema-" + "k" * 40
SKYNET_TOKEN = "skyn-" + "j" * 40
CONSOLE_TOKEN = "cons-" + "m" * 40
ENV = {
    "LAUNCH_GATE_SEMAPHORE_TOKEN": SEMA_TOKEN,
    "LAUNCH_GATE_CALLER_TOKEN_SKYNET": SKYNET_TOKEN,
    "LAUNCH_GATE_CALLER_TOKEN_CONSOLE": CONSOLE_TOKEN,
}
TEMPLATE = "Deploy agentgateway (Dev)"
REQUIRED_CONFIG_KEYS = [
    "semaphore_url",
    "semaphore_project",
    "opa_url",
    "allowlist",
    "callers",
    "listen_host",
    "listen_port",
    "tls_cert",
    "tls_key",
]


def _config_doc():
    return {
        "semaphore_url": "https://semaphore.internal.test/",
        "semaphore_project": 2,
        "opa_url": "https://opa.internal.test:8181/",
        "allowlist": ["Deploy agentgateway (Dev)", "Check o11y Liveness (Dev)"],
        "callers": {"skynet": ["infra-agent", "service-agent"], "console": []},
        "listen_host": "0.0.0.0",
        "listen_port": 8443,
        "tls_cert": "/certs/launch-gate-server/current/cert.pem",
        "tls_key": "/certs/launch-gate-server/current/key.pem",
    }


def _load(tmp_path, doc=None, env=None):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config_doc() if doc is None else doc), encoding="utf-8")
    return lg.load_config(str(path), dict(ENV if env is None else env))


@pytest.fixture
def cfg(tmp_path):
    return _load(tmp_path)


def _doc(**changes):
    doc = {"template": TEMPLATE, "agent": "service-agent", "workflow_id": "onboard-x", "step": "ssh-keys"}
    doc.update(changes)
    return doc


def _raw(doc):
    return json.dumps(doc).encode("utf-8")


def _req(**changes):
    base = {
        "template": TEMPLATE,
        "survey": {},
        "agent": "service-agent",
        "workflow_id": "onboard-x",
        "step": "ssh-keys",
        "dry_run": False,
        "proposal": None,
        "context": None,
    }
    base.update(changes)
    return lg.LaunchRequest(**base)


def _tpl(survey_vars):
    return {"name": TEMPLATE, "id": 12, "survey_vars": survey_vars}


# ---------- constants and refusal shape ----------


def test_constants_match_the_reviewed_values():
    expected_terminal = frozenset({"success", "error", "stopped"})
    expected_versions = frozenset({"v2.17.31", "v2.18.12", "v2.19.11"})
    assert (expected_terminal, expected_versions) == (lg.TERMINAL, lg.VERIFIED_DRY_RUN_VERSIONS)
    expected_limits = (4096, 65536)
    assert expected_limits == (lg.MAX_VALUE, lg.MAX_BODY)


def test_refusal_carries_status_code_and_detail():
    exc = lg.Refusal(409, "duplicate", "task 7 still running")
    assert (exc.status, exc.code, exc.detail) == (409, "duplicate", "task 7 still running")
    assert str(exc) == "task 7 still running"


# ---------- load_config ----------


def test_load_config_valid(cfg):
    assert cfg.semaphore_url == "https://semaphore.internal.test"
    assert cfg.opa_url == "https://opa.internal.test:8181"
    assert cfg.semaphore_project == 2
    assert cfg.semaphore_token == SEMA_TOKEN
    assert cfg.opa_ca_bundle is None
    assert cfg.allowlist == frozenset({"Deploy agentgateway (Dev)", "Check o11y Liveness (Dev)"})
    assert cfg.callers == {"skynet": frozenset({"infra-agent", "service-agent"}), "console": frozenset()}
    assert cfg.caller_tokens == {"skynet": SKYNET_TOKEN, "console": CONSOLE_TOKEN}
    assert cfg.listen_port == 8443
    assert cfg.local_mode is False


def test_optional_fields_when_given(tmp_path):
    doc = _config_doc()
    doc.update(
        opa_ca_bundle="/certs/step-ca-bundle.crt",
        local_mode=True,
        opa_url="http://127.0.0.1:8181",
        semaphore_url="http://127.0.0.1:3000",
    )
    cfg = _load(tmp_path, doc)
    assert cfg.opa_ca_bundle == "/certs/step-ca-bundle.crt"
    assert cfg.local_mode is True
    assert cfg.opa_url == "http://127.0.0.1:8181"


def test_unknown_config_key_refused_by_name(tmp_path):
    doc = _config_doc()
    doc["extra_knob"] = 1
    with pytest.raises(lg.ConfigError, match="extra_knob"):
        _load(tmp_path, doc)


@pytest.mark.parametrize("key", REQUIRED_CONFIG_KEYS)
def test_missing_required_config_key_refused(tmp_path, key):
    doc = _config_doc()
    del doc[key]
    with pytest.raises(lg.ConfigError, match=key):
        _load(tmp_path, doc)


def test_config_file_must_be_json_object(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(lg.ConfigError, match="JSON object"):
        lg.load_config(str(path), ENV)


def test_config_file_not_json_refused(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(lg.ConfigError, match="not JSON"):
        lg.load_config(str(path), ENV)


def test_config_file_missing_refused(tmp_path):
    with pytest.raises(lg.ConfigError, match="unreadable"):
        lg.load_config(str(tmp_path / "absent.json"), ENV)


@pytest.mark.parametrize("value", [0, -1, "7", True, 2.0, None])
def test_semaphore_project_must_be_positive_int(tmp_path, value):
    doc = _config_doc()
    doc["semaphore_project"] = value
    with pytest.raises(lg.ConfigError, match="semaphore_project"):
        _load(tmp_path, doc)


@pytest.mark.parametrize("value", [0, 65536, "443", True, 443.0])
def test_listen_port_must_be_1_to_65535(tmp_path, value):
    doc = _config_doc()
    doc["listen_port"] = value
    with pytest.raises(lg.ConfigError, match="listen_port"):
        _load(tmp_path, doc)


def test_listen_port_boundaries_accepted(tmp_path):
    for port in (1, 65535):
        doc = _config_doc()
        doc["listen_port"] = port
        assert _load(tmp_path, doc).listen_port == port


@pytest.mark.parametrize("value", ["Deploy agentgateway (Dev)", [""], [1], ["ok", 3]])
def test_allowlist_must_be_list_of_nonempty_strings(tmp_path, value):
    doc = _config_doc()
    doc["allowlist"] = value
    with pytest.raises(lg.ConfigError, match="allowlist"):
        _load(tmp_path, doc)


@pytest.mark.parametrize("name", ["Skynet", "1abc", "a" * 33, "a_b", "skynet\n", ""])
def test_caller_name_must_match_pattern(tmp_path, name):
    doc = _config_doc()
    doc["callers"] = {name: []}
    with pytest.raises(lg.ConfigError, match="caller names"):
        _load(tmp_path, doc, {"LAUNCH_GATE_SEMAPHORE_TOKEN": SEMA_TOKEN})


def test_caller_name_length_boundary_accepted(tmp_path):
    name = "a" * 32
    doc = _config_doc()
    doc["callers"] = {name: []}
    env = {"LAUNCH_GATE_SEMAPHORE_TOKEN": SEMA_TOKEN, "LAUNCH_GATE_CALLER_TOKEN_" + "A" * 32: SKYNET_TOKEN}
    assert _load(tmp_path, doc, env).caller_tokens == {name: SKYNET_TOKEN}


@pytest.mark.parametrize("agents", ["infra-agent", [""], [3], None])
def test_caller_agents_must_be_list_of_nonempty_strings(tmp_path, agents):
    doc = _config_doc()
    doc["callers"] = {"skynet": agents}
    with pytest.raises(lg.ConfigError, match="callers.skynet"):
        _load(tmp_path, doc)


def test_callers_must_be_object(tmp_path):
    doc = _config_doc()
    doc["callers"] = ["skynet"]
    with pytest.raises(lg.ConfigError, match="callers must be"):
        _load(tmp_path, doc)


def test_caller_token_variable_is_named_when_missing(tmp_path):
    env = {"LAUNCH_GATE_SEMAPHORE_TOKEN": SEMA_TOKEN, "LAUNCH_GATE_CALLER_TOKEN_SKYNET": SKYNET_TOKEN}
    with pytest.raises(lg.ConfigError, match="LAUNCH_GATE_CALLER_TOKEN_CONSOLE is missing or empty") as info:
        _load(tmp_path, env=env)
    assert SKYNET_TOKEN not in str(info.value)


def test_caller_token_variable_name_maps_hyphen_to_underscore(tmp_path):
    doc = _config_doc()
    doc["callers"] = {"my-agent": []}
    env = {"LAUNCH_GATE_SEMAPHORE_TOKEN": SEMA_TOKEN}
    with pytest.raises(lg.ConfigError, match="LAUNCH_GATE_CALLER_TOKEN_MY_AGENT"):
        _load(tmp_path, doc, env)
    env["LAUNCH_GATE_CALLER_TOKEN_MY_AGENT"] = SKYNET_TOKEN
    assert _load(tmp_path, doc, env).caller_tokens == {"my-agent": SKYNET_TOKEN}


def test_empty_caller_token_refused(tmp_path):
    env = dict(ENV, LAUNCH_GATE_CALLER_TOKEN_CONSOLE="")
    with pytest.raises(lg.ConfigError, match="LAUNCH_GATE_CALLER_TOKEN_CONSOLE is missing or empty"):
        _load(tmp_path, env=env)


@pytest.mark.parametrize(("length", "accepted"), [(31, False), (32, True)])
def test_caller_token_minimum_length(tmp_path, length, accepted):
    env = dict(ENV, LAUNCH_GATE_CALLER_TOKEN_CONSOLE="c" * length)
    if accepted:
        assert _load(tmp_path, env=env).caller_tokens["console"] == "c" * 32
    else:
        with pytest.raises(lg.ConfigError, match="LAUNCH_GATE_CALLER_TOKEN_CONSOLE is shorter than 32"):
            _load(tmp_path, env=env)


def test_semaphore_token_missing_and_short_refused(tmp_path):
    env = dict(ENV)
    del env["LAUNCH_GATE_SEMAPHORE_TOKEN"]
    with pytest.raises(lg.ConfigError, match="LAUNCH_GATE_SEMAPHORE_TOKEN is missing or empty"):
        _load(tmp_path, env=env)
    with pytest.raises(lg.ConfigError, match="LAUNCH_GATE_SEMAPHORE_TOKEN is shorter than 32"):
        _load(tmp_path, env=dict(ENV, LAUNCH_GATE_SEMAPHORE_TOKEN="s" * 31))


def test_duplicate_caller_token_refused_without_printing_it(tmp_path):
    env = dict(ENV, LAUNCH_GATE_CALLER_TOKEN_CONSOLE=SKYNET_TOKEN)
    with pytest.raises(lg.ConfigError) as info:
        _load(tmp_path, env=env)
    message = str(info.value)
    assert "LAUNCH_GATE_CALLER_TOKEN_CONSOLE repeats the token of LAUNCH_GATE_CALLER_TOKEN_SKYNET" in message
    assert SKYNET_TOKEN not in message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("opa_url", "http://opa.internal.test:8181"),
        ("semaphore_url", "http://semaphore.internal.test"),
        ("opa_url", "ftp://opa.internal.test"),
        ("opa_url", "https://"),
        ("semaphore_url", "javascript:alert(1)"),
        ("opa_url", ""),
    ],
)
def test_urls_must_be_https_unless_local_mode(tmp_path, field, value):
    doc = _config_doc()
    doc[field] = value
    with pytest.raises(lg.ConfigError, match=field):
        _load(tmp_path, doc)


def test_local_mode_must_be_bool(tmp_path):
    doc = _config_doc()
    doc["local_mode"] = "yes"
    with pytest.raises(lg.ConfigError, match="local_mode"):
        _load(tmp_path, doc)


@pytest.mark.parametrize("value", ["", 5])
def test_opa_ca_bundle_must_be_nonempty_string_or_null(tmp_path, value):
    doc = _config_doc()
    doc["opa_ca_bundle"] = value
    with pytest.raises(lg.ConfigError, match="opa_ca_bundle"):
        _load(tmp_path, doc)


def test_repr_hides_every_token(cfg):
    for text in (repr(cfg), str(cfg)):
        assert SEMA_TOKEN not in text
        assert SKYNET_TOKEN not in text
        assert CONSOLE_TOKEN not in text
    assert "<redacted>" in repr(cfg)
    assert "semaphore.internal.test" in repr(cfg)


# ---------- authenticate ----------


def test_authenticate_returns_the_matching_caller(cfg):
    assert lg.authenticate(f"Bearer {SKYNET_TOKEN}", cfg) == "skynet"
    assert lg.authenticate(f"Bearer {CONSOLE_TOKEN}", cfg) == "console"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer ",
        "Bearer",
        SKYNET_TOKEN,
        f"Basic {SKYNET_TOKEN}",
        f"bearer {SKYNET_TOKEN}",
        f"Bearer {SKYNET_TOKEN[:-1]}",
        f"Bearer {SKYNET_TOKEN}x",
        f"Bearer {SKYNET_TOKEN} ",
        "Bearer " + "z" * len(SKYNET_TOKEN),
        "Bearer " + "é" * 45,
    ],
)
def test_authenticate_refuses_missing_or_wrong_token(cfg, header):
    with pytest.raises(lg.Refusal) as info:
        lg.authenticate(header, cfg)
    assert (info.value.status, info.value.code) == (401, "unauthorized")
    assert info.value.detail == "missing or invalid bearer token"


@pytest.mark.parametrize("token", [SKYNET_TOKEN, CONSOLE_TOKEN])
def test_authenticate_compares_every_token_without_early_exit(cfg, monkeypatch, token):
    calls = []
    real_compare = hmac.compare_digest

    def spy(a, b):
        calls.append(b)
        return real_compare(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    lg.authenticate(f"Bearer {token}", cfg)
    assert len(calls) == len(cfg.caller_tokens) == 2


# ---------- parse_request ----------


def test_parse_minimal_request_fills_defaults(cfg):
    req = lg.parse_request(_raw(_doc()))
    assert req == lg.LaunchRequest(
        template=TEMPLATE,
        survey={},
        agent="service-agent",
        workflow_id="onboard-x",
        step="ssh-keys",
        dry_run=False,
        proposal=None,
        context=None,
    )


def test_parse_full_request():
    body = _raw(
        _doc(
            survey={"service_name": "tududi", "branch": "dev"},
            dry_run=True,
            proposal={"hosts": 1},
            context={"groups": ["platform-admins"]},
        )
    )
    req = lg.parse_request(body)
    assert req.survey == {"service_name": "tududi", "branch": "dev"}
    assert req.dry_run is True
    assert req.proposal == {"hosts": 1}
    assert req.context == {"groups": ["platform-admins"]}


def test_parse_refuses_non_utf8_body():
    with pytest.raises(lg.Refusal) as info:
        lg.parse_request(b"\xff\xfe{}")
    assert (info.value.status, info.value.code) == (400, "bad_request")


def test_parse_refuses_invalid_json():
    with pytest.raises(lg.Refusal, match="not UTF-8 JSON") as info:
        lg.parse_request(b"{not json")
    assert info.value.status == 400


def test_parse_refuses_deeply_nested_json_as_bad_request():
    with pytest.raises(lg.Refusal) as info:
        lg.parse_request(b"[" * 100000 + b"]" * 100000)
    assert info.value.status == 400


def test_parse_refuses_non_object_body():
    with pytest.raises(lg.Refusal, match="JSON object") as info:
        lg.parse_request(b"[]")
    assert info.value.status == 400


@pytest.mark.parametrize("key", ["inventory_id", "limit", "arguments", "git_branch", "human_approved", "tags"])
def test_parse_refuses_unknown_key_by_name(key):
    with pytest.raises(lg.Refusal, match=key) as info:
        lg.parse_request(_raw(_doc(**{key: "x"})))
    assert info.value.status == 400
    assert info.value.code == "bad_request"


@pytest.mark.parametrize("field", ["template", "agent", "workflow_id", "step"])
def test_parse_refuses_missing_required_field(field):
    doc = _doc()
    del doc[field]
    with pytest.raises(lg.Refusal, match=field) as info:
        lg.parse_request(_raw(doc))
    assert info.value.status == 400


@pytest.mark.parametrize(
    ("field", "value"),
    [("template", 1), ("agent", None), ("workflow_id", ["x"]), ("step", True)],
)
def test_parse_refuses_non_string_required_field(field, value):
    with pytest.raises(lg.Refusal, match=field):
        lg.parse_request(_raw(_doc(**{field: value})))


def test_parse_refuses_empty_template():
    with pytest.raises(lg.Refusal, match="template must be 1 to 200"):
        lg.parse_request(_raw(_doc(template="")))


def test_parse_template_length_boundary():
    assert lg.parse_request(_raw(_doc(template="t" * 200))).template == "t" * 200
    with pytest.raises(lg.Refusal, match="template must be 1 to 200"):
        lg.parse_request(_raw(_doc(template="t" * 201)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent", "infra agent"),
        ("agent", "-agent"),
        ("agent", "a" * 129),
        ("agent", "infra-agent\n"),
        ("workflow_id", "../etc"),
        ("workflow_id", "a;b"),
        ("step", ""),
        ("step", "ssh keys"),
        ("step", "x" * 129),
    ],
)
def test_parse_refuses_malformed_identifier(field, value):
    with pytest.raises(lg.Refusal, match=field):
        lg.parse_request(_raw(_doc(**{field: value})))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent", "o11y-agent"),
        ("step", "a" * 128),
        ("workflow_id", "service-onboarding:3f2a1c9e-0000-4000-8000-000000000000"),
    ],
)
def test_parse_accepts_well_formed_identifier(field, value):
    assert getattr(lg.parse_request(_raw(_doc(**{field: value}))), field) == value


@pytest.mark.parametrize("survey", [["a"], "x", None, 1])
def test_parse_refuses_survey_that_is_not_an_object(survey):
    with pytest.raises(lg.Refusal, match="survey must be a JSON object"):
        lg.parse_request(_raw(_doc(survey=survey)))


@pytest.mark.parametrize("key", ["Bad-Key", "1abc", "a" * 65, "k\n", "has space"])
def test_parse_refuses_malformed_survey_key(key):
    with pytest.raises(lg.Refusal) as info:
        lg.parse_request(_raw(_doc(survey={key: "v"})))
    assert info.value.status == 400


def test_parse_survey_key_length_boundary():
    req = lg.parse_request(_raw(_doc(survey={"a" * 64: "v"})))
    assert list(req.survey) == ["a" * 64]


@pytest.mark.parametrize("value", [True, 1, None, [], {}, 1.5])
def test_parse_refuses_non_string_survey_value(value):
    with pytest.raises(lg.Refusal, match="survey field service_name must be a string"):
        lg.parse_request(_raw(_doc(survey={"service_name": value})))


def test_parse_survey_value_length_boundary():
    assert lg.parse_request(_raw(_doc(survey={"k": "a" * 4096}))).survey["k"] == "a" * 4096
    with pytest.raises(lg.Refusal, match="survey field k is longer than 4096"):
        lg.parse_request(_raw(_doc(survey={"k": "a" * 4097})))


@pytest.mark.parametrize("marker", ["{{", "{%", "{#"])
def test_parse_refuses_template_marker_anywhere_in_value(marker):
    with pytest.raises(lg.Refusal, match="survey field branch contains a template marker") as info:
        lg.parse_request(_raw(_doc(survey={"branch": f"dev{marker}x"})))
    assert info.value.status == 400


def test_parse_template_marker_refusal_never_echoes_the_value():
    with pytest.raises(lg.Refusal) as info:
        lg.parse_request(_raw(_doc(survey={"branch": "secret-marker{{ PAYLOAD }}"})))
    assert "PAYLOAD" not in str(info.value)
    assert "secret-marker" not in str(info.value)
    assert "branch" in str(info.value)


def test_parse_single_braces_and_lone_closers_are_literal():
    req = lg.parse_request(_raw(_doc(survey={"a": "x{b}y", "b": "{}", "c": "}}"})))
    assert req.survey == {"a": "x{b}y", "b": "{}", "c": "}}"}


@pytest.mark.parametrize("value", ["yes", 1, None, "true"])
def test_parse_refuses_non_bool_dry_run(value):
    with pytest.raises(lg.Refusal, match="dry_run must be true or false"):
        lg.parse_request(_raw(_doc(dry_run=value)))


def test_parse_dry_run_true_accepted():
    assert lg.parse_request(_raw(_doc(dry_run=True))).dry_run is True


@pytest.mark.parametrize("key", ["proposal", "context"])
@pytest.mark.parametrize("value", [None, ["x"], "x", 1])
def test_parse_refuses_present_non_object_proposal_or_context(key, value):
    with pytest.raises(lg.Refusal, match=f"{key} must be a JSON object when present"):
        lg.parse_request(_raw(_doc(**{key: value})))


def test_parse_absent_proposal_is_none_and_empty_object_is_kept():
    assert lg.parse_request(_raw(_doc())).proposal is None
    assert lg.parse_request(_raw(_doc(proposal={}))).proposal == {}


# ---------- authorize_local ----------


def test_authorize_local_allows_permitted_agent_and_template(cfg):
    assert lg.authorize_local("skynet", _req(), cfg) is None


def test_authorize_local_refuses_agent_outside_callers_set(cfg):
    with pytest.raises(lg.Refusal) as info:
        lg.authorize_local("skynet", _req(agent="security-agent"), cfg)
    assert (info.value.status, info.value.code) == (403, "agent_not_permitted")


def test_authorize_local_console_cannot_act_as_skynets_agents(cfg):
    with pytest.raises(lg.Refusal) as info:
        lg.authorize_local("console", _req(agent="infra-agent"), cfg)
    assert info.value.code == "agent_not_permitted"


def test_authorize_local_unknown_caller_refused(cfg):
    with pytest.raises(lg.Refusal) as info:
        lg.authorize_local("ghost", _req(), cfg)
    assert info.value.code == "agent_not_permitted"


def test_authorize_local_refuses_template_off_allowlist(cfg):
    with pytest.raises(lg.Refusal) as info:
        lg.authorize_local("skynet", _req(template="Destroy VM"), cfg)
    assert (info.value.status, info.value.code) == (403, "template_not_allowed")


def test_authorize_local_checks_agent_before_template(cfg):
    with pytest.raises(lg.Refusal) as info:
        lg.authorize_local("skynet", _req(agent="security-agent", template="Destroy VM"), cfg)
    assert info.value.code == "agent_not_permitted"


# ---------- merge_survey ----------


@pytest.mark.parametrize("survey_vars", [None, []])
def test_merge_survey_no_declared_fields(survey_vars):
    assert lg.merge_survey(_tpl(survey_vars), {}) == {}


def test_merge_survey_template_without_survey_key():
    assert lg.merge_survey({"name": TEMPLATE, "id": 12}, {}) == {}


def test_merge_survey_refuses_undeclared_keys_sorted_and_named():
    with pytest.raises(lg.Refusal) as info:
        lg.merge_survey(_tpl([{"name": "branch"}]), {"branch": "x", "zeta": "1", "alpha": "2"})
    assert (info.value.status, info.value.code) == (400, "undeclared_field")
    assert "alpha, zeta" in info.value.detail
    assert "branch" not in info.value.detail


def test_merge_survey_refuses_any_key_when_no_fields_declared():
    with pytest.raises(lg.Refusal, match="undeclared_field|not survey fields") as info:
        lg.merge_survey(_tpl(None), {"x": "1"})
    assert info.value.code == "undeclared_field"


def test_merge_survey_fills_declared_default():
    assert lg.merge_survey(_tpl([{"name": "branch", "default_value": "dev"}]), {}) == {"branch": "dev"}


def test_merge_survey_does_not_fill_empty_or_missing_default():
    survey_vars = [{"name": "a", "default_value": ""}, {"name": "b", "default_value": None}, {"name": "c"}]
    assert lg.merge_survey(_tpl(survey_vars), {}) == {}


def test_merge_survey_explicit_value_wins_over_default():
    survey_vars = [{"name": "branch", "default_value": "dev"}]
    assert lg.merge_survey(_tpl(survey_vars), {"branch": "feat"}) == {"branch": "feat"}


@pytest.mark.parametrize("default", [True, 0, ["x"]])
def test_merge_survey_refuses_non_string_default_when_filled(default):
    with pytest.raises(lg.Refusal) as info:
        lg.merge_survey(_tpl([{"name": "n", "default_value": default}]), {})
    assert (info.value.status, info.value.code) == (502, "bad_default")


def test_merge_survey_non_string_default_never_read_when_explicit():
    assert lg.merge_survey(_tpl([{"name": "n", "default_value": 3}]), {"n": "5"}) == {"n": "5"}


def test_merge_survey_required_missing_refused():
    with pytest.raises(lg.Refusal) as info:
        lg.merge_survey(_tpl([{"name": "branch", "required": True}]), {})
    assert (info.value.status, info.value.code) == (400, "missing_required")
    assert "branch" in info.value.detail


def test_merge_survey_required_empty_string_refused():
    with pytest.raises(lg.Refusal, match="missing_required|has no value") as info:
        lg.merge_survey(_tpl([{"name": "branch", "required": True}]), {"branch": ""})
    assert info.value.code == "missing_required"


def test_merge_survey_required_satisfied_by_default_or_explicit_value():
    assert lg.merge_survey(_tpl([{"name": "b", "required": True, "default_value": "dev"}]), {}) == {"b": "dev"}
    assert lg.merge_survey(_tpl([{"name": "b", "required": True}]), {"b": "x"}) == {"b": "x"}


def test_merge_survey_required_only_when_exactly_true():
    assert lg.merge_survey(_tpl([{"name": "x", "required": "yes"}]), {}) == {}


@pytest.mark.parametrize(
    "survey_vars",
    ["branch", "", {}, {"name": "x"}, [{"default_value": "x"}], [{"name": 3}], [None], ["branch"]],
)
def test_merge_survey_unreadable_survey_fields_refused(survey_vars):
    with pytest.raises(lg.Refusal) as info:
        lg.merge_survey(_tpl(survey_vars), {})
    assert (info.value.status, info.value.code) == (502, "semaphore_unreadable")


# ---------- survey value types ----------


@pytest.mark.parametrize("kind", ["int", "integer"])
@pytest.mark.parametrize("value", ["5", "0", "-7", "007", "123456789012345678901234567890"])
def test_integer_field_accepts_plain_digits_with_an_optional_minus(kind, value):
    assert lg.merge_survey(_tpl([{"name": "n", "type": kind}]), {"n": value}) == {"n": value}


@pytest.mark.parametrize("kind", ["int", "integer"])
@pytest.mark.parametrize(
    "value",
    ["1.5", "x", "+1", "", "-", "--1", " 5", "5 ", "5\n", "1_0", "١٢"],  # last: Arabic-Indic digits
)
def test_integer_field_refuses_anything_else_and_names_only_the_field(kind, value):
    with pytest.raises(lg.Refusal) as info:
        lg.merge_survey(_tpl([{"name": "n", "type": kind}]), {"n": value})
    assert (info.value.status, info.value.code) == (400, "bad_value")
    assert info.value.detail == "survey field n must be an integer"


def test_integer_default_is_checked_when_filled():
    assert lg.merge_survey(_tpl([{"name": "n", "type": "int", "default_value": "3"}]), {}) == {"n": "3"}
    with pytest.raises(lg.Refusal, match="must be an integer"):
        lg.merge_survey(_tpl([{"name": "n", "type": "int", "default_value": "three"}]), {})


ENUM_VARS = [
    {
        "name": "mode",
        "type": "enum",
        "default_value": "logs",
        "values": [{"name": "Logs", "value": "logs"}, {"name": "Metrics", "value": "metrics"}],
    }
]


def test_enum_field_accepts_one_of_its_declared_values():
    assert lg.merge_survey(_tpl(ENUM_VARS), {"mode": "metrics"}) == {"mode": "metrics"}


def test_enum_field_fills_its_declared_default():
    assert lg.merge_survey(_tpl(ENUM_VARS), {}) == {"mode": "logs"}


@pytest.mark.parametrize("value", ["Logs", "logs ", "", "traces", "METRICS"])
def test_enum_field_refuses_a_value_outside_its_declared_values(value):
    with pytest.raises(lg.Refusal) as info:
        lg.merge_survey(_tpl(ENUM_VARS), {"mode": value})
    assert (info.value.status, info.value.code) == (400, "bad_value")
    assert info.value.detail == "survey field mode is not one of its declared values"


def test_enum_field_with_no_declared_values_refuses_every_value():
    with pytest.raises(lg.Refusal, match="not one of its declared values"):
        lg.merge_survey(_tpl([{"name": "mode", "type": "enum", "values": []}]), {"mode": "logs"})


@pytest.mark.parametrize(
    "options",
    [None, "logs", [{"name": "Logs"}], ["logs"], [{"value": 3}], [{"value": None}]],
)
def test_enum_field_with_unreadable_values_is_502(options):
    with pytest.raises(lg.Refusal) as info:
        lg.merge_survey(_tpl([{"name": "mode", "type": "enum", "values": options}]), {"mode": "logs"})
    assert (info.value.status, info.value.code) == (502, "semaphore_unreadable")


@pytest.mark.parametrize("kind", ["secret", ["int"], 3, {"x": 1}])
def test_unknown_or_unreadable_field_type_is_502_when_a_value_is_present(kind):
    with pytest.raises(lg.Refusal) as info:
        lg.merge_survey(_tpl([{"name": "n", "type": kind}]), {"n": "x"})
    assert (info.value.status, info.value.code) == (502, "unsupported_field_type")
    assert info.value.detail == f"survey field n of {TEMPLATE} has an unsupported type"


def test_unknown_field_type_is_not_read_when_no_value_is_present():
    assert lg.merge_survey(_tpl([{"name": "n", "type": "secret"}]), {}) == {}


@pytest.mark.parametrize("kind", ["string", "text", "", None])
def test_string_fields_take_any_string_untouched(kind):
    field = {"name": "n"} if kind is None else {"name": "n", "type": kind}
    for value in ("1.5", "x", "", "a b\nc"):
        assert lg.merge_survey(_tpl([field]), {"n": value}) == {"n": value}


def test_fields_without_a_value_are_not_checked_against_their_type():
    survey_vars = [{"name": "n", "type": "int"}, {"name": "m", "type": "enum", "values": None}]
    assert lg.merge_survey(_tpl(survey_vars), {}) == {}


# ---------- pin_branch ----------

BRANCH_VARS = [{"name": "service_branch", "type": "string", "default_value": "main"}]


def test_pin_branch_without_a_branch_field_returns_the_record_branch_and_values_unchanged():
    assert lg.pin_branch(_tpl([]), {}, {"x": "1"}, "dev") == ("dev", {"x": "1"})


def test_pin_branch_overrides_a_declared_default_with_the_record_branch():
    template = _tpl(BRANCH_VARS)
    values = lg.merge_survey(template, {})
    assert values == {"service_branch": "main"}
    assert lg.pin_branch(template, {}, values, "dev") == ("dev", {"service_branch": "dev"})


def test_pin_branch_fills_a_declared_field_that_has_no_default():
    assert lg.pin_branch(_tpl([{"name": "service_branch"}]), {}, {}, "dev") == ("dev", {"service_branch": "dev"})


def test_pin_branch_accepts_a_caller_value_equal_to_the_record_branch():
    template = _tpl(BRANCH_VARS)
    survey = {"service_branch": "dev"}
    values = lg.merge_survey(template, survey)
    assert lg.pin_branch(template, survey, values, "dev") == ("dev", {"service_branch": "dev"})


@pytest.mark.parametrize("value", ["feature-x", "main", "", "dev "])
def test_pin_branch_refuses_a_caller_value_other_than_the_record_branch(value):
    template = _tpl(BRANCH_VARS)
    survey = {"service_branch": value}
    values = lg.merge_survey(template, survey)
    with pytest.raises(lg.Refusal) as info:
        lg.pin_branch(template, survey, values, "dev")
    assert (info.value.status, info.value.code) == (400, "branch_mismatch")
    assert info.value.detail == "service_branch must be the template's branch dev"


@pytest.mark.parametrize("extra", [{}, {"git_branch": None}, {"git_branch": ""}, {"git_branch": "dev"}])
def test_pin_branch_accepts_a_template_with_no_branch_of_its_own_or_the_record_branch(extra):
    template = {"name": TEMPLATE, "id": 12, "survey_vars": [], **extra}
    assert lg.pin_branch(template, {}, {}, "dev") == ("dev", {})


def test_pin_branch_refuses_a_template_branch_that_differs_from_the_record():
    template = {"name": TEMPLATE, "id": 12, "git_branch": "other", "survey_vars": []}
    with pytest.raises(lg.Refusal) as info:
        lg.pin_branch(template, {}, {}, "dev")
    assert (info.value.status, info.value.code) == (502, "branch_ambiguous")


@pytest.mark.parametrize("own", [3, True, ["dev"]])
def test_pin_branch_refuses_an_unreadable_template_branch(own):
    template = {"name": TEMPLATE, "id": 12, "git_branch": own, "survey_vars": []}
    with pytest.raises(lg.Refusal) as info:
        lg.pin_branch(template, {}, {}, "dev")
    assert (info.value.status, info.value.code) == (502, "semaphore_unreadable")


# ---------- opa_input ----------


def test_opa_input_exact_shape_without_optional_fields():
    values = {"branch": "dev"}
    out = lg.opa_input(_req(), "dev", values)
    assert out == {
        "agent": "service-agent",
        "service": "semaphore",
        "action": "run_task",
        "template_name": TEMPLATE,
        "human_approved": False,
        "step": "ssh-keys",
        "git_branch": "dev",
        "settings": {"branch": "dev"},
    }
    assert out["settings"] is not values


def test_opa_input_adds_proposal_and_context_when_present():
    out = lg.opa_input(_req(proposal={"hosts": 1}, context={"groups": []}), "main", {})
    assert out["proposal"] == {"hosts": 1}
    assert out["context"] == {"groups": []}
    assert out["settings"] == {}


def test_opa_input_keeps_empty_proposal_object():
    assert lg.opa_input(_req(proposal={}), "main", {})["proposal"] == {}


def test_opa_input_never_carries_caller_controlled_fields():
    out = lg.opa_input(_req(dry_run=True, survey={}), "main", {})
    assert out["human_approved"] is False
    assert out["service"] == "semaphore"
    assert out["action"] == "run_task"
    assert "dry_run" not in out
    assert "workflow_id" not in out


# ---------- task_message and task_body ----------


def test_task_message_matches_skynet_tag_form():
    assert lg.task_message("skynet", "onboard-x", "ssh-keys") == "skynet onboard-x ssh-keys"


def test_task_body_without_values_or_dry_run():
    assert lg.task_body(12, 2, {}, dry_run=False, message="skynet onboard-x ssh-keys") == {
        "project_id": 2,
        "template_id": 12,
        "message": "skynet onboard-x ssh-keys",
    }


def test_task_body_with_values_is_sorted_json_environment():
    body = lg.task_body(12, 2, {"b": "2", "a": "1"}, dry_run=False, message="m")
    assert body == {
        "project_id": 2,
        "template_id": 12,
        "message": "m",
        "environment": '{"a": "1", "b": "2"}',
    }


def test_task_body_dry_run_sets_check_mode_params_only():
    assert lg.task_body(12, 2, {}, dry_run=True, message="m") == {
        "project_id": 2,
        "template_id": 12,
        "message": "m",
        "params": {"dry_run": True, "diff": True},
    }


def test_task_body_dry_run_with_values():
    body = lg.task_body(12, 2, {"a": "1"}, dry_run=True, message="m")
    assert body == {
        "project_id": 2,
        "template_id": 12,
        "message": "m",
        "environment": '{"a": "1"}',
        "params": {"dry_run": True, "diff": True},
    }


def test_task_body_never_carries_overrides():
    body = lg.task_body(12, 2, {"a": "1"}, dry_run=True, message="m")
    assert set(body) == {"project_id", "template_id", "message", "environment", "params"}
    for forbidden in ("git_branch", "arguments", "limit", "inventory_id", "playbook", "environment_id", "tags"):
        assert forbidden not in body
