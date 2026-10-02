"""The o11y liveness watcher, run for real against a synthetic OpenBao, Grafana and Discord.

One loopback server plays all three. A healthy receiver posts nothing; each failure mode posts
exactly one Discord message naming the failure without any secret; check mode posts nothing.
All values are synthetic.
"""

import json
import os
import subprocess

import pytest
import yaml
from seed_harness import ROLE, ROOT, FakeBao, run_seed, serve

PLAYBOOK = "platform/playbooks/check-o11y-liveness.yml"
TOKEN = "synthetic-watcher-token-value"
HOOK_SECRET = "synthetic-webhook-secret-value"


def make_handler(grafana="up", datasource="ok", token=TOKEN):
    class Handler(FakeBao):
        requests = []
        posts = []

        def do_POST(self):
            self.record("POST")
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            if self.path.startswith("/api/webhooks/"):
                type(self).posts.append(self.body())
                self.send_response(204)
                self.end_headers()
                return None
            return self.reply({}, 404)

        def do_GET(self):
            self.record("GET")
            if self.path == "/v1/secret/data/services/o11y":
                data = {"alert_discord_webhook_url": f"{self.addr}/api/webhooks/123/{HOOK_SECRET}"}
                if token:
                    data["watcher_token"] = token
                return self.reply({"data": {"data": data}})
            if self.path == "/api/health":
                if grafana == "down":
                    return self.reply({"message": "bad gateway"}, 502)
                return self.reply({"database": "ok"})
            if self.path == "/api/datasources/uid/prometheus/health":
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self.reply({"message": "invalid API key"}, 401)
                if datasource == "down":
                    return self.reply({"status": "ERROR", "message": "connection refused"}, 400)
                return self.reply({"status": "OK"})
            return self.reply({}, 404)

    return Handler


def run(tmp_path, handler, verbose=True):
    with serve(handler) as addr:
        handler.addr = addr
        extra = {"o11y_watch_url": addr, "o11y_watch_webhook_hosts": ["127.0.0.1"]}
        rc, out = run_seed(tmp_path, PLAYBOOK, addr, extra=extra, verbose=verbose)
    for secret in (TOKEN, HOOK_SECRET):
        assert secret not in out
    return rc, out


def test_healthy_receiver_posts_nothing(tmp_path):
    handler = make_handler()
    rc, out = run(tmp_path, handler)
    assert rc == 0, out
    assert "o11y liveness OK" in out
    assert handler.posts == []


@pytest.mark.parametrize(
    ("kwargs", "named"),
    [
        ({"grafana": "down"}, "grafana /api/health failed (status 502)"),
        ({"datasource": "down"}, "prometheus datasource unhealthy (status 400)"),
        ({"token": "synthetic-stale-token"}, "watcher token rejected by Grafana (status 401)"),
        ({"token": ""}, "watcher_token is not in secret/services/o11y"),
    ],
)
def test_each_failure_posts_one_named_message(tmp_path, kwargs, named):
    handler = make_handler(**kwargs)
    rc, out = run(tmp_path, handler)
    assert rc != 0
    assert len(handler.posts) == 1, out
    content = handler.posts[0]["content"]
    assert named in content
    assert TOKEN not in json.dumps(handler.posts) and HOOK_SECRET not in json.dumps(handler.posts)
    assert named in out


def test_check_mode_posts_nothing(tmp_path):
    handler = make_handler(grafana="down")
    with serve(handler) as addr:
        handler.addr = addr
        inventory = tmp_path / "inventory.yml"
        inventory.write_text(json.dumps({"all": {"vars": {"openbao_addr": addr}}}))
        env = {k: v for k, v in os.environ.items() if k not in ("OPENBAO_ADDR", "BAO_ROLE_ID", "BAO_SECRET_ID")}
        env.update(ANSIBLE_LOCAL_TEMP=str(tmp_path), ANSIBLE_STDOUT_CALLBACK="default", ANSIBLE_NOCOLOR="1")
        extra = {**ROLE, "o11y_watch_url": addr, "o11y_watch_webhook_hosts": ["127.0.0.1"]}
        result = subprocess.run(
            ["ansible-playbook", "--check", "-i", str(inventory), "-c", "local", PLAYBOOK, "-e", json.dumps(extra)],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
        )
    out = result.stdout + result.stderr
    assert "grafana /api/health failed (status 502)" in out, out
    assert handler.posts == []
    for secret in (TOKEN, HOOK_SECRET, "synthetic-login-value"):
        assert secret not in out


def test_watcher_is_scheduled_every_ten_minutes_from_dev():
    templates = yaml.safe_load((ROOT / "platform/semaphore/templates.yml").read_text())["templates"]
    watcher = [t for t in templates if t["playbook"] == PLAYBOOK]
    assert len(watcher) == 1
    assert watcher[0]["schedule"] == {"cron": "*/10 * * * *"}
    assert watcher[0]["repository"] == "agent-cloud dev"
    assert any(t["playbook"] == "platform/playbooks/provision-o11y-watcher-token.yml" for t in templates)
