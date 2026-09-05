"""Exercise real Ansible control flow against a disposable n8n/provider HTTP stub."""

import copy
import json
import os
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[5]
PLAYBOOKS = ROOT / "platform/playbooks"
PROVISION = yaml.safe_load((PLAYBOOKS / "provision-tududi-github-sync.yml").read_text())[-1]
TOKEN = yaml.safe_load((PLAYBOOKS / "store-tududi-api-token.yml").read_text())[-1]


class Engine(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, value):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(value).encode())

    def do_GET(self):
        state = self.server.state
        state["reads"].append(self.path)
        path = self.path.split("?")[0]
        if path.startswith("/v1/secret/data/services/"):
            key = "n8n_api_key" if path.endswith("/n8n") else "api_token"
            self.respond({"data": {"data": {key: "fixture-value"}}})
        elif path == "/api/v1/projects":
            self.respond({"projects": [{"name": "dev-test"}]})
        elif path == "/api/v1/workflows":
            self.respond({"data": state["workflows"], "nextCursor": state.get("workflow_cursor")})
        elif path == "/api/v1/credentials":
            self.respond({"data": state["credentials"], "nextCursor": state.get("credential_cursor")})
        elif path.startswith("/api/v1/workflows/"):
            self.respond(next(w for w in state["workflows"] if w["id"] == path.split("/")[-1]))
        else:
            raise AssertionError(f"unexpected GET {path}")

    def mutate(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        state = self.server.state
        if self.path == "/v1/auth/approle/login":
            self.respond({"auth": {"client_token": "fixture-value"}})
            return
        state["writes"].append((self.command, self.path))
        if self.path.endswith(("/deactivate", "/activate")):
            workflow = next(w for w in state["workflows"] if w["id"] == self.path.split("/")[-2])
            if not state.get("ignore_deactivation"):
                workflow["active"] = self.path.endswith("/activate")
            self.respond(workflow)
        elif "/credentials" in self.path:
            if self.command == "POST":
                credential = {"id": body["name"], "name": body["name"]}
                state["credentials"].append(credential)
                self.respond(credential)
            else:
                self.respond({"id": self.path.split("/")[-1]})
        elif self.path == "/api/v1/workflows":
            workflow = dict(body, id="cycle", active=False)
            state["workflows"].append(workflow)
            self.respond(workflow)
        elif self.path.startswith("/api/v1/workflows/"):
            workflow = next(w for w in state["workflows"] if w["id"] == self.path.split("/")[-1])
            workflow.update(body)
            self.respond(workflow)
        else:
            raise AssertionError(f"unexpected {self.command} {self.path}")

    do_POST = mutate
    do_PUT = mutate
    do_PATCH = mutate
    do_DELETE = mutate


def prepare(value):
    if isinstance(value, list):
        return [prepare(item) for item in value]
    if not isinstance(value, dict):
        return value.replace("playbook_dir", repr(str(PLAYBOOKS))) if isinstance(value, str) else value
    result = {key: prepare(item) for key, item in value.items()}
    include = result.get("ansible.builtin.include_tasks")
    if include == "tasks/mint-tududi-sync-github-token.yml":
        # External GitHub signing is the only substituted include; engine calls
        # and all deactivation/refusal tasks execute exactly as committed.
        return {"name": result["name"], "ansible.builtin.set_fact": {"_mt_token": "fixture-value"}}
    if isinstance(include, str):
        result["ansible.builtin.include_tasks"] = str(PLAYBOOKS / include)
    return result


def run_play(play, directory):
    file = directory / "run.yml"
    file.write_text(yaml.safe_dump([play], sort_keys=False))
    env = dict(os.environ, ANSIBLE_LOCAL_TEMP=str(directory / "ansible"), ANSIBLE_NOCOLOR="1")
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(file)],
        env=env, text=True, capture_output=True, timeout=90,
    )


def initial_state():
    return {
        "workflows": [
            {"id": "cycle", "name": "tududi-github-sync: cycle", "active": True},
            {"id": "old", "name": "tududi-github-sync: old", "active": True},
            {"id": "other", "name": "tududi-github-sync-scratch", "active": True},
        ],
        "credentials": [
            {"id": "tududi", "name": "tududi-sync-api"},
            {"id": "github", "name": "github-sync-api"},
            {"id": "other", "name": "unrelated"},
        ],
        "reads": [], "writes": [],
    }


