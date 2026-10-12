"""probe-agentgateway-gate.yml: the read-only gate probe for the route switch
(inference-gateway-agentgateway tasks 4.5 and 6.4, and the runbook's open /health item).

The playbook runs for real against the local TLS listener of test_agw_client_tls_probe.py, which
requires a client certificate from a synthetic CA and applies the gateway's SAN-then-key order. The
verifier leaf is the allowlisted one there, so the key check answers. `Listener.script` gives each
request in turn its own status, which is how /health (and an unhealthy gateway) is simulated.
"""

# ruff: noqa: F811
import json
import sys
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml
from test_agw_client_tls_probe import (  # noqa: F401  (pki and listener are fixtures; F811 below is their use as arguments)
    BODY,
    DEPLOY_SUBDIR,
    VALID_KEY,
    _bundle,
    listener,
    pki,
)

REPO = playbook_yaml.REPO
PLAYBOOK = REPO / "platform/playbooks/probe-agentgateway-gate.yml"
VERIFIER = "agw-verifier"


def _leaves(pki: Path) -> list[dict]:
    # the verifier leaf is the allowlisted client leaf of the shared fixture (SAN caddy.example.internal)
    return [{"name": VERIFIER, "host": "gw", "profile": "client", "dir": str(pki / "leaves/caddy"),
             "sans": ["caddy.example.internal"]}]


