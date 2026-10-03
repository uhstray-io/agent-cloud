"""Synthetic inference probe on the o11y host (change inference-telemetry-production, task 3.3).

The probe script runs against a stub curl, so the tests exercise the real script: the metrics it
writes, how it writes them (temporary file + rename in the same directory), and where the API key
travels (curl's stdin, never an argument list or the script's output). The deploy wiring is
evaluated from deploy-o11y.yml itself: with the probe disabled, the secrets read and the files
rendered are exactly what they were before the probe existed.
"""

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "platform/services/o11y/deployment"
PROBE_DIR = DEPLOY / "probe"
SCRIPT = PROBE_DIR / "inference-probe.sh"
PLAYBOOK = REPO / "platform/playbooks/deploy-o11y.yml"
TEXTFILE_DIR = "/var/lib/node_exporter/textfile"

KEY = "sk-probe-TESTKEY-0123456789abcdef"
MODEL = "served-model-a"
URL = "https://inference.example.test/v1"

GOOD_BODY = '{"id":"x","choices":[{"index":0,"message":{"role":"assistant","content":"pong"},"finish_reason":"stop"}]}'

# A stub curl: records its argv and stdin, writes the canned body to --output, prints the canned
# write-out, exits with the canned status. The key must reach it on stdin only.
STUB_CURL = r"""#!/usr/bin/env bash
printf '%s\n' "$@" > "$STUB_DIR/argv"
cat > "$STUB_DIR/stdin"
out=""
prev=""
for a in "$@"; do
  [ "$prev" = "--output" ] && out="$a"
  prev="$a"
done
[ -n "$out" ] && cat "$STUB_DIR/body" > "$out"
cat "$STUB_DIR/writeout"
exit "$(cat "$STUB_DIR/rc")"
"""

