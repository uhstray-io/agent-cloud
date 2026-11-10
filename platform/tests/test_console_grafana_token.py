"""Dispatch Console Grafana token: the playbook's structure, and its behaviour against fake APIs.

Structure checks read the playbook and template YAML. Behaviour runs the playbook for real
against a synthetic Grafana and OpenBao (seed_harness), so the reuse, mint, verify-before-store
and fail-closed store paths are exercised, not just described. All values are synthetic.
"""

import json
import os
import re
import subprocess
from urllib.parse import urlsplit

import pytest
import yaml
from seed_harness import ROLE, ROOT, FakeBao, serve

PLAYBOOK = "platform/playbooks/provision-console-grafana-token.yml"
TEMPLATES = ROOT / "platform/semaphore/templates.yml"
TEMPLATE_NAME = "Provision Console Grafana Token"
TTL = 7776000
DAY = 86400
STORED = "synthetic-stored-console-token"
NEW_KEY = "synthetic-minted-console-token"
RACED = "synthetic-raced-console-token"
ADMIN = "synthetic-admin-password"
ENGINE = f"""#!/bin/sh
shift            # exec
[ "$1" = "-i" ] && shift
shift            # container name
GF_SECURITY_ADMIN_PASSWORD={ADMIN} exec "$@"
"""

# Any task whose stdin, stdout, argv or registered result can carry a token, or whose value is
# the token's stored form. These must be no_log: a debug or failure would print the key.
TOKEN_BEARING = re.compile(
    r"secondsToLive|_new_key|_new_token_id|_sa_token\b|_stored_token\b|_stored_check|_new_check"
    r"|_console_guard|console_grafana_token\b|_bao_auth|_o11y_secret"
)


def _playbook():
    return yaml.safe_load((ROOT / PLAYBOOK).read_text())


def _provision_play():
    return next(p for p in _playbook() if isinstance(p, dict) and p.get("hosts") == "o11y_svc")


