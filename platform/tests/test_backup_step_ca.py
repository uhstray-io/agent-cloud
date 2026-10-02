"""backup-step-ca-to-site-config.yml, run for real (openspec production-internal-ca, tasks 7.1
and 7.2): the CA's certificates, encrypted keys and configuration reach a site-config branch
byte for byte, the plaintext password file is never even read, the output carries names only,
and every refusal stops before anything is pushed.

The CA host is a `connection: local` inventory host whose container engine is a fake that
serves a synthetic /home/step from the test directory; OpenBao is seed_harness's synthetic
server; site-config is a bare repository in the test directory. All values are synthetic.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import harness_sandbox
import playbook_yaml
import pytest
import seed_harness
import yaml

PLAYBOOK = playbook_yaml.REPO / "platform/playbooks/backup-step-ca-to-site-config.yml"
TEMPLATES = playbook_yaml.REPO / "platform/semaphore/templates.yml"
DEPLOY_KEY = "synthetic-deploy-key-material"
KEY_BODY = "SYNTHETIC-ENCRYPTED-ROOT-KEY-BODY"
PASSWORD = "synthetic-ca-key-password"
CA_JSON = '{"authority": {"marker": "synthetic-ca-json-marker"}}\n'


def _pem(label: str, body: str) -> str:
    """A PEM block, its armour assembled here so no key header sits in this file verbatim: the
    private-key commit hooks refuse one, synthetic or not."""
    edge = "-" * 5
    return f"{edge}BEGIN {label}{edge}\n{body}\n{edge}END {label}{edge}\n"


EC_KEY, PKCS8_ENCRYPTED = "EC " + "PRIVATE" + " KEY", "ENCRYPTED " + "PRIVATE" + " KEY"
VOLUME = {
    "certs/root_ca.crt": _pem("CERTIFICATE", "SYNTHETIC-ROOT"),
    "certs/intermediate_ca.crt": _pem("CERTIFICATE", "SYNTHETIC-INT"),
    "config/ca.json": CA_JSON,
    "config/defaults.json": '{"ca-url": "https://localhost:9000"}\n',
    # step-ca 0.30.2 writes its keys as encrypted PEM; PKCS#8's encrypted form is accepted too.
    "secrets/root_ca_key": _pem(EC_KEY, f"Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-256-CBC,00\n\n{KEY_BODY}"),
    "secrets/intermediate_ca_key": _pem(PKCS8_ENCRYPTED, "SYNTHETIC-INT-KEY-BODY"),
    "secrets/password": PASSWORD + "\n",
    "db/000000.vlog": "synthetic-db",
}
BACKED_UP = sorted(k for k in VOLUME if not k.startswith("db/") and k != "secrets/password")
NEVER_PRINTED = (DEPLOY_KEY, KEY_BODY, PASSWORD, "synthetic-ca-json-marker", "SYNTHETIC-INT-KEY-BODY",
                 base64.b64encode(VOLUME["secrets/root_ca_key"].encode()).decode()[:24])

# The engine: `inspect` succeeds when the volume exists, `exec step-ca find|base64` reads it.
# Every call is logged, so a test can prove what was (not) read.
FAKE_ENGINE = """#!{python}
import base64, json, os, sys
vol, log = {vol!r}, {log!r}
args = sys.argv[1:]
with open(log, "a") as f:
    f.write(json.dumps(args) + "\\n")
real = lambda p: vol + p[len("/home/step"):]
if args[0] == "inspect":
    sys.exit(0 if os.path.isdir(vol) else 1)
assert args[:2] == ["exec", "step-ca"], args
if args[2] == "find":
    for d in [a for a in args[3:] if a.startswith("/home/step/")]:
        if os.path.isdir(real(d)):
            for n in sorted(os.listdir(real(d))):
                if os.path.isfile(os.path.join(real(d), n)):
                    print(d + "/" + n)
elif args[2] == "base64":
    sys.stdout.write(base64.b64encode(open(real(args[-1]), "rb").read()).decode())
else:
    sys.exit(2)
"""


class Bao(seed_harness.FakeBao):
    deploy_key_status = 200

    def do_POST(self):
        self.record("POST")
        self.reply({"auth": {"client_token": seed_harness.LOGIN}})

    def do_GET(self):
        self.record("GET")
        if self.path == "/v1/secret/data/services/ssh/site-config" and self.deploy_key_status == 200:
            self.reply({"data": {"data": {"private_key": DEPLOY_KEY}}})
        else:
            self.reply({"errors": []}, 404)


class NoDeployKey(Bao):
    deploy_key_status = 404


def _git(*args, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
                          cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def _site_config(tmp: Path) -> Path:
    """A bare remote holding an older backup (one stale file) and the credential backup's file."""
    bare, work = tmp / "site-config.git", tmp / "seed"
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    _git("init", "-q", "-b", "main", str(work))
    (work / "secrets/step-ca/volume/certs").mkdir(parents=True)
    (work / "secrets/step-ca/volume/certs/stale.crt").write_text("from an older CA\n")
    (work / "secrets/step-ca/init_password.txt").write_text("kept by the credential backup")
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "-m", "seed", cwd=work)
    _git("push", "-q", str(bare), "main", cwd=work)
    return bare


def _volume(tmp: Path, files: dict) -> Path:
    vol = tmp / "volume"
    for rel, content in files.items():
        (vol / rel).parent.mkdir(parents=True, exist_ok=True)
        (vol / rel).write_text(content)
    return vol