def provision_case(server, directory, mode, fault=None):
    state = initial_state()
    if mode == "initial":
        state["workflows"] = state["workflows"][2:]
        state["credentials"] = state["credentials"][2:]
    if fault == "duplicate":
        state["credentials"].append({"id": "duplicate", "name": "tududi-sync-api"})
    if fault == "duplicate_workflow":
        state["workflows"].append({"id": "duplicate", "name": "tududi-github-sync: cycle", "active": True})
    if fault in ("workflow_cursor", "credential_cursor", "ignore_deactivation"):
        state[fault] = "more" if fault.endswith("cursor") else True
    server.state = state
    mapping = directory / "mapping.yml"
    mapping.write_text(yaml.safe_dump({"sync_pairs": [
        {"tududi_project": "dev-test", "github_repo": "uhstray-io/dev-test", "enabled": mode != "disabled"},
    ]}))
    play = prepare(copy.deepcopy(PROVISION))
    play.update(hosts="localhost", connection="local")
    base = f"http://127.0.0.1:{server.server_port}"
    play["vars"].update(_bao_url=base, _bao_role_id="fixture", _bao_secret_id="fixture",
                         _n8n_base=base, _wf_tududi_url=base, _mapping_path=str(mapping))
    if mode in (False, "false"):
        play["vars"]["sync_enabled"] = mode
        mapping.write_text("malformed: [")
    if mode == "blank":
        play["vars"]["sync_enabled"] = ""
    result = run_play(play, directory)
    assert (result.returncode != 0) == bool(fault), result.stdout + result.stderr
    if fault:
        expected = {
            "duplicate": "credential list is truncated or sync names are duplicated",
            "duplicate_workflow": "Multiple cycle workflows have the same name",
            "workflow_cursor": "workflow list is truncated",
            "credential_cursor": "credential list is truncated or sync names are duplicated",
            "ignore_deactivation": "remains active after deactivation",
        }
        assert expected[fault] in result.stdout, result.stdout + result.stderr
    assert not any(method == "DELETE" for method, _ in state["writes"]), state["writes"]
    assert next(w for w in state["workflows"] if w["id"] == "other") == {
        "id": "other", "name": "tududi-github-sync-scratch", "active": True,
    }
    assert next(c for c in state["credentials"] if c["id"] == "other") == {"id": "other", "name": "unrelated"}
    assert len(state["workflows"]) == (2 if mode == "initial" else 4 if fault == "duplicate_workflow" else 3)
    assert len(state["credentials"]) == (4 if fault == "duplicate" else 3)
    if fault in ("duplicate", "duplicate_workflow", "workflow_cursor", "credential_cursor"):
        assert not state["writes"], state["writes"]
    elif not fault:
        if mode != "initial":
            assert not next(w for w in state["workflows"] if w["id"] == "old")["active"]
        if mode in (False, "false", "disabled"):
            assert not state["workflows"][0]["active"]
            assert not any(path == "/api/v1/projects" for path in state["reads"])
            assert not any("credentials" in path for _, path in state["writes"])
            # A repeated stop is a read-back-only no-op for inactive workflows.
            state["writes"].clear()
            result = run_play(play, directory)
            assert result.returncode == 0, result.stdout + result.stderr
            assert not state["writes"], state["writes"]
        else:
            assert next(w for w in state["workflows"] if w["id"] == "cycle")["active"]
            if mode != "initial":
                assert ("PATCH", "/api/v1/credentials/tududi") in state["writes"]
                assert ("PATCH", "/api/v1/credentials/github") in state["writes"]
                assert ("POST", "/api/v1/credentials") not in state["writes"]
    print(f"PASS provisioning {mode!r} {fault or 'preserves objects'}")


def token_guards(directory):
    start = next(i for i, task in enumerate(TOKEN["tasks"]) if task["name"] == "Decide convergence")
    end = next(i for i, task in enumerate(TOKEN["tasks"]) if task["name"].startswith("Mint (and capture)"))
    tasks = TOKEN["tasks"][start:end] + [
        {"name": "Reached initial mint", "ansible.builtin.debug": {"msg": "INITIAL_MINT"}, "when": "not _converged"},
    ]
    for validate, present, rows, proof in [
        (True, True, 1, 0), (True, False, 0, 1), (True, True, 1, 1),
        (False, True, 1, 0), (False, True, 1, 1), (False, False, 1, 1), (False, True, 0, 1), (False, False, 0, 1),
    ]:
        play = {"name": "Token preservation guards", "hosts": "localhost", "gather_facts": False,
                "vars": {"_validate_only": validate, "_bao_has_token": present, "_active_rows": rows,
                         "_stored_proof": {"rc": proof}, "_bao_path": "services/tududi", "_bao_key": "api_token",
                         "_token_label": "fixture", "_login_email": "fixture"}, "tasks": tasks}
        result = run_play(play, directory)
        converged = present and proof == 0
        success = converged or (not validate and not present and rows == 0)
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        assert ("INITIAL_MINT" in result.stdout) == (success and not validate and not converged), result.stdout
    print("PASS token proof-only and existing-access refusal guards")


def main():
    with tempfile.TemporaryDirectory(prefix="tududi-safety-") as tmp:
        directory = Path(tmp)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for mode, fault in [
                (False, None), ("false", None), ("disabled", None), ("enabled", None),
                ("initial", None), ("blank", None), ("enabled", "duplicate"), ("enabled", "duplicate_workflow"),
                ("enabled", "workflow_cursor"), (False, "workflow_cursor"), ("enabled", "credential_cursor"),
                (False, "ignore_deactivation"),
            ]:
                provision_case(server, directory, mode, fault)
            token_guards(directory)
        finally:
            server.shutdown()
            server.server_close()
    print("PROVISIONING SAFETY PASS")


if __name__ == "__main__":
    main()