# A PATH shim for mv that logs each call before delegating, to observe the rename.
STUB_MV = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DIR/mv.log"
exec /bin/mv "$@"
"""


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def probe(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_exec(stub / "curl", STUB_CURL)
    _write_exec(bindir / "mv", STUB_MV)

    def run(*, http="200", latency="0.532", rc=0, body=GOOD_BODY, env=None):
        (stub / "body").write_text(body)
        (stub / "writeout").write_text(f"{http} {latency}")
        (stub / "rc").write_text(str(rc))
        for name in ("argv", "stdin"):
            (stub / name).unlink(missing_ok=True)
        e = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STUB_DIR": str(stub),
            "INFERENCE_PROBE_CURL": str(stub / "curl"),
            "INFERENCE_PROBE_URL": URL,
            "INFERENCE_PROBE_MODEL": MODEL,
            "INFERENCE_PROBE_KEY": KEY,
            "INFERENCE_PROBE_TIMEOUT_SECONDS": "30",
            "INFERENCE_PROBE_TEXTFILE_DIR": str(textfile),
        }
        e.update(env or {})
        e = {k: v for k, v in e.items() if v is not None}
        done = subprocess.run(["bash", str(SCRIPT)], env=e, text=True, capture_output=True)
        metrics = textfile / "inference_probe.prom"
        return done, (metrics.read_text() if metrics.exists() else None)

    run.stub = stub
    run.textfile = textfile
    return run


def _samples(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.fullmatch(r'([a-z_]+)\{model_name="([^"]*)"\} (\S+)', line)
        assert m, f"unparseable exposition line: {line!r}"
        out[m.group(1)] = (m.group(2), m.group(3))
    return out


# ── metrics ──────────────────────────────────────────────────────────────────

def test_a_successful_completion_records_success_and_latency(probe):
    done, text = probe()
    assert done.returncode == 0, done.stderr
    s = _samples(text)
    assert s["inference_probe_success"] == (MODEL, "1")
    assert s["inference_probe_latency_seconds"] == (MODEL, "0.532")
    assert re.fullmatch(r"[0-9]{10,}", s["inference_probe_last_run_timestamp_seconds"][1])
    assert "# TYPE inference_probe_success gauge" in text
    assert "# TYPE inference_probe_latency_seconds gauge" in text


@pytest.mark.parametrize("http,rc,body,why", [
    ("401", 0, '{"error":"unauthorized"}', "rejected key"),
    ("502", 0, "bad gateway", "upstream down"),
    ("000", 28, "", "latency budget exceeded (curl timeout)"),
    ("200", 0, '{"object":"error"}', "200 without a completion"),
])
def test_a_failed_completion_records_failure_with_its_latency(probe, http, rc, body, why):
    done, text = probe(http=http, rc=rc, body=body, latency="30.004")
    assert done.returncode == 0, why
    s = _samples(text)
    assert s["inference_probe_success"] == (MODEL, "0"), why
    assert s["inference_probe_latency_seconds"] == (MODEL, "30.004"), why
    assert f"HTTP {http}" in done.stderr


def test_the_request_is_one_short_chat_completion_at_effort_none(probe):
    probe()
    argv = (probe.stub / "argv").read_text().splitlines()
    # curl honours --disable (skip ~/.curlrc) only as its FIRST argument (curl(1), -q).
    assert argv[0] == "--disable"
    assert argv[-1] == f"{URL}/chat/completions"
    payload = json.loads(argv[argv.index("--data-binary") + 1])
    assert payload["model"] == MODEL
    assert payload["reasoning_effort"] == "none"
    assert payload["stream"] is False
    assert payload["max_tokens"] <= 16
    assert argv[argv.index("--max-time") + 1] == "30"
    assert argv[argv.index("--proto") + 1] == "=https"


HAS_JQ = shutil.which("jq") is not None
# The script reads the body with jq when the host has it, else python3; run both.
PARSERS = [
    pytest.param("jq", marks=pytest.mark.skipif(not HAS_JQ, reason="jq not installed")),
    pytest.param("no-such-jq-binary", id="python3"),
]


@pytest.mark.parametrize("parser", PARSERS)
@pytest.mark.parametrize("body,expect,why", [
    (GOOD_BODY, "1", "a choice with content"),
    ('{"choices":[{"message":{"content":""},"finish_reason":"length"}]}', "1", "a finish_reason alone"),
    ('{"choices":[]}', "0", "empty choices"),
    ('{"choices":[{"message":{"content":""},"finish_reason":null}]}', "0", "a choice that produced nothing"),
    ('{"choices":[{}]}', "0", "an empty choice object"),
    ('{"choices":"pong"}', "0", "choices that is not a list"),
    ('<html>"choices" "finish_reason"</html>', "0", "a non-JSON page naming the fields"),
])
def test_a_200_succeeds_only_with_a_choice_that_produced_output(probe, parser, body, expect, why):
    done, text = probe(body=body, env={"INFERENCE_PROBE_JQ": parser})
    assert done.returncode == 0, why
    assert _samples(text)["inference_probe_success"][1] == expect, why


@pytest.mark.parametrize("env,why", [
    ({"INFERENCE_PROBE_KEY": None}, "no key rendered"),
    ({"INFERENCE_PROBE_URL": "http://inference.example.test/v1"}, "cleartext URL"),
    ({"INFERENCE_PROBE_KEY": 'abc" \nurl = "https://evil.test'}, "key that escapes the curl config quote"),
    ({"INFERENCE_PROBE_TIMEOUT_SECONDS": "0"}, "zero latency budget"),
])
def test_a_misconfiguration_is_a_recorded_failure_and_curl_is_not_called(probe, env, why):
    done, text = probe(env=env)
    assert done.returncode == 0, why
    assert _samples(text)["inference_probe_success"][1] == "0", why
    assert not (probe.stub / "argv").exists(), f"curl ran despite: {why}"


# ── atomic write ─────────────────────────────────────────────────────────────

def test_metrics_are_published_by_renaming_a_temporary_file_in_the_same_directory(probe):
    target = probe.textfile / "inference_probe.prom"
    target.write_text("old\n")
    before = target.stat().st_ino
    done, text = probe()
    assert done.returncode == 0
    # A rename replaces the directory entry; an in-place rewrite would keep the inode.
    assert target.stat().st_ino != before
    calls = (probe.stub / "mv.log").read_text().splitlines()
    assert len(calls) == 1
    flag, src, dst = calls[0].split(" ")
    assert flag == "-f"
    assert Path(src).parent == probe.textfile and Path(src).name.startswith(".inference_probe.prom.")
    # node_exporter reads only *.prom, so the temporary name is never collected.
    assert not Path(src).name.endswith(".prom")
    assert Path(dst) == target
    assert list(probe.textfile.iterdir()) == [target], "temporary file left behind"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_textfile_directory_fails_and_keeps_the_previous_sample(probe):
    target = probe.textfile / "inference_probe.prom"
    target.write_text("previous sample\n")
    probe.textfile.chmod(0o555)
    try:
        done, text = probe()
    finally:
        probe.textfile.chmod(0o755)
    assert done.returncode == 1
    assert text == "previous sample\n"


# ── the key ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("http,rc", [("200", 0), ("401", 0), ("000", 28)])
def test_the_key_reaches_curl_on_stdin_only_and_is_never_printed(probe, http, rc):
    done, text = probe(http=http, rc=rc)
    argv = (probe.stub / "argv").read_text()
    assert KEY not in argv, "key in curl's argument list (visible in ps)"
    assert (probe.stub / "stdin").read_text() == f'header = "Authorization: Bearer {KEY}"\n'
    assert KEY not in done.stdout + done.stderr
    assert KEY not in text


def test_the_script_holds_no_literal_key_and_passes_shellcheck():
    src = SCRIPT.read_text()
    assert not re.search(r"Bearer [A-Za-z0-9._~+/=-]{8,}", src)
    assert not re.search(r"\bsk-[A-Za-z0-9]{8,}", src)
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    subprocess.run(["shellcheck", "-S", "warning", str(SCRIPT)], check=True)


# ── units, env file, overlay ─────────────────────────────────────────────────

def _render(path: Path, **ctx) -> str:
    return Environment(undefined=StrictUndefined, keep_trailing_newline=True).from_string(
        path.read_text()).render(**ctx)


def test_the_timer_fires_the_probe_service_every_five_minutes():
    timer = (PROBE_DIR / "inference-probe.timer").read_text()
    assert re.search(r"^OnCalendar=\*:0/5$", timer, re.M)
    assert re.search(r"^Unit=inference-probe\.service$", timer, re.M)
    assert re.search(r"^WantedBy=timers\.target$", timer, re.M)


def test_the_service_unit_reads_the_key_from_its_environment_file_not_its_command_line():
    unit = _render(PROBE_DIR / "inference-probe.service.j2", _probe_user="deploy",
                   _probe_env_file="/srv/agent-cloud/o11y/probe/inference-probe.env",
                   _probe_script="/srv/agent-cloud/o11y/probe/inference-probe.sh",
                   _probe_textfile_dir=TEXTFILE_DIR)
    lines = dict(ln.split("=", 1) for ln in unit.splitlines() if "=" in ln and not ln.startswith("#"))
    assert lines["Type"] == "oneshot"
    assert lines["User"] == "deploy"
    assert lines["EnvironmentFile"].endswith("/probe/inference-probe.env")
    assert lines["ExecStart"] == "/bin/bash /srv/agent-cloud/o11y/probe/inference-probe.sh"
    assert lines["ReadWritePaths"] == TEXTFILE_DIR
    assert lines["ProtectSystem"] == "strict"
    assert lines["NoNewPrivileges"] == "yes"
    # The unit outlives the curl budget, so systemd never kills a sample mid-write.
    assert int(lines["TimeoutStartSec"]) > 30


def test_the_env_file_carries_the_shared_read_key_and_inventory_settings():
    env = _render(PROBE_DIR / "inference-probe.env.j2", o11y_inference_probe_url=URL,
                  o11y_inference_probe_model=MODEL, o11y_inference_probe_key_field="client_o11y-probe",
                  secrets={"client_o11y-probe": KEY, "grafana_admin_password": "other"},
                  _probe_textfile_dir=TEXTFILE_DIR)
    values = dict(ln.split("=", 1) for ln in env.splitlines() if "=" in ln and not ln.startswith("#"))
    assert values == {
        "INFERENCE_PROBE_URL": URL,
        "INFERENCE_PROBE_MODEL": MODEL,
        "INFERENCE_PROBE_KEY": KEY,
        "INFERENCE_PROBE_TIMEOUT_SECONDS": "30",
        "INFERENCE_PROBE_TEXTFILE_DIR": TEXTFILE_DIR,
    }


def test_the_overlay_is_the_base_exporter_command_plus_only_the_textfile_collector():
    base_svc = yaml.safe_load((DEPLOY / "compose.yml").read_text())["services"]["node-exporter"]
    base = base_svc["command"]
    overlay = yaml.safe_load((PROBE_DIR / "compose.textfile.yml").read_text())
    assert list(overlay["services"]) == ["node-exporter"]
    # `restart` is repeated only for the per-directory restart-policy guard, and must equal the base.
    assert sorted(overlay["services"]["node-exporter"]) == ["command", "restart"]
    assert overlay["services"]["node-exporter"]["restart"] == base_svc["restart"]
    assert overlay["services"]["node-exporter"]["command"] == base + [
        "--collector.textfile",
        f"--collector.textfile.directory=/host{TEXTFILE_DIR}",
    ]
    # The host directory reaches the container through the existing read-only root mount.
    vols = yaml.safe_load((DEPLOY / "compose.yml").read_text())["services"]["node-exporter"]["volumes"]
    assert "/:/host:ro,rslave" in vols


def test_the_key_file_is_gitignored():
    done = subprocess.run(["git", "check-ignore", "-q", str(PROBE_DIR / "inference-probe.env")], cwd=REPO)
    assert done.returncode == 0


# ── deploy wiring: flag off changes nothing ──────────────────────────────────

def _plays():
    return yaml.safe_load(PLAYBOOK.read_text())


def _phase(prefix):
    return next(p for p in _plays() if str(p.get("name", "")).startswith(prefix))


def test_every_probe_task_is_gated_on_the_inventory_flag():
    p1 = _phase("Phase 1")
    probe_tasks = [t for t in p1["tasks"] if "probe" in t.get("name", "").lower()]
    assert len(probe_tasks) == 7
    for t in probe_tasks:
        assert "_probe_enabled" in json.dumps(t.get("when")), t["name"]
    install = next(t for t in probe_tasks if t["name"] == "Install the synthetic inference probe")
    assert install["when"] == "_probe_enabled | bool"
    # Under --check the unit files are only simulated: a first enable has no timer to start.
    start, = (t for t in install["block"] if "ansible.builtin.systemd_service" in t)
    assert start["when"] == "not ansible_check_mode"
    p3 = _phase("Phase 3")
    verify = next(t for t in p3["tasks"] if t.get("name") == "Verify the synthetic inference probe after a real deploy")
    assert "o11y_inference_probe_enabled | default(false) | bool" in verify["when"]
    assert "not ansible_check_mode" in verify["when"]
    assert p1["vars"]["_probe_textfile_dir"] == TEXTFILE_DIR


needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _evaluate(tmp_path, host_vars):
    """Evaluate the deploy's own secrets/env/overlay expressions for one host."""
    p1 = _phase("Phase 1")
    p2 = _phase("Phase 2")
    run = next(t for t in p2["tasks"] if t.get("name") == "Run deploy.sh (container lifecycle)")
    names = ["_probe_enabled", "_probe_textfile_dir", "_shared_reads", "_env_templates"]
    pv = {k: p1["vars"][k] for k in names}
    pv["_overlays"] = run["environment"]["COMPOSE_OVERLAYS"]
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {**pv, **host_vars},
        "tasks": [{"ansible.builtin.debug": {"msg": "WIRING {{ {'shared': _shared_reads, 'env': _env_templates, "
                   "'overlays': _overlays} | to_json }}"}}],
    }]
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env, text=True,
                          capture_output=True, check=True, stdin=subprocess.DEVNULL)
    line = next(ln for ln in done.stdout.splitlines() if "WIRING " in ln)
    return json.loads(json.loads(line.split('"msg": ', 1)[1]).split("WIRING ", 1)[1])


