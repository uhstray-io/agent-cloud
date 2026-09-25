"""The repository stdout callback keeps request headers out of nested results (MISTAKES 4.6).

Runs a real play from the repository root, so ansible.cfg selects the callback exactly as it
does for Semaphore, against a local HTTP server, with a synthetic token in the request headers.
"""

import http.server
import importlib.util
import os
import subprocess
import textwrap
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "SYNTHETIC-HEADER-VALUE-4f1c"
PLAY = """
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    _h: {Authorization: "Token {{ lookup('env', 'SYNTH_TOKEN') }}"}
  tasks:
    - name: Read with the token in the headers
      ansible.builtin.uri: {url: "http://127.0.0.1:{{ port }}/", headers: "{{ _h }}", status_code: [200]}
      register: _reads
      loop: [a, b]
    - name: A visible loop over the reads that succeeds (prints items from -v)
      ansible.builtin.debug: {msg: "{{ item.status }}"}
      loop: "{{ _reads.results }}"
      loop_control: {label: "{{ item.item }}"}
    - name: A visible loop over the reads that fails (prints each failed item whole)
      ansible.builtin.assert: {that: "item.status == 999"}
      loop: "{{ _reads.results }}"
      loop_control: {label: "{{ item.item }}"}
      ignore_errors: true
    - name: The same loop with no label (the item itself is displayed as its label)
      ansible.builtin.assert: {that: "item.status == 999"}
      loop: "{{ _reads.results }}"
"""


@pytest.fixture(scope="module")
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def run(tmp_path, port, *extra, callback=None):
    play = tmp_path / "play.yml"
    play.write_text(textwrap.dedent(PLAY))
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANSIBLE_STDOUT_CALLBACK", "ANSIBLE_CONFIG", "ANSIBLE_CALLBACK_PLUGINS")}
    env.update(SYNTH_TOKEN=TOKEN, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path))
    if callback:
        env["ANSIBLE_STDOUT_CALLBACK"] = callback
    result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(play), "-e", f"port={port}", *extra],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    return result.returncode, result.stdout + result.stderr


@pytest.mark.parametrize("verbosity", [[], ["-v"], ["-vv"]])
def test_no_request_header_reaches_the_output_from_nested_results(tmp_path, server, verbosity):
    code, output = run(tmp_path, server, *verbosity)
    assert code != 0, output  # the failing loop ran, so its items were printed
    assert "failed: [localhost] (item=a)" in output, output
    assert "failed: [localhost] (item={" in output, output  # the unlabeled loop ran too
    assert TOKEN not in output


def test_the_scenario_leaks_under_the_stock_default_callback(tmp_path, server):
    # The control: without this callback the same play prints the token on ansible-core
    # 2.16-2.20 (the production and local controllers). 2.21 withholds it by itself.
    code, output = run(tmp_path, server, callback="default")
    if TOKEN not in output:
        pytest.skip("this ansible-core does not print nested invocations; nothing to redact here")
    assert code != 0


def test_the_repository_config_selects_this_callback(tmp_path, server):
    # Guards the wiring: a callback that is never selected protects nothing.
    code, output = run(tmp_path, server, "--list-tasks")
    assert code == 0, output
    probe = subprocess.run(["ansible-config", "dump", "--only-changed"], cwd=ROOT, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL,
                           env={k: v for k, v in os.environ.items() if k != "ANSIBLE_STDOUT_CALLBACK"})
    assert "redact_requests" in probe.stdout, probe.stdout + probe.stderr


def test_strip_keeps_the_top_level_and_everything_but_nested_invocations():
    pytest.importorskip("ansible")  # the plugin imports ansible-core in-process
    spec = importlib.util.spec_from_file_location("redact", ROOT / "callback_plugins/redact_requests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = {"invocation": {"module_args": {}}, "msg": "x",
              "item": {"status": 200, "invocation": {"module_args": {"headers": {"Authorization": TOKEN}}}},
              # An API response field that happens to be named `invocation` is dropped too.
              "results": [{"invocation": {}, "json": {"invocation": "an API field of that name"}}]}
    stripped = module.strip_nested_invocations(result)
    assert stripped["invocation"] == {"module_args": {}}  # top level: ansible-core's own rule decides
    assert stripped["item"] == {"status": 200}
    assert stripped["results"] == [{"json": {}}]
    assert "invocation" in result["item"]  # the input is not mutated
