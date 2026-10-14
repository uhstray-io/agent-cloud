"""The platform administrators are rendered from ONE declared list, and the render is the same
account set the fixed blueprints used to declare (agentgateway-mcp tasks 2.1-2.3, 2.6).

The fixed blocks (stray-admin.yaml, agent-cloud-admin.yaml, service-account.yaml and the wisward
and jacob blocks of platform-users.yaml) were replaced by templates/platform-admins.yaml.j2. The
golden file holds what those blocks declared, normalised, for two input sets: the placeholder
defaults and a synthetic set of per-user overrides using site-config's variable names. The
render must equal it for the SAME inputs, whether the list comes from the committed default or
from an inventory `platform_admins` that spells the same accounts out.

Requires ansible-playbook (the real `template` module renders the files, as the deploy does).
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
DEPLOYMENT = REPO / "platform/services/authentik/deployment"
GOLDEN = Path(__file__).parent / "fixtures" / "authentik_platform_admins_golden.json"

pytestmark = needs_ansible

# Synthetic per-user overrides in site-config's variable names; none is a real value.
OVERRIDES = {
    "stray_admin_email": "stray.synthetic@example.invalid",
    "wisward_username": "synthetic.alpha",
    "wisward_name": "Synthetic Alpha",
    "wisward_email": "synthetic.alpha@example.invalid",
    "jacob_username": "synthetic.beta",
    "jacob_name": "Synthetic Beta",
    "jacob_email": "synthetic.beta@example.invalid",
}
# The same accounts, spelled out as an inventory `platform_admins`.
EXPLICIT_LIST = [
    {"key": "stray_admin", "username": "stray", "name": "stray", "email": OVERRIDES["stray_admin_email"]},
    {
        "key": "agent_cloud_admin",
        "username": "agent-cloud-admin",
        "name": "agent-cloud Admin",
        "email": "agent-cloud-admin@agent-cloud.test",
    },
    {
        "key": "wisward",
        "username": OVERRIDES["wisward_username"],
        "name": OVERRIDES["wisward_name"],
        "email": OVERRIDES["wisward_email"],
    },
    {
        "key": "jacob",
        "username": OVERRIDES["jacob_username"],
        "name": OVERRIDES["jacob_name"],
        "email": OVERRIDES["jacob_email"],
    },
    {
        "key": "svc_automation",
        "username": "svc-automation",
        "name": "Platform Service Account",
        "type": "service_account",
    },
]


# ── normalising a blueprint ─────────────────────────────────────────────────────
def _loader(env: dict, context: dict):
    class L(yaml.SafeLoader):
        pass

    def env_tag(loader, node):
        if isinstance(node, yaml.SequenceNode):
            name, default = (loader.construct_object(n) for n in node.value)
            return env.get(name, default)
        return {"env": loader.construct_scalar(node)}

    L.add_constructor("!Env", env_tag)
    L.add_constructor("!Context", lambda ld, n: context.get(ld.construct_scalar(n)))
    L.add_constructor("!KeyOf", lambda ld, n: {"keyof": ld.construct_scalar(n)})
    L.add_constructor("!Find", lambda ld, n: {"find": ld.construct_sequence(n, deep=True)})
    return L


def _parse(text: str, env: dict, context: dict):
    loader = _loader(env, context)(text)  # a SafeLoader subclass: only the tags above are added
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def normalise(text: str, env: dict, declared: set) -> list:
    """The administrator accounts (and API tokens) a blueprint declares, resolved against `env`.

    `declared` is the set of OpenBao field names the deploy generates, so a password is recorded
    with whether its field exists as well as the value the worker env would hand it."""
    context = _parse(text, env, {}).get("context") or {}
    doc = _parse(text, env, context)
    ids = {e["id"]: e["identifiers"]["username"] for e in doc["entries"] if e.get("id")}
    out = []
    for e in doc["entries"]:
        attrs = e.get("attrs") or {}
        if e["model"] == "authentik_core.user":
            groups = [g["find"][1][1] for g in attrs.get("groups", [])]
            if "platform-admins" not in groups or e.get("state") == "absent":
                continue
            pw = attrs.get("password")
            entry = {
                "kind": "user",
                "username": e["identifiers"]["username"],
                "name": attrs.get("name"),
                "email": attrs.get("email"),
                "is_active": attrs.get("is_active"),
                "path": attrs.get("path"),
                "type": attrs.get("type", "internal"),
                "groups": groups,
                "password": None,
            }
            if pw is not None:
                name = pw["env"]
                entry["password"] = {
                    "env": name,
                    "value": env.get(name, "<UNSET>"),
                    "field_declared": name.lower() in declared,
                }
            out.append(entry)
        elif e["model"] == "authentik_core.token":
            key = attrs["key"]["env"]
            out.append(
                {
                    "kind": "token",
                    "identifier": e["identifiers"]["identifier"],
                    "user": ids[attrs["user"]["keyof"]],
                    "intent": attrs["intent"],
                    "key": {"env": key, "value": env.get(key, "<UNSET>")},
                    "expiring": attrs["expiring"],
                    "description": attrs["description"],
                }
            )
    return sorted(out, key=lambda x: (x["kind"], x.get("username") or x["identifier"]))


def parse_env(text: str) -> dict:
    return dict(ln.split("=", 1) for ln in text.splitlines() if re.match(r"^[A-Z][A-Z0-9_]*=", ln))


# ── rendering the way the deploy does ───────────────────────────────────────────
def _run(tmp: Path, play: dict, extra: dict):
    (tmp / "vars").symlink_to(PLAYBOOKS / "vars")  # the secret declaration reads vars/ next to the playbook
    (tmp / "play.yml").write_text(yaml.safe_dump([play]))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    done = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(tmp / "play.yml"), "-e", json.dumps(extra)],
        cwd=tmp,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def render_new(tmp: Path, inventory: dict) -> tuple[list, dict]:
    """Render env.j2 and platform-admins.yaml.j2 for an inventory; return (entries, declared fields)."""
    play = {
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars_files": [
            str(PLAYBOOKS / "vars/platform-admins.yml"),
            str(PLAYBOOKS / "vars/secret-declarations/authentik.yml"),
        ],
        "tasks": [
            {
                "ansible.builtin.set_fact": {
                    "secrets": "{{ _secret_definitions | map(attribute='name') | "
                    "map('regex_replace', '^(.*)$', 'S_\\1') | list }}"
                }
            },
            {
                "ansible.builtin.set_fact": {
                    "secrets": "{{ dict(_secret_definitions | map(attribute='name') | list | zip(secrets)) }}"
                }
            },
            {
                "ansible.builtin.template": {
                    "src": str(DEPLOYMENT / "templates/env.j2"),
                    "dest": str(tmp / "env"),
                    "mode": "0600",
                }
            },
            {
                "ansible.builtin.template": {
                    "src": str(DEPLOYMENT / "templates/platform-admins.yaml.j2"),
                    "dest": str(tmp / "bp.yaml"),
                    "mode": "0644",
                }
            },
            {
                "ansible.builtin.copy": {
                    "dest": str(tmp / "defs.json"),
                    "content": "{{ _secret_definitions | to_json }}",
                }
            },
        ],
    }
    _run(tmp, play, inventory)
    declared = {d["name"] for d in json.loads((tmp / "defs.json").read_text())}
    # Every other secret env.j2 reads is a static one the stub also supplies.
    return normalise((tmp / "bp.yaml").read_text(), parse_env((tmp / "env").read_text()), declared), declared


CASES = {
    "placeholder-defaults": {},
    "per-user-overrides": OVERRIDES,
}


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text())


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_render_equals_what_the_fixed_blocks_declared(tmp_path, golden, case):
    # username, name, email, active, path, type, groups and the password's env name, value source
    # and OpenBao field: identical to the old fixed blocks for the same inputs.
    entries, _ = render_new(tmp_path, CASES[case])
    assert entries == golden["cases"][case]


def test_an_inventory_list_replaces_the_default_and_renders_the_same_accounts(tmp_path, golden):
    # `platform_admins` in the inventory is a whole list that wins over the committed default.
    entries, _ = render_new(tmp_path, {"platform_admins": EXPLICIT_LIST})
    assert entries == golden["cases"]["per-user-overrides"]


def test_the_old_per_user_vars_are_ignored_once_a_list_is_declared(tmp_path):
    # The list replaces the default completely: a stale wisward_username does not leak in.
    entries, _ = render_new(tmp_path, {"platform_admins": [EXPLICIT_LIST[0]], **OVERRIDES})
    assert [e["username"] for e in entries if e["kind"] == "user"] == ["stray"]


def test_no_password_value_is_rendered_into_the_blueprint(tmp_path):
    # The rendered file is mode 0644 on the host: it may name the env variable, never carry a value.
    render_new(tmp_path, {})
    body = (tmp_path / "bp.yaml").read_text()
    assert "S_" not in body
    assert len(re.findall(r": !Env \w+$", body, re.M)) == 5  # four passwords + the service account token


def test_every_password_the_blueprint_reads_is_a_declared_openbao_field(tmp_path):
    entries, declared = render_new(tmp_path, {})
    pw = [e["password"] for e in entries if e["kind"] == "user" and e["password"]]
    assert len(pw) == 4
    assert all(p["field_declared"] and p["value"] != "<UNSET>" for p in pw), pw
    assert "service_account_token" in declared and "svc_automation_password" not in declared


def test_the_existing_openbao_field_names_are_kept(tmp_path):
    _, declared = render_new(tmp_path, {})
    assert {
        "stray_admin_password",
        "agent_cloud_admin_password",
        "wisward_password",
        "jacob_password",
        "andrew_password",
        "service_account_token",
    } <= declared


def test_the_placeholders_carry_no_real_address():
    # Placeholders only: every committed address is on the reserved .test TLD. A real address
    # lives in site-config; this repo is public.
    defaults = yaml.safe_load((PLAYBOOKS / "vars/platform-admins.yml").read_text())["_platform_admins_default"]
    text = json.dumps(defaults)
    addresses = re.findall(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+", text)
    assert addresses and all(a.endswith("@agent-cloud.test") for a in addresses), addresses
    example = (REPO / "platform/inventory/local-dev.yml.example").read_text()
    block = "\n".join(ln for ln in example.splitlines() if re.match(r"\s*#\s+- \{ key: ", ln))
    assert all(a.endswith("@agent-cloud.test") for a in re.findall(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+", block)), block


def test_the_inventory_example_documents_the_same_default():
    # local-dev.yml.example only DOCUMENTS the var (as comments); what it shows must stay the
    # committed default, key for key, so the documentation cannot drift from the code.
    example = (REPO / "platform/inventory/local-dev.yml.example").read_text()
    lines = [ln for ln in example.splitlines() if re.match(r"\s*#\s+- \{ key: ", ln)]
    shown = [re.search(r"key: (\w+)", ln).group(1) for ln in lines]
    defaults = yaml.safe_load((PLAYBOOKS / "vars/platform-admins.yml").read_text())["_platform_admins_default"]
    assert shown == [d["key"] for d in defaults]


# ── the deploy's own validation of the list ─────────────────────────────────────
def _deploy_tasks() -> list:
    plays = yaml.safe_load((PLAYBOOKS / "deploy-authentik.yml").read_text())
    return [t for p in plays for t in p.get("tasks", [])]


def _validate(tmp: Path, inventory: dict) -> subprocess.CompletedProcess:
    """Run the deploy's two list validations (lifted by name) against an inventory."""
    tasks = _deploy_tasks()
    names = [t.get("name", "") for t in tasks]
    picked = [
        tasks[names.index("Refuse a malformed administrator list")],
        tasks[names.index("Refuse an empty list, duplicate administrators and secret-field collisions")],
    ]
    (tmp / "vars").symlink_to(PLAYBOOKS / "vars")
    play = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars_files": [
                str(PLAYBOOKS / "vars/platform-admins.yml"),
                str(PLAYBOOKS / "vars/secret-declarations/authentik.yml"),
            ],
            "tasks": picked,
        }
    ]
    (tmp / "validate.yml").write_text(yaml.safe_dump(play))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(tmp / "validate.yml"), "-e", json.dumps(inventory)],
        cwd=tmp,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _entry(**kw) -> dict:
    return {"key": "carol", "username": "carol", "name": "Carol Placeholder", "email": "carol@agent-cloud.test", **kw}


