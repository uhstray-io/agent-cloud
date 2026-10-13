import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude/hooks/guard-secret-stdin.py"
SPEC = importlib.util.spec_from_file_location("guard_secret_stdin", HOOK)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def test_blocks_heredoc_interpreter_with_secret_file_stdin():
    assert guard.unsafe_command("python3 - <<'PY' < site-config/secrets/operator/token\nprint(1)\nPY")
    assert guard.unsafe_command("python3 -u - <<'PY' < site-config/secrets/operator/token\nprint(1)\nPY")
    assert guard.unsafe_command("python3 -u <<'PY' < site-config/secrets/operator/token\nprint(1)\nPY")
    assert guard.unsafe_command("bash -s <<'SH' < /tmp/secrets/token")


def test_allows_script_file_or_inline_code_with_secret_stdin():
    assert not guard.unsafe_command("python3 scripts/read-token.py < site-config/secrets/operator/token")
    assert not guard.unsafe_command("python3 -c 'print(1)' < site-config/secrets/operator/token")
    assert not guard.unsafe_command(
        "python3 scripts/read-token.py < site-config/secrets/operator/token; python3 -u - <<'PY'\nprint(1)\nPY")


def test_hook_stdin_protocol_blocks_without_echoing_command():
    command = "python3 - <<'PY' < site-config/secrets/operator/token\nsecret payload\nPY"
    result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"tool_input": {"command": command}}),
                            text=True, capture_output=True, check=False)
    assert result.returncode == 2
    assert "secret payload" not in result.stderr
    assert "token" not in result.stderr


def test_hook_allows_safe_script_file_command():
    result = subprocess.run([sys.executable, str(HOOK)],
                            input=json.dumps({"tool_input": {
                                "command": "python3 scripts/read-token.py < site-config/secrets/operator/token"}}),
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0


def test_project_settings_registers_the_bash_pretool_guard():
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    hook = settings["hooks"]["PreToolUse"][0]
    assert hook["matcher"] == "Bash"
    assert hook["hooks"][0]["type"] == "command"
    assert "guard-secret-stdin.py" in hook["hooks"][0]["command"]
