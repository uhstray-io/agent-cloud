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
        (_gateway(host="256.1.1.1"), None),
        (_gateway(host="1.2.3.999"), None),
        (_gateway(host="01.2.3.4"), None),
    ],
    ids=["loopback", "not-ipv4", "collides-with-dns_records", "octet-over-255", "octet-999", "leading-zero"],
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


def test_a_valid_ipv4_address_is_accepted(tmp_path):
    r, zone = _render(tmp_path, {"gw1": _gateway(host="198.51.100.1")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "198.51.100.1" in zone


def test_check_mode_skips_the_gateway_record_dig(tmp_path):
    # --check skips render and deploy, so live DNS still holds the old records; the dig
    # must not run (or it fails comparing stale answers). The engine is `false`, so a
    # run that did execute fails loudly.
    phase3 = next(p for p in yaml.safe_load(DEPLOY_DNS.read_text()) if p["name"].startswith("Phase 3"))
    task = next(t for t in phase3["tasks"] if t["name"] == "Resolve each gateway server leaf record")
    extra = {
        "ansible_python_interpreter": "python3",
        "_engine": "false",
        "_dns_agw_records": [{"fqdn": f"gateway.lab.{ZONE}", "value": "192.0.2.10"}],
    }
    r = _run(tmp_path, [task], _inventory({}), extra, check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipping" in r.stdout
    r = _run(tmp_path, [task], _inventory({}), extra)
    assert r.returncode != 0, "a real run must still dig and fail on a wrong answer"


def test_check_mode_skips_the_wildcard_compare(tmp_path):
    # --check skips render and reload, so live DNS still serves the previous wildcard; a
    # changed dns_wildcard_target must not fail against it. A real run still compares.
    phase3 = next(p for p in yaml.safe_load(DEPLOY_DNS.read_text()) if p["name"].startswith("Phase 3"))
    task = next(t for t in phase3["tasks"] if t["name"] == "Assert the wildcard resolves to the configured target")
    extra = {
        "ansible_python_interpreter": "python3",
        "dns_wildcard_target": "192.0.2.50",
        "_dig_wild": {"stdout": "192.0.2.1"},
    }
    r = _run(tmp_path, [task], _inventory({}), extra, check=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipping" in r.stdout
    r = _run(tmp_path, [task], _inventory({}), extra)
    assert r.returncode != 0, "a real run must still fail on a wrong wildcard answer"


def test_only_the_shared_resolution_task_writes_the_marker_line():
    """Ratchet: the interim agw-probe hosts line has exactly one writer.

    Rule: under platform/ and scripts/, any file except Markdown docs that carries the marker
    text `agent-cloud-managed: agw-probe` must be tasks/agw-probe-resolution.yml or a test
    under platform/tests/. Any other file carrying it (a lineinfile, copy, template, shell or
    script) is a second writer and fails here.
    """
    allowed = {str(RESOLUTION.relative_to(REPO))}
    carriers = sorted(
        rel
        for root in ("platform", "scripts")
        for p in (REPO / root).rglob("*")
        if p.is_file() and p.suffix != ".md" and "/.git/" not in str(p)
        and (rel := str(p.relative_to(REPO))) not in allowed
        and not (rel.startswith("platform/tests/") and p.name.startswith("test_"))
        and b"agent-cloud-managed: agw-probe" in p.read_bytes()
    )
    assert carriers == []


KEEP = "127.0.0.1 localhost\n192.0.2.9 keep.me # agent-cloud-managed: other\n"
HOSTS = KEEP + f"192.0.2.1 old.name {MARKER}\n192.0.2.2 older.name {MARKER}\n"


def _resolve(tmp_path, extra, check=False, hosts=HOSTS):
    hf = tmp_path / "hosts"
    hf.write_text(hosts)
    tasks = [{"name": "inc", "ansible.builtin.include_tasks": str(RESOLUTION)}]
    base = {"_agwr_hosts_file": str(hf), "ansible_become": False, "ansible_python_interpreter": "python3"}
    r = _run(tmp_path, tasks, _inventory({}), {**base, **extra}, check=check)
    return r, hf.read_text()


@pytest.mark.parametrize("extra,why", [
    ({"_agwr_name": "gateway.lab.x", "_agwr_ip": "999.999.999.999"}, "octets above 255"),
    ({"_agwr_name": "gateway.lab.x", "_agwr_ip": "256.1.1.1"}, "one octet above 255"),
    ({"_agwr_name": "gateway.lab.x", "_agwr_ip": "192.0.2.1.5"}, "five octets"),
    ({"_agwr_name": "gateway.lab.x", "_agwr_ip": "gateway-vm"}, "a name where the address goes"),
    ({"_agwr_name": "gateway.lab.x", "_agwr_ip": ""}, "no address"),
    ({"_agwr_name": "gateway.lab.x other.name", "_agwr_ip": "192.0.2.1"}, "a second name in the line"),
    ({"_agwr_name": "", "_agwr_ip": "192.0.2.1"}, "no name"),
])
def test_interim_mode_refuses_a_value_the_hosts_line_cannot_carry(tmp_path, extra, why):
    r, text = _resolve(tmp_path, extra, hosts="127.0.0.1 localhost\n")
    assert r.returncode != 0, why
    assert text == "127.0.0.1 localhost\n", f"hosts file written for {why}"
    # The refusal is rendered, not printed as template source.
    assert "and _agwr_ip (the published bind, an IPv4 address)." in r.stdout, why
    assert "{ '" not in r.stdout and "' }" not in r.stdout, why


def test_authoritative_mode_refuses_a_malformed_name_without_asking_for_an_address(tmp_path):
    r, text = _resolve(tmp_path, {"agw_internal_dns_authoritative": True, "_agwr_name": "two names"})
    assert r.returncode != 0
    assert text == HOSTS
    assert "needs _agwr_name (the server leaf SAN, a DNS name)." in r.stdout
    assert "_agwr_ip" not in r.stdout.split("needs _agwr_name", 1)[1].split("\n", 1)[0]


@pytest.mark.parametrize("ip", ["0.0.0.0", "255.255.255.255", "192.0.2.199", "203.0.113.250"])
def test_interim_mode_accepts_every_valid_ipv4_address(tmp_path, ip):
    r, text = _resolve(tmp_path, {"_agwr_name": "gateway.lab.x", "_agwr_ip": ip}, hosts="127.0.0.1 localhost\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"{ip} gateway.lab.x {MARKER}" in text


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


LINE = f"127.0.0.1 gateway.lab.x {MARKER}\n"


@pytest.mark.parametrize(
    "authoritative,hosts,ok",
    [
        (False, "127.0.0.1 localhost\n" + LINE, True),
        (False, "127.0.0.1 localhost\n", False),
        (False, f"127.0.0.9 gateway.lab.x {MARKER}\n", False),
        (False, LINE + f"192.0.2.1 old.name {MARKER}\n", False),
        (True, "127.0.0.1 localhost\n", True),
        (True, "127.0.0.1 localhost\n" + LINE, False),
    ],
    ids=["interim-exact", "interim-missing", "interim-different", "interim-extra", "auth-clean", "auth-leftover"],
)
def test_check_only_reads_and_never_writes(tmp_path, authoritative, hosts, ok):
    extra = {"_agwr_check_only": True, "_agwr_name": "localhost" if authoritative else "gateway.lab.x",
             "_agwr_ip": "127.0.0.1", "agw_internal_dns_authoritative": authoritative}
    r, text = _resolve(tmp_path, extra, hosts=hosts)
    assert text == hosts, "check-only must never write"
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    if not ok:
        assert "Run Deploy agentgateway" in r.stdout
