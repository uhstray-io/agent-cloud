"""Private Discord destination sync keeps unrelated local inventory untouched."""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

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
    monkeypatch.setattr(sync, "git", lambda _root, *args:
                        str(private) if args == ("rev-parse", "--show-toplevel") else
                        sha + "\trefs/heads/dev" if args[0] == "ls-remote" else
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
    monkeypatch.setattr(sys, "argv", argv + ["--apply"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("synthetic-token"))
    assert sync.main() == 0
    assert len(writes) == 1
    assert writes[0]["inventory"] == sync.overlay(CURRENT, VALUES)
    assert all(value not in capsys.readouterr().out for value in VALUES.values())
