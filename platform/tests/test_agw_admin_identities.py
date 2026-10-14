"""Personal gateway identities from the declared admin list (agentgateway-mcp tasks 2.4, 2.5, 2.7-2.9).

Inventory `platform_admins` (one mapping per admin: key, username, name, email, optional
agw_identity) gives each admin a personal gateway identity: `agw_identity` if set, else the
username. They merge with `agw_clients` into `agw_identities`, computed once in
vars/secret-declarations/agentgateway.yml, which the deploy, the key-management playbook and
config.yaml.j2 all read. The real vars file, the real guard tasks from both playbooks and the real
config template run on localhost through harness_sandbox, so nothing is written outside the
test's directory.

What each test pins: the derivation and its order; that a refused entry is refused BEFORE the
render (the guard runs ahead of manage-secrets); that an identity moved from `agw_clients` into
`platform_admins` keeps the identical keyHash line through the overlap and the removal (the same
client_<name> field, so nothing is minted twice) and the key-hash count never drops; and that the
render check names a dropped or extra identity without printing a hash or key.
"""

import json
import re
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import yaml

REPO = playbook_yaml.REPO
DEPLOY = REPO / "platform/playbooks/deploy-agentgateway.yml"
MANAGE = REPO / "platform/playbooks/manage-agentgateway-client-key.yml"
DECL = REPO / "platform/playbooks/vars/secret-declarations/agentgateway.yml"
CONFIG = REPO / "platform/services/agentgateway/deployment/templates/config.yaml.j2"

PHASE1 = next(p for p in yaml.safe_load(DEPLOY.read_text()) if p.get("name", "").startswith("Phase 1"))
MANAGE_PLAY = next(p for p in yaml.safe_load(MANAGE.read_text()) if p.get("name", "").startswith("Rotate or revoke"))

LOAD_TASK = "Load the shared platform admin list"
PROBLEMS_TASK = "Refuse derived admin identities that collide, are reserved or are malformed"
OVERLAP_TASK = "Report identities declared in both platform_admins and agw_clients"
CHARSET_TASK = "Refuse identity or group names outside the safe charset"
READ_TASK = "Read the identity names and key-entry count the render wrote"
CHECK_MISSING = "Require the rendered config to enrol every declared identity"
CHECK_EXTRA = "Require the rendered config to enrol only declared identities"
CHECK_COUNT = "Require the rendered config to hold one key entry per declared identity"


def _task(play: dict, name: str) -> dict:
    return next(t for t in play["tasks"] if t.get("name") == name)


def admin(key: str, username: str, **extra) -> dict:
    return {"key": key, "username": username, "name": key.title(), "email": f"{key}@example.invalid", **extra}


def _ansible(tmp: Path, host_vars: dict, tasks: list, play_vars: dict | None = None):
    # A test that declares an admin list means to derive from it: the gate is on unless it says.
    # `agw_admin_identities_enabled: None` leaves the variable undefined.
    if "platform_admins" in host_vars:
        host_vars = {"agw_admin_identities_enabled": True, **host_vars}
    host_vars = {k: v for k, v in host_vars.items() if not (k == "agw_admin_identities_enabled" and v is None)}
    inv = {"all": {"hosts": {"gw": {"ansible_connection": "local", **host_vars}}}}
    (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
    (tmp / "play.yml").write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "all",
                    "gather_facts": False,
                    "vars_files": [str(DECL)],
                    "vars": play_vars or {},
                    "tasks": tasks,
                }
            ]
        )
    )
    return harness_sandbox.run(
        ["ansible-playbook", "-i", str(tmp / "inv.yml"), str(tmp / "play.yml")],
        tmp,
        cwd=REPO,
        env=harness_sandbox.env_for(tmp),
    )


def _eval(tmp: Path, host_vars: dict) -> dict:
    """The vars file's computed values for one inventory."""
    out = tmp / "eval.json"
    expr = (
        "{{ {'identities': agw_identities, 'overlap': _agw_identity_overlap, "
        "'problems': _agw_identity_problems, 'defs': _secret_definitions | map(attribute='name') | list} | to_json }}"
    )
    r = _ansible(tmp, host_vars, [{"ansible.builtin.copy": {"content": expr, "dest": str(out)}}])
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(out.read_text())