def _leaves(tasks):
    """Every task in document order, descending into block/rescue/always. Blocks are not leaves."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        if any(k in task for k in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                yield from _leaves(task.get(key))
        else:
            yield task


def _find(tasks, name):
    matches = [t for t in _leaves(tasks) if t.get("name") == name]
    assert len(matches) == 1, f"expected one task named {name!r}, found {len(matches)}"
    return matches[0]


def _block_holding(tasks, name):
    for task in tasks:
        if any(child.get("name") == name for child in _leaves(task.get("block"))):
            return task
    raise AssertionError(f"no block holds {name!r}")


# ---- structure -------------------------------------------------------------------------------


def test_refusal_import_is_the_first_play():
    assert _playbook()[0]["ansible.builtin.import_playbook"] == "refuse-internal-extra-vars.yml"


def test_template_is_a_dev_variant_with_no_survey_or_binding():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    entries = [t for t in templates if t.get("name") == TEMPLATE_NAME]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["playbook"] == PLAYBOOK
    assert entry["dev_variant"] is True
    assert "survey_vars" not in entry and "repository" not in entry and "inventory" not in entry


def test_console_is_a_viewer_and_the_token_lifetime_is_the_literal_ninety_days():
    play = _provision_play()
    assert play["vars"]["_sa_name"] == "console"
    assert play["vars"]["_sa_role"] == "Viewer"
    assert play["vars"]["_ttl_seconds"] == TTL == 7776000
    assert re.search(r"_ttl_seconds:\s*7776000\b", (ROOT / PLAYBOOK).read_text())
    assert play["vars"]["console_grafana_token_renew_days"] == 30


def test_token_name_is_utc_epoch_from_the_date_binary_not_strftime_percent_s():
    # now(fmt='%s') reads the UTC wall clock as local time: off by the controller's UTC offset.
    name = _find(_provision_play()["tasks"], "Pick the token name from the controller's UTC epoch seconds")
    value = name["ansible.builtin.set_fact"]["_console_token_name"]
    assert "fmt=" not in value and "now(" not in value
    assert "date -u +%s" in value


def test_mint_is_no_log_and_only_runs_outside_check_mode():
    play = _provision_play()
    mint = _find(play["tasks"], "Mint the console token (90-day lifetime; the key is returned once)")
    assert mint.get("no_log") is True
    assert "secondsToLive" in json.dumps(mint["ansible.builtin.command"])
    block = _block_holding(play["tasks"], mint["name"])
    assert "not ansible_check_mode" in str(block["when"])


def test_every_token_bearing_task_is_no_log():
    play = _provision_play()
    for task in _leaves(play["tasks"]):
        if TOKEN_BEARING.search(yaml.safe_dump(task)):
            assert task.get("no_log") is True, f"token-bearing task without no_log: {task.get('name')}"


@pytest.mark.parametrize("name", [
    "Find the console service account",
    "Create the console service account (Viewer)",
    "Report the decision (names and days only, never a value)",
])
def test_account_lookup_create_and_report_are_not_no_log(name):
    assert not _find(_provision_play()["tasks"], name).get("no_log")


def test_store_comes_after_the_minted_token_is_proven():
    tasks = _provision_play()["tasks"]
    names = [t.get("name") for t in _leaves(tasks)]
    proof = "Require the minted token to authenticate as the exact console account before storing it"
    store = "Store the verified token and its id in secret/services/o11y"
    assert names.index(proof) < names.index(store)
    # The store sits inside the mint block, so it is reached only through that proof.
    assert store in [c.get("name") for c in _leaves(_block_holding(tasks, store)["block"])]


def test_reuse_needs_the_exact_account_and_more_than_the_renewal_window():
    tasks = _provision_play()["tasks"]
    expr = _find(tasks, "Decide whether the stored token is reused")["ansible.builtin.set_fact"]["_reuse_token"]
    for needle in ("_stored_status == '200'", "'service-account:'", "_stored_seconds_left",
                   "console_grafana_token_renew_days", "86400"):
        assert needle in expr, needle
    seconds = _find(tasks, "Find the stored token's remaining lifetime in seconds (-1 when it is not listed)")
    assert "secondsUntilExpiration" in seconds["ansible.builtin.set_fact"]["_stored_seconds_left"]


def test_store_is_a_guarded_fail_closed_merge_of_exactly_the_two_fields():
    tasks = _provision_play()["tasks"]
    store = _find(tasks, "Store the verified token and its id in secret/services/o11y")
    assert store["ansible.builtin.include_tasks"] == "tasks/bao-merge-keys.yml"
    assert store["vars"]["_bm_path"] == "services/o11y"
    assert store["vars"]["_bm_on_missing"] == "fail"
    assert set(store["vars"]["_bm_data"]) == {"console_grafana_token", "console_grafana_token_id"}
    assert "_bm_guard_values" in store["vars"] and "_bm_guard_absent_keys" in store["vars"]


def test_report_names_the_decision_and_never_a_value():
    msg = str(_find(_provision_play()["tasks"], "Report the decision (names and days only, never a value)")
              ["ansible.builtin.debug"]["msg"])
    assert "reused" in msg and "minted" in msg
    for leak in ("_new_key", "_stored_token ", "_console_guard", "_sa_token"):
        assert leak not in msg


def test_template_is_not_declared_with_a_schedule_or_a_redeploy():
    templates = yaml.safe_load(TEMPLATES.read_text())["templates"]
    entry = next(t for t in templates if t.get("name") == TEMPLATE_NAME)
    assert "schedule" not in entry
    assert "deploy-console" not in json.dumps(_playbook())


# ---- behaviour against a synthetic Grafana and OpenBao ----------------------------------------


def make_handler(*, stored=STORED, stored_id=11, seconds_left=80 * DAY, role="Viewer", disabled=False,
                 account=True, health=200, rejection=401, mint_accepted=True, race_on_store=False,
                 identities=None):
    """`stored=None` means no console token is stored yet. `seconds_left` is the stored token's
    remaining lifetime as the token list reports it."""

    class Handler(FakeBao):
        requests = []
        minted = []
        store = {
            "grafana_admin_password": "synthetic-gf",
            **({"console_grafana_token": stored, "console_grafana_token_id": stored_id} if stored else {}),
        }
        accepted = {stored} if stored else set()
        identity_by_token = {STORED: "service-account:7", NEW_KEY: "service-account:7"}
        identity_by_token.update(identities or {})
        sa = {"id": 7, "name": "console", "role": role, "isDisabled": disabled} if account else None
        tokens = ([{"id": stored_id, "name": "console-old", "role": "Viewer",
                    "secondsUntilExpiration": seconds_left, "hasExpired": False}] if stored else [])
        version = 1
        inject_race = race_on_store

        def _drain(self):
            return self.body() if int(self.headers.get("Content-Length", 0)) else {}

        def do_POST(self):
            self.record("POST")
            body = self._drain()
            cls = type(self)
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            if self.path == "/v1/secret/data/services/o11y":
                if cls.inject_race:
                    # A second rotation lands after the helper's fresh read but before its CAS.
                    cls.inject_race = False
                    cls.store["console_grafana_token"] = RACED
                    cls.version += 1
                if body.get("options", {}).get("cas") != cls.version:
                    return self.reply({"errors": ["check-and-set parameter did not match current version"]}, 400)
                cls.store = body["data"]
                cls.version += 1
                return self.reply({"data": {"version": cls.version}})
            if self.path == "/api/serviceaccounts":
                cls.sa = {"id": 7, "name": "console", "role": body["role"], "isDisabled": False}
                return self.reply(cls.sa, 201)
            if self.path == "/api/serviceaccounts/7/tokens":
                cls.minted.append(body)
                cls.tokens.append({"id": 12, "name": body["name"], "role": "Viewer",
                                   "secondsUntilExpiration": body["secondsToLive"], "hasExpired": False})
                if mint_accepted:
                    cls.accepted.add(NEW_KEY)
                return self.reply({"id": 12, "name": body["name"], "key": NEW_KEY})
            return self.reply({}, 404)

        def do_GET(self):
            self.record("GET")
            cls = type(self)
            path = urlsplit(self.path).path
            if path == "/v1/secret/data/services/o11y":
                return self.reply({"data": {"data": cls.store, "metadata": {"version": cls.version}}})
            if path == "/api/user":
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                if token not in cls.accepted:
                    return self.reply({"message": "invalid API key"}, rejection)
                return self.reply({"uid": cls.identity_by_token.get(token, "")}, health)
            if path == "/api/serviceaccounts/search":
                return self.reply({"serviceAccounts": [cls.sa] if cls.sa else []})
            if path == "/api/serviceaccounts/7":
                return self.reply(cls.sa) if cls.sa else self.reply({}, 404)
            if path == "/api/serviceaccounts/7/tokens":
                return self.reply(cls.tokens)
            return self.reply({}, 404)

    return Handler


def run(tmp_path, handler, *, check=False):
    engine = tmp_path / "engine"
    engine.write_text(ENGINE)
    engine.chmod(0o755)
    with serve(handler) as addr:
        inventory = tmp_path / "inventory.yml"
        inventory.write_text(json.dumps({"all": {
            "vars": {"openbao_addr": addr},
            "children": {"o11y_svc": {"hosts": {"localhost": {
                "ansible_connection": "local", "container_engine": str(engine),
                "o11y_grafana_local_url": addr}}}},
        }}))
        env = {k: v for k, v in os.environ.items() if k not in ("OPENBAO_ADDR", "BAO_ROLE_ID", "BAO_SECRET_ID")}
        env.update(ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
        result = subprocess.run(
            ["ansible-playbook", "-v", *(["--check"] if check else []), "-i", str(inventory), PLAYBOOK,
             "-e", json.dumps(ROLE)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=180, stdin=subprocess.DEVNULL,
        )
    out = result.stdout + result.stderr
    for secret in (STORED, NEW_KEY, RACED, ADMIN, "synthetic-login-value", "synthetic-role-secret"):
        assert secret not in out
    return result.returncode, out


def writes(handler):
    return [r for r in handler.requests
            if r[0] in ("PATCH", "DELETE") or (r[0] == "POST" and r[1] != "/v1/auth/approle/login")]


NEAR_EXPIRY = 10 * DAY


def test_healthy_token_is_reused_and_nothing_is_written(tmp_path):
    handler = make_handler()
    rc, out = run(tmp_path, handler)
    assert rc == 0, out
    assert "reused" in out and "80 days remaining" in out
    assert writes(handler) == [] and handler.minted == []
    assert handler.store["console_grafana_token"] == STORED


def test_token_inside_the_renewal_window_is_minted_proven_then_stored(tmp_path):
    handler = make_handler(seconds_left=NEAR_EXPIRY)
    rc, out = run(tmp_path, handler)
    assert rc == 0, out
    assert "minted" in out
    assert handler.minted and handler.minted[0]["secondsToLive"] == TTL
    assert handler.store["console_grafana_token"] == NEW_KEY
    assert handler.store["console_grafana_token_id"] == 12
    assert writes(handler) == [("POST", "/api/serviceaccounts/7/tokens"),
                               ("POST", "/v1/secret/data/services/o11y")]


def test_absent_stored_token_is_minted_and_stored_under_the_absent_guard(tmp_path):
    handler = make_handler(stored=None)
    rc, out = run(tmp_path, handler)
    assert rc == 0, out
    assert handler.store["console_grafana_token"] == NEW_KEY
    assert handler.store["console_grafana_token_id"] == 12


def test_minted_token_that_does_not_authenticate_is_never_stored(tmp_path):
    handler = make_handler(seconds_left=NEAR_EXPIRY, mint_accepted=False)
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "did not authenticate as the console service account" in out
    assert handler.store["console_grafana_token"] == STORED
    assert ("POST", "/v1/secret/data/services/o11y") not in writes(handler)


@pytest.mark.parametrize("health", [503])
def test_outage_status_fails_without_minting(tmp_path, health):
    handler = make_handler(seconds_left=NEAR_EXPIRY, health=health)
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert f"status {health}" in out
    assert writes(handler) == [] and handler.minted == []


def test_check_mode_reports_the_mint_and_writes_nothing(tmp_path):
    handler = make_handler(seconds_left=NEAR_EXPIRY)
    rc, out = run(tmp_path, handler, check=True)
    assert rc == 0, out
    assert "would be minted" in out
    assert writes(handler) == [] and handler.minted == []
    assert handler.store["console_grafana_token"] == STORED


def test_concurrent_rotation_fails_closed_and_keeps_the_rotated_value(tmp_path):
    handler = make_handler(seconds_left=NEAR_EXPIRY, race_on_store=True)
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "version-guarded update" in out
    assert handler.store["console_grafana_token"] == RACED


def test_account_with_the_wrong_role_is_refused_before_any_mint(tmp_path):
    handler = make_handler(seconds_left=NEAR_EXPIRY, role="Admin")
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert "needs an enabled Viewer" in out
    assert handler.minted == [] and writes(handler) == []


def test_token_of_another_account_is_not_reused(tmp_path):
    handler = make_handler(identities={STORED: "service-account:99"})
    rc, out = run(tmp_path, handler)
    assert rc == 0, out
    assert handler.minted and handler.store["console_grafana_token"] == NEW_KEY
