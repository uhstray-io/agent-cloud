"""The just-in-time runner half of github_app_token.py (openspec add-ephemeral-github-runners).

What these hold is what goes wrong silently: a runner minted into the wrong group, a prune
that deletes a persistent registration or a runner still holding a stocked configuration,
and a configuration that reaches anything but stdout. The forge is replaced by a recorder;
no network is touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import github_app_token as gat  # noqa: E402

ORG = "uhstray-io"
PREFIX = "gh-runner-01-jit-"


class Forge:
    """Records every call and answers from fixed state."""

    def __init__(self, groups=None, runners=None):
        self.groups = groups if groups is not None else [{"id": 7, "name": "uhstray-selfhosted"}]
        self.runners = runners or []
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, path, bearer, method="GET", body=None):
        self.calls.append((method, path, body))
        assert bearer == "itok"
        if path.startswith(f"/orgs/{ORG}/actions/runner-groups"):
            return {"runner_groups": self.groups}
        if path.startswith(f"/orgs/{ORG}/actions/runners/generate-jitconfig"):
            return {"runner": {"id": 900 + len(self.calls), "name": body["name"]}, "encoded_jit_config": "Q09ORklH"}
        if path.startswith(f"/orgs/{ORG}/actions/runners?"):
            return {"runners": self.runners}
        if method == "DELETE":
            return None
        raise AssertionError(f"unexpected call {method} {path}")

    def deleted(self):
        return [p.rsplit("/", 1)[1] for m, p, _ in self.calls if m == "DELETE"]


def runner(rid, name, status="offline", busy=False, ephemeral=True):
    return {"id": rid, "name": name, "status": status, "busy": busy, "ephemeral": ephemeral}


def test_jit_configs_bind_name_group_labels_and_work_folder():
    forge = Forge()
    with patch.object(gat, "api", forge):
        out = gat.jit_configs("itok", ORG, "uhstray-selfhosted", ["self-hosted", "uhstray-lan"], PREFIX, 2)
    posts = [(p, b) for m, p, b in forge.calls if m == "POST"]
    assert len(posts) == 2 and len(out) == 2
    for (path, body), item in zip(posts, out, strict=True):
        assert path == f"/orgs/{ORG}/actions/runners/generate-jitconfig"
        assert body["runner_group_id"] == 7
        assert body["labels"] == ["self-hosted", "uhstray-lan"]
        assert body["work_folder"] == "_work"
        assert body["name"].startswith(PREFIX) and body["name"] == item["name"]
        assert item["encoded_jit_config"] == "Q09ORklH"
    assert out[0]["name"] != out[1]["name"]


def test_an_unknown_group_is_refused_before_anything_is_minted():
    # A runner must never land in another group: the default group admits every repository.
    forge = Forge(groups=[{"id": 1, "name": "Default"}])
    with patch.object(gat, "api", forge), pytest.raises(SystemExit):
        gat.jit_configs("itok", ORG, "uhstray-selfhosted", ["uhstray-lan"], PREFIX, 1)
    assert not [c for c in forge.calls if c[0] == "POST"]


@pytest.mark.parametrize("bad", ["", "gh-runner-01-", "gh-runner-01", "-jit-", "x y-jit-", "gh/runner-jit-"])
def test_a_prefix_must_name_one_hosts_ephemeral_runners(bad):
    # prune deletes by prefix, so a prefix that could match other runners is refused.
    with pytest.raises(SystemExit):
        gat.check_prefix(bad)


def test_the_prefix_shape_is_accepted():
    assert gat.check_prefix(PREFIX) == PREFIX


def test_prune_deletes_only_idle_unheld_ephemeral_runners_of_the_prefix():
    forge = Forge(
        runners=[
            runner(1, PREFIX + "stale"),  # deleted
            runner(2, PREFIX + "stocked"),  # held in the stock
            runner(3, PREFIX + "online", status="online"),  # running, idle
            runner(4, PREFIX + "busy", status="online", busy=True),  # running a job
            runner(5, PREFIX + "persistentlike", ephemeral=False),  # not ephemeral
            runner(6, "gh-runner-02-jit-other"),  # another host
            runner(7, "gh-runner-01"),  # the persistent registration
        ]
    )
    with patch.object(gat, "api", forge):
        out = gat.prune("itok", ORG, PREFIX, [PREFIX + "stocked"], None)
    assert forge.deleted() == ["1"]
    assert out["deleted"] == [PREFIX + "stale"]
    assert out["online"] == [PREFIX + "online"]
    assert out["busy"] == [PREFIX + "busy"]
    assert out["retired"] is None


def test_retire_deletes_the_persistent_registration_unless_busy():
    forge = Forge(runners=[runner(7, "gh-runner-01", status="offline", ephemeral=False)])
    with patch.object(gat, "api", forge):
        out = gat.prune("itok", ORG, PREFIX, [], "gh-runner-01")
    assert out["retired"] == "gh-runner-01" and forge.deleted() == ["7"]

    busy = Forge(runners=[runner(7, "gh-runner-01", status="online", busy=True, ephemeral=False)])
    with patch.object(gat, "api", busy), pytest.raises(SystemExit):
        gat.prune("itok", ORG, PREFIX, [], "gh-runner-01")
    assert busy.deleted() == []


def test_retire_refuses_a_name_inside_the_prefix():
    with patch.object(gat, "api", Forge()), pytest.raises(SystemExit):
        gat.prune("itok", ORG, PREFIX, [], PREFIX + "x")


def test_listing_pages_until_a_short_page():
    pages = {1: [runner(i, f"{PREFIX}{i}") for i in range(100)], 2: [runner(100, PREFIX + "last")]}
    seen = []

    def fake(path, bearer, method="GET", body=None):
        page = int(path.rsplit("page=", 1)[1])
        seen.append(page)
        return {"runners": pages.get(page, [])}

    with patch.object(gat, "api", fake):
        out = gat.runners_with_prefix("itok", ORG, PREFIX)
    assert seen == [1, 2] and len(out) == 101


def run_main(argv, stdin=b"KEY"):
    out = io.StringIO()
    fake_stdin = io.TextIOWrapper(io.BytesIO(stdin))
    with (
        patch.object(sys, "argv", ["github_app_token.py", *argv]),
        patch.object(sys, "stdin", fake_stdin),
        patch.object(gat, "installation_token", return_value="itok") as itok,
        contextlib.redirect_stdout(out),
    ):
        gat.main()
    return out.getvalue(), itok


def test_jit_config_cli_prints_one_json_object_per_runner_and_reads_the_key_from_stdin():
    forge = Forge()
    with patch.object(gat, "api", forge):
        out, itok = run_main(
            ["jit-config", "--issuer", "i", "--org", ORG, "--prefix", PREFIX, "--group", "uhstray-selfhosted",
             "--label", "self-hosted", "--label", "uhstray-lan", "--count", "3"]
        )
    lines = [json.loads(line) for line in out.splitlines()]
    assert len(lines) == 3 and all(set(x) == {"name", "id", "encoded_jit_config"} for x in lines)
    assert itok.call_args.args == ("i", b"KEY", ORG)


def test_list_and_prune_cli_print_names_never_configurations():
    forge = Forge(runners=[runner(1, PREFIX + "a")])
    with patch.object(gat, "api", forge):
        out, _ = run_main(["list-runners", "--issuer", "i", "--org", ORG, "--prefix", PREFIX])
    assert json.loads(out) == {"online": [], "busy": [], "offline": [PREFIX + "a"]}
    assert forge.deleted() == []


def test_jit_config_requires_a_group():
    with patch.object(gat, "api", Forge()), pytest.raises(SystemExit):
        run_main(["jit-config", "--issuer", "i", "--org", ORG, "--prefix", PREFIX])


def test_api_sends_a_json_body_only_when_given_one():
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout):
        sent.append(req)
        return Resp()

    with patch.object(gat.urllib.request, "urlopen", fake_urlopen):
        gat.api("/x", "t")
        gat.api("/y", "t", method="POST", body={"a": 1})
    assert sent[0].data is None and "Content-type" not in sent[0].headers
    assert json.loads(sent[1].data) == {"a": 1} and sent[1].headers["Content-type"] == "application/json"


def test_a_young_unheld_record_survives_a_prune():
    # The stock was read before the prune; a concurrent run may have minted and stocked this
    # one since (CodeRabbit review of #716). Deleting it would strand a stocked configuration.
    young = gat.jit_name(PREFIX)
    old = PREFIX + "20200101000000-abcdef"
    forge = Forge(runners=[runner(1, young), runner(2, old)])
    with patch.object(gat, "api", forge):
        out = gat.prune("itok", ORG, PREFIX, [], None, grace_minutes=30)
    assert forge.deleted() == ["2"] and out["deleted"] == [old]

    forge = Forge(runners=[runner(1, young)])
    with patch.object(gat, "api", forge):
        gat.prune("itok", ORG, PREFIX, [], None, grace_minutes=0)
    assert forge.deleted() == ["1"]


def test_the_mint_time_is_read_back_from_the_name():
    name = gat.jit_name(PREFIX)
    assert abs(gat.minted_at(name, PREFIX) - time.time()) < 5
    assert gat.minted_at(PREFIX + "notatime-abc", PREFIX) is None