def _guard(tmp: Path, host_vars: dict, names=(PROBLEMS_TASK,), play=PHASE1, play_vars=None):
    return _ansible(tmp, host_vars, [_task(play, n) for n in names], play_vars)


# ── Derivation (2.4) ──────────────────────────────────────────────────────────


def test_one_identity_per_admin_named_by_the_override_else_the_username(tmp_path):
    got = _eval(
        tmp_path,
        {
            "agw_clients": ["skynet"],
            "platform_admins": [admin("alice", "alice"), admin("bob", "bob.smith", agw_identity="bob")],
        },
    )
    assert got["identities"] == ["skynet", "alice", "bob"]
    # client_<name> is declared for each, so manage-secrets mints it once and reuses it after.
    assert got["defs"][-3:] == ["client_skynet", "client_alice", "client_bob"]
    assert got["problems"] == [] and got["overlap"] == []


@pytest.mark.parametrize("hv", [{}, {"platform_admins": []}])
def test_no_declared_admins_leaves_the_identities_equal_to_agw_clients(tmp_path, hv):
    got = _eval(tmp_path, {"agw_clients": ["workstation", "pi"], **hv})
    assert got["identities"] == ["workstation", "pi"]
    assert got["defs"][-2:] == ["client_workstation", "client_pi"]


def test_a_name_in_both_lists_is_merged_once_and_reported_by_name(tmp_path):
    hv = {"agw_clients": ["workstation", "pi"], "platform_admins": [admin("w", "workstation"), admin("c", "carol")]}
    got = _eval(tmp_path, hv)
    assert got["identities"] == ["workstation", "pi", "carol"]
    assert got["defs"].count("client_workstation") == 1
    assert got["overlap"] == ["workstation"]
    r = _guard(tmp_path, hv, (PROBLEMS_TASK, OVERLAP_TASK, CHARSET_TASK))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "migration overlap" in r.stdout and "workstation" in r.stdout


def test_no_overlap_task_output_when_nothing_overlaps(tmp_path):
    r = _guard(tmp_path, {"agw_clients": ["a"], "platform_admins": [admin("b", "b")]}, (OVERLAP_TASK,))
    assert r.returncode == 0 and "migration overlap" not in r.stdout


# ── Refusals (2.4, 2.8) ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "admins,entry,why",
    [
        ([admin("alice", "alice"), admin("al2", "alice")], "al2", "more than once"),
        ([admin("alice", "alice"), admin("al2", "bob", agw_identity="alice")], "alice", "more than once"),
        ([admin("ann", "Ann.Smith")], "ann", "agw_identity"),
        ([admin("ann", "ann", agw_identity="under_score")], "ann", "agw_identity"),
        ([admin("brk", "legacy-shared")], "brk", "reserved"),
        ([admin("svc", "x", agw_identity="mcp-self")], "svc", "reserved"),
    ],
)
def test_a_bad_derived_name_is_refused_naming_the_entry(tmp_path, admins, entry, why):
    r = _guard(tmp_path, {"agw_clients": ["skynet"], "platform_admins": admins})
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert f"platform_admins entry {entry}" in out and why in out


def test_an_entry_with_no_key_is_named_by_position(tmp_path):
    r = _guard(tmp_path, {"platform_admins": [{"username": "ok"}, {"username": "Bad Name"}]})
    assert r.returncode != 0 and "platform_admins entry #2" in r.stdout + r.stderr


def test_an_entry_with_no_username_or_override_is_refused(tmp_path):
    r = _guard(tmp_path, {"platform_admins": [{"key": "ghost"}]})
    assert r.returncode != 0 and "platform_admins entry ghost" in r.stdout + r.stderr


def test_the_refusal_runs_before_manage_secrets_and_the_render():
    names = [t.get("name") for t in PHASE1["tasks"]]
    refuse = names.index(PROBLEMS_TASK)
    assert refuse < names.index("Manage secrets and render env + config")
    assert refuse < names.index("Place the monorepo + ensure podman/compose")
    assert refuse < names.index("Refuse a cleartext OpenBao endpoint")


def test_the_key_management_play_refuses_a_bad_list_before_it_touches_the_store():
    names = [t.get("name") for t in MANAGE_PLAY["tasks"]]
    assert names.index(PROBLEMS_TASK) < names.index("Authenticate to OpenBao")
    assert names.index(PROBLEMS_TASK) < names.index(
        "Revoke: remove the client field (shared merge task, merge-patch null, absence verified)"
    )


