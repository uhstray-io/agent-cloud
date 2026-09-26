"""Exercise the recovery playbook against a disposable Podman-shaped host."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "platform/playbooks/recover-authentik-audit-runtime.yml"
NAMES = (
    "authentik-postgresql",
    "authentik-redis",
    "authentik-server",
    "authentik-worker",
)


def test_recovery_starts_only_existing_audit_dependencies():
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        playbook = root / "platform/playbooks/recover-authentik-audit-runtime.yml"
        playbook.parent.mkdir(parents=True)
        playbook.write_bytes(PLAYBOOK.read_bytes())
        inventory = root / "inventory.yml"
        inventory.write_text(
            "all:\n  children:\n    authentik_svc:\n      hosts:\n"
            "        localhost:\n          ansible_connection: local\n"
        )
        (root / ".gitignore").write_text("inventory.yml\nbin/\nstate.json\nremote-tmp/\n")
        state_file = root / "state.json"
        state_file.write_text(json.dumps(dict.fromkeys(NAMES, "created")))
        bin_dir = root / "bin"
        bin_dir.mkdir()
        podman = bin_dir / "podman"
        podman.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "p=Path(os.environ['FAKE_PODMAN_STATE'])\n"
            "state=json.loads(p.read_text())\n"
            "name=sys.argv[-1]\n"
            "if name not in state: sys.exit(1)\n"
            "if sys.argv[1]=='start':\n"
            "    state[name]='running'; p.write_text(json.dumps(state)); print(name)\n"
            "elif sys.argv[1]=='inspect':\n"
            "    fmt=sys.argv[3]\n"
            "    if 'json .Mounts' in fmt:\n"
            "        dest='/var/lib/postgresql/data' if name.endswith('postgresql') else '/data'\n"
            "        volume=('authentik_authentik-postgres' if name.endswith('postgresql')\n"
            "                else 'authentik_authentik-redis')\n"
            "        if name.endswith('postgresql') and os.environ.get('FAKE_PODMAN_WRONG_VOLUME'):\n"
            "            volume='other-data'\n"
            "        print(json.dumps([{'Type':'volume','Name':volume,'Destination':dest}]))\n"
            "    elif 'Health.Status' in fmt: print('healthy' if state[name]=='running' else 'starting')\n"
            "    elif 'ImageName' in fmt:\n"
            "        print('other-image' if os.environ.get('FAKE_PODMAN_WRONG_IMAGE')\n"
            "              else 'ghcr.io/goauthentik/server:2024.12.3')\n"
            "    else: print('id-'+name+' image-'+name+' '+state[name]+' always')\n"
            "else: sys.exit(2)\n"
        )
        podman.chmod(0o755)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "platform", ".gitignore"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-qm", "fixture"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "update-ref", "refs/remotes/origin/dev", "HEAD"],
            check=True,
        )
        env = os.environ | {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_PODMAN_STATE": str(state_file),
            "ANSIBLE_LOCAL_TEMP": str(root / "remote-tmp"),
            "ANSIBLE_REMOTE_TEMP": str(root / "remote-tmp"),
        }

        def run(extra):
            return subprocess.run(
                ["ansible-playbook", "-i", str(inventory), str(playbook), *extra],
                cwd=root, env=env, text=True, capture_output=True, check=False,
            )

        preview = run([])
        assert preview.returncode == 0, preview.stdout + preview.stderr
        assert json.loads(state_file.read_text()) == dict.fromkeys(NAMES, "created")

        applied = run(["-e", "authentik_runtime_apply=true"])
        assert applied.returncode == 0, applied.stdout + applied.stderr
        assert json.loads(state_file.read_text()) == {
            **dict.fromkeys(NAMES[:3], "running"),
            "authentik-worker": "created",
        }

        state_file.write_text(json.dumps(dict.fromkeys(NAMES, "created")))
        env["FAKE_PODMAN_WRONG_VOLUME"] = "1"
        wrong_volume = run(["-e", "authentik_runtime_apply=true"])
        assert wrong_volume.returncode != 0
        assert "no declared named data volume" in wrong_volume.stdout
        assert json.loads(state_file.read_text()) == dict.fromkeys(NAMES, "created")
        del env["FAKE_PODMAN_WRONG_VOLUME"]

        env["FAKE_PODMAN_WRONG_IMAGE"] = "1"
        wrong_image = run(["-e", "authentik_runtime_apply=true"])
        assert wrong_image.returncode != 0
        assert "does not use the declared Authentik image" in wrong_image.stdout
        assert json.loads(state_file.read_text()) == dict.fromkeys(NAMES, "created")
        del env["FAKE_PODMAN_WRONG_IMAGE"]

        missing = dict.fromkeys(NAMES[:3], "created")
        state_file.write_text(json.dumps(missing))
        refused_missing = run(["-e", "authentik_runtime_apply=true"])
        assert refused_missing.returncode != 0
        assert "is missing, unexpected" in refused_missing.stdout
        assert json.loads(state_file.read_text()) == missing

        state_file.write_text(json.dumps(dict.fromkeys(NAMES, "running")))
        refused = run(["-e", "authentik_runtime_apply=true"])
        assert refused.returncode != 0
        assert "blueprint worker is running" in refused.stdout
