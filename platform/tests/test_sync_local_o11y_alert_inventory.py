"""Private Discord destination sync keeps unrelated local inventory untouched."""

import base64
import importlib.util
import io
import json
import re
import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/sync-local-o11y-alert-inventory.py"
SPEC = importlib.util.spec_from_file_location("sync_local_o11y_alert_inventory", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)

CURRENT = """[o11y_svc]
o11y-local ansible_host=127.0.0.1

[o11y_svc:vars]
service_name=o11y
local_mode=true

[other_svc]
other-local ansible_host=127.0.0.1
"""
VALUES = {"o11y_alert_discord_guild_id": "1" * 18,
          "o11y_alert_discord_channel_id": "2" * 19}


def test_overlay_changes_only_the_declared_group_and_converges():
    updated = sync.overlay(CURRENT, VALUES)
    assert updated.split("[other_svc]")[1] == CURRENT.split("[other_svc]")[1]
    assert updated.count("o11y_alert_discord_guild_id=") == 1
    assert updated.count("o11y_alert_discord_channel_id=") == 1
    assert sync.overlay(updated, VALUES) == updated
    assert "local_mode=true\no11y_alert_discord_guild_id=" in sync.overlay(
        CURRENT.split("[other_svc]")[0].rstrip("\n"), VALUES)


def test_overlay_refuses_ambiguous_section_or_variable():
    for inventory in (CURRENT.replace("[other_svc]", "[o11y_svc:vars]\n[other_svc]"),
                      CURRENT.replace("service_name=o11y", "o11y_alert_discord_guild_id=1\n"
                                      "o11y_alert_discord_guild_id=2\nservice_name=o11y"),
                      CURRENT.replace("[o11y_svc:vars]", "[wrong:vars]"),
                      CURRENT.replace("o11y-local ansible_host", "other-local ansible_host")):
        with pytest.raises(sync.Refusal):
            sync.overlay(inventory, VALUES)


def test_destination_reads_only_private_o11y_declaration(tmp_path):
    source = tmp_path / "production.yml"
    source.write_text(f"""agent_cloud:
  children:
    o11y_svc:
      vars:
        o11y_alert_discord_guild_id: '{VALUES["o11y_alert_discord_guild_id"]}'
        o11y_alert_discord_channel_id: '{VALUES["o11y_alert_discord_channel_id"]}'
    unrelated:
      vars:
        private_address: '192.0.2.1'
""")
    assert sync.destination(source) == VALUES
    source.write_text(source.read_text().replace(VALUES["o11y_alert_discord_channel_id"], "invalid"))
    with pytest.raises(sync.Refusal):
        sync.destination(source)