def test_a_derived_name_outside_the_charset_is_also_stopped_by_the_charset_assert(tmp_path):
    # Defence in depth: the merged list is what the YAML/CEL interpolation sees.
    r = _guard(tmp_path, {"agw_clients": ["Bad_Client"]}, (CHARSET_TASK,))
    assert r.returncode != 0 and "Bad_Client" in r.stdout + r.stderr


# ── Rotate / revoke validate against the merged list (2.5, 2.8) ───────────────


def _manage(tmp: Path, host_vars: dict, client: str, action: str):
    names = [
        "Require an explicit client and action",
        "Rotate: the identity must be declared in inventory",
        "Revoke: the identity must already be gone from inventory",
    ]
    play_vars = {k: v for k, v in MANAGE_PLAY["vars"].items() if k in ("_client", "_action", "_field", "_declared")}
    return _ansible(
        tmp, {**host_vars, "client": client, "action": action}, [_task(MANAGE_PLAY, n) for n in names], play_vars
    )


def test_rotate_accepts_a_declared_admin_and_refuses_an_unknown_name(tmp_path):
    hv = {"agw_clients": ["skynet"], "platform_admins": [admin("alice", "alice")]}
    assert _manage(tmp_path, hv, "alice", "rotate").returncode == 0
    assert _manage(tmp_path, hv, "skynet", "rotate").returncode == 0
    r = _manage(tmp_path, hv, "mallory", "rotate")
    assert r.returncode != 0 and "not in agw_identities" in r.stdout + r.stderr


def test_revoke_needs_the_name_removed_from_the_merged_list(tmp_path):
    hv = {"agw_clients": ["skynet"], "platform_admins": [admin("alice", "alice")]}
    for name in ("alice", "skynet"):
        r = _manage(tmp_path, hv, name, "revoke")
        assert r.returncode != 0 and "still in agw_identities" in r.stdout + r.stderr, name
    # Removed from BOTH lists: the revoke may proceed.
    assert _manage(tmp_path, {"agw_clients": ["skynet"]}, "alice", "revoke").returncode == 0


# ── Render level: an identity keeps its hash through the migration (2.7) ──────

IDS = ["alice", "bob"]
SECRETS = {
    "vllm_api_key": "v",
    "agw_db_password": "p",
    "agw_oidc_cookie_seed": "s",
    **{f"client_{n}": f"stored-key-for-{n}" for n in ["alice", "bob", "carol", "skynet"]},
}


def _render(tmp: Path, **hv) -> str:
    hv = {
        "agw_models": [{"name": "m"}],
        "agw_upstream_base_url": "http://u.invalid/v1",
        "agw_ui_enabled": False,
        "secrets": SECRETS,
        **hv,
    }
    task = {"ansible.builtin.template": {"src": str(CONFIG), "dest": str(tmp / "config.yaml"), "mode": "0644"}}
    r = _ansible(tmp, hv, [task])
    assert r.returncode == 0, r.stdout + r.stderr
    return (tmp / "config.yaml").read_text()


def _hash_lines(text: str) -> dict:
    """identity -> its keyHash line, read from the rendered key entries (the hash line only)."""
    return {
        m.group(2): m.group(1)
        for m in re.finditer(r"^\s*- (keyHash: sha256:[0-9a-f]{64})\n\s+metadata:\n\s+name: (\S+)$", text, re.M)
    }


def test_a_migrated_identity_keeps_its_hash_through_overlap_and_removal(tmp_path):
    steps = {
        "declared in agw_clients only": {"agw_clients": IDS},
        "overlap: also in platform_admins": {"agw_clients": IDS, "platform_admins": [admin(n, n) for n in IDS]},
        "removed from agw_clients": {"agw_clients": [], "platform_admins": [admin(n, n) for n in IDS]},
    }
    rendered = {}
    for i, (label, hv) in enumerate(steps.items()):
        d = tmp_path / str(i)
        d.mkdir()
        rendered[label] = _hash_lines(_render(d, **hv))
    before = rendered["declared in agw_clients only"]
    assert sorted(before) == IDS and len(set(before.values())) == 2
    for label, got in rendered.items():
        assert got == before, label  # same identities, byte-identical hash lines, count never drops


