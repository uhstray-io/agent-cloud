"""Gateway listener TLS: inference-gateway-agentgateway task 6.1, production-internal-ca task 5.2.

Renders config.yaml.j2 for real and runs deploy-agentgateway.yml's own guard tasks on localhost
(through harness_sandbox, so nothing can be written outside the test's directory). The runtime
behaviour the rule relies on was measured on agentgateway v1.5.0, 2026-10-02: a certificate
outside the SAN list is 403 even with a valid key, and the rule runs before key authentication.
"""

import re
import subprocess
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

REPO = playbook_yaml.REPO
DEPLOY = REPO / "platform/playbooks/deploy-agentgateway.yml"
DEPLOY_DIR = REPO / "platform/services/agentgateway/deployment"
CONFIG = DEPLOY_DIR / "templates/config.yaml.j2"
ZONE = "dc1.example.internal"
D = "/srv/agw"  # the deploy directory the guards are given
LEAVES = [
    {"name": "caddy", "host": "caddy", "profile": "client", "dir": "/srv/caddy/certs/caddy",
     "sans": [f"vm01.caddy.{ZONE}", f"caddy.{ZONE}"]},
    {"name": "agw-verifier", "host": "gw", "profile": "client", "dir": f"{D}/certs/agw-verifier",
     "sans": [f"verifier.gateway.{ZONE}"]},
    {"name": "agw-server", "host": "gw", "profile": "server", "dir": f"{D}/certs/agw-server",
     "sans": [f"vm01.gateway.{ZONE}", f"gateway.{ZONE}", f"inference.{ZONE}"]},
    {"name": "bench", "host": "bench", "profile": "client", "dir": "/srv/bench/certs/bench",
     "sans": [f"bench.{ZONE}"]},
]


