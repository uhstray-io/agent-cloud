"""Connection identity cannot be set from outside the run (service-deployment-workflow 7.10).

The inventory defines where a run connects and as whom, and an extra var outranks it. With
host-key checking off under Semaphore, a forged `-e ansible_host=...` would send the run, and
the become password it hands to sudo, to a host the launcher chose. refuse-internal-extra-vars.yml
refuses each name below when it is a command-line extra var, asking the extra_var_names filter
where the value came from (hostvars cannot tell: the inventory defines the same names). These
tests run a real ansible-playbook, so they hold on whatever ansible-core is installed instead
of pinning its version, and unit-test the filter's two rules: names only, fail closed.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import forgeries
import playbook_yaml
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "platform/playbooks"
GUARD = PLAYBOOKS / "refuse-internal-extra-vars.yml"
PLUGIN = PLAYBOOKS / "filter_plugins/extra_var_names.py"

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None,
                                   reason="ansible-playbook not installed")

# Every public connection and become option except the passwords (those are refused by the
# guard's pattern) and the underscore names. Task 7.10 lists them.
CONNECTION_NAMES = [
    "ansible_host", "ansible_ssh_host", "ansible_user", "ansible_ssh_user", "ansible_port", "ansible_ssh_port",
    "ansible_connection", "ansible_ssh_common_args", "ansible_ssh_extra_args", "ansible_ssh_args",
    "ansible_ssh_executable", "ansible_scp_extra_args", "ansible_sftp_extra_args", "ansible_become_method",
    "ansible_become_user", "ansible_become_exe", "ansible_become_flags", "ansible_private_key_file",
    "ansible_ssh_private_key_file",
    # Beyond the 19 named in task 7.10, read from ansible-core's ssh and sudo options: inline key
    # material, key passphrases (the password pattern does not match them), other binaries to
    # run, a PKCS11 library to load, sudo's own spelling of become, host-key checking.
    "ansible_private_key", "ansible_ssh_private_key", "ansible_private_key_passphrase",
    "ansible_ssh_private_key_passphrase", "ansible_scp_executable", "ansible_sftp_executable",
    "ansible_ssh_pkcs11_provider", "ansible_sudo_user", "ansible_sudo_exe", "ansible_sudo_flags",
    "ansible_sudo_chdir", "ansible_host_key_checking", "ansible_ssh_host_key_checking",
]
SECRET = "s3cr3t-forged-value"
# Names whose value Ansible parses to set up the guard's own connection before the guard runs:
# a forgery must be valid there to reach the guard (an invalid one fails the run earlier).
VALID = {"ansible_port": (22, 2222), "ansible_ssh_port": (22, 2222), "ansible_connection": ("local", "local")}


def _run(playbook: Path, tmp_path: Path, *args: str, inventory: str = "localhost,"):
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_CONFIG"}
    env.update(ANSIBLE_NOCOLOR="1", ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_REMOTE_TEMP=str(tmp_path))
    return subprocess.run(["ansible-playbook", "-i", inventory, str(playbook), *args], cwd=REPO, env=env, text=True,
                          capture_output=True, check=False, timeout=120, stdin=subprocess.DEVNULL)


def _guarded(tmp_path: Path) -> Path:
    play = tmp_path / "play.yml"
    play.write_text(json.dumps([
        {"ansible.builtin.import_playbook": str(GUARD)},
        {"hosts": "all", "gather_facts": False, "tasks": [{"ansible.builtin.debug": {"msg": "WORK RAN"}}]}]))
    return play


def test_the_guard_refuses_exactly_the_listed_names():
    assert playbook_yaml.load(GUARD)[0]["tasks"][1]["vars"]["_conn_names"] == CONNECTION_NAMES
    # The passwords stay with the guard's pattern, not this list.
    assert not any(re.search(r"pass(word)?$", n) for n in CONNECTION_NAMES)


@needs_ansible
def test_the_guard_covers_what_ansible_reads_a_connection_from():
    # Every name in the ssh connection and sudo become options' `vars`, read from the installed
    # ansible-core, is either listed or a password (the guard's pattern) or a setting that
    # cannot redirect a connection. A new alias in an upgrade fails here instead of becoming a
    # forgeable name.
    script = (
        "import yaml, ansible.plugins.connection.ssh as ssh, ansible.plugins.become.sudo as sudo\n"
        "names = set()\n"
        "for mod in (ssh, sudo):\n"
        "    for opt in yaml.safe_load(mod.DOCUMENTATION)['options'].values():\n"
        "        names |= {v['name'] for v in opt.get('vars', [])}\n"
        "print(' '.join(sorted(names)))")
    python = Path(shutil.which("ansible-playbook")).read_text().splitlines()[0].removeprefix("#!").strip()
    read = subprocess.run([python, "-c", script], text=True, capture_output=True, check=True).stdout.split()
    # ansible-core 2.19 also lists expressions such as "delegated_vars['ansible_host']" as host
    # sources. They read a name already listed here, so only plain variable names are judged.
    read = [n for n in read if re.fullmatch(r"[A-Za-z_]\w*", n)]
    assert set(CONNECTION_NAMES) - set(read) <= PLAY_CONTEXT, sorted(set(CONNECTION_NAMES) - set(read))
    password = re.compile(r"ansible_(password|\w+_pass|\w+_password)$")  # the guard's own pattern
    unlisted = sorted(n for n in read if n not in CONNECTION_NAMES and not password.match(n))
    assert unlisted == HARMLESS, f"decide for each: list it in the guard, or in HARMLESS: {unlisted}"


# Listed names that pick the plugin rather than being one of its options (constants.py
# MAGIC_VARIABLE_MAPPING), so neither plugin's DOCUMENTATION lists them.
PLAY_CONTEXT = {"ansible_connection", "ansible_become_method"}


# Options the guard leaves alone: none of them can move a connection to another host or
# account, or change what runs there. Exact, so a new option is a decision.
HARMLESS = ["ansible_control_path", "ansible_control_path_dir", "ansible_pipelining", "ansible_sftp_batch_mode",
            "ansible_ssh_password_mechanism", "ansible_ssh_pipelining", "ansible_ssh_retries",
            "ansible_ssh_timeout", "ansible_ssh_transfer_method", "ansible_ssh_use_tty", "ansible_ssh_verbosity",
            "ansible_sshpass_prompt", "inventory_hostname"]


@needs_ansible
def test_an_inventory_that_defines_the_connection_identity_is_not_refused(tmp_path):
    inv = tmp_path / "inv.yml"
    inv.write_text(yaml.safe_dump({"all": {
        "vars": {"ansible_user": "deploy", "ansible_port": 22, "ansible_become_method": "sudo"},
        "hosts": {"localhost": {"ansible_connection": "local", "ansible_host": "127.0.0.1"}}}}))
    proc = _run(_guarded(tmp_path), tmp_path, inventory=str(inv))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WORK RAN" in proc.stdout


@needs_ansible
@pytest.mark.parametrize("name, forge", [
    pytest.param(n, f.values[0], id=f"{n}-{f.id}")
    for n in CONNECTION_NAMES
    for f in forgeries.templated_forgeries(n, *VALID.get(n, ("honest", SECRET)))
])
def test_a_forged_connection_identity_is_refused(name, forge, tmp_path):
    # The inventory defines the name too, so only where the value came from tells them apart.
    inv = tmp_path / "inv.yml"
    inv.write_text(yaml.safe_dump({"all": {"hosts": {"localhost": {"ansible_connection": "local"}},
                                           "vars": {} if name == "ansible_connection" else {name: "from-inventory"}}}))
    proc = _run(_guarded(tmp_path), tmp_path, "-e", forge(tmp_path), inventory=str(inv))
    assert proc.returncode != 0, proc.stdout
    assert f"Refusing to run: {name} set as an extra var" in proc.stdout, proc.stdout + proc.stderr
    assert "WORK RAN" not in proc.stdout
    # Names only: neither the forged value nor the inventory's appears. (A templated forgery IS
    # rendered by Ansible itself to set up the guard task's own connection; the refusal reads
    # the name, so what it renders to cannot matter.)
    assert SECRET not in proc.stdout + proc.stderr and "from-inventory" not in proc.stdout + proc.stderr


@needs_ansible
def test_every_extra_var_form_is_read(tmp_path):
    # key=value and @file (plain and templated JSON are in the forgery test above), all names
    # at once: the message names every one and none of the values.
    file = tmp_path / "vars.yml"
    pairs = {n: VALID.get(n, (None, SECRET))[1] for n in CONNECTION_NAMES}
    file.write_text(yaml.safe_dump(pairs))
    for args in (["-e", " ".join(f"{n}={v}" for n, v in pairs.items())], ["-e", f"@{file}"]):
        proc = _run(_guarded(tmp_path), tmp_path, *args)
        assert proc.returncode != 0, proc.stdout
        assert all(n in proc.stdout for n in CONNECTION_NAMES), proc.stdout
        assert SECRET not in proc.stdout + proc.stderr and "WORK RAN" not in proc.stdout


@needs_ansible
def test_other_extra_vars_are_still_accepted(tmp_path):
    proc = _run(_guarded(tmp_path), tmp_path, "-e", "target_service=demo_svc", "-e",
                '{"ansible_become_password_file": 1}')
    assert proc.returncode == 0 and "WORK RAN" in proc.stdout, proc.stdout + proc.stderr


@needs_ansible
def test_a_nested_guard_run_still_skips_and_the_importer_still_refuses(tmp_path):
    inner = tmp_path / "inner.yml"
    inner.write_text(json.dumps([{"ansible.builtin.import_playbook": str(GUARD)},
                                 {"hosts": "localhost", "gather_facts": False,
                                  "tasks": [{"ansible.builtin.debug": {"msg": "INNER RAN"}}]}]))
    outer = tmp_path / "outer.yml"
    outer.write_text(json.dumps([{"ansible.builtin.import_playbook": str(GUARD)},
                                 {"ansible.builtin.import_playbook": str(inner),
                                  "vars": {"_extra_var_guard_nested": True}}]))
    assert "INNER RAN" in _run(outer, tmp_path).stdout
    proc = _run(outer, tmp_path, "-e", "ansible_host=1.2.3.4")
    assert proc.returncode != 0 and "INNER RAN" not in proc.stdout
    assert "Refusing to run: ansible_host set as an extra var" in proc.stdout


# The filter in isolation: names only, and fail closed. Run in the interpreter ansible-playbook
# uses (the test interpreter need not have ansible-core installed).
def _ansible_python() -> str:
    return Path(shutil.which("ansible-playbook")).read_text().splitlines()[0].removeprefix("#!").strip()


def _call_filter(fault: str) -> subprocess.CompletedProcess:
    script = f"""
