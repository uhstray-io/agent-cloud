"""The production budget receipt reports per-day Loki ingestion and Prometheus TSDB growth.

inference-telemetry-production task 4.1 asks for the Prometheus TSDB size and Loki ingestion
per day. The receipt reads them through the same read-only exec path as the head-series count.
A metric gap (an empty vector) is reported as `unavailable`; it must not fail the receipt. The
fold task is run by ansible itself, so the Jinja the production run evaluates is the Jinja
tested here.
"""

import json
import os
import subprocess
from pathlib import Path

import yaml
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "platform/playbooks/verify-o11y-production-budgets.yml"

QUERIES = {
    "loki_ingested_bytes_24h": 'sum(increase(loki_distributor_bytes_received_total{job="loki"}[24h]))',
    "loki_ingested_lines_24h": 'sum(increase(loki_distributor_lines_received_total{job="loki"}[24h]))',
    "prometheus_samples_appended_24h": (
        'sum(increase(prometheus_tsdb_head_samples_appended_total{job="prometheus"}[24h]))'
    ),
    "prometheus_tsdb_blocks_bytes": 'sum(prometheus_tsdb_storage_blocks_bytes{job="prometheus"})',
    "prometheus_tsdb_blocks_bytes_change_24h": (
        'sum(delta(prometheus_tsdb_storage_blocks_bytes{job="prometheus"}[24h]))'
    ),
}

READ = "Read daily Loki ingestion and Prometheus TSDB growth"
FOLD = "Fold the daily ingestion readings into named figures"
REPORT = "Report the sanitized production budget receipt"


def _tasks() -> list:
    plays = yaml.safe_load(PLAYBOOK.read_text())
    return next(p for p in plays if p.get("name") == "Read production retention and cardinality settings")["tasks"]


def _task(name: str) -> dict:
    return next(t for t in _tasks() if t["name"] == name)


def test_the_five_queries_are_read_verbatim():
    loop = _task(READ)["loop"]
    assert {i["name"]: i["query"] for i in loop} == QUERIES
    assert len(loop) == len(QUERIES)
    argv = _task(READ)["ansible.builtin.command"]["argv"]
    assert argv[-1] == "http://prometheus:9090/api/v1/query"
    assert argv[argv.index("--data-urlencode") + 1] == "query={{ item.query }}"


def test_the_reads_are_read_only_and_not_swallowed():
    task = _task(READ)
    assert task["changed_when"] is False and task["check_mode"] is False
    # A curl/HTTP failure fails the task like the head-series read; only an empty vector is a gap.
    assert "ignore_errors" not in task and "failed_when" not in task
    assert "no_log" not in task
    assert "when" not in _task(FOLD)


def test_the_figures_reach_the_receipt():
    msg = _task(REPORT)["ansible.builtin.debug"]["msg"]
    assert "_daily_ingestion" in msg["daily_ingestion"]


def _reading(name: str, body: dict) -> dict:
    return {"item": {"name": name}, "stdout": json.dumps(body)}


def _vector(value: str) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, value]}]}}


@needs_ansible
def test_figures_are_numbers_and_an_empty_vector_is_unavailable(tmp_path):
    readings = [
        _reading("loki_ingested_bytes_24h", _vector("1234.6")),
        _reading("loki_ingested_lines_24h", {"status": "success", "data": {"resultType": "vector", "result": []}}),
        _reading("prometheus_samples_appended_24h", _vector("7")),
        _reading("prometheus_tsdb_blocks_bytes", _vector("NaN")),
        _reading("prometheus_tsdb_blocks_bytes_change_24h", _vector("-2.5e+03")),
    ]
    play = [{
        "hosts": "localhost", "gather_facts": False,
        "vars": {"_daily_ingestion_reads": {"results": readings}},
        "tasks": [
            _task(FOLD),
            {"name": "fold is a mapping", "ansible.builtin.assert": {"that": "_daily_ingestion is mapping"}},
            {"name": "report", "ansible.builtin.set_fact":
                {"_figures": _task(REPORT)["ansible.builtin.debug"]["msg"]["daily_ingestion"]}},
            {"name": "show", "ansible.builtin.debug": {"var": "_figures"}},
        ],
    }]
    book = tmp_path / "play.yml"
    book.write_text(yaml.safe_dump(play))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path),
               ANSIBLE_STDOUT_CALLBACK="json")
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(book)],
                          cwd=tmp_path, env=env, text=True, capture_output=True, check=False, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    tasks = json.loads(proc.stdout)["plays"][0]["tasks"]
    shown = next(t for t in tasks if t["task"]["name"] == "show")["hosts"]["localhost"]["_figures"]
    assert isinstance(shown, dict)
    assert shown == {
        "loki_ingested_bytes_24h": 1235,
        "loki_ingested_lines_24h": "unavailable",
        "prometheus_samples_appended_24h": 7,
        "prometheus_tsdb_blocks_bytes": "unavailable",
        "prometheus_tsdb_blocks_bytes_change_24h": -2500,
    }
