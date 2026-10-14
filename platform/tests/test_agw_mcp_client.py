"""scripts/agw-mcp-client.py: the secret-free installer for the agentgateway MCP server
(agentgateway-mcp task 7.3, validation scenarios of 7.8).

Every run uses a temporary HOME and a fake `claude` on PATH that behaves as the installed CLI
does (add-json refuses an existing name, remove refuses an absent one; both found with Claude
Code 2.1.299). One test drives the real CLI, isolated by HOME and CLAUDE_CONFIG_DIR, and is skipped
where it is not installed. Nothing here reads or writes the real home.
"""

import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/agw-mcp-client.py"
HELPER = ROOT / "scripts/agw-mcp-key"
HOST = "mcp.example.test"

FAKE_CLAUDE = textwrap.dedent(
    f"""\
    #!{sys.executable}
    import json, os, sys
    from pathlib import Path

    base = os.environ.get("CLAUDE_CONFIG_DIR")
    cfg = (Path(base) if base else Path.home()) / ".claude.json"
    args = sys.argv[1:]
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
        log.write(args[1] + "\\n")
    data = json.loads(cfg.read_text()) if cfg.exists() else {{}}
    servers = data.setdefault("mcpServers", {{}})
    if args[:2] == ["mcp", "add-json"] and args[2:4] == ["-s", "user"]:
        if args[4] in servers or os.environ.get("FAKE_CLAUDE_FAIL_ADD"):
            sys.exit(1)
        servers[args[4]] = json.loads(args[5])
    elif args[:2] == ["mcp", "remove"] and args[2:4] == ["-s", "user"]:
        if args[4] not in servers:
            sys.exit(1)
        del servers[args[4]]
    else:
        sys.exit(2)
    cfg.write_text(json.dumps(data, indent=2))
    """
)

OTHER_CLAUDE = {
    "numStartups": 7,
    "mcpServers": {
        "alpha": {"type": "http", "url": "https://alpha.example.test/mcp"},
        "beta": {"type": "stdio", "command": "beta-server", "args": ["--x"], "env": {"K": "v"}},
    },
    "projects": {"/some/dir": {"allowedTools": []}},
}
OTHER_PI = {
    "settings": {"toolPrefix": "short"},
    "mcpServers": {
        "gamma": {"url": "https://gamma.example.test/mcp", "headers": {"X-Trace": "1"}},
        "delta": {"command": "delta-server", "args": []},
    },
}


