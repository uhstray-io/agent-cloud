"""scripts/agw-mcp-key: the connect-time key helper for both MCP clients (agentgateway-mcp 7.4).

A fake `security` on PATH stands in for the macOS Keychain. The helper must print only what the
harness reads on stdout, names only on stderr, and write nothing.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/agw-mcp-key"
KEYCHAIN = "keychain-key-0123456789abcdef"
FILE_KEY = "file-key-0123456789abcdef"

FAKE_SECURITY = textwrap.dedent(
    f"""\
    #!{sys.executable}
    import os, sys
    want = ["find-generic-password", "-s", "agentgateway-mcp", "-a", "alice", "-w"]
    if sys.argv[1:] == want and "FAKE_KEYCHAIN_HEX" in os.environ:
        sys.stdout.buffer.write(bytes.fromhex(os.environ["FAKE_KEYCHAIN_HEX"]) + b"\\n")
        sys.exit(0)
    if sys.argv[1:] == want and "FAKE_KEYCHAIN_VALUE" in os.environ:
        print(os.environ["FAKE_KEYCHAIN_VALUE"])
        sys.exit(0)
    sys.exit(44)
    """
)


class Box:
    def __init__(self, tmp_path):
        self.path = tmp_path
        self.cwd = tmp_path / "work"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self.cwd.mkdir()
        (bin_dir / "security").write_text(FAKE_SECURITY)
        (bin_dir / "security").chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if k != "AGW_MCP_KEY_FILE"}
        self.env.update(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", HOME=str(tmp_path))

    def run(self, *args, **extra):
        return subprocess.run(
            [str(HELPER), *args], env={**self.env, **extra}, cwd=self.cwd, capture_output=True, text=True, check=False
        )

    def files(self):
        return sorted(str(p) for p in self.path.rglob("*"))


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def test_keychain_key_in_both_formats(box):
    claude = box.run("--format", "claude", "--identity", "alice", FAKE_KEYCHAIN_VALUE=KEYCHAIN)
    assert claude.returncode == 0 and claude.stderr == ""
    assert json.loads(claude.stdout) == {"Authorization": f"Bearer {KEYCHAIN}"}
    pi = box.run("--format", "pi", "--identity", "alice", FAKE_KEYCHAIN_VALUE=KEYCHAIN)
    assert pi.returncode == 0 and pi.stderr == ""
    assert pi.stdout == f"Bearer {KEYCHAIN}\n"


def test_handout_file_is_the_fallback_and_its_newline_is_stripped(box):
    handout = box.path / "client_alice.txt"
    handout.write_text(FILE_KEY + "\n")
    done = box.run("--format", "pi", "--identity", "alice", "--key-file", str(handout))
    assert done.returncode == 0 and done.stdout == f"Bearer {FILE_KEY}\n"


def test_env_path_variable_names_the_file_and_the_flag_wins(box):
    one, two = box.path / "one.txt", box.path / "two.txt"
    one.write_text("one-key-0123456789")
    two.write_text("two-key-0123456789")
    via_env = box.run("--format", "pi", "--identity", "alice", AGW_MCP_KEY_FILE=str(one))
    assert via_env.stdout == "Bearer one-key-0123456789\n"
    both = box.run("--format", "pi", "--identity", "alice", "--key-file", str(two), AGW_MCP_KEY_FILE=str(one))
    assert both.stdout == "Bearer two-key-0123456789\n"


def test_keychain_is_read_before_the_file(box):
    handout = box.path / "client_alice.txt"
    handout.write_text(FILE_KEY)
    done = box.run("--format", "pi", "--identity", "alice", "--key-file", str(handout), FAKE_KEYCHAIN_VALUE=KEYCHAIN)
    assert done.stdout == f"Bearer {KEYCHAIN}\n"


def test_a_malformed_keychain_value_falls_through_to_the_file(box):
    handout = box.path / "client_alice.txt"
    handout.write_text(FILE_KEY)
    done = box.run("--format", "pi", "--identity", "alice", "--key-file", str(handout), FAKE_KEYCHAIN_VALUE="two words")
    assert done.stdout == f"Bearer {FILE_KEY}\n"


@pytest.mark.parametrize("content", ["", "\n", "two words", "line1\nline2"])
def test_a_malformed_file_key_is_refused(box, content):
    handout = box.path / "client_alice.txt"
    handout.write_text(content)
    done = box.run("--format", "claude", "--identity", "alice", "--key-file", str(handout))
    assert done.returncode == 1 and done.stdout == ""


def test_neither_source_fails_with_names_only_and_writes_nothing(box):
    before = box.files()
    done = box.run("--format", "claude", "--identity", "alice")
    assert done.returncode == 1
    assert done.stdout == ""
    assert "alice" in done.stderr and "agentgateway-mcp" in done.stderr
    assert box.files() == before


def test_an_unreadable_key_file_fails_without_output(box):
    done = box.run("--format", "claude", "--identity", "alice", "--key-file", str(box.path / "missing.txt"))
    assert done.returncode == 1 and done.stdout == ""


def test_success_writes_nothing_and_stdout_is_only_the_header(box):
    before = box.files()
    done = box.run("--format", "claude", "--identity", "alice", FAKE_KEYCHAIN_VALUE=KEYCHAIN)
    assert list(json.loads(done.stdout)) == ["Authorization"]
    assert box.files() == before


@pytest.mark.parametrize("identity", ["a b", "a/b", "a;b", "", "$(id)"])
def test_identity_shape_is_enforced(box, identity):
    done = box.run("--format", "pi", "--identity", identity, FAKE_KEYCHAIN_VALUE=KEYCHAIN)
    assert done.returncode == 2 and done.stdout == ""


SECRET = "sentinel-secret-9f8e7d6c5b4a"


@pytest.mark.parametrize("content", [f"{SECRET} extra", f"{SECRET}\nline2"])
def test_a_malformed_file_key_never_reaches_stdout_or_stderr(box, content):
    handout = box.path / "client_alice.txt"
    handout.write_text(content)
    handout.chmod(0o600)
    done = box.run("--format", "claude", "--identity", "alice", "--key-file", str(handout))
    assert done.returncode == 1
    assert SECRET not in done.stdout + done.stderr


def test_a_malformed_keychain_value_never_reaches_stdout_or_stderr(box):
    done = box.run("--format", "claude", "--identity", "alice", FAKE_KEYCHAIN_VALUE=f"{SECRET} extra")
    assert done.returncode == 1
    assert SECRET not in done.stdout + done.stderr


def test_a_binary_keychain_value_is_a_names_only_failure(box):
    done = box.run("--format", "claude", "--identity", "alice", FAKE_KEYCHAIN_HEX="fffe80")
    assert done.returncode == 1 and done.stdout == ""
    assert "Traceback" not in done.stderr and "alice" in done.stderr


def test_a_binary_keychain_value_falls_through_to_the_file(box):
    handout = box.path / "client_alice.txt"
    handout.write_text(FILE_KEY)
    handout.chmod(0o600)
    done = box.run("--format", "pi", "--identity", "alice", "--key-file", str(handout), FAKE_KEYCHAIN_HEX="fffe80")
    assert done.stdout == f"Bearer {FILE_KEY}\n"


def test_a_key_file_readable_by_others_warns_by_name_but_is_still_read(box):
    handout = box.path / "client_alice.txt"
    handout.write_text(FILE_KEY)
    handout.chmod(0o644)
    done = box.run("--format", "pi", "--identity", "alice", "--key-file", str(handout))
    assert done.stdout == f"Bearer {FILE_KEY}\n"
    assert "key file is group- or other-accessible" in done.stderr
    assert FILE_KEY not in done.stderr
    handout.chmod(0o600)
    quiet = box.run("--format", "pi", "--identity", "alice", "--key-file", str(handout))
    assert quiet.stdout == f"Bearer {FILE_KEY}\n" and quiet.stderr == ""
