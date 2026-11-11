"""scripts/plane-mcp-client.py: the secret-free installer for Plane's MCP server in Claude Code and pi.

Every run uses a temporary HOME and a fake `claude` on PATH that behaves as the installed CLI does
(add-json refuses an existing name, remove refuses an absent one). One test drives the real CLI,
isolated by HOME and CLAUDE_CONFIG_DIR, and is skipped where it is not installed. Nothing here reads
or writes the real home.

Author: Joseph A. Wisneski IV
"""

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import harness_sandbox
import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/plane-mcp-client.py"
CLIENTS = ROOT / "platform/services/plane-mcp/context/clients"
HOST = "plane-mcp.example.test"
URL = f"https://{HOST}/http/mcp"
BEARER_VALUE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}")
KEY_SHAPED = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")

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
        if args[4] in servers:
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
        "agentgateway": {"type": "http", "url": "https://mcp.example.test/mcp", "headersHelper": "/x/agw-mcp-key"},
        "beta": {"type": "stdio", "command": "beta-server", "args": ["--x"], "env": {"K": "v"}},
    },
    "projects": {"/some/dir": {"allowedTools": []}},
}
OTHER_PI = {
    "settings": {"toolPrefix": "short"},
    "mcpServers": {
        "gamma": {"url": "https://gamma.example.test/mcp", "headers": {"X-Trace": "1"}},
        "agentgateway": {"url": "https://mcp.example.test/mcp", "headers": {"Authorization": "!/x/agw-mcp-key"}},
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
        self.env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "PLANE_MCP_HOST")}
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
            [sys.executable, str(INSTALLER), *args], env=self.env, capture_output=True, text=True, check=False,
            timeout=harness_sandbox.DEFAULT_TIMEOUT_SECONDS,
        )

    def install(self, *extra, host=HOST):
        return self.run("--host", host, *extra)

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
    out["mcpServers"].pop("plane", None)
    return out


def leaks(text):
    return BEARER_VALUE.findall(text) + KEY_SHAPED.findall(text)