PRE_PROBE_SHARED = [{"from_service": "authentik", "read_keys": ["grafana_oidc_client_secret"]}]
PRE_PROBE_ENV = [{"src": "env.j2", "dest": ".env", "mode": "0600"}]


@needs_ansible
@pytest.mark.parametrize("host_vars", [{}, {"o11y_inference_probe_enabled": False}])
def test_probe_off_reads_and_renders_exactly_the_pre_probe_set(tmp_path, host_vars):
    w = _evaluate(tmp_path, host_vars)
    assert w["shared"] == PRE_PROBE_SHARED
    assert w["env"] == PRE_PROBE_ENV
    assert w["overlays"] == ""


@needs_ansible
def test_probe_on_reads_its_key_from_the_owning_service_into_a_separate_file(tmp_path):
    w = _evaluate(tmp_path, {"o11y_inference_probe_enabled": True,
                             "o11y_inference_probe_key_field": "client_o11y-probe"})
    assert w["shared"] == PRE_PROBE_SHARED + [{"from_service": "agentgateway", "read_keys": ["client_o11y-probe"]}]
    assert w["env"] == PRE_PROBE_ENV + [
        {"src": "../probe/inference-probe.env.j2", "dest": "probe/inference-probe.env", "mode": "0600"}]
    assert w["overlays"] == "probe/compose.textfile.yml"
    assert (DEPLOY / "templates" / w["env"][1]["src"]).resolve() == PROBE_DIR / "inference-probe.env.j2"
    assert (DEPLOY / w["overlays"]).is_file()


