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
    hv = {"agw_clients": ["stray"], "agw_models": [{"name": "m"}], "agw_upstream_base_url": "http://u.invalid/v1",
          "internal_leaves": LEAVES,
          "secrets": {"client_stray": "k", "vllm_api_key": "v", "agw_db_password": "p",
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
    assert "compose.tls.yml" in env["COMPOSE_OVERLAYS"] and "not (local_mode" in env["COMPOSE_OVERLAYS"]


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
