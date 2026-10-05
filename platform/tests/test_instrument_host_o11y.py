"""Instrument Host Observability (workflow step instrument-host) converges, declares and proves.

Change service-deployment-workflow task 7.3. Runs the real playbook through ansible-playbook:
each service host gets a fake container engine (a script keeping its containers in a state
file), and local HTTP servers play the host exporters and the receiver's Prometheus, whose
query answers come from the scrape file it last reloaded. local_mode allows the loopback
exporter address and skips linger.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from fake_http import DrainingHandler

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/instrument-host-o11y.yml"
O11Y = REPO / "platform/services/o11y/deployment"
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

FAKE_ENGINE = """#!/usr/bin/env python3
import json, pathlib, sys
here = pathlib.Path(__file__)
state, log = here.with_suffix('.state'), here.with_suffix('.log')
args = sys.argv[1:]
with log.open('a') as f:
    f.write(' '.join(args) + '\\n')
containers = json.loads(state.read_text()) if state.exists() else {}
if args[0] == 'inspect':
    if args[-1] not in containers:
        sys.stderr.write('no such container\\n')
        sys.exit(125)
    print(containers[args[-1]] + ' running')
    sys.exit(0)
if args[0] == 'rm':
    containers.pop(args[-1], None)
if args[0] == 'run':
    containers[args[args.index('--name') + 1]] = args[args.index('--label') + 1].split('=', 1)[1]