def _ansible(tmp: Path, host_vars: dict, tasks: list, play_vars: dict | None = None) -> subprocess.CompletedProcess:
    inv = {"all": {"hosts": {"gw": {"ansible_connection": "local", **host_vars}}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    (tmp / "play.yml").write_text(yaml.safe_dump(
        [{"hosts": "all", "gather_facts": False, "vars": play_vars or {}, "tasks": tasks}]))
    return harness_sandbox.run(["ansible-playbook", "-i", str(tmp / "inv.yml"), str(tmp / "play.yml")],
                               tmp, cwd=REPO, env=harness_sandbox.env_for(tmp))


def _render(tmp: Path, **hv) -> dict:
    hv = {"agw_clients": ["workstation"], "agw_models": [{"name": "m"}], "agw_upstream_base_url": "http://u.invalid/v1",
          "internal_leaves": LEAVES,
          "secrets": {"client_workstation": "k", "vllm_api_key": "v", "agw_db_password": "p",
                      "agw_oidc_cookie_seed": "s", "agentgateway_oidc_client_secret": "c"}, **hv}
    task = {"ansible.builtin.template": {"src": str(CONFIG), "dest": str(tmp / "config.yaml"), "mode": "0644"}}
    r = _ansible(tmp, hv, [task])
    assert r.returncode == 0, r.stdout + r.stderr
    return yaml.safe_load((tmp / "config.yaml").read_text())


def _rule_sans(gw: dict) -> list:
    rule = gw["authorization"]["rules"][0]["require"]
    assert rule.startswith("source.subjectAltNames.exists(n, n in ["), rule
    return re.findall(r'"([^"]+)"', rule)


# ── Render ────────────────────────────────────────────────────────────────────

def test_both_listeners_serve_tls_require_a_ca_client_cert_and_carry_the_san_rule(tmp_path):
    cfg = _render(tmp_path, agw_listener_tls=True, agw_client_cert_allowlist=["caddy", "agw-verifier"])
    for name in ("default", "ui"):
        gw = cfg["gateways"][name]
        assert gw["tls"] == {"cert": "/certs/agw-server/current/cert.pem", "key": "/certs/agw-server/current/key.pem",
                             "root": "/certs/step-ca-bundle.crt"}
        assert sorted(_rule_sans(gw)) == sorted([f"vm01.caddy.{ZONE}", f"caddy.{ZONE}", f"verifier.gateway.{ZONE}"])


def test_the_default_allowlist_is_caddy_alone(tmp_path):
    cfg = _render(tmp_path, agw_listener_tls=True)
    assert sorted(_rule_sans(cfg["gateways"]["default"])) == sorted([f"vm01.caddy.{ZONE}", f"caddy.{ZONE}"])


def test_a_server_leaf_named_in_the_allowlist_is_not_admitted_as_a_client(tmp_path):
    # Only client-profile leaves resolve: a server leaf's SANs never become client identities.
    cfg = _render(tmp_path, agw_listener_tls=True, agw_client_cert_allowlist=["caddy", "agw-server"])
    assert f"gateway.{ZONE}" not in _rule_sans(cfg["gateways"]["default"])


def test_without_listener_tls_nothing_changes(tmp_path):
    cfg = _render(tmp_path)
    for gw in cfg["gateways"].values():
        assert "tls" not in gw and "authorization" not in gw


# ── Guards (deploy-agentgateway.yml Phase 1, before anything is rendered) ─────

PHASE1 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 1"))
GUARDS = ["Refuse a client-certificate allowlist entry that names no declared client leaf",
          "Refuse listener TLS without the server leaf and the verifier declared for this host"]


def _guards(tmp: Path, **hv) -> subprocess.CompletedProcess:
    tasks = [next(t for t in PHASE1["tasks"] if t["name"] == g) for g in GUARDS]
    pv = {k: v for k, v in PHASE1["vars"].items() if k in ("_tls", "_server_leaf", "_verifier_leaf", "_allowlist")}
    hv = {"agw_listener_tls": True, "internal_leaves": LEAVES, "dns_site": "dc1", "dns_zone": "example.internal",
          "agw_client_cert_allowlist": ["caddy", "agw-verifier"], **hv}
    return _ansible(tmp, hv, tasks, play_vars={**pv, "_deploy_dir": D})


def test_a_declared_allowlist_passes_the_guards(tmp_path):
    r = _guards(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("hv,message", [
    ({"agw_client_cert_allowlist": ["caddy", "agw-verifier", "nobody"]}, "names nobody, which no internal_leaves"),
    ({"agw_client_cert_allowlist": ["caddy", "agw-verifier", "agw-server"]},
     "names agw-server, which no internal_leaves"),
    ({"agw_client_cert_allowlist": ["caddy"]}, "must include agw-verifier"),
    ({"internal_leaves": [x for x in LEAVES if x["name"] != "agw-server"]}, "must declare `agw-server`"),
    ({"internal_leaves": [{**x, "dir": "/elsewhere"} if x["name"] == "agw-server" else x for x in LEAVES]},
     "must declare `agw-server`"),
    ({"agw_tls_server_name": f"other.{ZONE}"}, "must declare `agw-server`"),
], ids=["undeclared", "server-as-client", "no-verifier", "no-server", "server-elsewhere", "name-not-a-san"])
def test_a_declaration_the_gateway_cannot_serve_is_refused_naming_it(tmp_path, hv, message):
    r = _guards(tmp_path, **hv)
    assert r.returncode != 0 and message in r.stdout, r.stdout


def test_the_guards_run_before_secrets_are_rendered_or_the_gateway_restarted():
    names = [t["name"] for t in PHASE1["tasks"]]
    for g in GUARDS + ["Refuse listener TLS before its leaves are issued"]:
        assert names.index(g) < names.index("Manage secrets and render env + config"), g


# ── Compose ───────────────────────────────────────────────────────────────────

def test_production_overlay_mounts_the_directory_and_keeps_the_process_non_root():
    svc = yaml.safe_load((DEPLOY_DIR / "compose.tls.yml").read_text())["services"]["agentgateway"]
    assert svc["userns_mode"] == "keep-id:uid=65532,gid=65532"
    assert svc["volumes"] == ["./certs:/certs:ro"]
    phase2 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 2"))
    env = next(t for t in phase2["tasks"] if t["name"] == "Run deploy.sh (container lifecycle)")["environment"]
    # The environment is the one mapping every deploy.sh caller shares (test_agw_deploy_env.py).
    assert env == "{{ _agw_deploy_env }}"
    shared = yaml.safe_load((DEPLOY.parent / "vars/agw-deploy-env.yml").read_text())["_agw_deploy_env"]
    assert "compose.tls.yml" in shared["COMPOSE_OVERLAYS"] and "not (local_mode" in shared["COMPOSE_OVERLAYS"]


class _ComposeLoader(yaml.SafeLoader):
    """Accepts compose's `!override` / `!reset` tags, keeping the tagged value."""


_ComposeLoader.add_multi_constructor("!", lambda loader, suffix, node: (
    loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode) else
    loader.construct_mapping(node) if isinstance(node, yaml.MappingNode) else loader.construct_scalar(node)))


def test_local_overlay_mounts_the_directory_not_the_single_bundle_file():
    svc = yaml.load((DEPLOY_DIR / "compose.local.yml").read_text(), Loader=_ComposeLoader)["services"]["agentgateway"]
    assert svc["volumes"] == ["./certs:/certs:ro"]
    assert svc["environment"]["SSL_CERT_FILE"] == "/certs/step-ca-bundle.crt"
    assert svc["user"] == "${AGW_RUN_AS:-65532:65532}"


# ── The probe path's target (review of af16a670) ─────────────────────────────

PHASE3 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 3"))


@pytest.mark.parametrize("hv,want", [
    ({"agw_listener_tls": True}, f"https://gateway.{ZONE}:4000"),
    ({"agw_listener_tls": True, "agw_verify_base_url": f"https://gateway.{ZONE}:4000"},
     f"https://gateway.{ZONE}:4000"),
    ({"agw_listener_tls": True, "agw_verify_base_url": "https://agw.local.test:4000"}, "https://agw.local.test:4000"),
    ({}, "http://127.0.0.1:4000"),
    ({"agw_verify_base_url": "http://agentgateway:4000"}, "http://agentgateway:4000"),
], ids=["tls-prod", "tls-declared", "tls-local-other-name", "plain", "plain-local"])
def test_a_declared_verify_url_wins_and_production_names_the_server_san(tmp_path, hv, want):
    name = "Probe path: the base URL, the presented leaf and the trust bundle"
    task = next(t for t in PHASE3["tasks"] if t["name"] == name)
    dump = {"ansible.builtin.copy": {"content": "{{ _agwp_base | trim }}", "dest": str(tmp_path / "base"),
                                     "mode": "0600"}}
    pv = {k: PHASE3["vars"][k] for k in ("_tls", "_agw_server_name", "_agw_probe_ip", "_plain_url")}
    r = _ansible(tmp_path, {"dns_site": "dc1", "dns_zone": "example.internal", "internal_leaves": LEAVES, **hv},
                 [task, dump], play_vars={**pv, "_deploy_dir": D})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "base").read_text() == want


def test_the_hosts_line_is_written_only_when_no_verify_url_is_declared():
    hosts = next(t for t in PHASE3["tasks"] if t["name"].startswith("Probe path: resolve the server leaf"))
    assert "agw_verify_base_url is not defined" in hosts["when"]


MARKER = "# agent-cloud-managed: agw-probe (interim, task 7.2)"


@pytest.mark.parametrize("check", [False, True], ids=["run", "check"])
def test_the_deploy_resolves_the_probe_name_through_the_shared_step(tmp_path, check):
    # The deploy's own task, run for real: with the flag unset it writes the interim line
    # (same bytes as before the shared step), and under --check it writes nothing.
    task = dict(next(t for t in PHASE3["tasks"] if t["name"].startswith("Probe path: resolve the server leaf")))
    assert task["ansible.builtin.include_tasks"] == "tasks/agw-probe-resolution.yml"
    task["ansible.builtin.include_tasks"] = str(REPO / "platform/playbooks" / task["ansible.builtin.include_tasks"])
    hf = tmp_path / "hosts"
    hf.write_text("127.0.0.1 localhost\n")
    pv = {k: PHASE3["vars"][k] for k in ("_tls", "_agw_server_name", "_agw_probe_ip")}
    hv = {"agw_listener_tls": True, "agw_bind": "0.0.0.0", "dns_site": "dc1", "dns_zone": "example.internal",
          "ansible_become": False, "_agwr_hosts_file": str(hf)}
    inv = {"all": {"hosts": {"gw": {"ansible_connection": "local", **hv}}}}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump(inv))
    (tmp_path / "play.yml").write_text(yaml.safe_dump(
        [{"hosts": "all", "gather_facts": False, "vars": pv, "tasks": [task]}]))
    cmd = ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml"),
           *(["--check"] if check else [])]
    r = harness_sandbox.run(cmd, tmp_path, cwd=REPO, env=harness_sandbox.env_for(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    want = "127.0.0.1 localhost\n" + ("" if check else f"127.0.0.1 gateway.{ZONE} {MARKER}\n")
    assert hf.read_text() == want


def test_the_bundle_step_lets_the_shared_task_pick_the_ca_hosts_engine():
    step = next(t for t in PHASE1["tasks"] if t["name"] == "Distribute the step-ca trust bundle into ./certs")
    assert "_ca_engine" not in step["vars"]


def test_every_model_declares_completions_and_responses(tmp_path):
    """Responses passes through to the upstream instead of being translated to chat completions
    (prod conformance task 2681: translation dropped reasoning items). agentgateway v1.5.0 picks
    the native format first when the custom provider declares it (crates/agentgateway/src/llm/mod.rs:324)."""
    cfg = _render(tmp_path, agw_models=[{"name": "a"}, {"name": "b", "upstream_model": "org/b"}])
    models = cfg["llm"]["models"]
    assert [m["name"] for m in models] == ["a", "b"]
    for m in models:
        types = [f["type"] for f in m["provider"]["custom"]["formats"]]
        assert types == ["completions", "responses"], (m["name"], types)
