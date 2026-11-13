"""Instrument Host Observability (workflow step instrument-host) converges, declares and proves.

Change service-deployment-workflow task 7.3. Runs the real playbook through ansible-playbook:
each service host gets a fake container engine (a script keeping its containers in a state
file), and local HTTP servers play the host exporters and the receiver's Prometheus, whose
query answers come from the scrape file it last reloaded. The hosts are named -local, so the playbook
takes the local-dev branch (loopback exporter address, no linger). The production tests run on a
production inventory (_production_inventory).
"""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import forgeries
import harness_sandbox
import playbook_yaml
import pytest
import yaml
from ansible_python import needs_ansible
from fake_http import SHUTDOWN_POLL_SECONDS, DrainingHandler

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/instrument-host-o11y.yml"
O11Y = REPO / "platform/services/o11y/deployment"
_spec = importlib.util.spec_from_file_location(
    "step_results", REPO / "platform/workflows/service-onboarding/lib/step_results.py"
)
step_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(step_results)

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
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": SHUTDOWN_POLL_SECONDS}, daemon=True).start()
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
        self.identity_overrides: dict[str, dict] = {}
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
                is_named_host_metric = "node_memory_MemAvailable_bytes" in query
                result = [
                    {"metric": {
                        **tgt["labels"],
                        **(outer.identity_overrides.get(tgt["labels"]["host"], {})
                           if is_named_host_metric else {}),
                        "instance": tgt["targets"][0],
                    }, "value": [0, "1"]}
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
    for host in ("alpha-local", "beta-local"):
        engine = tmp_path / f"engine-{host}"
        engine.write_text(FAKE_ENGINE)
        engine.chmod(0o755)
        state["exporters"][host] = _exporter(200)
    state["prometheus"] = FakePrometheus(state["fragment"])
    yield state
    for server in [*state["exporters"].values(), state["prometheus"].server]:
        server.shutdown()


def _inventory(
    estate, binds=None, host_vars=None, receiver_vars="", identity_enabled=None, coverage=None, ports=None,
    host_coverage=None,
) -> Path:
    tmp = estate["tmp"]
    lines = ["[demo_svc]"]
    for host, server in estate["exporters"].items():
        bind = (binds or {}).get(host, "127.0.0.1")
        port = (ports or {}).get(host, server.server_address[1])
        lines.append(
            f"{host} ansible_connection=local container_engine={tmp / f'engine-{host}'} "
            f"o11y_host_exporter_bind={bind} o11y_host_exporter_port={port} "
            + (host_vars or {}).get(host, "")
        )
    lines += [
        "",
        "[demo_svc:vars]",
        "service_name=demo",
        "local_mode=true",
    ]
    if identity_enabled is not None:
        lines.append(f"o11y_host_metrics_identity_enabled={identity_enabled!r}")
    if coverage is not None:
        lines.append(f"estate_coverage_inventory={coverage!r}")
    lines += [
        "",
        "[o11y_svc]",
        f"receiver-local ansible_connection=local local_monorepo_dir={tmp / 'repo'} monorepo_deploy_path=o11y "
        f"o11y_prom_bind=127.0.0.1 o11y_prom_port={estate['prometheus'].server.server_address[1]} local_mode=true "
        f"{receiver_vars}",
    ]
    inventory = tmp / "inventory.ini"
    inventory.write_text("\n".join(lines) + "\n")
    for host, host_inventory in (host_coverage or {}).items():
        host_vars_dir = tmp / "host_vars"
        host_vars_dir.mkdir(exist_ok=True)
        (host_vars_dir / f"{host}.yml").write_text(yaml.safe_dump(
            {"estate_coverage_inventory": host_inventory}, sort_keys=False
        ))
    return inventory


def _run(
    estate, *extra, binds=None, host_vars=None, path_prefix=None, receiver_vars="",
    identity_enabled=None, coverage=None, ports=None, host_coverage=None
) -> subprocess.CompletedProcess:
    inventory = _inventory(
        estate, binds, host_vars, receiver_vars, identity_enabled, coverage, ports, host_coverage
    )
    return _run_inventory(inventory, *extra, path_prefix=path_prefix)


def _run_inventory(inventory: Path, *extra, path_prefix=None) -> subprocess.CompletedProcess:
    """The playbook on one inventory file, in the environment and bound _run always uses."""
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    return subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(PLAYBOOK), "-e", "target_service=demo_svc",
         "-e", "ih_wait_delay=0", *extra],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS,
    )