def _run(tmp: Path, pki: Path, base: str | None, *, host=None, check=False):
    mono = _bundle(tmp, pki)
    host_vars = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
                 "agw_listener_tls": True, "internal_leaves": _leaves(pki), "local_monorepo_dir": str(mono),
                 **({"agw_verify_base_url": base} if base else {}), **(host or {})}
    inv = {"all": {"children": {"agentgateway_svc": {"hosts": {"gw": host_vars}}}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    cmd = ["ansible-playbook", "-v", "-i", str(tmp / "inv.yml"), str(PLAYBOOK), *(["--check"] if check else [])]
    env = {**harness_sandbox.env_for(tmp), "ANSIBLE_STDOUT_CALLBACK": "default"}
    r = harness_sandbox.run(cmd, tmp, cwd=REPO, env=env, timeout=300)
    return r.returncode, r.stdout + r.stderr


def _no_leak(out: str):
    assert BODY not in out
    assert "Bearer" not in out
    assert "drill-invalid-key" not in out
    assert VALID_KEY not in out


def _url(gw) -> str:
    return f"https://localhost:{gw.server_port}"


# ── the three probes ──────────────────────────────────────────────────────────

def test_a_gateway_that_refuses_both_keys_passes_and_the_health_answer_is_recorded(tmp_path, pki, listener):
    # san-first with the admitted verifier leaf: no key and a wrong key are 401, and so is /health
    gw = listener("san-first")
    rc, out = _run(tmp_path, pki, _url(gw))
    assert rc == 0, out
    assert "1. keyless /v1/models: HTTP 401 (want 401)" in out, out
    assert "2. invalid-key /v1/models: HTTP 401 (want 401)" in out, out
    assert ("health: HTTP 401 (200 = public /health survives gateway mode; "
            "else the switch needs a /health handler)") in out, out
    # exactly what the gateway received: the verifier leaf every time, the literal invalid key once
    assert [(s["path"], s["auth"], s["sans"]) for s in gw.seen] == [
        ("/v1/models", None, ["caddy.example.internal"]),
        ("/v1/models", "Bearer drill-invalid-key", ["caddy.example.internal"]),
        ("/health", None, ["caddy.example.internal"]),
    ]
    _no_leak(out)


@pytest.mark.parametrize("health", [200, 204, 401, 403, 404, 500, 503])
def test_the_health_status_is_recorded_and_never_fails_the_run(tmp_path, pki, listener, health):
    gw = listener("san-first")
    gw.script = ["status-401", "status-401", f"status-{health}"]
    rc, out = _run(tmp_path, pki, _url(gw))
    assert rc == 0, out
    assert f"health: HTTP {health} (" in out, out
    _no_leak(out)


@pytest.mark.parametrize("keyless, invalid", [
    pytest.param(200, 401, id="keyless-admitted"),
    pytest.param(401, 200, id="invalid-key-admitted"),
    pytest.param(403, 401, id="keyless-403"),
    pytest.param(401, 403, id="invalid-key-403"),
    pytest.param(503, 503, id="unhealthy-gateway"),
    pytest.param(404, 404, id="route-missing"),
])
def test_anything_but_401_on_either_models_probe_fails_the_run_and_still_reports_health(
        tmp_path, pki, listener, keyless, invalid):
    gw = listener("san-first")
    gw.script = [f"status-{keyless}", f"status-{invalid}", "status-200"]
    rc, out = _run(tmp_path, pki, _url(gw))
    assert rc != 0 and "Gate failed" in out, out
    assert f"1. keyless /v1/models: HTTP {keyless} (want 401)" in out, out
    assert f"2. invalid-key /v1/models: HTTP {invalid} (want 401)" in out, out
    # the report is printed before the assertion, so the failing run still answers the /health question
    assert "health: HTTP 200 (" in out, out
    _no_leak(out)


def test_a_gateway_that_is_not_listening_fails_the_run_with_no_http_answer(tmp_path, pki, listener):
    gw = listener("san-first")
    port = gw.server_port
    gw.shutdown()
    gw.server_close()
    rc, out = _run(tmp_path, pki, f"https://localhost:{port}")
    assert rc != 0 and "no HTTP answer" in out and "Gate failed" in out, out
    _no_leak(out)


# ── dry run and refusals ──────────────────────────────────────────────────────

def test_a_dry_run_sends_nothing(tmp_path, pki, listener):
    gw = listener("san-first")
    rc, out = _run(tmp_path, pki, _url(gw), check=True)
    assert rc == 0 and "Nothing was sent" in out, out
    assert gw.seen == [] and gw.refused == 0


def test_a_verifier_leaf_that_is_not_issued_is_refused_before_any_request(tmp_path, pki, listener):
    gw = listener("san-first")
    leaves = [{**_leaves(pki)[0], "dir": str(tmp_path / "never-issued")}]
    rc, out = _run(tmp_path, pki, _url(gw), host={"internal_leaves": leaves})
    assert rc != 0 and "never-issued/current/cert.pem" in out, out
    assert gw.seen == [] and gw.refused == 0


def test_an_undeclared_verifier_leaf_is_refused_before_any_request(tmp_path, pki, listener):
    gw = listener("san-first")
    rc, out = _run(tmp_path, pki, _url(gw), host={"internal_leaves": []})
    assert rc != 0 and "the agw-verifier leaf" in out, out
    assert gw.seen == [] and gw.refused == 0


def test_the_verifier_leaf_name_follows_the_inventory(tmp_path, pki, listener):
    gw = listener("san-first")
    leaves = [{**_leaves(pki)[0], "name": "other-verifier"}]
    rc, out = _run(tmp_path, pki, _url(gw), host={"internal_leaves": leaves, "agw_verifier_leaf": "other-verifier"})
    assert rc == 0, out
    assert len(gw.seen) == 3


# ── static properties ─────────────────────────────────────────────────────────

def _tasks():
    """The play's tasks in file order, a block's children right after the block."""
    def walk(tasks):
        for t in tasks:
            yield t
            yield from walk(t.get("block", []))
    return walk(playbook_yaml.load(PLAYBOOK)[2]["tasks"])


def test_every_request_goes_through_the_shared_probe_path_and_nothing_touches_openbao():
    tasks = list(_tasks())
    text = yaml.safe_dump(playbook_yaml.load(PLAYBOOK))  # the code, not the header comment
    sends = [t for t in tasks if t.get("ansible.builtin.include_tasks") == "tasks/agw-probe.yml"]
    assert [t["vars"]["_agwp_path"] for t in sends] == ["/v1/models", "/v1/models", "/health"]
    assert not [t for t in tasks if "ansible.builtin.uri" in t or "ansible.builtin.command" in t
                or "ansible.builtin.shell" in t or "ansible.builtin.raw" in t]
    for needle in ("openbao", "bao_", "hashi_vault", "vault"):
        assert needle not in text.lower(), needle


def test_the_only_key_is_the_literal_invalid_one():
    sends = [t for t in _tasks() if t.get("ansible.builtin.include_tasks") == "tasks/agw-probe.yml"]
    keys = [t["vars"]["_agwp_key"] for t in sends]
    assert keys == ["", "{{ _invalid_key }}", ""]
    assert "_invalid_key: drill-invalid-key" in PLAYBOOK.read_text()
    assert DEPLOY_SUBDIR in PLAYBOOK.read_text()


def test_the_dry_run_message_is_present():
    assert "Nothing was sent" in PLAYBOOK.read_text()


# ── wiring ────────────────────────────────────────────────────────────────────

def _templates():
    return yaml.safe_load((REPO / "platform/semaphore/templates.yml").read_text())["templates"]


def test_the_semaphore_template_is_dev_bound_with_no_survey():
    t = next(t for t in _templates() if t["name"] == "Probe agentgateway Gate (Dev)")
    assert t["playbook"] == "platform/playbooks/probe-agentgateway-gate.yml"
    assert t["repository"] == "agent-cloud dev"
    assert "survey_vars" not in t and "dev_variant" not in t


def test_the_playbook_is_launchable_and_guarded_first():
    doc = playbook_yaml.load(PLAYBOOK)
    assert doc[0].get("ansible.builtin.import_playbook") == "refuse-internal-extra-vars.yml"
    assert doc[1].get("ansible.builtin.import_playbook") == "preflight-target-group.yml"
    assert doc[2]["hosts"] == "agentgateway_svc"
    assert json.dumps(doc[1]["vars"]) == json.dumps(
        {"preflight_group": "agentgateway_svc", "preflight_group_expected": "agentgateway_svc"})