# ── disabled cleanup: each artefact on its own, sudo only for one that exists ─

CLEANUP_FIRST = "Look for probe artefacts an earlier enable left on the host"
CLEANUP_LAST = "Remove the probe key file while the probe is disabled"


def _cleanup(tmp_path, present, *, forbid_become):
    """Run the deploy's own disabled-path tasks against temporary unit/textfile/deploy dirs.

    systemd calls become debug markers (no systemd here). With forbid_become the become
    executable does not exist, so any privileged step that runs fails the play: proof that a
    host with nothing to clean is never escalated. Otherwise `become` is stripped so the real
    file removals run unprivileged in the temporary tree.
    """
    p1 = _phase("Phase 1")
    names = [t.get("name") for t in p1["tasks"]]
    tasks = p1["tasks"][names.index(CLEANUP_FIRST):names.index(CLEANUP_LAST) + 1]
    harness_tasks = []
    for t in tasks:
        t = dict(t)
        if "ansible.builtin.systemd_service" in t:
            t.pop("ansible.builtin.systemd_service")
            t["ansible.builtin.debug"] = {"msg": f"SYSTEMD {t['name']}"}
        if not forbid_become:
            t.pop("become", None)
        harness_tasks.append(t)
    units, textfile, deploy = tmp_path / "units", tmp_path / "textfile", tmp_path / "deploy"
    for d in (units, textfile, deploy / "probe"):
        d.mkdir(parents=True)
    paths = {
        "timer": units / "inference-probe.timer",
        "service": units / "inference-probe.service",
        "metric": textfile / "inference_probe.prom",
        "key": deploy / "probe" / "inference-probe.env",
    }
    for name in present:
        paths[name].write_text("x\n")
    pv = {k: p1["vars"][k] for k in ("_probe_host_artefacts", "_probe_present")}
    pv.update({"_probe_enabled": False, "_probe_unit_dir": str(units), "_probe_textfile_dir": str(textfile),
               "_deploy_dir": str(deploy)})
    if forbid_become:
        pv["ansible_become_exe"] = str(tmp_path / "no-such-sudo")
    harness = [{"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": pv,
                "tasks": harness_tasks}]
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env, text=True,
                          capture_output=True, stdin=subprocess.DEVNULL)
    return done, {k: p.exists() for k, p in paths.items()}