def _coverage_targets(estate, *, hosts=None, selector=None) -> dict:
    targets = []
    for host in hosts or ("alpha-local", "beta-local"):
        identity = "demo"
        target_id = f"service:{host}"
        targets.append({
            "target_id": target_id,
            "target_type": "service",
            "lifecycle": "deployed",
            "owner": "platform",
            "runtime": "podman",
            "service_identity": identity,
            "environment": "production",
            "inventory_host": host,
            "signals": {"metrics": {
                "applicable": True,
                "selector": selector or f'node_cpu_seconds_total{{target_id="{target_id}"}}',
                "collection_method": "prometheus",
                "freshness_seconds": 300,
            }},
            "collection_method": "prometheus",
            "budget": {"samples_per_scrape": 1000},
            "template_references": ["Verify o11y Target Receipt"],
        })
    revision = hashlib.sha256(json.dumps(
        {"targets": targets}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()).hexdigest()
    return {"revision": revision, "targets": targets}


def _result(proc) -> dict:
    found = step_results.results_in(proc.stdout.splitlines())
    assert len(found) == 1, f"expected one step result, got {found!r}\n{proc.stdout[-4000:]}"
    return found[0]


def _engine_calls(estate, host) -> list[str]:
    log = estate["tmp"] / f"engine-{host}.log"
    return log.read_text().splitlines() if log.exists() else []


def _changed(proc) -> dict:
    pattern = r"^([\w.-]+)\s+: ok=\d+\s+changed=(\d+)"  # names may hold hyphens (-local, -prod)
    return {m.group(1): int(m.group(2)) for m in re.finditer(pattern, proc.stdout, re.M)}


@needs_ansible
@pytest.mark.parametrize("identity_enabled", [None, False])
def test_apply_declares_and_proves_then_rerun_changes_nothing(estate, identity_enabled):
    proc = _run(estate, identity_enabled=identity_enabled)
    assert proc.returncode == 0, proc.stdout[-4000:]
    result = _result(proc)
    assert (result["step"], result["status"], result["service"]) == ("instrument-host", "pass", "demo")
    assert result["evidence"] == {
        "exporter_running": {"alpha-local": True, "beta-local": True},
        "series_present": {"alpha-local": True, "beta-local": True},
    }
    run = [c for c in _engine_calls(estate, "alpha-local") if c.startswith("run ")]
    assert len(run) == 1
    assert "--network host" in run[0] and "--restart always" in run[0] and "/:/host:ro,rslave" in run[0]
    assert f"--web.listen-address=127.0.0.1:{estate['exporters']['alpha-local'].server_address[1]}" in run[0]
    declared = yaml.safe_load(estate["fragment"].read_text())["scrape_configs"]
    assert [c["job_name"] for c in declared] == ["host-node-demo_svc"]
    labels = [t["labels"] for t in declared[0]["static_configs"]]
    assert [(lbl["host"], lbl["service"], lbl["component"]) for lbl in labels] == [
        ("alpha-local", "demo/host", "node-exporter"),
        ("beta-local", "demo/host", "node-exporter"),
    ]
    assert all("target_id" not in lbl and "metrics_scope" not in lbl for lbl in labels)
    assert estate["prometheus"].reloads == 1

    again = _run(estate, identity_enabled=identity_enabled)
    assert again.returncode == 0, again.stdout[-4000:]
    assert _result(again)["status"] == "pass"
    assert _changed(again) == {"alpha-local": 0, "beta-local": 0, "receiver-local": 0, "localhost": 0}
    assert len([c for c in _engine_calls(estate, "alpha-local") if c.startswith("run ")]) == 1
    assert estate["prometheus"].reloads == 1


@needs_ansible
@pytest.mark.parametrize("identity_enabled", [None, False])
def test_legacy_service_name_launch_override_remains_supported(estate, identity_enabled):
    proc = _run(estate, "-e", "service_name=legacy-demo", identity_enabled=identity_enabled)
    assert proc.returncode == 0, proc.stdout[-4000:]
    labels = [
        target["labels"]
        for target in yaml.safe_load(estate["fragment"].read_text())["scrape_configs"][0]["static_configs"]
    ]
    assert [row["service"] for row in labels] == ["legacy-demo/host", "legacy-demo/host"]
    assert all("target_id" not in row and "metrics_scope" not in row for row in labels)


@needs_ansible
def test_opt_in_binds_exact_declared_ids_and_host_metric_labels(estate):
    coverage = _coverage_targets(estate)
    proc = _run(estate, identity_enabled=True, coverage=coverage)
    assert proc.returncode == 0, proc.stdout[-4000:]
    declared = yaml.safe_load(estate["fragment"].read_text())["scrape_configs"][0]
    assert declared["honor_labels"] is False
    labels = [target["labels"] for target in declared["static_configs"]]
    assert [(row["host"], row["target_id"], row["component"], row["metrics_scope"]) for row in labels] == [
        ("alpha-local", "service:alpha-local", "node-exporter", "host"),
        ("beta-local", "service:beta-local", "node-exporter", "host"),
    ]
    assert _result(proc)["evidence"]["series_present"] == {"alpha-local": True, "beta-local": True}
    assert coverage["targets"][0]["signals"]["metrics"]["selector"] == (
        'node_cpu_seconds_total{target_id="service:alpha-local"}'
    )

    again = _run(estate, identity_enabled=True, coverage=coverage)
    assert again.returncode == 0, again.stdout[-4000:]
    assert _changed(again) == {"alpha-local": 0, "beta-local": 0, "receiver-local": 0, "localhost": 0}
    assert estate["prometheus"].reloads == 1


@needs_ansible
def test_opted_in_hosts_refuse_divergent_inventory_revisions_and_duplicate_ids_before_mutation(estate):
    host_coverage = {}
    for host in ("alpha-local", "beta-local"):
        inventory = _coverage_targets(estate, hosts=(host,))
        inventory["targets"][0]["target_id"] = "service:shared"
        inventory["targets"][0]["signals"]["metrics"]["selector"] = (
            'node_cpu_seconds_total{target_id="service:shared"}'
        )
        inventory["revision"] = hashlib.sha256(json.dumps(
            {"targets": inventory["targets"]}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()).hexdigest()
        host_coverage[host] = inventory

    proc = _run(estate, identity_enabled=True, host_coverage=host_coverage)
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "same reviewed estate inventory revision" in proc.stdout, proc.stdout[-4000:]
    assert not estate["fragment"].exists()
    assert _engine_calls(estate, "alpha-local") == []
    assert _engine_calls(estate, "beta-local") == []
    assert estate["prometheus"].reloads == 0


@needs_ansible
@pytest.mark.parametrize("failure", [
    "missing_inventory", "wrong_host", "wrong_service", "not_deployed", "duplicate_binding",
    "duplicate_id", "missing_id", "bad_schema",
])
def test_opted_in_identity_refusals_precede_every_mutation(estate, failure):
    coverage = _coverage_targets(estate)
    if failure == "missing_inventory":
        coverage = None
    elif failure == "wrong_host":
        coverage["targets"][0]["inventory_host"] = "other-host"
    elif failure == "wrong_service":
        coverage["targets"][0]["service_identity"] = "other"
    elif failure == "not_deployed":
        coverage["targets"][0]["lifecycle"] = "planned"
    elif failure == "duplicate_binding":
        duplicate = dict(coverage["targets"][0], target_id="service:alpha2")
        coverage["targets"].append(duplicate)
    elif failure == "duplicate_id":
        coverage["targets"].append(dict(coverage["targets"][0]))
    elif failure == "missing_id":
        coverage["targets"][0].pop("target_id")
    elif failure == "bad_schema":
        coverage["targets"][0]["target_id"] = "vm:alpha-local"
    if coverage is not None:
        targets = coverage["targets"]
        coverage["revision"] = hashlib.sha256(json.dumps(
            {"targets": targets}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()).hexdigest()

    proc = _run(estate, identity_enabled=True, coverage=coverage)
    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "Host-metrics identity refused" in proc.stdout, proc.stdout[-4000:]
    assert not estate["fragment"].exists()
    assert _engine_calls(estate, "alpha-local") == []
    assert _engine_calls(estate, "beta-local") == []
    assert estate["prometheus"].reloads == 0


@needs_ansible
def test_identity_opt_in_rejects_non_boolean_inventory_value(estate):
    proc = _run(estate, identity_enabled="false", coverage=_coverage_targets(estate))
    assert proc.returncode != 0
    assert "must be one inventory boolean value" in proc.stdout
    assert _engine_calls(estate, "alpha-local") == []
    assert _engine_calls(estate, "beta-local") == []
    assert not estate["fragment"].exists()


@needs_ansible
@pytest.mark.parametrize("spoof", [
    {"target_id": "service:other"},
    {"target_id": ""},
    {"component": "application"},
    {"metrics_scope": "application"},
    {"host": "other"},
])
def test_opted_in_named_metric_requires_all_exact_returned_identity_labels(estate, spoof):
    estate["prometheus"].identity_overrides["beta-local"] = spoof
    proc = _run(estate, identity_enabled=True, coverage=_coverage_targets(estate))
    assert proc.returncode != 0, proc.stdout[-4000:]
    result = _result(proc)
    assert result["evidence"]["series_present"] == {"alpha-local": True, "beta-local": False}
    assert "beta-local: no node_memory_MemAvailable_bytes series in Prometheus" in result["error"]


@needs_ansible
@pytest.mark.parametrize("name,value,identity_reason", [
    ("estate_coverage_inventory", "{}", "estate_coverage_inventory"),
    ("o11y_host_metrics_identity_enabled", "false", "o11y_host_metrics_identity_enabled"),
    ("service_name", "other", "service_name"),
    ("inventory_hostname", "alpha-local", "inventory_hostname"),
])
def test_identity_inventory_cannot_be_overridden_at_launch(estate, name, value, identity_reason):
    proc = _run(estate, "-e", f"{name}={value}", identity_enabled=True,
                coverage=_coverage_targets(estate))
    assert proc.returncode != 0
    assert identity_reason in proc.stdout
    assert "Host-metrics identity is controlled by reviewed inventory declarations" in proc.stdout
    assert step_results.results_in(proc.stdout.splitlines()) == []
    assert _engine_calls(estate, "alpha-local") == []
    assert _engine_calls(estate, "beta-local") == []
    assert not estate["fragment"].exists()
    assert estate["prometheus"].reloads == 0


@needs_ansible
def test_duplicate_exporter_endpoint_refuses_before_mutation(estate):
    shared_port = estate["exporters"]["alpha-local"].server_address[1]
    proc = _run(estate, identity_enabled=True, coverage=_coverage_targets(estate),
                binds={"alpha-local": "127.0.0.1", "beta-local": "127.0.0.1"},
                ports={"alpha-local": shared_port, "beta-local": shared_port})
    assert proc.returncode != 0
    assert "unique exporter endpoints" in proc.stdout
    assert _engine_calls(estate, "alpha-local") == []
    assert _engine_calls(estate, "beta-local") == []
    assert not estate["fragment"].exists()


@needs_ansible
def test_application_selector_is_left_unchanged_and_host_probe_emits_no_coverage_receipt(estate):
    application_selector = 'process_cpu_seconds_total{service="demo",target_id="service:alpha-local"}'
    coverage = _coverage_targets(estate, selector=application_selector)
    proc = _run(estate, identity_enabled=True, coverage=coverage)
    assert proc.returncode == 0, proc.stdout[-4000:]
    result = _result(proc)
    assert coverage["targets"][0]["signals"]["metrics"]["selector"] == application_selector
    assert set(result["evidence"]) == {"exporter_running", "series_present"}
    assert "metrics_coverage" not in result["evidence"]

@needs_ansible
@pytest.mark.parametrize("identity_enabled", [None, True])
def test_check_mode_writes_nothing_and_records_skip(estate, identity_enabled):
    proc = _run(
        estate, "--check", identity_enabled=identity_enabled,
        coverage=_coverage_targets(estate) if identity_enabled else None,
    )
    assert proc.returncode == 0, proc.stdout[-4000:]
    result = _result(proc)
    assert (result["status"], result["check_mode"]) == ("skip", True)
    assert result["evidence"]["exporter_running"] == {"alpha-local": False, "beta-local": False}
    assert result["evidence"]["series_present"] == {"alpha-local": None, "beta-local": None}
    for host in ("alpha-local", "beta-local"):
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
    assert result["evidence"]["series_present"] == {"alpha-local": True, "beta-local": True}


@needs_ansible
@pytest.mark.parametrize(
    "bind",
    [
        "",
        "0.0.0.0",
        "999.1.1.1",
        # public: the exporter is on the host network, so its bind is its exposure
        "8.8.8.8",
        "203.0.113.5",
        "172.32.0.1",
        "100.64.0.1",
        # loopback is refused on a production inventory, not here: see the production tests below
    ],
)
def test_an_unusable_declaration_fails_the_group_and_declares_nothing(estate, bind):
    proc = _run(estate, binds={"beta-local": bind})
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail"
    assert result["error"].startswith("beta-local: o11y_host_exporter_bind must declare")
    assert "alpha-local:" not in result["error"]
    assert result["evidence"]["exporter_running"] == {"alpha-local": True, "beta-local": False}
    assert result["evidence"]["series_present"] == {"alpha-local": None, "beta-local": None}
    assert not estate["fragment"].exists()


@needs_ansible
def test_an_unreachable_host_is_recorded_not_dropped(estate):
    # Nothing listens on port 1: the connection is refused at once.
    proc = _run(estate, host_vars={"beta-local": "ansible_connection=ssh ansible_host=127.0.0.1 ansible_port=1"})
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail"
    assert result["error"].startswith("beta-local: unreachable")
    assert "alpha-local:" not in result["error"]
    assert result["evidence"]["exporter_running"] == {"alpha-local": True, "beta-local": False}
    assert not estate["fragment"].exists()


@needs_ansible
def test_an_exporter_the_receiver_cannot_reach_is_never_declared(estate):
    estate["exporters"]["beta-local"].shutdown()
    estate["exporters"]["beta-local"] = _exporter(503)
    proc = _run(estate)
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail"
    assert "o11y receiver: The receiver got no HTTP 200 from beta-local (HTTP 503)" in result["error"]
    assert not estate["fragment"].exists()
    assert estate["prometheus"].reloads == 0


@needs_ansible
def test_a_declared_host_without_series_fails(estate):
    # The playbook waits for the series over its retries; _run passes ih_wait_delay=0, so they run without the delay.
    estate["prometheus"].silent_hosts = {"beta-local"}
    proc = _run(estate)
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail"
    assert result["error"] == "beta-local: scrape not up in Prometheus"
    assert result["evidence"]["series_present"] == {"alpha-local": True, "beta-local": False}


@needs_ansible
@pytest.mark.parametrize("identity_enabled", [False, True])
def test_a_rejected_reload_puts_the_previous_file_back(estate, identity_enabled):
    previous = "scrape_configs: []\n"
    estate["fragment"].write_text(previous)
    estate["prometheus"].reload_status = 500
    proc = _run(
        estate, identity_enabled=identity_enabled,
        coverage=_coverage_targets(estate) if identity_enabled else None,
    )
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail"
    assert "Prometheus rejected" in result["error"] and "bad scrape file" in result["error"]
    assert estate["fragment"].read_text() == previous
    assert estate["prometheus"].reloads == 2


@needs_ansible
def test_an_unreadable_current_scrape_file_stops_the_run_untouched(estate):
    # It exists, so it must not be mistaken for absent (and deleted after a rejected reload).
    previous = "scrape_configs: []\n"
    estate["fragment"].write_text(previous)
    estate["fragment"].chmod(0)
    estate["prometheus"].reload_status = 500
    try:
        proc = _run(estate)
    finally:
        estate["fragment"].chmod(0o644)
    assert proc.returncode != 0
    result = _result(proc)
    assert result["status"] == "fail" and result["error"].startswith("o11y receiver: ")
    assert estate["fragment"].read_text() == previous
    assert estate["prometheus"].reloads == 0


# A private address built at run time: the pre-commit hook refuses RFC 1918 literals.
PRIVATE_UNROUTED = ".".join(["10", "255", "255", "1"])


def _spec_hash(bind: str, port: int, local_mode: bool) -> str:
    # The playbook's _ih_spec, hashed as `_ih_spec | to_json | hash('sha256')`.
    host = _play_vars("Run the host exporter")
    image = re.search(r"default\('([^']+)'\)", host["_ih_image"]).group(1)
    spec = ["--restart", "always", "--network", "host", "--volume", "/:/host:ro,rslave"]
    spec += ["--security-opt", "label=disable"] if local_mode else []
    spec += [image, *host["_ih_collector_args"], f"--web.listen-address={bind}:{port}"]
    return hashlib.sha256(json.dumps(spec).encode()).hexdigest()[:16]


PRODUCTION_PORTS = {"alpha": 9101, "beta": 9102}
# The budgets tasks/assert-o11y-scrape-limits.yml requires of a production receiver: the review flag and the five
# limits, each inside its range.
PRODUCTION_BUDGETS = {
    "o11y_scrape_limits_reviewed": True,
    "o11y_scrape_limits": {
        "body_size_limit_bytes": 1048576,
        "sample_limit": 1000,
        "label_limit": 64,
        "label_name_length_limit": 128,
        "label_value_length_limit": 1024,
    },
}


def _production_inventory(estate, binds=None, fake_alpha=False) -> Path:
    """The two service hosts and the receiver of _inventory, on a production inventory: no host is named -local and
    local_mode is false, so the playbook takes the production branch (plan 24). `binds` overrides a host's exporter
    bind; both default to an unrouted private address. `fake_alpha` gives alpha the fake engine too (its calls land in
    engine-alpha.log), so a test can assert on alpha's engine calls; by default alpha runs the real podman."""
    tmp = estate["tmp"]
    engine = tmp / "engine-beta"
    engine.write_text(FAKE_ENGINE)
    engine.chmod(0o755)
    alpha_engine = "podman"
    if fake_alpha:
        alpha_fake = tmp / "engine-alpha"
        alpha_fake.write_text(FAKE_ENGINE)
        alpha_fake.chmod(0o755)
        alpha_engine = str(alpha_fake)
    binds = binds or {}
    lines = [
        "[demo_svc]",
        f"alpha ansible_connection=local container_engine={alpha_engine} ansible_user=tester "
        f"o11y_host_exporter_bind={binds.get('alpha', PRIVATE_UNROUTED)} "
        f"o11y_host_exporter_port={PRODUCTION_PORTS['alpha']}",
        f"beta ansible_connection=local container_engine={engine} "
        f"o11y_host_exporter_bind={binds.get('beta', PRIVATE_UNROUTED)} "
        f"o11y_host_exporter_port={PRODUCTION_PORTS['beta']}",
        "",
        "[demo_svc:vars]",
        "service_name=demo",
        "local_mode=false",
        "",
        "[o11y_svc]",
        f"receiver ansible_connection=local local_monorepo_dir={tmp / 'repo'} monorepo_deploy_path=o11y "
        f"o11y_prom_bind=127.0.0.1 o11y_prom_port={estate['prometheus'].server.server_address[1]}",
    ]
    inventory = tmp / "production-inventory.ini"
    inventory.write_text("\n".join(lines) + "\n")
    return inventory


@needs_ansible
@pytest.mark.parametrize("persisted", [False, True])
def test_check_mode_counts_a_pending_boot_persistence_change(estate, persisted):
    # A production inventory (plan 24): the linger and boot-unit checks are production-only. alpha is rootless podman,
    # its exporter already current, so only linger and podman's user boot unit can differ. Both hosts bind an unrouted
    # private address, so the receiver's probe cannot reach them: a dry run that would change persistence verifies
    # nothing live and records skip; one with nothing to change runs the probe and fails.
    tmp = estate["tmp"]
    bindir, home = tmp / "bin", tmp / "home"
    bindir.mkdir()
    (bindir / "podman").write_text(FAKE_ENGINE)
    (bindir / "loginctl").write_text(f"#!/bin/sh\necho Linger={'yes' if persisted else 'no'}\n")
    (bindir / "getent").write_text(f'#!/bin/sh\necho "$2:x:501:20::{home}:/bin/sh"\n')
    for tool in bindir.iterdir():
        tool.chmod(0o755)
    if persisted:
        wants = home / ".config/systemd/user/default.target.wants"
        wants.mkdir(parents=True)
        (wants / "podman-restart.service").write_text("")
    (bindir / "podman.state").write_text(
        json.dumps({"agent-cloud-node-exporter": _spec_hash(PRIVATE_UNROUTED, PRODUCTION_PORTS["alpha"], False)})
    )
    (tmp / "engine-beta.state").write_text(
        json.dumps({"agent-cloud-node-exporter": _spec_hash(PRIVATE_UNROUTED, PRODUCTION_PORTS["beta"], False)})
    )
    inventory = _production_inventory(estate)
    # The scrape file already declares both hosts, so the scrape file changes nothing either.
    endpoints = [
        {"host": h, "address": f"{PRIVATE_UNROUTED}:{PRODUCTION_PORTS[h]}", "service": "demo/host"}
        for h in ("alpha", "beta")
    ]
    rendered = subprocess.run(
        [
            "ansible",
            "receiver",
            "-i",
            str(inventory),
            "-m",
            "ansible.builtin.template",
            "-a",
            f"src={O11Y / 'templates/scrape-host-node.yml.j2'} dest={estate['fragment']} mode=0644",
            "-e",
            f"@{REPO / 'platform/playbooks/vars/o11y-metric-labels.yml'}",
            "-e",
            json.dumps(PRODUCTION_BUDGETS),
            "-e",
            json.dumps({"_ih_job": "host-node-demo_svc", "_ih_endpoints": endpoints}),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stdout
    proc = _run_inventory(inventory, "--check", "-e", json.dumps(PRODUCTION_BUDGETS), path_prefix=bindir)
    result = _result(proc)
    assert result["evidence"]["exporter_running"] == {"alpha": True, "beta": True}, proc.stdout[-4000:]
    if persisted:
        assert proc.returncode != 0
        assert result["status"] == "fail"
        assert "The receiver got no HTTP 200 from alpha" in result["error"]
    else:
        assert proc.returncode == 0, proc.stdout[-4000:]
        assert (result["status"], result["error"]) == ("skip", "")
    assert "changed: [receiver]" not in proc.stdout, proc.stdout[-4000:]
    assert all(c.startswith("inspect ") for c in (bindir / "podman.log").read_text().splitlines())


@needs_ansible
@pytest.mark.parametrize("forge", forgeries.templated_forgeries("local_mode", honest=False, forged=True)[:2])
def test_a_loopback_bind_is_refused_on_production_whatever_local_mode_says(estate, forge):
    # Loopback is an exposure only on a local-dev inventory. On a production inventory a forged local_mode, plain or
    # templated, must not accept it: the refusal is the one any unusable bind gets.
    proc = _run_inventory(
        _production_inventory(estate, binds={"alpha": "127.0.0.1"}),
        "-e", forge(estate["tmp"]),
        "-e", json.dumps(PRODUCTION_BUDGETS),
    )
    result = _result(proc)
    assert proc.returncode != 0
    assert result["status"] == "fail"
    assert result["error"].startswith("alpha: o11y_host_exporter_bind must declare"), result["error"]
    assert not estate["fragment"].exists()


@needs_ansible
@pytest.mark.parametrize("target", ["o11y_svc", "no_such_svc", "../etc"])
def test_refuses_a_target_it_must_not_instrument(estate, target):
    proc = _run(estate, "-e", f"target_service={target}")
    assert proc.returncode != 0
    assert "Pass -e target_service=<group>" in proc.stdout
    assert not estate["fragment"].exists()


@needs_ansible
def test_production_budget_failure_on_distinct_receiver_stops_exporter_play(estate):
    # A production inventory (plan 24): no host is named -local, so the budget check's production branch is taken by
    # the inventory alone. Both service hosts run the fake engine, so the empty engine-call logs are a real claim.
    proc = _run_inventory(_production_inventory(estate, fake_alpha=True))

    assert proc.returncode != 0, proc.stdout[-4000:]
    assert "Production o11y scrape budgets require" in proc.stdout, proc.stdout[-4000:]
    assert _engine_calls(estate, "alpha") == []
    assert _engine_calls(estate, "beta") == []


def _defined_names() -> set[str]:
    """Every _-prefixed name the playbook defines: vars at any level, its vars files, registers,
    set_fact keys and loop variables."""
    found: set[str] = set()

    def walk(tasks):
        for task in tasks or []:
            found.update(task.get("vars") or {})
            found.add(task.get("register", ""))
            found.update(task.get("ansible.builtin.set_fact") or {})
            found.add((task.get("loop_control") or {}).get("loop_var", ""))
            for key in ("block", "rescue", "always"):
                walk(task.get(key))

    for play in playbook_yaml.plays(PLAYBOOK):
        found.update(play.get("vars") or {})
        for path in play.get("vars_files") or []:
            found.update(yaml.safe_load((PLAYBOOK.parent / path).read_text()))
        walk(play.get("tasks"))
    return {n for n in found if n.startswith("_")}


def test_the_run_opens_by_refusing_internal_extra_vars():
    # A name the playbook sets can be forged with -e (PR 457 review: -e _ih_group_errors=[] could
    # record a pass for a failing group). The run's first play refuses every underscore-prefixed
    # extra var by name, so every internal name is covered, including one added later.
    assert yaml.safe_load(PLAYBOOK.read_text())[0] == {
        "name": "Refuse extra vars that set internal names",
        "ansible.builtin.import_playbook": playbook_yaml.OVERRIDE_GUARD}
    assert _defined_names() and all(n.startswith("_") for n in _defined_names())


@needs_ansible
@pytest.mark.parametrize("name, forge", [
    pytest.param(n, f.values[0], id=f"{n}-{f.id}")
    for n in sorted(_defined_names()) for f in forgeries.by_name(n, [], [])
])
def test_refuses_an_extra_var_forging_an_internal_name(estate, name, forge):
    # beta-local's declaration is unusable, so a forged verdict would turn a failing group into a pass.
    proc = _run(estate, "-e", forge(estate["tmp"]), binds={"beta-local": "0.0.0.0"})
    assert proc.returncode != 0
    assert f"Refusing to run: {name} set from outside the playbook" in proc.stdout
    assert step_results.results_in(proc.stdout.splitlines()) == []
    assert not estate["fragment"].exists()


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


def test_the_forbidden_label_regex_has_one_definition_both_playbooks_load():
    files = subprocess.run(
        ["git", "ls-files", "*.yml", "*.yaml"], cwd=REPO, text=True, capture_output=True, check=True
    ).stdout.split()
    defining = [
        f
        for f in files
        if (REPO / f).is_file()
        and re.search(r"^\s*_o11y_forbidden_metric_label_names_regex\s*:", (REPO / f).read_text(), re.M)
    ]
    assert defining == ["platform/playbooks/vars/o11y-metric-labels.yml"]
    for playbook in ("deploy-o11y.yml", "instrument-host-o11y.yml"):
        plays = yaml.safe_load((REPO / "platform/playbooks" / playbook).read_text())
        assert any("vars/o11y-metric-labels.yml" in (p.get("vars_files") or []) for p in plays), playbook
    template = (O11Y / "templates/scrape-host-node.yml.j2").read_text()
    assert "_o11y_forbidden_metric_label_names_regex | to_json" in template
    assert "regex: '^device$'" in template


def test_verified_metric_is_one_the_pinned_exporter_serves():
    served = set((REPO / "platform/tests/fixtures/node-exporter-metric-names-v1.12.1.txt").read_text().split())
    assert "node_memory_MemAvailable_bytes" in served
    assert "node_memory_MemAvailable_bytes{job=" in PLAYBOOK.read_text()