state.write_text(json.dumps(containers))
"""


def _serve(handler) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _exporter(status: int):
    class Exporter(DrainingHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"node_load1 0.1\n")

        def log_message(self, *args):
            pass

    return _serve(Exporter)


class FakePrometheus:
    """Answers /-/reload (with reload_status) and /api/v1/query from the scrape file it loaded."""

    def __init__(self, fragment: Path, reload_status: int = 200):
        self.fragment, self.reload_status, self.loaded, self.reloads = fragment, reload_status, {}, 0
        self.silent_hosts: set[str] = set()  # declared but never scraped successfully
        outer = self

        class Handler(DrainingHandler):
            def do_POST(self):  # noqa: N802
                outer.reloads += 1
                if outer.reload_status == 200 and outer.fragment.exists():
                    outer.loaded = yaml.safe_load(outer.fragment.read_text())
                self.send_response(outer.reload_status)
                self.end_headers()
                self.wfile.write(b"" if outer.reload_status == 200 else b"bad scrape file")

            def do_GET(self):  # noqa: N802
                query = parse_qs(urlparse(self.path).query)["query"][0]
                job = re.search(r'job="([^"]+)"', query).group(1)
                result = [
                    {"metric": {"host": tgt["labels"]["host"], "instance": tgt["targets"][0]}, "value": [0, "1"]}
                    for cfg in outer.loaded.get("scrape_configs", [])
                    if cfg["job_name"] == job
                    for tgt in cfg["static_configs"]
                    if tgt["labels"]["host"] not in outer.silent_hosts
                ]
                body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        self.server = _serve(Handler)


@pytest.fixture
def estate(tmp_path):
    """Two service hosts and one receiver, all local connections."""
    scrape_dir = tmp_path / "repo/o11y/config/scrape.d"
    scrape_dir.mkdir(parents=True)
    state = {"tmp": tmp_path, "fragment": scrape_dir / "host-demo_svc.yml", "exporters": {}}
    for host in ("alpha", "beta"):
        engine = tmp_path / f"engine-{host}"
        engine.write_text(FAKE_ENGINE)
        engine.chmod(0o755)
        state["exporters"][host] = _exporter(200)
    state["prometheus"] = FakePrometheus(state["fragment"])
    yield state
    for server in [*state["exporters"].values(), state["prometheus"].server]:
        server.shutdown()


def _run(estate, *extra, binds=None, host_vars=None) -> subprocess.CompletedProcess:
    tmp = estate["tmp"]
    lines = ["[demo_svc]"]
    for host, server in estate["exporters"].items():
        bind = (binds or {}).get(host, "127.0.0.1")
        lines.append(
            f"{host} ansible_connection=local container_engine={tmp / f'engine-{host}'} "
            f"o11y_host_exporter_bind={bind} o11y_host_exporter_port={server.server_address[1]} "
            + (host_vars or {}).get(host, "")
        )
    lines += [
        "",
        "[demo_svc:vars]",
        "service_name=demo",
        "local_mode=true",
        "",
        "[o11y_svc]",
        f"receiver ansible_connection=local local_monorepo_dir={tmp / 'repo'} monorepo_deploy_path=o11y "
        f"o11y_prom_bind=127.0.0.1 o11y_prom_port={estate['prometheus'].server.server_address[1]} local_mode=true",
    ]
    inventory = tmp / "inventory.ini"
    inventory.write_text("\n".join(lines) + "\n")
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(PLAYBOOK), "-e", "target_service=demo_svc", *extra],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _result(proc) -> dict:
    found = step_results.results_in(proc.stdout.splitlines())
    assert len(found) == 1, f"expected one step result, got {found!r}\n{proc.stdout[-4000:]}"
    return found[0]


def _engine_calls(estate, host) -> list[str]:
    log = estate["tmp"] / f"engine-{host}.log"
    return log.read_text().splitlines() if log.exists() else []


def _changed(proc) -> dict:
    return {m.group(1): int(m.group(2)) for m in re.finditer(r"^(\w+)\s+: ok=\d+\s+changed=(\d+)", proc.stdout, re.M)}


@needs_ansible
def test_apply_declares_and_proves_then_rerun_changes_nothing(estate):
    proc = _run(estate)
    assert proc.returncode == 0, proc.stdout[-4000:]
    result = _result(proc)
    assert (result["step"], result["status"], result["service"]) == ("instrument-host", "pass", "demo")
    assert result["evidence"] == {
        "exporter_running": {"alpha": True, "beta": True},
        "series_present": {"alpha": True, "beta": True},
    }
    run = [c for c in _engine_calls(estate, "alpha") if c.startswith("run ")]
    assert len(run) == 1
    assert "--network host" in run[0] and "--restart always" in run[0] and "/:/host:ro,rslave" in run[0]
    assert f"--web.listen-address=127.0.0.1:{estate['exporters']['alpha'].server_address[1]}" in run[0]
    declared = yaml.safe_load(estate["fragment"].read_text())["scrape_configs"]
    assert [c["job_name"] for c in declared] == ["host-node-demo_svc"]
    labels = [t["labels"] for t in declared[0]["static_configs"]]
    assert [(lbl["host"], lbl["service"], lbl["component"]) for lbl in labels] == [
        ("alpha", "demo/host", "node-exporter"),
        ("beta", "demo/host", "node-exporter"),
    ]
    assert estate["prometheus"].reloads == 1

    again = _run(estate)
    assert again.returncode == 0, again.stdout[-4000:]
    assert _result(again)["status"] == "pass"
    assert _changed(again) == {"alpha": 0, "beta": 0, "receiver": 0, "localhost": 0}
    assert len([c for c in _engine_calls(estate, "alpha") if c.startswith("run ")]) == 1
    assert estate["prometheus"].reloads == 1


@needs_ansible
def test_check_mode_writes_nothing_and_records_skip(estate):
    proc = _run(estate, "--check")
    assert proc.returncode == 0, proc.stdout[-4000:]
    result = _result(proc)
    assert (result["status"], result["check_mode"]) == ("skip", True)
    assert result["evidence"]["exporter_running"] == {"alpha": False, "beta": False}
    assert result["evidence"]["series_present"] == {"alpha": None, "beta": None}
    for host in ("alpha", "beta"):
        assert all(c.startswith("inspect ") for c in _engine_calls(estate, host)), _engine_calls(estate, host)
    assert not estate["fragment"].exists()
    assert estate["prometheus"].reloads == 0


@needs_ansible
def test_check_mode_after_apply_verifies_live(estate):
    assert _run(estate).returncode == 0
    proc = _run(estate, "--check")
    assert proc.returncode == 0, proc.stdout[-4000:]
    result = _result(proc)
    assert (result["status"], result["check_mode"]) == ("pass", True)
    assert result["evidence"]["series_present"] == {"alpha": True, "beta": True}


@needs_ansible
@pytest.mark.parametrize("bind", ["", "0.0.0.0", "999.1.1.1"])
def test_an_unusable_declaration_fails_the_group_and_declares_nothing(estate, bind):
    proc = _run(estate, binds={"beta": bind})
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail"
    assert result["error"].startswith("beta: o11y_host_exporter_bind must declare")
    assert "alpha:" not in result["error"]
    assert result["evidence"]["exporter_running"] == {"alpha": True, "beta": False}
    assert result["evidence"]["series_present"] == {"alpha": None, "beta": None}
    assert not estate["fragment"].exists()


@needs_ansible
def test_an_unreachable_host_is_recorded_not_dropped(estate):
    # Nothing listens on port 1: the connection is refused at once.
    proc = _run(estate, host_vars={"beta": "ansible_connection=ssh ansible_host=127.0.0.1 ansible_port=1"})
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail"
    assert result["error"].startswith("beta: unreachable")
    assert "alpha:" not in result["error"]
    assert result["evidence"]["exporter_running"] == {"alpha": True, "beta": False}
    assert not estate["fragment"].exists()


@needs_ansible
def test_an_exporter_the_receiver_cannot_reach_is_never_declared(estate):
    estate["exporters"]["beta"].shutdown()
    estate["exporters"]["beta"] = _exporter(503)
    proc = _run(estate)
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail"
    assert "o11y receiver: The receiver got no HTTP 200 from beta (HTTP 503)" in result["error"]
    assert not estate["fragment"].exists()
    assert estate["prometheus"].reloads == 0


@needs_ansible
def test_a_declared_host_without_series_fails(estate):
    # The playbook waits two scrape intervals for the series (about 40 s here).
    estate["prometheus"].silent_hosts = {"beta"}
    proc = _run(estate)
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail"
    assert result["error"] == "beta: scrape not up in Prometheus"
    assert result["evidence"]["series_present"] == {"alpha": True, "beta": False}


@needs_ansible
def test_a_rejected_reload_puts_the_previous_file_back(estate):
    previous = "scrape_configs: []\n"
    estate["fragment"].write_text(previous)
    estate["prometheus"].reload_status = 500
    proc = _run(estate)
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail"
    assert "Prometheus rejected" in result["error"] and "bad scrape file" in result["error"]
    assert estate["fragment"].read_text() == previous
    assert estate["prometheus"].reloads == 2


@needs_ansible
@pytest.mark.parametrize("target", ["o11y_svc", "no_such_svc", "../etc"])
def test_refuses_a_target_it_must_not_instrument(estate, target):
    proc = _run(estate, "-e", f"target_service={target}")
    assert proc.returncode != 0
    assert "Pass -e target_service=<group>" in proc.stdout
    assert not estate["fragment"].exists()


@needs_ansible
def test_refuses_an_extra_var_forging_a_verdict(estate):
    proc = _run(estate, "-e", '{"_ih_errors": []}')
    assert proc.returncode != 0
    assert "_ih_errors is internal to this play" in proc.stdout


# Pins: the host exporter is the receiver's own, and the scrape job drops what every job drops.


def _play_vars(name: str) -> dict:
    plays = yaml.safe_load(PLAYBOOK.read_text())
    return next(p for p in plays if p["name"].startswith(name))["vars"]


def test_host_exporter_runs_the_receivers_image_and_collectors():
    compose = yaml.safe_load((O11Y / "compose.yml").read_text())["services"]["node-exporter"]
    host = _play_vars("Run the host exporter")
    assert host["_ih_collector_args"] == compose["command"]
    pin = re.search(r"default\('([^']+)'\)", host["_ih_image"]).group(1)
    assert compose["image"] == "${O11Y_NODE_EXPORTER_IMAGE:-" + pin + "}"
    assert f"default('{pin}')" in (O11Y / "templates/env.j2").read_text()


def test_scrape_file_drops_the_shared_forbidden_labels():
    deploy = yaml.safe_load((REPO / "platform/playbooks/deploy-o11y.yml").read_text())
    inline = next(
        p["vars"]["_o11y_forbidden_metric_label_names_regex"]
        for p in deploy
        if "_o11y_forbidden_metric_label_names_regex" in p.get("vars", {})
    )
    shared = yaml.safe_load((REPO / "platform/playbooks/vars/o11y-metric-labels.yml").read_text())
    assert shared["_o11y_forbidden_metric_label_names_regex"] == inline
    template = (O11Y / "templates/scrape-host-node.yml.j2").read_text()
    assert "_o11y_forbidden_metric_label_names_regex | to_json" in template
    assert "regex: '^device$'" in template


def test_verified_metric_is_one_the_pinned_exporter_serves():
    served = set((REPO / "platform/tests/fixtures/node-exporter-metric-names-v1.12.1.txt").read_text().split())
    assert "node_memory_MemAvailable_bytes" in served
    assert "node_memory_MemAvailable_bytes{job=" in PLAYBOOK.read_text()