import importlib.util, sys
from ansible.errors import AnsibleFilterError
from ansible.utils import vars as ansible_vars
fault, secret = {fault!r}, {SECRET!r}
if fault == "raises":
    def boom(loader):
        raise ValueError("could not parse " + secret)
    ansible_vars.load_extra_vars = boom
elif fault == "missing":
    del ansible_vars.load_extra_vars
else:
    ansible_vars.load_extra_vars = lambda loader: {{"b": secret, "a": {{"k": secret}}}}
spec = importlib.util.spec_from_file_location("extra_var_names", {str(PLUGIN)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    print("RESULT", module.FilterModule().filters()["extra_var_names"](""))
except AnsibleFilterError as exc:
    print("ERROR", exc, "| cause:", exc.__cause__, "| suppressed:", exc.__suppress_context__)
"""
    return subprocess.run([_ansible_python(), "-c", script], text=True, capture_output=True, check=True,
                          stdin=subprocess.DEVNULL)


@needs_ansible
def test_the_filter_returns_names_never_values():
    out = _call_filter("names").stdout
    assert "RESULT ['a', 'b']" in out and SECRET not in out, out


@needs_ansible
@pytest.mark.parametrize("fault", ["raises", "missing"])
def test_the_filter_fails_closed_without_quoting_the_cause(fault):
    out = _call_filter(fault).stdout
    assert out.startswith("ERROR cannot read the extra vars"), out
    assert SECRET not in out and "cause: None" in out and "suppressed: True" in out, out
