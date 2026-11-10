"""Plane's .env GitHub block (templates/env.j2) and the App webhook step (github_app_token.py set-hook-secret).

The edge skips the Cloudflare challenge on Plane's GitHub webhook path because silo verifies
X-Hub-Signature-256 once GITHUB_WEBHOOK_SECRET is set (spike 0.16). These hold the two halves of
that: env.j2 renders the GitHub settings all together or not at all (a declared App with any value
empty fails the render), and set-hook-secret sends the secret to GitHub without ever printing it.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from ansible_python import needs_ansible

REPO = Path(__file__).resolve().parents[2]
ENV_TPL = REPO / "platform/services/plane/deployment/templates/env.j2"
sys.path.insert(0, str(REPO / "platform/lib"))
import github_app_token as gat  # noqa: E402

OVERRIDES = [
    "MACHINE_SIGNATURE",
    "SECRET_KEY",
    "LIVE_SERVER_SECRET_KEY",
    "SILO_HMAC_SECRET_KEY",
    "AES_SECRET_KEY",
    "PI_INTERNAL_SECRET",
    "CURSOR_WEBHOOK_SECRET",
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
    "FOLLOWER_POSTGRES_URI",
    "PLANE_PI_DATABASE_URL",
    "RABBITMQ_PASSWORD",
    "RABBITMQ_DEFAULT_PASS",
    "AMQP_URL",
    "MINIO_ROOT_PASSWORD",
    "AWS_SECRET_ACCESS_KEY",
]
GITHUB = [
    "GITHUB_CLIENT_ID",
    "GITHUB_CLIENT_SECRET",
    "GITHUB_APP_NAME",
    "GITHUB_APP_ID",
    "GITHUB_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
]
SECRETS = {
    n: "fake-" + n
    for n in [
        "plane_db_password",
        "plane_rabbitmq_password",
        "plane_minio_password",
        "plane_machine_signature",
        "plane_secret_key",
        "plane_live_server_secret_key",
        "plane_silo_hmac_secret_key",
        "plane_aes_secret_key",
        "plane_pi_internal_secret",
        "plane_cursor_webhook_secret",
        "plane_github_client_secret",
        "plane_github_private_key",
        "plane_github_webhook_secret",
    ]
}
APP = {"plane_github_app_id": 123, "plane_github_app_name": "fake-app", "plane_github_client_id": "Iv-fake"}


def render(tmp_path: Path, extra: dict, secrets: dict = SECRETS) -> subprocess.CompletedProcess:
    vendor = "\n".join([f"{k}=shipped" for k in OVERRIDES] + [f"{k}=" for k in GITHUB])
    variables = {
        "_plane_setup": {"variables.env": vendor},
        "_plane_public_url": "https://plane.example.test",
        "_plane_version": "v0",
        "plane_trusted_proxies": "10.0.0.0/8",
        "secrets": secrets,
        **extra,
    }
    (tmp_path / "vars.json").write_text(json.dumps(variables))
    (tmp_path / "play.yml").write_text(
        "- hosts: localhost\n  gather_facts: false\n  tasks:\n"
        f"    - ansible.builtin.template: {{src: '{ENV_TPL}', dest: '{tmp_path}/out.env'}}\n"
    )
    return subprocess.run(
        [
            "ansible-playbook",
            "-i",
            "localhost,",
            "-c",
            "local",
            "-e",
            f"@{tmp_path}/vars.json",
            str(tmp_path / "play.yml"),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def env_lines(tmp_path: Path) -> dict:
    return dict(
        line.split("=", 1)
        for line in (tmp_path / "out.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )


@needs_ansible
def test_no_app_declared_leaves_vendor_blanks(tmp_path):
    proc = render(tmp_path, {})
    assert proc.returncode == 0, proc.stdout[-2000:]
    env = env_lines(tmp_path)
    assert all(env[k] == "" for k in GITHUB)


@needs_ansible
def test_declared_app_renders_all_six(tmp_path):
    proc = render(tmp_path, APP)
    assert proc.returncode == 0, proc.stdout[-2000:]
    env = env_lines(tmp_path)
    assert env["GITHUB_APP_ID"] == "123"
    assert env["GITHUB_APP_NAME"] == "fake-app"
    assert env["GITHUB_CLIENT_ID"] == "Iv-fake"
    assert env["GITHUB_CLIENT_SECRET"] == "fake-plane_github_client_secret"
    assert env["GITHUB_PRIVATE_KEY"] == "fake-plane_github_private_key"
    assert env["GITHUB_WEBHOOK_SECRET"] == "fake-plane_github_webhook_secret"


@needs_ansible
@pytest.mark.parametrize("missing", ["plane_github_webhook_secret", "plane_github_client_secret"])
def test_declared_app_with_an_empty_value_fails_the_render(tmp_path, missing):
    proc = render(tmp_path, APP, {**SECRETS, missing: ""})
    assert proc.returncode != 0
    assert "the GitHub integration needs all six values" in proc.stdout
    assert not (tmp_path / "out.env").exists()


def run_set_hook_secret(stdin: dict, hook_url: str = "https://plane.example.test/silo/api/github/github-webhook"):
    calls = []

    def fake_api(path, bearer, method="GET", body=None):
        calls.append({"path": path, "method": method, "body": body})
        return {"url": body["url"], "content_type": body["content_type"], "secret": "********"}

    out, err = io.StringIO(), io.StringIO()
    raw = io.TextIOWrapper(io.BytesIO(json.dumps(stdin).encode()))
    argv = ["github_app_token.py", "set-hook-secret", "--issuer", "Iv-fake", "--org", "o", "--hook-url", hook_url]
    with (
        patch.object(gat, "api", fake_api),
        patch.object(gat, "sign_assertion", lambda i, k: "jwt"),
        patch.object(sys, "argv", argv),
        patch.object(sys, "stdin", raw),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        try:
            gat.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue(), calls


def test_set_hook_secret_patches_the_app_hook_and_never_prints_the_secret():
    key = base64.b64encode(b"fake key bytes; signing is stubbed").decode()
    code, out, err, calls = run_set_hook_secret({"key_b64": key, "secret": "s3cr3t-value"})
    assert code == 0, err
    assert calls == [
        {
            "path": "/app/hook/config",
            "method": "PATCH",
            "body": {
                "url": "https://plane.example.test/silo/api/github/github-webhook",
                "content_type": "json",
                "insecure_ssl": "0",
                "secret": "s3cr3t-value",
            },
        }
    ]
    assert json.loads(out) == {
        "url": "https://plane.example.test/silo/api/github/github-webhook",
        "content_type": "json",
        "secret_set": True,
    }
    assert "s3cr3t-value" not in out + err


@pytest.mark.parametrize(
    "stdin,url",
    [
        ({"key_b64": "not base64!", "secret": "s"}, "https://x.test/h"),
        ({"key_b64": "YQ==", "secret": ""}, "https://x.test/h"),
        ({"key_b64": "YQ==", "secret": "s"}, "http://x.test/h"),
    ],
)
def test_set_hook_secret_refuses_bad_input_before_any_call(stdin, url):
    code, out, _err, calls = run_set_hook_secret(stdin, url)
    assert code == 2 and calls == [] and out == ""


def test_set_hook_secret_refuses_a_non_object_response():
    key = base64.b64encode(b"fake key bytes; signing is stubbed").decode()
    out, err = io.StringIO(), io.StringIO()
    raw = io.TextIOWrapper(io.BytesIO(json.dumps({"key_b64": key, "secret": "s"}).encode()))
    argv = [
        "github_app_token.py",
        "set-hook-secret",
        "--issuer",
        "Iv-fake",
        "--org",
        "o",
        "--hook-url",
        "https://x.test/h",
    ]
    with (
        patch.object(gat, "api", lambda *a, **k: None),
        patch.object(gat, "sign_assertion", lambda i, k: "jwt"),
        patch.object(sys, "argv", argv),
        patch.object(sys, "stdin", raw),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
        pytest.raises(SystemExit) as exc,
    ):
        gat.main()
    assert exc.value.code == 1 and out.getvalue() == ""
    assert "unexpected response" in err.getvalue()


@needs_ansible
def test_a_null_app_id_fails_the_render_instead_of_rendering_none(tmp_path):
    proc = render(tmp_path, {**APP, "plane_github_app_id": None})
    assert proc.returncode != 0
    assert "GITHUB_APP_ID is empty" in proc.stdout


def _plays():
    import yaml

    return yaml.safe_load((REPO / "platform/playbooks/deploy-plane.yml").read_text())


def test_the_webhook_secret_reaches_github_on_stdin_only_and_never_in_check_mode():
    play = next(p for p in _plays() if p.get("name") == "Phase 3d: GitHub App webhook")
    block = next(t for t in play["tasks"] if "block" in t)
    assert block["when"] == ["plane_github_app_id is defined", "not ansible_check_mode"]
    patch_task = block["block"][0]
    assert patch_task["no_log"] is True and patch_task["delegate_to"] == "localhost"
    argv = patch_task["ansible.builtin.command"]["argv"]
    assert "set-hook-secret" in argv
    assert not any("_resolved" in str(a) for a in argv)
    stdin = patch_task["ansible.builtin.command"]["stdin"]
    assert "_resolved.plane_github_private_key" in stdin and "_resolved.plane_github_webhook_secret" in stdin


def test_the_unsigned_delivery_probe_is_gated_on_the_app_and_skipped_in_check_mode():
    verify = next(p for p in _plays() if p.get("name") == "Phase 4: Verify plane")
    probe = next(t for t in verify["tasks"] if t.get("name") == "GitHub webhook refuses an unsigned delivery (401)")
    assert probe["when"] == ["plane_github_app_id is defined", "not ansible_check_mode"]
    assert "assert c == 401" in probe["ansible.builtin.command"]["argv"][-1]


@pytest.mark.parametrize("action", ["jit-config", "list-runners", "prune-runners", "installation-token"])
def test_every_org_action_refuses_a_missing_org_before_any_call(action):
    calls = []
    argv = ["github_app_token.py", action, "--issuer", "Iv-fake", "--prefix", "r-jit-", "--group", "g"]
    with (
        patch.object(gat, "api", lambda *a, **k: calls.append(a)),
        patch.object(sys, "argv", argv),
        contextlib.redirect_stderr(io.StringIO()),
        pytest.raises(SystemExit) as exc,
    ):
        gat.main()
    assert exc.value.code == 2 and calls == []


@needs_ansible
@pytest.mark.parametrize(
    "summary,ok",
    [
        ({"url": "https://x.test/h", "content_type": "json", "secret_set": False}, True),
        ({"error": "ERROR: set-hook-secret: boom"}, False),
    ],
)
def test_the_webhook_assert_renders_with_and_without_an_error(tmp_path, summary, ok):
    # ansible-core 2.19+ renders fail_msg even when the assertion holds (Deploy Plane task 14711).
    import yaml

    play = next(p for p in _plays() if p.get("name") == "Phase 3d: GitHub App webhook")
    block = next(t for t in play["tasks"] if "block" in t)
    task = next(t for t in block["block"] if t.get("name") == "App webhook converged")
    (tmp_path / "play.yml").write_text(
        yaml.safe_dump(
            [{"hosts": "localhost", "gather_facts": False, "vars": {"_plane_hook_summary": summary}, "tasks": [task]}]
        )
    )
    proc = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(tmp_path / "play.yml")],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert (proc.returncode == 0) is ok, proc.stdout[-1500:]
    if not ok:
        assert "boom" in proc.stdout


APPS_SCRIPT = REPO / "platform/services/plane/deployment/ensure_marketplace_apps.py"


def test_the_marketplace_script_creates_without_resetting_secrets():
    src = APPS_SCRIPT.read_text()
    compile(src, str(APPS_SCRIPT), "exec")
    assert "create_applications(admin.user.id)" in src
    # reset deletes the secrets connected integrations depend on; it must never run on a deploy.
    assert "reset_marketplace" not in src.split('"""', 2)[2]
    # Secrets are generated inside the call: its output is discarded, an error reported by type only.
    assert "redirect_stdout" in src and "redirect_stderr" in src and "type(exc).__name__" in src


