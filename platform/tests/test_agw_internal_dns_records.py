"""Internal DNS records for the gateway server leaf (inference-gateway-agentgateway task 7.2).

deploy-dns.yml derives one A record per SAN of each listener-TLS gateway's server leaf that falls
inside dns_zone, refuses a record the zone cannot serve, and renders it into the zone file.
tasks/agw-probe-resolution.yml keeps the interim /etc/hosts line until
`agw_internal_dns_authoritative` is declared, then removes every marker line (and only those).
The real tasks run on localhost through ansible-playbook against a scratch inventory and a
scratch hosts file.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY_DNS = REPO / "platform/playbooks/deploy-dns.yml"
RESOLUTION = REPO / "platform/playbooks/tasks/agw-probe-resolution.yml"
ZONE_TPL = REPO / "platform/services/dns/deployment/templates/zone.local-dev.j2"
MARKER = "# agent-cloud-managed: agw-probe (interim, task 7.2)"
ZONE = "agent-cloud.test"

PHASE1 = next(p for p in yaml.safe_load(DEPLOY_DNS.read_text()) if p["name"].startswith("Phase 1"))
TASKS = {t["name"]: t for t in PHASE1["tasks"]}
DERIVE = TASKS["Derive the gateway server leaf records"]
REFUSE = TASKS["Refuse a gateway record the zone cannot serve"]


def _gateway(bind="0.0.0.0", host="192.0.2.10", tls=True, sans=None):
    return {
        "ansible_host": host,
        "agw_bind": bind,
        "agw_listener_tls": tls,
        "internal_leaves": [
            {
                "name": "agw-server",
                "profile": "server",
                "sans": sans or [f"gateway.lab.{ZONE}", "gateway.other.example"],
            },
            {"name": "agw-verifier", "profile": "client", "sans": ["agw-verifier"]},
        ],
    }


def _run(tmp_path, tasks, inventory, extra=None, check=False):
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump([{"name": "t", "hosts": "dns_svc", "gather_facts": False, "tasks": tasks}]))
    inv = tmp_path / "inv.yml"
    inv.write_text(yaml.safe_dump(inventory))
    cmd = ["ansible-playbook", "-i", str(inv), str(pb), "-e", json.dumps(extra or {})]
    if check:
        cmd.append("--check")
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO / "platform/playbooks")


def _inventory(gateways, dns_vars=None):
    return {
        "all": {
            "children": {
                "dns_svc": {"hosts": {"dns1": {"ansible_connection": "local", "dns_zone": ZONE, **(dns_vars or {})}}},
                "agentgateway_svc": {"hosts": gateways},
            }
        }
    }


def _render(tmp_path, gateways, dns_vars=None):
    out = tmp_path / "zone"
    tasks = [DERIVE, REFUSE, {"name": "render", "ansible.builtin.template": {"src": str(ZONE_TPL), "dest": str(out)}}]
    r = _run(tmp_path, tasks, _inventory(gateways, dns_vars), {"ansible_python_interpreter": "python3"})
    return r, (out.read_text() if out.exists() else "")


def test_server_leaf_san_inside_the_zone_is_published_at_the_gateway_address(tmp_path):
    r, zone = _render(tmp_path, {"gw1": _gateway()})
    assert r.returncode == 0, r.stdout + r.stderr
    body = zone.split("\n*", 1)[1].splitlines()[1:]
    assert [ln.split() for ln in body if ln.strip()] == [["gateway.lab", "IN", "A", "192.0.2.10"]]


def test_a_specific_bind_is_the_record_target(tmp_path):
    r, zone = _render(tmp_path, {"gw1": _gateway(bind="192.0.2.20")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "192.0.2.20" in zone and "192.0.2.10" not in zone


def test_a_gateway_without_listener_tls_publishes_nothing(tmp_path):
    r, zone = _render(tmp_path, {"gw1": _gateway(tls=False)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "gateway.lab" not in zone


@pytest.mark.parametrize(
    "gw,dns_vars",
    [
        (_gateway(bind="127.0.0.1"), None),
        (_gateway(host="gw1.example"), None),
        (_gateway(), {"dns_records": [{"name": "gateway.lab", "value": "192.0.2.99"}]}),
    ],
    ids=["loopback", "not-ipv4", "collides-with-dns_records"],
)
def test_a_record_the_zone_cannot_serve_is_refused(tmp_path, gw, dns_vars):
    r, zone = _render(tmp_path, {"gw1": gw}, dns_vars)
    assert r.returncode != 0
    assert "Refuse a gateway record the zone cannot serve" in r.stdout
    assert zone == ""


def test_dns_verify_digs_every_gateway_record():
    phase3 = next(p for p in yaml.safe_load(DEPLOY_DNS.read_text()) if p["name"].startswith("Phase 3"))
    task = next(t for t in phase3["tasks"] if t["name"] == "Resolve each gateway server leaf record")
    assert task["loop"] == "{{ _dns_agw_records | default([]) }}"
    assert "item.fqdn" in task["ansible.builtin.command"]
    assert task["failed_when"] == "(_dig_agw.stdout | trim) != item.value"


KEEP = "127.0.0.1 localhost\n192.0.2.9 keep.me # agent-cloud-managed: other\n"
HOSTS = KEEP + f"192.0.2.1 old.name {MARKER}\n192.0.2.2 older.name {MARKER}\n"


def _resolve(tmp_path, extra, check=False, hosts=HOSTS):
    hf = tmp_path / "hosts"
    hf.write_text(hosts)
    tasks = [{"name": "inc", "ansible.builtin.include_tasks": str(RESOLUTION)}]
    base = {"_agwr_hosts_file": str(hf), "ansible_become": False, "ansible_python_interpreter": "python3"}
    r = _run(tmp_path, tasks, _inventory({}), {**base, **extra}, check=check)
    return r, hf.read_text()


def test_interim_mode_keeps_exactly_one_marker_line(tmp_path):
    r, text = _resolve(
        tmp_path, {"_agwr_name": "gateway.lab.x", "_agwr_ip": "127.0.0.1"}, hosts="127.0.0.1 localhost\n"
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert text.count(MARKER) == 1 and f"127.0.0.1 gateway.lab.x {MARKER}" in text


def test_authoritative_mode_removes_every_marker_line_and_only_those(tmp_path):
    r, text = _resolve(tmp_path, {"agw_internal_dns_authoritative": True, "_agwr_name": "localhost"})
    assert r.returncode == 0, r.stdout + r.stderr
    assert MARKER not in text
    assert text == KEEP


def test_authoritative_mode_in_check_mode_writes_nothing(tmp_path):
    r, text = _resolve(tmp_path, {"agw_internal_dns_authoritative": True, "_agwr_name": "localhost"}, check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert text == HOSTS
    assert "changed=1" in r.stdout


def test_authoritative_mode_refuses_a_name_that_does_not_resolve(tmp_path):
    r, text = _resolve(tmp_path, {"agw_internal_dns_authoritative": True, "_agwr_name": "no-such-name.invalid"})
    assert r.returncode != 0
    assert "refuse a name the internal zone does not answer" in r.stdout


def test_authoritative_mode_refuses_the_wrong_answer(tmp_path):
    r, _ = _resolve(
        tmp_path, {"agw_internal_dns_authoritative": True, "_agwr_name": "localhost", "_agwr_expect": "192.0.2.77"}
    )
    assert r.returncode != 0
    assert "expected 192.0.2.77" in r.stdout
