"""The agentgateway MCP client templates carry no secret and no live repo-root config
(agentgateway-mcp 7.2, 7.5, 7.8 scenario "Client templates contain no secret")."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENTS = ROOT / "platform/services/agentgateway/context/clients"
INSTALLER = ROOT / "scripts/agw-mcp-client.py"
BEARER_VALUE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}")
KEY_SHAPED = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")


def leaks(text):
    return BEARER_VALUE.findall(text) + KEY_SHAPED.findall(text)


def installer():
    spec = importlib.util.spec_from_file_location("agw_mcp_client", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_scan_catches_what_it_is_meant_to():
    assert leaks('{"Authorization": "Bearer abcdef1234567890"}')
    assert leaks("k0123456789012345678901234567890123456789012345")
    assert not leaks('{"url": "https://mcp.example.invalid/mcp"}')


@pytest.mark.parametrize("name", ["claude-code.json", "pi-mcp.json"])
def test_a_template_parses_and_holds_no_bearer_value_or_key_shaped_string(name):
    text = (CLIENTS / name).read_text()
    json.loads(text)
    assert leaks(text) == []


def test_only_the_two_templates_exist_and_both_are_scanned():
    # Files only: a template is a file here. pi-provider/ is a package (its own tests), not a template.
    assert sorted(p.name for p in CLIENTS.iterdir() if p.is_file()) == ["claude-code.json", "pi-mcp.json"]


def test_templates_use_the_placeholder_host_and_a_command_reference():
    claude = json.loads((CLIENTS / "claude-code.json").read_text())
    pi = json.loads((CLIENTS / "pi-mcp.json").read_text())
    assert claude["url"] == "https://mcp.example.invalid/mcp"
    assert pi["url"] == "https://mcp.example.invalid/mcp"
    assert pi["headers"]["Authorization"].startswith("!")
    assert "headersHelper" in claude and "Bearer" not in json.dumps([claude, pi])


def test_rendered_entries_have_exactly_the_documented_fields_and_no_placeholder_left(monkeypatch):
    """The fields are those the two harnesses document; a template gaining one must be a decision."""
    mod = installer()
    # The helper path is the checkout's own, so a long directory name would look key-shaped to the scan.
    monkeypatch.setattr(mod, "HELPER", Path("/opt/agw/agw-mcp-key"))
    claude = mod.render_claude("mcp.example.test", "alice", None)
    pi = mod.render_pi("mcp.example.test", "alice", None)
    assert set(claude) == {"type", "url", "headersHelper"}
    assert set(pi) == {"url", "headers"} and set(pi["headers"]) == {"Authorization"}
    text = json.dumps([claude, pi])
    assert "example.invalid" not in text and "<" not in text
    assert leaks(text) == []


def test_no_repo_root_mcp_json_and_it_stays_ignored():
    assert not (ROOT / ".mcp.json").exists()
    assert "/.mcp.json" in (ROOT / ".gitignore").read_text().splitlines()
