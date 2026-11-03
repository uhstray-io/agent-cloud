"""deploy-plane-mcp.yml wiring the registration script's tests cannot see.

The script is tested against a stub (test_plane_register_mcp_app.py); these pin what the playbook
feeds it: the MCP server's two OAuth callbacks, and the client id the held secret belongs to.
"""

import pathlib

import yaml

PLAYBOOK = pathlib.Path(__file__).resolve().parents[2] / "platform/playbooks/deploy-plane-mcp.yml"
PLAYS = yaml.safe_load(PLAYBOOK.read_text())


def phase1():
    return next(p for p in PLAYS if p.get("name", "").startswith("Phase 1"))


def register_task():
    block = next(t for t in phase1()["tasks"] if "block" in t)
    return next(t for t in block["block"] if t["name"].startswith("Register or repair"))


def test_both_server_callbacks_are_registered():
    # fastmcp's OAuth proxy calls back on <base_url>/auth/callback; the server's HTTP transport
    # has base_url <public>/http and its SSE transport <public> (plane_mcp/__main__.py).
    assert phase1()["vars"]["_mcp_redirect_uris"] == (
        "{{ plane_mcp_public_url }}/http/auth/callback {{ plane_mcp_public_url }}/auth/callback")


def test_the_held_secret_travels_with_the_client_id_it_belongs_to():
    stdin = register_task()["ansible.builtin.command"]["stdin"]
    assert "'have_secret':" in stdin
    assert "'stored_client_id': _plane_secret.json.data.data.plane_mcp_client_id" in stdin


def test_the_registration_output_stays_out_of_the_log():
    task = register_task()
    assert task["no_log"] is True
    assert "client_secret" not in str(task["changed_when"])


def phase3():
    return next(p for p in PLAYS if p.get("name", "").startswith("Phase 3"))


def test_the_anonymous_probe_reads_no_stdin():
    # Over SSH an `exec -i` with empty stdin never sees end-of-file, so a readline() hangs the
    # deploy (production Semaphore task 11731).
    anon = next(t for t in phase3()["tasks"] if t["name"] == "An anonymous call is refused")
    argv = anon["ansible.builtin.command"]["argv"]
    assert "-i" not in argv and "stdin" not in anon["ansible.builtin.command"]
    assert argv[-1] == "anonymous"
    probe = phase3()["vars"]["_probe"]
    assert 'sys.stdin.readline().strip() if sys.argv[2:3] == ["token"] else ""' in probe


def test_the_token_probe_says_a_token_comes():
    task = next(t for t in phase3()["tasks"] if t["name"].startswith("The token endpoint lists"))
    argv = task["ansible.builtin.command"]["argv"]
    assert "-i" in argv and argv[-1] == "token"
    # Without stdin Ansible opens no pipe, the exec inherits the SSH stdin and readline() hangs.
    assert task["ansible.builtin.command"].get("stdin")
