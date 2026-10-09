"""A failed `no_log` task prints no run-time value, on every ansible-core (MISTAKES 4.6).

ansible-core 2.19 moved a failed task's error out of the result dict into an object beside it,
and the censoring that empties the result keeps that object, so the stock default callback
prints "[ERROR]: Task failed", the exception's message, its "caused by" chain and the source
context for a `no_log` task: a filter's input quoted in its own error, an assert's rendered
`fail_msg`, a rendered argument quoted by the "Finalization of task args" failure. 2.18 printed
none of that. The same censoring keeps a task's warnings and deprecations, which a module can
word around a value it was handed. The repository stdout callback
(callback_plugins/redact_requests.py) hides both.

Runs a real play from the repository root, so ansible.cfg selects the callback as it does for
Semaphore, with a synthetic secret read from the environment.

The installed ansible-playbook always runs. NOLOG_ANSIBLE_BINS (an os.pathsep list of `bin`
directories, one virtualenv per ansible-core) adds more versions to the matrix; a path that
does not exist is skipped, so the matrix is whatever the machine has.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SECRET = "ZZ-SYNTHETIC-RUNTIME-VALUE-7c2e"
PLAIN = "PLAINVALUE-NOT-A-SECRET"
# A module that warns about, and deprecates, the value it was handed; optionally then fails.
NOISY = """
from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec={"value": {"type": "str"}, "fail": {"type": "bool", "default": False}})
    value = module.params["value"]
    module.warn("warned about " + value)
    module.deprecate("deprecated for " + value, version="9.9.9", collection_name="ansible.builtin")
    if module.params["fail"]:
        module.fail_json(msg="failed on " + value)
    module.exit_json(changed=False)


main()
"""
PLAY = """
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    secret: "{{ lookup('env', 'NOLOG_SECRET') }}"
    plain: "{{ lookup('env', 'NOLOG_PLAIN') }}"
  tasks:
    - name: to_datetime on the value
      ansible.builtin.set_fact: {a: "{{ secret | to_datetime('%Y') }}"}
      no_log: true
      ignore_errors: true
    - name: from_yaml on the value
      ansible.builtin.set_fact: {b: "{{ (secret ~ ': [') | from_yaml }}"}
      no_log: true
      ignore_errors: true
    - name: b64decode on the value
      ansible.builtin.set_fact: {c: "{{ secret | b64decode }}"}
      no_log: true
      ignore_errors: true
    - name: bad regex built from the value
      ansible.builtin.set_fact: {d: "{{ 'x' | regex_search(secret ~ '(') }}"}
      no_log: true
      ignore_errors: true
    - name: assert whose fail_msg renders the value
      ansible.builtin.assert: {that: "secret == 'nope'", fail_msg: "bad {{ secret }}"}
      no_log: true
      ignore_errors: true
    - name: uri to a host named by the value
      ansible.builtin.uri: {url: "http://{{ secret }}.invalid/", timeout: 2}
      no_log: true
      ignore_errors: true
    - name: command with the value in argv
      ansible.builtin.command: {argv: ["/nonexistent-binary", "{{ secret }}"]}
      no_log: true
      ignore_errors: true
    - name: shell that fails after echoing the value
      ansible.builtin.shell: "echo {{ secret }}; echo {{ secret }} >&2; exit 3"
      no_log: true
      ignore_errors: true
    - name: failing loop items rendered from the value
      ansible.builtin.assert: {that: "item != secret", fail_msg: "bad {{ item }}"}
      loop: ["{{ secret }}"]
      no_log: true
      ignore_errors: true
    - name: block-level no_log on a failing task
      block:
        - ansible.builtin.set_fact: {e: "{{ secret | to_datetime('%Y') }}"}
      no_log: true
      ignore_errors: true
    - name: finalization failure with the value in a conditional
      ansible.builtin.debug: {msg: ok}
      when: "{{ secret | to_datetime('%Y') }}"
      no_log: true
      ignore_errors: true
    - name: warnings of a no_log task that succeeds
      noisy: {value: "{{ secret }}"}
      no_log: true
    - name: warnings of a no_log task that fails
      noisy: {value: "{{ secret }}", fail: true}
      no_log: true
      ignore_errors: true
    - name: warnings of a visible task stay
      noisy: {value: "{{ plain }}"}
    - name: a visible failure keeps its error
      ansible.builtin.set_fact: {f: "{{ plain | to_datetime('%Y') }}"}
      ignore_errors: true
    - name: a failure no_log hides and no ignore_errors ends the run
      ansible.builtin.assert: {that: "secret == 'nope'", fail_msg: "bad {{ secret }}"}
      no_log: true