def test_a_new_declared_admin_is_enrolled_beside_the_existing_identities(tmp_path):
    before = _hash_lines(_render(tmp_path, agw_clients=["skynet"]))
    d = tmp_path / "after"
    d.mkdir()
    after = _hash_lines(_render(d, agw_clients=["skynet"], platform_admins=[admin("c", "carol")]))
    assert set(after) == {"skynet", "carol"} and after["skynet"] == before["skynet"]


def test_a_removed_admin_drops_out_of_the_render(tmp_path):
    with_admin = _hash_lines(_render(tmp_path, agw_clients=["skynet"], platform_admins=[admin("c", "carol")]))
    d = tmp_path / "removed"
    d.mkdir()
    without = _hash_lines(_render(d, agw_clients=["skynet"], platform_admins=[]))
    assert set(with_admin) - set(without) == {"carol"}


def test_no_declared_admins_renders_exactly_what_the_fallback_renders(tmp_path):
    # With platform_admins empty the merged list IS agw_clients, so the render equals one made by a
    # caller that never loaded the vars file (the template's fallback).
    with_list = _render(tmp_path, agw_clients=["skynet", "alice"], platform_admins=[])
    d = tmp_path / "bare"
    d.mkdir()
    inv = {
        "all": {
            "hosts": {
                "gw": {
                    "ansible_connection": "local",
                    "agw_clients": ["skynet", "alice"],
                    "agw_models": [{"name": "m"}],
                    "agw_upstream_base_url": "http://u.invalid/v1",
                    "agw_ui_enabled": False,
                    "secrets": SECRETS,
                }
            }
        }
    }
    (d / "inv.yml").write_text(yaml.safe_dump(inv))
    task = {"ansible.builtin.template": {"src": str(CONFIG), "dest": str(d / "config.yaml"), "mode": "0644"}}
    (d / "play.yml").write_text(yaml.safe_dump([{"hosts": "all", "gather_facts": False, "tasks": [task]}]))
    r = harness_sandbox.run(
        ["ansible-playbook", "-i", str(d / "inv.yml"), str(d / "play.yml")], d, cwd=REPO, env=harness_sandbox.env_for(d)
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (d / "config.yaml").read_text() == with_list


# ── Render check (2.9, gateway half) ──────────────────────────────────────────


def _check(tmp: Path, config_text: str, host_vars: dict, legacy_active: bool = False):
    (tmp / "config.yaml").write_text(config_text)
    play_vars = {"_deploy_dir": str(tmp), "_legacy_shared_active": legacy_active}
    names = [READ_TASK, CHECK_MISSING, CHECK_EXTRA, CHECK_COUNT]
    return _ansible(tmp, host_vars, [_task(PHASE1, n) for n in names], play_vars)


def _drop_entry(text: str, name: str) -> str:
    """The rendered config without one identity's whole key entry."""
    pattern = re.compile(
        r"^\s*- keyHash: sha256:[0-9a-f]{64}\n\s+metadata:\n\s+name: " + re.escape(name) + r"\n"
        r"(?:(?!\s*- keyHash:)(?!\s*# Request-rate)(?!\s*localRateLimit).*\n)*",
        re.M,
    )
    out, n = pattern.subn("", text, count=1)
    assert n == 1
    return out


HV = {"agw_clients": ["skynet"], "platform_admins": [admin("a", "alice"), admin("b", "bob")]}


def test_the_render_check_passes_for_the_declared_identities(tmp_path):
    r = _check(tmp_path, _render(tmp_path, **HV), HV)
    assert r.returncode == 0, r.stdout + r.stderr


MISSING_MSG = "does not enrol these declared identities: "
EXTRA_MSG = "enrols identities that are not declared: "
COUNT_MSG = "key entries for"


def test_the_render_check_names_a_dropped_identity_and_prints_no_hash(tmp_path):
    text = _render(tmp_path, **HV)
    r = _check(tmp_path, _drop_entry(text, "alice"), HV)
    out = r.stdout + r.stderr
    assert r.returncode != 0 and MISSING_MSG + "alice." in out and EXTRA_MSG not in out
    assert not re.search(r"sha256:[0-9a-f]{16}", out) and "stored-key-for" not in out


def test_the_render_check_names_an_unexpected_identity(tmp_path):
    # Nothing is missing and only the name is extra, so only the extra-name assert can say it.
    text = _render(tmp_path, **HV)
    r = _check(tmp_path, text, {**HV, "platform_admins": [admin("a", "alice")]})
    out = r.stdout + r.stderr
    assert r.returncode != 0 and EXTRA_MSG + "bob." in out and MISSING_MSG not in out


def test_the_render_check_counts_key_entries_as_well_as_names(tmp_path):
    # Every name present once over: only the count is wrong.
    text = _render(tmp_path, **HV)
    first = re.search(r"^\s*- keyHash: sha256:[0-9a-f]{64}$", text, re.M).group(0)
    r = _check(tmp_path, text.replace(first, first + "\n" + first, 1), HV)
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "holds 4 key entries for 3 declared identities" in out
    assert MISSING_MSG not in out and EXTRA_MSG not in out


def test_the_render_check_catches_a_swapped_name_that_keeps_the_count(tmp_path):
    # The same number of key entries, one under the wrong name: only the name comparison sees it.
    text = _render(tmp_path, **HV)
    swapped = text.replace("name: alice\n", "name: mallory\n", 1)
    assert swapped != text
    r = _check(tmp_path, swapped, HV)
    assert r.returncode != 0 and MISSING_MSG + "alice." in r.stdout + r.stderr


def test_the_render_check_expects_legacy_shared_only_while_its_grace_period_runs(tmp_path):
    text = _render(tmp_path, **HV, _legacy_shared_active=True)
    assert "name: legacy-shared" in text
    assert _check(tmp_path, text, HV, legacy_active=True).returncode == 0
    r = _check(tmp_path, text, HV, legacy_active=False)
    assert r.returncode != 0 and EXTRA_MSG + "legacy-shared." in r.stdout + r.stderr


def _with_mcp_keys(text: str, names: list) -> str:
    """The config with a second, top-level policy carrying its own key list (the section 3 mcp loop)."""
    entries = "".join(f"      - keyHash: sha256:{'ab' * 32}\n        metadata:\n          name: {n}\n" for n in names)
    return text.rstrip("\n") + "\nmcp:\n  policies:\n    apiKey:\n      keys:\n" + entries


def test_the_render_check_counts_the_llm_keys_only(tmp_path):
    # Another policy's key list must not be counted as the llm one, or section 3's mcp loop (the
    # same identities again) would fail every deploy.
    text = _render(tmp_path, **HV)
    assert _check(tmp_path, _with_mcp_keys(text, ["skynet", "alice", "bob"]), HV).returncode == 0


def test_a_name_held_only_by_another_policy_does_not_cover_a_dropped_llm_key(tmp_path):
    text = _render(tmp_path, **HV)
    broken = _with_mcp_keys(_drop_entry(text, "alice"), ["skynet", "alice", "bob"])
    r = _check(tmp_path, broken, HV)
    assert r.returncode != 0 and MISSING_MSG + "alice." in r.stdout + r.stderr


# ── The gate and the shared list (agw_admin_identities_enabled) ───────────────

BAD = [admin("brk", "legacy-shared"), admin("dup", "x"), admin("dup2", "x")]


@pytest.mark.parametrize("gate", [{"agw_admin_identities_enabled": None}, {"agw_admin_identities_enabled": False}])
def test_the_gate_off_derives_nothing_and_checks_nothing(tmp_path, gate):
    # The list is non-empty by default; off, the gateway must behave exactly as before. Even a list
    # that would be refused is ignored, because it is not read.
    hv = {"agw_clients": ["workstation"], "platform_admins": [admin("alice", "alice"), *BAD], **gate}
    got = _eval(tmp_path, hv)
    assert got["identities"] == ["workstation"] and got["problems"] == [] and got["overlap"] == []
    assert got["defs"][-1:] == ["client_workstation"]
    r = _guard(tmp_path, hv, (PROBLEMS_TASK, CHARSET_TASK))
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_gate_off_renders_exactly_what_a_deploy_without_a_list_renders(tmp_path):
    off = _render(
        tmp_path, agw_clients=["skynet"], platform_admins=[admin("a", "alice")], agw_admin_identities_enabled=False
    )
    d = tmp_path / "bare"
    d.mkdir()
    assert _render(d, agw_clients=["skynet"]) == off
    assert set(_hash_lines(off)) == {"skynet"}


def test_the_gate_on_derives_for_every_entry_service_accounts_included(tmp_path):
    svc = admin("svc", "svc-automation-user", type="service_account")
    hv = {"agw_clients": ["skynet"], "platform_admins": [admin("alice", "alice"), svc]}
    got = _eval(tmp_path, hv)
    assert got["identities"] == ["skynet", "alice", "svc-automation-user"]
    assert got["defs"][-2:] == ["client_alice", "client_svc-automation-user"]
    d = tmp_path / "render"
    d.mkdir()
    assert set(_hash_lines(_render(d, **hv, secrets={**SECRETS, "client_svc-automation-user": "k"}))) == {
        "skynet",
        "alice",
        "svc-automation-user",
    }


# The shared file's shape (the Authentik branch owns the real one): `_platform_admins` is the
# inventory's `platform_admins` when set, else a default built from per-user vars.
SHARED_FILE = """---
_platform_admins_default:
  - {key: stray_admin, username: stray, name: stray, email: stray@agent-cloud.test}
  - {key: wisward, username: "{{ wisward_username | default('wisward') }}", name: W, email: w@agent-cloud.test}
  - {key: svc_automation, username: svc-automation-user, name: Service, type: service_account}
_platform_admins: "{{ platform_admins if platform_admins is defined else _platform_admins_default }}"
"""
LOADER = yaml.safe_load((REPO / "platform/playbooks/tasks/load-platform-admins.yml").read_text())


def _load(tmp: Path, host_vars: dict, shared: str | None):
    """Run the shared loader tasks in a directory that holds (or lacks) vars/platform-admins.yml, then
    write what the vars file computed."""
    if shared is not None:
        (tmp / "vars").mkdir(exist_ok=True)
        (tmp / "vars/platform-admins.yml").write_text(shared)
    out = tmp / "loaded.json"
    expr = "{{ {'identities': agw_identities, 'loaded': _platform_admins | default('UNSET')} | to_json }}"
    tasks = [*LOADER, {"ansible.builtin.copy": {"content": expr, "dest": str(out)}}]
    r = _ansible(tmp, host_vars, tasks)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads(out.read_text())
    return got["identities"], r.stdout, got["loaded"]


def test_the_gate_on_loads_the_shared_list_with_every_entry_service_account_included(tmp_path):
    ids, out, _ = _load(tmp_path, {"agw_clients": ["skynet"], "agw_admin_identities_enabled": True}, SHARED_FILE)
    assert ids == ["skynet", "stray", "wisward", "svc-automation-user"] and "absent" not in out


def test_an_inventory_list_wins_over_the_files_default(tmp_path):
    hv = {"agw_clients": [], "agw_admin_identities_enabled": True, "platform_admins": [admin("zed", "zed")]}
    ids, _, _ = _load(tmp_path, hv, SHARED_FILE)
    assert ids == ["zed"]


def test_the_gate_off_does_not_load_the_shared_list(tmp_path):
    ids, out, loaded = _load(tmp_path, {"agw_clients": ["skynet"]}, SHARED_FILE)
    assert ids == ["skynet"] and "absent" not in out
    assert loaded == "UNSET"  # the file itself was not read


def test_the_gate_on_with_no_shared_list_says_so_and_derives_nothing(tmp_path):
    ids, out, _ = _load(tmp_path, {"agw_clients": ["skynet"], "agw_admin_identities_enabled": True}, None)
    assert ids == ["skynet"] and "admin list file absent" in out


def test_both_playbooks_load_the_list_before_the_refusal():
    for play in (PHASE1, MANAGE_PLAY):
        names = [t.get("name") for t in play["tasks"]]
        assert names.index(LOAD_TASK) < names.index(PROBLEMS_TASK)
        assert _task(play, LOAD_TASK)["ansible.builtin.include_tasks"] == "tasks/load-platform-admins.yml"


@pytest.mark.parametrize("path", ["rollback-inference-route.yml", "run-agw-conformance.yml"])
def test_every_reader_of_the_merged_list_loads_the_declaration_and_the_admin_list(path):
    plays = yaml.safe_load((REPO / "platform/playbooks" / path).read_text())
    play = next(p for p in plays if "vars/secret-declarations/agentgateway.yml" in p.get("vars_files", []))
    assert _task(play, LOAD_TASK)["ansible.builtin.include_tasks"] == "tasks/load-platform-admins.yml"
    assert "agw_identities" in (REPO / "platform/playbooks" / path).read_text()
