"""The production budget receipt reports the seven-day inference usage peak.

inference-telemetry-production task 4.2 needs the peak token and request rates so the
route-switch gate can judge whether the `legacy-shared` identity fits 2,000,000 tokens/hour and
the 120 requests/minute global ceiling. The receipt reads them through the same read-only exec
path as the daily ingestion figures, under its own key. A metric gap (an empty vector) is
reported as `unavailable`; it must not fail the receipt. The fold task is run by ansible itself,
so the Jinja the production run evaluates is the Jinja tested here.
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
    "peak_tokens_per_hour": (
        'max_over_time((sum(increase(vllm:prompt_tokens_total{job="dgx-spark-vllm"}[1h])) + '
        'sum(increase(vllm:generation_tokens_total{job="dgx-spark-vllm"}[1h])))[7d:1h])'
    ),
    "peak_generation_tokens_per_hour": (
        'max_over_time(sum(increase(vllm:generation_tokens_total{job="dgx-spark-vllm"}[1h]))[7d:1h])'
    ),
    "peak_requests_per_minute": (
        'max_over_time((sum(rate(vllm:request_success_total{job="dgx-spark-vllm"}[5m])) * 60)[7d:5m])'
    ),
    "peak_running_requests": 'max_over_time(sum(vllm:num_requests_running{job="dgx-spark-vllm"})[7d:5m])',
    "peak_waiting_requests": 'max_over_time(sum(vllm:num_requests_waiting{job="dgx-spark-vllm"})[7d:5m])',
}

READ = "Read seven-day inference usage peak from vLLM"
FOLD = "Fold the inference peak readings into named figures"
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


def test_the_figures_reach_the_receipt_under_their_own_key():
    msg = _task(REPORT)["ansible.builtin.debug"]["msg"]
    assert "_inference_peak" in msg["inference_peak_7d"]
    assert "_inference_peak" not in msg["daily_ingestion"]


def _reading(name: str, body: dict) -> dict:
    return {"item": {"name": name}, "stdout": json.dumps(body)}


def _vector(value: str) -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, value]}]}}


@needs_ansible
def test_figures_are_numbers_and_an_empty_vector_is_unavailable(tmp_path):
    empty = {"status": "success", "data": {"resultType": "vector", "result": []}}
    readings = [
        _reading("peak_tokens_per_hour", _vector("1234567.6")),
        _reading("peak_generation_tokens_per_hour", empty),
        _reading("peak_requests_per_minute", _vector("12.4")),
        _reading("peak_running_requests", _vector("NaN")),
        _reading("peak_waiting_requests", _vector("0")),
    ]
    play = [{
        "hosts": "localhost", "gather_facts": False,
        "vars": {"_inference_peak_reads": {"results": readings}},
        "tasks": [
            _task(FOLD),
            {"name": "fold is a mapping", "ansible.builtin.assert": {"that": "_inference_peak is mapping"}},
            {"name": "report", "ansible.builtin.set_fact":
                {"_figures": _task(REPORT)["ansible.builtin.debug"]["msg"]["inference_peak_7d"]}},
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
        "peak_tokens_per_hour": 1234568,
        "peak_generation_tokens_per_hour": "unavailable",
        "peak_requests_per_minute": 12,
        "peak_running_requests": "unavailable",
        "peak_waiting_requests": 0,
    }