@needs_ansible
@pytest.mark.parametrize(
    "stdout,rc,ok",
    [
        ('Creating application for github\n{"created": ["github"], "present": ["slack"], "missing": []}', 0, True),
        ('{"created": [], "present": ["github", "slack"], "missing": []}', 0, True),
        ('{"created": [], "present": ["slack"], "missing": ["github"]}', 0, False),
        ('{"error": "no instance admin yet; first-run setup has not completed"}', 0, False),
        ("Traceback (most recent call last):", 1, False),
    ],
)
def test_the_marketplace_phase_judges_the_script_summary(tmp_path, stdout, rc, ok):
    import yaml

    play = next(p for p in _plays() if p.get("name") == "Phase 3e: Marketplace applications")
    keep = [
        t
        for t in play["tasks"]
        if t.get("name") in ("Keep the marketplace summary", "Every marketplace application exists")
    ]
    assert len(keep) == 2
    fake = {"rc": rc, "stdout_lines": stdout.splitlines()}
    (tmp_path / "play.yml").write_text(
        yaml.safe_dump(
            [{"hosts": "localhost", "gather_facts": False, "vars": {"_plane_apps_run": fake}, "tasks": keep}]
        )
    )
    proc = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(tmp_path / "play.yml")],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert (proc.returncode == 0) is ok, proc.stdout[-1500:]


def test_the_marketplace_phase_runs_before_verify_and_never_in_check_mode():
    names = [p.get("name", "") for p in _plays()]
    assert names.index("Phase 3e: Marketplace applications") < names.index("Phase 4: Verify plane")
    play = next(p for p in _plays() if p.get("name") == "Phase 3e: Marketplace applications")
    run = next(t for t in play["tasks"] if t.get("name", "").startswith("Create missing"))
    assert run["when"] == "not ansible_check_mode"
    assert run["ansible.builtin.command"]["argv"][3:7] == ["python", "manage.py", "shell", "-c"]