class Env:
    def __init__(self, tmp_path):
        self.home = tmp_path / "home"
        self.bin = tmp_path / "bin"
        self.log = tmp_path / "claude.log"
        self.home.mkdir()
        self.bin.mkdir()
        self.log.write_text("")
        claude = self.bin / "claude"
        claude.write_text(FAKE_CLAUDE)
        claude.chmod(0o755)
        self.claude_json = self.home / ".claude.json"
        self.pi_json = self.home / ".pi/agent/mcp.json"
        self.env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        self.env.update(HOME=str(self.home), PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        self.env["FAKE_CLAUDE_LOG"] = str(self.log)

    def seed(self, claude=None, pi=None, pi_mode=0o640):
        if claude is not None:
            self.claude_json.write_text(json.dumps(claude, indent=2))
        if pi is not None:
            self.pi_json.parent.mkdir(parents=True)
            self.pi_json.write_text(json.dumps(pi, indent=2))
            self.pi_json.chmod(pi_mode)

    def run(self, *args):
        return subprocess.run(
            [sys.executable, str(INSTALLER), *args], env=self.env, capture_output=True, text=True, check=False
        )

    def install(self, *extra, host=HOST):
        return self.run("--host", host, "--identity", "alice", *extra)

    def claude_data(self):
        return json.loads(self.claude_json.read_text())

    def pi_data(self):
        return json.loads(self.pi_json.read_text())

    def claude_calls(self):
        return self.log.read_text().split()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def without_entry(data):
    out = json.loads(json.dumps(data))
    out["mcpServers"].pop("agentgateway", None)
    return out


def test_install_keeps_every_other_server_entry_equal(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    done = env.install()
    assert done.returncode == 0, done.stderr
    assert without_entry(env.claude_data()) == OTHER_CLAUDE
    assert without_entry(env.pi_data()) == OTHER_PI
    assert "agentgateway" in env.claude_data()["mcpServers"]
    assert "agentgateway" in env.pi_data()["mcpServers"]


def test_entries_reference_the_helper_and_carry_no_key(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    key_file = env.home / "handout.txt"
    assert env.install("--key-file", str(key_file)).returncode == 0
    claude = env.claude_data()["mcpServers"]["agentgateway"]
    pi = env.pi_data()["mcpServers"]["agentgateway"]
    assert claude["type"] == "http" and claude["url"] == f"https://{HOST}/mcp"
    assert pi["url"] == f"https://{HOST}/mcp"
    assert claude["headersHelper"].startswith(f"{HELPER} --format claude --identity alice")
    assert pi["headers"]["Authorization"].startswith(f"!{HELPER} --format pi --identity alice")
    assert str(key_file) in claude["headersHelper"] and str(key_file) in pi["headers"]["Authorization"]
    assert HELPER.stat().st_mode & stat.S_IXUSR


def test_second_install_changes_nothing(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    assert env.install().returncode == 0
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (env.claude_json, env.pi_json)}
    calls = env.claude_calls()
    again = env.install()
    assert again.returncode == 0
    assert again.stdout.count("unchanged agentgateway") == 2
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before
    assert env.claude_calls() == calls


def test_removal_restores_the_original_parsed_content(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    assert env.install().returncode == 0
    done = env.run("--remove")
    assert done.returncode == 0, done.stderr
    assert env.claude_data() == OTHER_CLAUDE
    assert env.pi_data() == OTHER_PI


def test_removal_without_an_entry_is_a_no_op(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    before = env.pi_json.read_bytes()
    done = env.run("--remove")
    assert done.returncode == 0
    assert done.stdout.count("absent agentgateway") == 2
    assert env.pi_json.read_bytes() == before
    assert env.claude_calls() == []


def test_a_changed_host_replaces_only_the_agentgateway_entry(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    assert env.install().returncode == 0
    done = env.install(host="mcp2.example.test")
    assert done.returncode == 0
    assert "replace agentgateway" in done.stdout
    assert env.claude_data()["mcpServers"]["agentgateway"]["url"] == "https://mcp2.example.test/mcp"
    assert env.pi_data()["mcpServers"]["agentgateway"]["url"] == "https://mcp2.example.test/mcp"
    assert without_entry(env.claude_data()) == OTHER_CLAUDE
    assert without_entry(env.pi_data()) == OTHER_PI


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]", '{"mcpServers": []}', ""])
def test_an_unparseable_pi_file_is_refused_and_untouched(env, bad):
    env.seed(claude=OTHER_CLAUDE)
    env.pi_json.parent.mkdir(parents=True)
    env.pi_json.write_text(bad)
    done = env.install("--harness", "pi")
    assert done.returncode != 0
    assert done.stderr.startswith("pi mcp.json:") and "left untouched" in done.stderr
    assert "Traceback" not in done.stderr
    assert env.pi_json.read_text() == bad
    assert [p.name for p in env.pi_json.parent.iterdir()] == ["mcp.json"]


def test_an_unparseable_claude_config_is_refused_before_the_cli_runs(env):
    env.claude_json.write_text("{broken")
    done = env.install("--harness", "claude")
    assert done.returncode != 0
    assert "left untouched" in done.stderr and "Traceback" not in done.stderr
    assert env.claude_json.read_text() == "{broken"
    assert env.claude_calls() == []


def test_one_refused_harness_does_not_stop_the_other_but_the_exit_is_non_zero(env):
    env.seed(claude=OTHER_CLAUDE)
    env.pi_json.parent.mkdir(parents=True)
    env.pi_json.write_text("{broken")
    done = env.install()
    assert done.returncode != 0
    assert "agentgateway" in env.claude_data()["mcpServers"]
    assert env.pi_json.read_text() == "{broken"


def test_pi_file_mode_is_preserved_and_a_new_file_is_private(env, tmp_path_factory):
    env.seed(pi=OTHER_PI, pi_mode=0o640)
    assert env.install("--harness", "pi").returncode == 0
    assert stat.S_IMODE(env.pi_json.stat().st_mode) == 0o640
    assert [p.name for p in env.pi_json.parent.iterdir()] == ["mcp.json"]
    fresh = Env(tmp_path_factory.mktemp("fresh"))
    assert fresh.install("--harness", "pi").returncode == 0
    assert stat.S_IMODE(fresh.pi_json.stat().st_mode) == 0o600
    assert list(fresh.pi_data()) == ["mcpServers"]
    assert list(fresh.pi_data()["mcpServers"]) == ["agentgateway"]


def test_dry_run_reports_and_changes_nothing(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    before = {p: p.read_bytes() for p in (env.claude_json, env.pi_json)}
    done = env.install("--dry-run")
    assert done.returncode == 0
    assert done.stdout.count("would add agentgateway") == 2
    assert {p: p.read_bytes() for p in before} == before
    assert env.claude_calls() == []
    assert env.install().returncode == 0
    removal = env.run("--remove", "--dry-run")
    assert removal.stdout.count("would remove agentgateway") == 2
    assert "agentgateway" in env.pi_data()["mcpServers"]
    assert "agentgateway" in env.claude_data()["mcpServers"]


def test_harness_selection_leaves_the_other_harness_alone(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    claude_before, pi_before = env.claude_json.read_bytes(), env.pi_json.read_bytes()
    assert env.install("--harness", "pi").returncode == 0
    assert env.claude_json.read_bytes() == claude_before and env.claude_calls() == []
    assert env.install("--harness", "claude").returncode == 0
    assert env.claude_calls() == ["add-json"]
    assert env.pi_json.read_bytes() != pi_before


@pytest.mark.parametrize("host", ["https://mcp.example.test", "mcp.example.test/mcp", "localhost", "a b.test", ""])
def test_a_host_that_is_not_a_bare_hostname_is_refused(env, host):
    done = env.install(host=host)
    assert done.returncode == 2
    assert not env.claude_json.exists() and not env.pi_json.exists()


def test_output_names_only(env):
    secret = "sentinelsecretvalue0123456789abcdefABCDEF"
    key_file = env.home / "handout.txt"
    key_file.write_text(secret)
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    done = env.install("--key-file", str(key_file))
    assert done.returncode == 0
    for text in (done.stdout, done.stderr, env.claude_json.read_text(), env.pi_json.read_text()):
        assert secret not in text
    assert done.stdout.splitlines() == ["claude: add agentgateway", "pi: add agentgateway"]


def test_missing_claude_cli_is_a_named_refusal(env):
    env.seed(pi=OTHER_PI)
    (env.bin / "claude").unlink()
    env.env["PATH"] = os.pathsep.join(
        p for p in os.environ["PATH"].split(os.pathsep) if not shutil.which("claude", path=p)
    )
    done = env.install()
    assert done.returncode != 0
    assert "claude: CLI not on PATH" in done.stderr
    assert "agentgateway" in env.pi_data()["mcpServers"]


def test_claude_config_dir_is_honoured(env, tmp_path):
    cfg = tmp_path / "cfgdir"
    cfg.mkdir()
    env.env["CLAUDE_CONFIG_DIR"] = str(cfg)
    (cfg / ".claude.json").write_text(json.dumps(OTHER_CLAUDE))
    assert env.install("--harness", "claude").returncode == 0
    assert "agentgateway" in json.loads((cfg / ".claude.json").read_text())["mcpServers"]
    assert not env.claude_json.exists()
    assert env.run("--remove", "--harness", "claude").returncode == 0
    assert json.loads((cfg / ".claude.json").read_text()) == OTHER_CLAUDE


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_real_claude_cli_agrees_with_the_fake(tmp_path):
    """The fake above is only evidence while the real CLI behaves the same way."""
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    home.mkdir()
    cfg.mkdir()
    env = {**os.environ, "HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg)}
    (cfg / ".claude.json").write_text(json.dumps(OTHER_CLAUDE))

    def run(*args):
        return subprocess.run(
            [sys.executable, str(INSTALLER), "--harness", "claude", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def servers():
        return json.loads((cfg / ".claude.json").read_text())["mcpServers"]

    assert run("--host", HOST, "--identity", "alice").returncode == 0
    entry = servers()["agentgateway"]
    assert entry["type"] == "http" and entry["url"] == f"https://{HOST}/mcp"
    assert entry["headersHelper"].startswith(f"{HELPER} --format claude")
    assert {k: v for k, v in servers().items() if k != "agentgateway"} == OTHER_CLAUDE["mcpServers"]
    assert "unchanged agentgateway" in run("--host", HOST, "--identity", "alice").stdout
    assert run("--host", "mcp2.example.test", "--identity", "alice").returncode == 0
    assert servers()["agentgateway"]["url"] == "https://mcp2.example.test/mcp"
    assert run("--remove").returncode == 0
    assert servers() == OTHER_CLAUDE["mcpServers"]


def test_a_failed_add_after_the_remove_says_so_and_exits_non_zero(env):
    env.seed(claude=OTHER_CLAUDE)
    assert env.install("--harness", "claude").returncode == 0
    env.env["FAKE_CLAUDE_FAIL_ADD"] = "1"
    done = env.install("--harness", "claude", host="mcp2.example.test")
    assert done.returncode != 0
    assert done.stderr.strip() == "claude: removed agentgateway; add failed, rerun the installer"
    assert env.claude_data() == OTHER_CLAUDE
    assert env.claude_calls() == ["add-json", "remove", "add-json"]


def test_a_plain_failed_add_is_a_named_failure_without_the_removed_note(env):
    env.env["FAKE_CLAUDE_FAIL_ADD"] = "1"
    done = env.install("--harness", "claude")
    assert done.returncode != 0
    assert "add-json" in done.stderr and "removed" not in done.stderr


def load_installer():
    spec = importlib.util.spec_from_file_location("agw_mcp_client_under_test", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_helper_command_is_shell_quoted_and_inert(tmp_path, monkeypatch):
    """A space, a quote and a command substitution in a path stay one literal argument each."""
    nasty_dir = tmp_path / "d i r'q$(touch pwned)"
    nasty_dir.mkdir()
    stub = nasty_dir / "agw-mcp-key"
    stub.write_text(f"#!{sys.executable}\nimport sys\nprint('|'.join(sys.argv[1:]))\n")
    stub.chmod(0o755)
    key_file = tmp_path / "k e'y$(touch pwned).txt"
    mod = load_installer()
    monkeypatch.setattr(mod, "HELPER", stub)
    cmd = mod.helper_command("pi", "alice", str(key_file))
    resolved = str(key_file.resolve())
    assert shlex.split(cmd) == [str(stub), "--format", "pi", "--identity", "alice", "--key-file", resolved]
    done = subprocess.run(["sh", "-c", cmd], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == f"--format|pi|--identity|alice|--key-file|{resolved}"
    assert not (tmp_path / "pwned").exists()
    claude = mod.render_claude("mcp.example.test", "alice", str(key_file))
    pi = mod.render_pi("mcp.example.test", "alice", str(key_file))
    assert shlex.split(claude["headersHelper"])[-1] == resolved
    assert shlex.split(pi["headers"]["Authorization"][1:])[-1] == resolved


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_make_target_passes_values_as_environment_not_shell_text(env, tmp_path):
    sentinel = tmp_path / "pwned"  # the payload writes only under the test's temp dir
    key_file = f"{tmp_path}/k e'y$$(touch {sentinel}).txt"
    done = subprocess.run(
        [
            "make",
            "-C",
            str(ROOT),
            "agw-mcp-client",
            f"HOST={HOST}",
            "IDENTITY=alice",
            f"KEY_FILE={key_file}",
            "HARNESS=pi",
            "DRY_RUN=1",
        ],
        env=env.env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "pi: would add agentgateway"
    assert not sentinel.exists()
    assert not env.pi_json.exists()
    bad = subprocess.run(
        ["make", "-C", str(ROOT), "agw-mcp-client", "HOST=", "IDENTITY=alice", "DRY_RUN=1"],
        env=env.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode != 0 and "--host" in bad.stderr


def test_the_installer_reads_its_defaults_from_the_environment(env):
    env.env.update(AGW_MCP_HOST=HOST, AGW_MCP_IDENTITY="alice", AGW_MCP_HARNESS="pi")
    done = env.run("--dry-run")
    assert done.returncode == 0 and done.stdout.strip() == "pi: would add agentgateway"
    env.env["AGW_MCP_HARNESS"] = "nonsense"
    assert env.run("--dry-run").returncode == 2
