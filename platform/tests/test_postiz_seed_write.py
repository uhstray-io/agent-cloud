"""seed-postiz-secrets.yml's write path, run for real against a stateful synthetic OpenBao.

The playbook writes through tasks/bao-merge-keys.yml (since 2026-09-25; it kept its own copy of
the merge-patch idiom before). Proves the behaviour the copy had: a new path is created with CAS
0, an existing path is merge-patched with its siblings kept, and a re-run writes nothing. All
values are synthetic.
"""

import shutil

import pytest
from seed_harness import FakeBao, run_seed, serve, writes

PLAYBOOK = "platform/playbooks/seed-postiz-secrets.yml"
PROVIDER = "synthetic-provider-value"

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def seed_once(tmp_path, stored):
    """Run the seed once with SEED_X_API_KEY set; `stored` is the path's data (None = absent)."""
    state = {"data": stored, "version": 0 if stored is None else 1}

    class Bao(FakeBao):
        requests = []

        def do_POST(self):  # noqa: N802
            self.record("POST")
            payload = self.body()
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            if self.path == "/v1/secret/data/services/postiz":
                if payload.get("options", {}).get("cas") == 0 and state["data"] is not None:
                    return self.reply({"errors": ["check-and-set parameter did not match"]}, 400)
                state["data"], state["version"] = payload["data"], state["version"] + 1
                return self.reply({"data": {"version": state["version"]}})
            return self.reply({}, 403)

        def do_GET(self):  # noqa: N802
            self.record("GET")
            if state["data"] is None:
                return self.reply({}, 404)
            return self.reply({"data": {"data": state["data"], "metadata": {"version": state["version"]}}})

        def do_PATCH(self):  # noqa: N802
            self.record("PATCH")
            if state["data"] is None:
                return self.reply({}, 404)
            state["data"] = {**state["data"], **self.body()["data"]}
            state["version"] += 1
            return self.reply({"data": {"version": state["version"]}})

    with serve(Bao) as address:
        code, output = run_seed(tmp_path, PLAYBOOK, address, inputs={"SEED_X_API_KEY": PROVIDER})
    return code, output, state, Bao.requests


def test_a_new_path_is_created_atomically(tmp_path):
    code, output, state, requests = seed_once(tmp_path, None)
    assert code == 0, output
    assert state["data"] == {"postiz_x_api_key": PROVIDER}
    assert writes(requests) == [("POST", "/v1/secret/data/services/postiz")]
    assert "Seeded 1 credential(s)" in output and "(new path)" in output


def test_an_existing_path_keeps_its_generated_siblings(tmp_path):
    generated = {"postiz_jwt_secret": "synthetic-generated", "postiz_db_password": "synthetic-db"}
    code, output, state, requests = seed_once(tmp_path, dict(generated))
    assert code == 0, output
    assert state["data"] == {**generated, "postiz_x_api_key": PROVIDER}
    assert writes(requests) == [("PATCH", "/v1/secret/data/services/postiz")]


def test_a_rerun_with_the_same_value_writes_nothing(tmp_path):
    code, output, state, requests = seed_once(tmp_path, {"postiz_x_api_key": PROVIDER, "other": "kept"})
    assert code == 0, output
    assert writes(requests) == []
    assert state["version"] == 1
    assert "Already stored, nothing written" in output