@needs_ansible
def test_a_host_that_never_ran_the_probe_is_never_escalated(tmp_path):
    done, left = _cleanup(tmp_path, [], forbid_become=True)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "SYSTEMD" not in done.stdout
    assert "changed=0" in done.stdout


@needs_ansible
@pytest.mark.parametrize("present,stops,reloads", [
    (["timer", "service", "metric", "key"], True, True),
    (["service", "metric"], False, True),       # install interrupted before the timer landed
    (["metric"], False, False),                 # removal interrupted after the units went
    (["timer"], True, True),
    (["key"], False, False),
])
def test_every_leftover_artefact_is_removed_on_its_own(tmp_path, present, stops, reloads):
    done, left = _cleanup(tmp_path, present, forbid_become=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not any(left.values()), left
    assert ("SYSTEMD Stop and disable a leftover probe timer" in done.stdout) is stops
    assert ("SYSTEMD Reload systemd after removing a probe unit" in done.stdout) is reloads


# ── deploy verification: only a fresh sample for the configured model passes ─

def _verify_task():
    p3 = _phase("Phase 3")
    block = next(t for t in p3["tasks"] if t.get("name") == "Verify the synthetic inference probe after a real deploy")
    read_name = "Read the forced probe sample from the private Prometheus API"
    read = next(t for t in block["block"] if t["name"] == read_name)
    return block, read


def _row(name, model, value):
    return {"metric": {"__name__": name, "job": "receiver-host", "model_name": model}, "value": [1790000000.0, value]}


STARTED = "1790978600"


def _fresh(model="served-model-a", ts="1790978605"):
    return [_row("inference_probe_success", model, "1"), _row("inference_probe_latency_seconds", model, "0.4"),
            _row("inference_probe_last_run_timestamp_seconds", model, ts)]


def _no_latency():
    return [r for r in _fresh() if r["metric"]["__name__"] != "inference_probe_latency_seconds"]


@needs_ansible
@pytest.mark.parametrize("rows,expect,why", [
    (_fresh(), True, "fresh sample for the configured model"),
    (_fresh(ts=STARTED), True, "sample stamped in the second the forced run started"),
    (_fresh(ts="1790978000"), False, "stale sample from an earlier run"),
    (_fresh(model="other-model"), False, "fresh sample for another model"),
    (_fresh()[:2], False, "last-run timestamp series missing"),
    (_no_latency(), False, "latency missing"),
    (_fresh(ts="1790978000") + _fresh(model="other-model"), False, "stale here, fresh only elsewhere"),
    (_no_latency() + _fresh(model="other-model"), False, "latency only from another model"),
    (_fresh() + _fresh(model="other-model", ts="1"), True, "extra series for another model are ignored"),
])
def test_verification_accepts_only_a_fresh_sample_for_the_configured_model(tmp_path, rows, expect, why):
    block, read = _verify_task()
    stdout = json.dumps({"status": "success", "data": {"resultType": "vector", "result": rows}})
    harness = [{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {**block["vars"], "o11y_inference_probe_model": MODEL,
                 "_probe_metrics": {"rc": 0, "stdout": stdout}, "_probe_started": {"stdout": STARTED + "\n"}},
        "tasks": [{"ansible.builtin.debug": {"msg": "UNTIL {{ (" + read["until"] + ") | to_json }}"}},
                  {"ansible.builtin.debug": {"msg": "QUERY {{ _probe_query | urlencode }}"}}],
    }]
    path = tmp_path / "harness.yml"
    path.write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    done = subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], cwd=REPO, env=env, text=True,
                          capture_output=True, check=True, stdin=subprocess.DEVNULL)
    until = next(ln for ln in done.stdout.splitlines() if "UNTIL " in ln)
    assert until.rstrip().endswith(f'UNTIL {str(expect).lower()}"'), (why, until)
    query = unquote(next(ln for ln in done.stdout.splitlines() if "QUERY " in ln).split("QUERY ", 1)[1])
    assert f'model_name="{MODEL}"' in query
    for name in ("inference_probe_success", "inference_probe_latency_seconds",
                 "inference_probe_last_run_timestamp_seconds"):
        assert name in query
    assert "{{ _probe_query | urlencode }}" in read["ansible.builtin.command"]["argv"][-1]


def test_the_readme_names_the_probe_alert_rules_that_exist():
    readme = (DEPLOY / "README.md").read_text()
    section = readme.split("## Synthetic inference probe", 1)[1].split("\n## ", 1)[0]
    alerts = (DEPLOY / "templates" / "alerts.yml.j2").read_text()
    assert "No alert rule on `inference_probe_success`" not in section
    for uid in ("inference_probe_failing", "inference_probe_stale"):
        assert f"`{uid}`" in section, uid
        assert f"'{uid}'" in alerts, uid
    assert "o11y_inference_probe_enabled" in section