def test_preview_then_apply_updates_only_the_local_inventory(monkeypatch, tmp_path, capsys):
    private = tmp_path / "site-config"
    (private / "inventory").mkdir(parents=True)
    (private / "inventory/production.yml").write_text(f"""agent_cloud:
  children:
    o11y_svc:
      vars:
        o11y_alert_discord_guild_id: '{VALUES["o11y_alert_discord_guild_id"]}'
        o11y_alert_discord_channel_id: '{VALUES["o11y_alert_discord_channel_id"]}'
""")
    sha = "a" * 40
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(sync, "PROJECTION", state / "o11y-alert-destination.json")
    monkeypatch.setattr(sync, "git", lambda _root, *args:
                        str(private) if args == ("rev-parse", "--show-toplevel") else
                        sha + "\t" + args[-1] if args[0] == "ls-remote" else
                        "" if args[0] == "status" else sha)
    record = {"id": 1, "project_id": 1, "name": "local", "type": "static",
              "ssh_key_id": 2, "become_key_id": None, "inventory": CURRENT}
    writes = []

    class Opener:
        def open(self, request, timeout):
            if request.get_method() == "PUT":
                writes.append(json.loads(request.data))
                record.update(writes[-1])
                return io.BytesIO(b"")
            return io.BytesIO(json.dumps(record).encode())

    monkeypatch.setattr(sync.urllib.request, "build_opener", lambda *_: Opener())
    argv = ["sync", "--site-config-root", str(private), "--site-config-sha", sha,
            "--expected-dev-sha", sha, "--url", "https://semaphore.example.test",
            "--inventory", "1"]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", io.StringIO("synthetic-token"))
    assert sync.main() == 0
    assert writes == []
    assert not sync.PROJECTION.exists()
    monkeypatch.setattr(sys, "argv", argv + ["--apply"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("synthetic-token"))
    assert sync.main() == 0
    assert len(writes) == 1
    assert writes[0]["inventory"] == sync.overlay(CURRENT, VALUES)
    assert json.loads(sync.PROJECTION.read_text()) == {"site_config_sha": sha, **VALUES}
    assert sync.PROJECTION.stat().st_mode & 0o777 == 0o600
    assert all(value not in capsys.readouterr().out for value in VALUES.values())


def test_readback_mismatch_does_not_save_projection(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(sync, "PROJECTION", state / "o11y-alert-destination.json")
    monkeypatch.setattr(sync, "git", lambda root, *args:
                        str(root) if args == ("rev-parse", "--show-toplevel") else
                        "a" * 40 + "\t" + args[-1] if args[0] == "ls-remote" else
                        "" if args[0] == "status" else "a" * 40)
    private = tmp_path / "private"
    (private / "inventory").mkdir(parents=True)
    (private / "inventory/production.yml").write_text(f"""agent_cloud:
  children:
    o11y_svc:
      vars:
        o11y_alert_discord_guild_id: '{VALUES["o11y_alert_discord_guild_id"]}'
        o11y_alert_discord_channel_id: '{VALUES["o11y_alert_discord_channel_id"]}'
""")
    record = {"id": 1, "project_id": 1, "name": "local", "type": "static", "inventory": CURRENT}

    class Opener:
        def open(self, request, timeout):
            return io.BytesIO(json.dumps(record).encode())

    monkeypatch.setattr(sync.urllib.request, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(sys, "argv", ["sync", "--site-config-root", str(private),
                                      "--site-config-sha", "a" * 40,
                                      "--expected-dev-sha", "a" * 40,
                                      "--url", "https://semaphore.example.test",
                                      "--inventory", "1", "--apply"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("synthetic-token"))
    assert sync.main() == 1
    assert not sync.PROJECTION.exists()


def test_bootstrap_renders_private_projection_only_when_present():
    play = yaml.safe_load((SCRIPT.parents[1] / "platform/playbooks/bootstrap-local-dev.yml").read_text())[0]
    task = next(item for item in play["tasks"] if item["name"] == "Create or update local inventory")
    block = task["vars"]["_inv_ini"].split("[o11y_svc:vars]")[1].split("[opa_svc]")[0]
    env = Environment()
    env.filters["b64decode"] = lambda value: base64.b64decode(value).decode()
    env.filters["from_json"] = json.loads
    absent = env.from_string(block).render(_bao_url_net="http://openbao",
                                           _o11y_projection_stat={"stat": {"exists": False}})
    assert "o11y_alert_discord_guild_id=" not in absent
    encoded = base64.b64encode(json.dumps(VALUES).encode()).decode()
    present = env.from_string(block).render(_bao_url_net="http://openbao",
                                            _o11y_projection_stat={"stat": {"exists": True}},
                                            _o11y_projection_raw={"content": encoded})
    assert f"o11y_alert_discord_guild_id={VALUES['o11y_alert_discord_guild_id']}" in present
    assert f"o11y_alert_discord_channel_id={VALUES['o11y_alert_discord_channel_id']}" in present


def test_bootstrap_refusals_evaluate_against_synthetic_inventory():
    play = yaml.safe_load((SCRIPT.parents[1] / "platform/playbooks/bootstrap-local-dev.yml").read_text())[0]
    env = Environment()
    env.tests["match"] = lambda value, pattern: bool(re.match(pattern, value))
    env.tests["search"] = lambda value, pattern: bool(re.search(pattern, value))

    def accepts(task_name, **context):
        task = next(item for item in play["tasks"] if item["name"] == task_name)
        conditions = task["ansible.builtin.assert"]["that"]
        return all(env.compile_expression(expression)(**context) for expression in conditions)

    complete = "Require complete local inventory before replacing it"
    assert accepts(complete, _existing_local={"inventory": CURRENT})
    assert not accepts(complete, _existing_local={"id": 1})

    validate = "Validate private local alert projection before inventory replacement"
    good = {"site_config_sha": "a" * 40, **VALUES}
    stat = {"stat": {"isreg": True, "mode": "0600"}}
    assert accepts(validate, _o11y_projection_stat=stat, _o11y_projection=good)
    for bad_stat, bad_value in [({"stat": {"isreg": False, "mode": "0600"}}, good),
                                ({"stat": {"isreg": True, "mode": "0644"}}, good),
                                (stat, {**good, "extra": "x"}),
                                (stat, {**good, "site_config_sha": "bad"}),
                                (stat, {**good, "o11y_alert_discord_channel_id": "bad"})]:
        assert not accepts(validate, _o11y_projection_stat=bad_stat, _o11y_projection=bad_value)

    missing = "Refuse to erase a previously synced local alert destination"
    assert accepts(missing, _existing_o11y_inventory=CURRENT)
    synced = sync.overlay(CURRENT, VALUES)
    assert not accepts(missing, _existing_o11y_inventory=synced)

    stale = "Refuse a stale local alert projection"
    assert accepts(stale, _existing_o11y_inventory=CURRENT, _o11y_projection=good)
    assert accepts(stale, _existing_o11y_inventory=synced, _o11y_projection=good)
    changed = {**good, "o11y_alert_discord_channel_id": "3" * 19}
    assert not accepts(stale, _existing_o11y_inventory=synced, _o11y_projection=changed)