@pytest.mark.parametrize(
    "admins",
    [
        [_entry()],
        [
            _entry(),
            _entry(key="dave", username="dave"),
            _entry(key="svc_one", username="svc-one", email="", type="service_account"),
        ],
    ],
)
def test_a_well_formed_list_is_accepted(tmp_path, admins):
    done = _validate(tmp_path, {"platform_admins": admins})
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_committed_default_is_accepted(tmp_path):
    done = _validate(tmp_path, {})
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    "admins, why",
    [
        ([], "empty list"),
        ([_entry(), _entry(key="dave")], "duplicate username"),
        ([_entry(), _entry(username="dave")], "duplicate key"),
        ([_entry(key="Carol")], "key not a lowercase slug"),
        ([_entry(key="1carol")], "key starts with a digit"),
        ([_entry(email="")], "a user without an email"),
        ([_entry(name="")], "no display name"),
        ([_entry(type="superuser")], "unknown type"),
        (
            [
                _entry(key="s1", username="s1", type="service_account"),
                _entry(key="s2", username="s2", type="service_account"),
            ],
            "two service accounts share one token",
        ),
        ([_entry(key="andrew")], "<key>_password collides with the platform-users password field"),
        ([_entry(key="service_account", username="svc")], "no collision here: service_account_token differs"),
    ],
)
def test_a_malformed_list_is_refused_before_anything_acts(tmp_path, admins, why):
    done = _validate(tmp_path, {"platform_admins": admins})
    if why.startswith("no collision"):
        assert done.returncode == 0, why  # control: a key merely resembling a static name is fine
        return
    assert done.returncode != 0, why
    assert "platform_admins" in done.stdout + done.stderr, done.stdout