def load_installer():
    spec = importlib.util.spec_from_file_location("plane_mcp_client_under_test", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_keeps_every_other_server_entry_equal(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    done = env.install()
    assert done.returncode == 0, done.stderr
    assert without_entry(env.claude_data()) == OTHER_CLAUDE
    assert without_entry(env.pi_data()) == OTHER_PI
    assert "plane" in env.claude_data()["mcpServers"]
    assert "plane" in env.pi_data()["mcpServers"]


def test_entries_are_url_only_and_carry_no_header_token_or_helper(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    assert env.install().returncode == 0
    assert env.claude_data()["mcpServers"]["plane"] == {"type": "http", "url": URL}
    assert env.pi_data()["mcpServers"]["plane"] == {"url": URL}


def test_a_port_is_kept_in_the_url_for_both_harnesses(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    assert env.install(host="plane-mcp.agent-cloud.test:8443").returncode == 0
    local = "https://plane-mcp.agent-cloud.test:8443/http/mcp"
    assert env.claude_data()["mcpServers"]["plane"]["url"] == local
    assert env.pi_data()["mcpServers"]["plane"]["url"] == local


def test_second_install_changes_nothing(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    assert env.install().returncode == 0
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (env.claude_json, env.pi_json)}
    calls = env.claude_calls()
    again = env.install()
    assert again.returncode == 0
    assert again.stdout.count("unchanged plane") == 2
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
    assert done.stdout.count("absent plane") == 2
    assert env.pi_json.read_bytes() == before
    assert env.claude_calls() == []


def test_removal_needs_no_host(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    assert env.install().returncode == 0
    done = env.run("--remove")
    assert done.returncode == 0, done.stderr
    assert "plane" not in env.pi_data()["mcpServers"]


def test_a_changed_host_replaces_only_the_plane_entry(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    assert env.install().returncode == 0
    done = env.install(host="plane-mcp2.example.test")
    assert done.returncode == 0
    assert "replace plane" in done.stdout
    assert env.claude_data()["mcpServers"]["plane"]["url"] == "https://plane-mcp2.example.test/http/mcp"
    assert env.pi_data()["mcpServers"]["plane"]["url"] == "https://plane-mcp2.example.test/http/mcp"
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
    assert "plane" in env.claude_data()["mcpServers"]
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
    assert list(fresh.pi_data()["mcpServers"]) == ["plane"]


def test_dry_run_reports_and_changes_nothing(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    before = {p: p.read_bytes() for p in (env.claude_json, env.pi_json)}
    done = env.install("--dry-run")
    assert done.returncode == 0
    assert done.stdout.count("would add plane") == 2
    assert {p: p.read_bytes() for p in before} == before
    assert env.claude_calls() == []
    assert env.install().returncode == 0
    removal = env.run("--remove", "--dry-run")
    assert removal.stdout.count("would remove plane") == 2
    assert "plane" in env.pi_data()["mcpServers"]
    assert "plane" in env.claude_data()["mcpServers"]


def test_harness_selection_leaves_the_other_harness_alone(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    claude_before, pi_before = env.claude_json.read_bytes(), env.pi_json.read_bytes()
    assert env.install("--harness", "pi").returncode == 0
    assert env.claude_json.read_bytes() == claude_before and env.claude_calls() == []
    assert env.install("--harness", "claude").returncode == 0
    assert env.claude_calls() == ["add-json"]
    assert env.pi_json.read_bytes() != pi_before


@pytest.mark.parametrize(
    "host",
    [
        "https://plane-mcp.example.test",
        "plane-mcp.example.test/http/mcp",
        "localhost",
        "a b.test",
        "",
        "plane-mcp.example.test:0",
        "plane-mcp.example.test:65536",
        "plane-mcp.example.test:08443",
        "plane-mcp.example.test:",
        "plane-mcp.example.test:8443/http/mcp",
        "plane-mcp.example.test:port",
        "-plane.example.test",
    ],
)
def test_a_host_that_is_not_a_hostname_with_optional_port_is_refused(env, host):
    done = env.install(host=host)
    assert done.returncode == 2
    assert not env.claude_json.exists() and not env.pi_json.exists()


@pytest.mark.parametrize(
    "host", [HOST, "plane-mcp.example.test:8443", "plane-mcp.example.test:1", "plane-mcp.example.test:65535"]
)
def test_an_accepted_host_lands_verbatim_in_both_urls(env, host):
    done = env.install(host=host)
    assert done.returncode == 0, done.stderr
    expected = f"https://{host}/http/mcp"
    assert env.claude_data()["mcpServers"]["plane"]["url"] == expected
    assert env.pi_data()["mcpServers"]["plane"]["url"] == expected


def test_output_names_only(env):
    env.seed(claude=OTHER_CLAUDE, pi=OTHER_PI)
    done = env.install()
    assert done.returncode == 0
    assert done.stdout.splitlines() == ["claude: add plane", "pi: add plane"]
    assert done.stderr == ""
    for text in (done.stdout, done.stderr, env.claude_json.read_text(), env.pi_json.read_text()):
        assert not leaks(text)


def test_missing_claude_cli_is_a_named_refusal(env):
    env.seed(pi=OTHER_PI)
    (env.bin / "claude").unlink()
    env.env["PATH"] = os.pathsep.join(
        p for p in os.environ["PATH"].split(os.pathsep) if not shutil.which("claude", path=p)
    )
    done = env.install()
    assert done.returncode != 0
    assert "claude: CLI not on PATH" in done.stderr
    assert "plane" in env.pi_data()["mcpServers"]


def test_claude_config_dir_is_honoured(env, tmp_path):
    cfg = tmp_path / "cfgdir"
    cfg.mkdir()
    env.env["CLAUDE_CONFIG_DIR"] = str(cfg)
    (cfg / ".claude.json").write_text(json.dumps(OTHER_CLAUDE))
    assert env.install("--harness", "claude").returncode == 0
    assert "plane" in json.loads((cfg / ".claude.json").read_text())["mcpServers"]
    assert not env.claude_json.exists()
    assert env.run("--remove", "--harness", "claude").returncode == 0
    assert json.loads((cfg / ".claude.json").read_text()) == OTHER_CLAUDE


def test_the_default_host_comes_from_the_environment(env):
    env.env["PLANE_MCP_HOST"] = "plane-mcp.agent-cloud.test:8443"
    done = env.run("--dry-run", "--harness", "pi")
    assert done.returncode == 0 and done.stdout.strip() == "pi: would add plane"
    env.env["PLANE_MCP_HOST"] = "not a host"
    assert env.run("--dry-run").returncode == 2


def test_rendered_entries_are_exactly_the_url_and_nothing_secret_shaped():
    mod = load_installer()
    claude = mod.render_claude(HOST)
    pi = mod.render_pi(HOST)
    assert claude == {"type": "http", "url": URL}
    assert pi == {"url": URL}
    text = json.dumps([claude, pi])
    assert "example.invalid" not in text and "<" not in text
    assert leaks(text) == []


def test_the_templates_hold_only_the_placeholder_host_and_parse():
    assert sorted(p.name for p in CLIENTS.iterdir() if p.is_file()) == ["claude-code.json", "pi-mcp.json"]
    for name in ("claude-code.json", "pi-mcp.json"):
        text = (CLIENTS / name).read_text()
        assert json.loads(text)["url"] == "https://plane-mcp.example.invalid/http/mcp"
        assert leaks(text) == []


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_real_claude_cli_agrees_with_the_fake(tmp_path):
    """The fake above is only evidence while the real CLI behaves the same way."""
    home, cfg = tmp_path / "home", tmp_path / "cfg"
    home.mkdir()
    cfg.mkdir()
    env = {**os.environ, "HOME": str(home), "CLAUDE_CONFIG_DIR": str(cfg)}
    env.pop("PLANE_MCP_HOST", None)
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

    assert run("--host", HOST).returncode == 0
    assert servers()["plane"] == {"type": "http", "url": URL}
    assert {k: v for k, v in servers().items() if k != "plane"} == OTHER_CLAUDE["mcpServers"]
    assert "unchanged plane" in run("--host", HOST).stdout
    assert run("--host", "plane-mcp.agent-cloud.test:8443").returncode == 0
    assert servers()["plane"]["url"] == "https://plane-mcp.agent-cloud.test:8443/http/mcp"
    assert run("--remove").returncode == 0
    assert servers() == OTHER_CLAUDE["mcpServers"]