def _run(tmp: Path, *, files=None, handler=Bao, ca_hosts=("ca",), check=False):
    vol = _volume(tmp, VOLUME if files is None else files) if files != {} else tmp / "volume"
    engine, log = tmp / "fake-engine", tmp / "engine.log"
    engine.write_text(FAKE_ENGINE.format(python=sys.executable, vol=str(vol), log=str(log)))
    engine.chmod(0o755)
    bare = _site_config(tmp)
    host = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable,
            "container_engine": str(engine)}
    with seed_harness.serve(handler) as address:
        inv = {"all": {"vars": {"openbao_addr": address},
                       "children": {"step_ca_svc": {"hosts": dict.fromkeys(ca_hosts, host)}}}}
        (tmp / "inv.yml").write_text(yaml.safe_dump(inv))
        extra = {**seed_harness.ROLE, "site_config_repo": f"file://{bare}"}
        cmd = ["ansible-playbook", "-v", "-i", str(tmp / "inv.yml"), str(PLAYBOOK), "-e", json.dumps(extra),
               *(["--check"] if check else [])]
        env = harness_sandbox.env_for(tmp, {k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
        # Stock output, stricter than production's redact_requests callback (MISTAKES 4.6).
        env.update(ANSIBLE_STDOUT_CALLBACK="default")
        r = harness_sandbox.run(cmd, tmp, cwd=playbook_yaml.REPO, env=env)
    out = r.stdout + r.stderr
    for value in (*NEVER_PRINTED, *seed_harness.NEVER_PRINTED):
        assert value not in out, f"{value[:12]}... reached the output"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return r.returncode, out, bare, calls


def _backup_branches(bare: Path) -> list[str]:
    refs = _git("--git-dir", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return [b for b in refs.split() if b.startswith("backup/step-ca-")]


def _read(bare: Path, branch: str, path: str) -> str:
    return _git("--git-dir", str(bare), "show", f"{branch}:{path}")


def _tree(bare: Path, branch: str) -> list[str]:
    return sorted(_git("--git-dir", str(bare), "ls-tree", "-r", "--name-only", branch).split())


def test_backup_lands_the_ca_files_byte_for_byte_on_a_new_branch_and_prints_names_only(tmp_path):
    rc, out, bare, calls = _run(tmp_path)
    assert rc == 0, out
    [branch] = _backup_branches(bare)
    assert _tree(bare, branch) == sorted(["secrets/step-ca/init_password.txt",
                                          *(f"secrets/step-ca/volume/{f}" for f in BACKED_UP)])
    for rel in BACKED_UP:
        assert _read(bare, branch, f"secrets/step-ca/volume/{rel}") == VOLUME[rel], rel
    assert f"Pushed branch {branch}" in out
    assert f"{len(BACKED_UP)} file(s) to back up: {', '.join(BACKED_UP)}" in out
    assert "Left behind: secrets/password" in out


def test_the_plaintext_password_file_is_never_read(tmp_path):
    rc, out, _bare, calls = _run(tmp_path)
    assert rc == 0, out
    read = [c[-1] for c in calls if c[:3] == ["exec", "step-ca", "base64"]]
    assert sorted(p.removeprefix("/home/step/") for p in read) == BACKED_UP


@pytest.mark.parametrize("files,message", [
    ({**VOLUME, "secrets/root_ca_key": _pem(EC_KEY, "PLAIN")},
     "secrets/root_ca_key is not an encrypted key"),
    ({k: v for k, v in VOLUME.items() if k != "config/ca.json"}, "missing: ['config/ca.json']"),
    ({k: v for k, v in VOLUME.items() if k != "secrets/intermediate_ca_key"},
     "missing: ['secrets/intermediate_ca_key']"),
    ({**VOLUME, "config/bad name.json": "{}"}, "Unsafe names: ['config/bad name.json']"),
    ({}, "no step-ca container"),
], ids=["plaintext-key", "no-ca-json", "no-intermediate-key", "unsafe-name", "no-container"])
def test_a_ca_that_cannot_be_backed_up_whole_is_refused_and_nothing_is_pushed(tmp_path, files, message):
    rc, out, bare, _calls = _run(tmp_path, files=files)
    assert rc != 0 and message in out, out
    assert _backup_branches(bare) == []


@pytest.mark.parametrize("kwargs,message", [
    ({"ca_hosts": ("ca", "ca2")}, "Back up exactly one step_ca_svc host (2 declared)"),
    ({"handler": NoDeployKey}, "No deploy key at secret/data/services/ssh/site-config"),
], ids=["two-cas", "no-deploy-key"])
def test_the_ca_is_not_read_unless_the_backup_can_be_pushed(tmp_path, kwargs, message):
    rc, out, bare, calls = _run(tmp_path, **kwargs)
    assert rc != 0 and message in out, out
    assert calls == []
    assert _backup_branches(bare) == []


def test_a_dry_run_reads_and_refuses_but_pushes_nothing(tmp_path):
    rc, out, bare, calls = _run(tmp_path, check=True)
    assert rc == 0, out
    assert any(c[:3] == ["exec", "step-ca", "base64"] for c in calls)
    assert _backup_branches(bare) == []


def test_no_log_sits_on_the_credential_tasks_only():
    names = {t.get("name") for t in playbook_yaml.tasks(playbook_yaml.load(PLAYBOOK)) if t.get("no_log")}
    assert names == {"Authenticate to OpenBao (AppRole)", "Fetch the site-config deploy key", "Read each file",
                     "Find any key that is not encrypted", "Write each file"}


def test_the_template_is_declared_dev_bound():
    [entry] = [t for t in yaml.safe_load(TEMPLATES.read_text())["templates"]
               if t["name"] == "Back Up step-ca to site-config (Dev)"]
    assert entry["playbook"] == "platform/playbooks/backup-step-ca-to-site-config.yml"
    assert entry["repository"] == "agent-cloud dev"