# ── the secrets step still evaluates the authentik declaration ──────────────────
def test_check_secrets_evaluates_the_computed_authentik_declaration(tmp_path):
    # A computed declaration left in the playbook's own vars makes Check Secrets fail closed
    # ("cannot be evaluated here"); it is in vars/secret-declarations/ so every reader evaluates it.
    from test_check_secrets_verdict import SECRETS_VARS, _verdict

    (tmp_path / "vars").symlink_to(PLAYBOOKS / "vars")
    verdict = _verdict(
        tmp_path,
        service="authentik",
        vars_file=SECRETS_VARS / "authentik.yml",
        deploy_raw=yaml.safe_dump([{"vars": {}}]),
        stored={"secret_key": "x"},
    )
    assert verdict["status"] == "pass", verdict
    assert not (verdict.get("error") and "cannot be evaluated" in verdict["error"]), verdict


# ── the print playbook's allow-list ─────────────────────────────────────────────
def test_the_credential_print_allow_list_is_the_stored_passwords(tmp_path):
    """Derived from the users blueprint plus every administrator that signs in, never a service account."""
    plays = yaml.safe_load((PLAYBOOKS / "print-platform-user-credentials.yml").read_text())
    play = next(p for p in plays if "vars" in p)
    (tmp_path / "vars").symlink_to(PLAYBOOKS / "vars")
    harness = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars_files": [str(PLAYBOOKS / "vars/platform-admins.yml")],
            "vars": {
                "_blueprint": str(DEPLOYMENT / "blueprints/platform-users.yaml"),
                "_allowed": play["vars"]["_allowed"],
            },
            "tasks": [
                {
                    "ansible.builtin.copy": {
                        "dest": str(tmp_path / "allowed.json"),
                        "content": "{{ _allowed | to_json }}",
                    }
                }
            ],
        }
    ]
    (tmp_path / "h.yml").write_text(yaml.safe_dump(harness))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    done = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(tmp_path / "h.yml")],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    allowed = json.loads((tmp_path / "allowed.json").read_text())
    assert sorted(allowed) == sorted(["andrew", "wisward", "jacob", "stray_admin", "agent_cloud_admin"])
    assert "svc_automation" not in allowed and "service_account_token" not in allowed