"""


def _bins() -> list:
    found = [shutil.which("ansible-playbook")]
    for d in filter(None, os.environ.get("NOLOG_ANSIBLE_BINS", "").split(os.pathsep)):
        found.append(str(Path(d) / "ansible-playbook") if (Path(d) / "ansible-playbook").exists() else None)
    return found


BINS = [b for b in dict.fromkeys(_bins()) if b]


def run(tmp_path, binary, *extra, callback=None):
    play = tmp_path / "play.yml"
    play.write_text(textwrap.dedent(PLAY))
    (tmp_path / "library").mkdir(exist_ok=True)
    (tmp_path / "library" / "noisy.py").write_text(NOISY)
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANSIBLE_STDOUT_CALLBACK", "ANSIBLE_CONFIG", "ANSIBLE_CALLBACK_PLUGINS")}
    # The binary's own virtualenv first: ansible-playbook re-reads `ansible-config` and friends
    # from PATH, and a mixed matrix must not pick another version's.
    env["PATH"] = str(Path(binary).parent) + os.pathsep + env.get("PATH", "")
    env.update(NOLOG_SECRET=SECRET, NOLOG_PLAIN=PLAIN, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path))
    if callback:
        env["ANSIBLE_STDOUT_CALLBACK"] = callback
    result = subprocess.run([binary, "-i", "localhost,", str(play), *extra], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=180,
                            stdin=subprocess.DEVNULL)
    # One stream, so a warning (stderr) sits under the task that raised it.
    return result.returncode, result.stdout


def _version(binary) -> tuple:
    out = subprocess.run([binary, "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    return tuple(int(n) for n in re.search(r"core (\d+)\.(\d+)", out).groups())


pytestmark = pytest.mark.skipif(not BINS, reason="ansible-playbook not installed")


@pytest.mark.parametrize("verbosity", [[], ["-v"], ["-vv"], ["-vvv"]])
@pytest.mark.parametrize("binary", BINS)
def test_a_failed_no_log_task_prints_no_runtime_value(tmp_path, binary, verbosity):
    code, output = run(tmp_path, binary, *verbosity)
    assert code != 0, output  # the last task ends the run, so every earlier one ran
    assert "a failure no_log hides and no ignore_errors ends the run" in output
    assert SECRET not in output


@pytest.mark.parametrize("binary", BINS)
def test_a_visible_failure_still_shows_its_error(tmp_path, binary):
    code, output = run(tmp_path, binary)
    assert code != 0, output
    task = output.split("TASK [a visible failure keeps its error]", 1)[1].split("TASK [", 1)[0]
    assert PLAIN in task, task  # the error names the value it could not parse
    assert "details hidden" not in task, task


@pytest.mark.parametrize("binary", BINS)
def test_a_visible_warning_still_shows(tmp_path, binary):
    code, output = run(tmp_path, binary)
    assert code != 0, output
    task = output.split("TASK [warnings of a visible task stay]", 1)[1].split("TASK [", 1)[0]
    assert f"warned about {PLAIN}" in task, task
    assert f"deprecated for {PLAIN}" in task, task
    assert "hidden: the task is no_log" not in task, task


@pytest.mark.parametrize("task", ["warnings of a no_log task that succeeds", "warnings of a no_log task that fails"])
@pytest.mark.parametrize("binary", BINS)
def test_a_hidden_warning_says_so_on_ansible_core_219_and_later(tmp_path, binary, task):
    if _version(binary) < (2, 19):
        pytest.skip("2.18 censors the warnings away; there is nothing to replace")
    code, output = run(tmp_path, binary)
    assert code != 0, output
    section = output.split(f"TASK [{task}]", 1)[1].split("TASK [", 1)[0]
    assert re.search(rf"\[WARNING\]: '{task}' emitted \d+ warning\(s\)/deprecation\(s\); hidden: the task is no_log",
                     section), section


@pytest.mark.parametrize("binary", BINS)
def test_the_hidden_error_says_so_on_ansible_core_219_and_later(tmp_path, binary):
    if _version(binary) < (2, 19):
        pytest.skip("2.18 prints no error detail for a censored result; there is nothing to replace")
    code, output = run(tmp_path, binary)
    assert code != 0, output
    assert "[ERROR]: Task failed: 'to_datetime on the value' (details hidden: the task is no_log)" in output
    assert "details hidden" in output.split("TASK [a visible failure keeps its error]", 1)[0]


@pytest.mark.parametrize("binary", BINS)
def test_the_scenario_leaks_under_the_stock_default_callback(tmp_path, binary):
    # The control: without this callback the same play prints the value on ansible-core 2.19+.
    code, output = run(tmp_path, binary, callback="default")
    if SECRET not in output:
        pytest.skip("this ansible-core prints no error detail for a no_log task; nothing to hide here")
    assert code != 0


def _callback_module(monkeypatch):
    # The pure detection function needs none of ansible-core, so the plugin's imports are stubbed:
    # the signals are then tested on a machine whose pytest interpreter has no ansible at all.
    names = ("ansible", "ansible.constants", "ansible.plugins", "ansible.plugins.callback",
             "ansible.plugins.callback.default")
    stubs = {name: ModuleType(name) for name in names}
    stubs["ansible"].constants = stubs["ansible.constants"]
    stubs["ansible.plugins.callback.default"].CallbackModule = type("CallbackModule", (), {})
    for name, stub in stubs.items():
        monkeypatch.setitem(sys.modules, name, stub)
    spec = importlib.util.spec_from_file_location("redact", ROOT / "callback_plugins/redact_requests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "result, task_no_log, expected",
    [
        ({"censored": "x"}, None, True),  # ansible-core's own marker; a task that failed before the module ran
        ({"_ansible_no_log": True}, None, True),  # the module's own no_log flag
        ({}, True, True),  # the task's declaration
        ({"_ansible_no_log": False}, False, False),
        ({"msg": "plain"}, None, False),
        ({"msg": "plain"}, "{{ templated }}", False),  # an unresolved expression is not a declaration
    ],
)
def test_each_no_log_signal_alone_marks_a_result_hidden(monkeypatch, result, task_no_log, expected):
    task_result = SimpleNamespace(task=SimpleNamespace(no_log=task_no_log))
    assert _callback_module(monkeypatch).is_no_log_result(result, task_result) is expected
