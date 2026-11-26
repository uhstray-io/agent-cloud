import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude/hooks/guard-secret-stdin.py"
SPEC = importlib.util.spec_from_file_location("guard_secret_stdin", HOOK)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def test_blocks_heredoc_interpreter_with_secret_file_stdin():
    assert guard.unsafe_command("python3 - <<'PY' < site-config/secrets/operator/token\nprint(1)\nPY")
    assert guard.unsafe_command("python3 -u - <<'PY' < site-config/secrets/operator/token\nprint(1)\nPY")
    assert guard.unsafe_command("python3 -Bu - <<'PY' < site-config/secrets/operator/token\nprint(1)\nPY")
    assert guard.unsafe_command("python3 -Iu - <<'PY' < site-config/secrets/operator/token\nprint(1)\nPY")
    assert guard.unsafe_command("python3 -u <<'PY' < site-config/secrets/operator/token\nprint(1)\nPY")
    assert guard.unsafe_command("bash -s <<'SH' < /tmp/secrets/token")


def test_allows_script_file_or_inline_code_with_secret_stdin():
    assert not guard.unsafe_command("python3 scripts/read-token.py < site-config/secrets/operator/token")
    assert not guard.unsafe_command("python3 -c 'print(1)' < site-config/secrets/operator/token")
    assert not guard.unsafe_command("python3 -Bu -c 'print(1)' < site-config/secrets/operator/token")
    assert not guard.unsafe_command("python3 -Iu scripts/read-token.py < site-config/secrets/operator/token")
    assert not guard.unsafe_command(
        "python3 scripts/read-token.py < site-config/secrets/operator/token; python3 -u - <<'PY'\nprint(1)\nPY")