# ── the collision guard sees the declared administrators ────────────────────────
def _collision_guard(tmp: Path, inventory: dict) -> subprocess.CompletedProcess:
    """Run the deploy's retirement collision guard (lifted by name) against an inventory."""
    tasks = _deploy_tasks()
    names = [t.get("name", "") for t in tasks]
    picked = tasks[
        names.index("Read the username declarations out of env.j2") : names.index(
            "Refuse to both create and delete the same account"
        )
        + 1
    ]
    # The guard reads env.j2 at playbook_dir/../services/..., so mirror that layout.
    (tmp / "playbooks").mkdir()
    (tmp / "services").symlink_to(REPO / "platform/services")
    play = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars_files": [str(PLAYBOOKS / "vars/platform-admins.yml")],
            "tasks": picked,
        }
    ]
    (tmp / "playbooks/guard.yml").write_text(yaml.safe_dump(play))
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env["ANSIBLE_NOCOLOR"] = "1"
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(tmp / "playbooks/guard.yml"), "-e", json.dumps(inventory)],
        cwd=tmp,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_retiring_a_declared_administrators_username_is_refused(tmp_path):
    # `stray` was a fixed username in a blueprint, invisible to the guard that reads env.j2. It is in
    # the declared list now, so a retirement naming it is refused instead of deleting the operator.
    done = _collision_guard(tmp_path, {"andrew_legacy_username": "stray"})
    assert done.returncode != 0, done.stdout
    assert "A retired username is also an ACTIVE username: stray" in done.stdout


def test_a_retirement_that_names_nobody_declared_passes_the_guard(tmp_path):
    done = _collision_guard(tmp_path, {"andrew_legacy_username": "someone-long-gone"})
    assert done.returncode == 0, done.stdout + done.stderr
