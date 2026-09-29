"""Run a test harness's ansible-playbook so it cannot write to the developer's machine.

The SSH-probe harnesses (test_materialise_ssh_key.py, test_harden_password_probe.py) execute
real playbook tasks with `connection: local`. A task, a regression or a mutation that aims a
write at a real path therefore hits the developer's machine: docs/MISTAKES.md 3.9 records a
mutation that overwrote ~/.ssh/known_hosts that way. A source-level guard refuses such tasks
before running (test_check_mode_contract.py), but a guard that reads source can be routed
around by indirection (PR #319 review), so the run itself is confined too:

- macOS: `sandbox-exec` with a DEFAULT-DENY write profile: every file write is refused except
  under the test's own directory (which also holds Ansible's own state), the temp root the
  scratch uses, the interpreter's temp dir, and /dev. Later rules win in a sandbox profile, so
  explicit denials (the proof tests' canaries) come last.
- Linux with `bwrap`: the root file system is bound read-only apart from the test's directory
  and the temp root.
- Linux without `bwrap` (the GitHub-hosted CI runner, unless it gains it): NO kernel sandbox.
  Ansible's own state is pointed into the test directory, and the source guard is the
  enforcement. `SANDBOX` says which one is active, so a test can say what it proved.

`$HOME` is deliberately not relied on: Ansible's local connection expands `~` through the
account, not the environment (MISTAKES 3.9: a HOME-only "sandbox" wrote into the real home).
"""

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120

if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
    SANDBOX = "sandbox-exec"
elif platform.system() == "Linux" and shutil.which("bwrap"):
    SANDBOX = "bwrap"
else:
    SANDBOX = None


def temp_root() -> str:
    """The temp root the scratch tasks use: TMPDIR or /tmp, resolved."""
    return os.path.realpath(os.environ.get("TMPDIR") or "/tmp")


def _profile(writable: list[str], denied: list[str]) -> str:
    def paths(items):
        return " ".join(f'(subpath "{p}")' for p in items)

    rules = ["(version 1)", "(allow default)", "(deny file-write*)",
             f'(allow file-write* {paths(writable)} (subpath "/dev"))']
    if denied:
        rules.append(f"(deny file-write* {paths(denied)})")
    return "".join(rules)


def env_for(tmp_path: Path, base: dict | None = None) -> dict:
    """The harness environment: ANSIBLE_CONFIG dropped (the repo's ansible.cfg applies via cwd),
    and Ansible's own writable state kept inside the test directory."""
    env = {k: v for k, v in (base if base is not None else os.environ).items() if k != "ANSIBLE_CONFIG"}
    state = tmp_path / ".ansible-state"
    for key, sub in (("ANSIBLE_HOME", "home"), ("ANSIBLE_LOCAL_TEMP", "local"),
                     ("ANSIBLE_REMOTE_TEMP", "remote")):
        (state / sub).mkdir(parents=True, exist_ok=True)
        env[key] = str(state / sub)
    env["ANSIBLE_NOCOLOR"] = "1"
    return env


def run(cmd: list[str], tmp_path: Path, *, cwd: Path, env: dict, denied: list[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS):
    """subprocess.run(cmd) confined as described above. `denied` adds explicit denials (the
    sandbox's own proof uses one); they only bind under a kernel sandbox. A timeout returns a
    sanitized failed result because captured Ansible output can contain fixture credentials."""
    denied = [os.path.realpath(d) for d in (denied or [])]
    writable = [os.path.realpath(tmp_path), temp_root(), os.path.realpath(tempfile.gettempdir())]
    if SANDBOX == "sandbox-exec":
        cmd = ["sandbox-exec", "-p", _profile(writable, denied), *cmd]
    elif SANDBOX == "bwrap":
        binds = []
        for path in dict.fromkeys(writable):
            binds += ["--bind", path, path]
        for path in denied:
            binds += ["--ro-bind-try", path, path]
        cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", *binds, "--", *cmd]
    try:
        return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd,
            124,
            stdout="",
            stderr=f"test command timed out after {timeout:g}s; captured output suppressed",
        )
