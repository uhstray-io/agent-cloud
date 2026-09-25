"""Run both seed playbooks' read-only access checks against a synthetic OpenBao.

The check must never write, must not pass on a GET 404 alone (OpenBao answers 404 for a
missing path AND for one the token cannot see), and must require the capability the real
seed uses: `create` to POST a new path, `patch` to PATCH an existing one (reviews of
PR #205). tasks/assert-bao-seed-access.yml is the shared implementation.
"""


import pytest
from seed_harness import FakeBao, run_seed, serve, writes

PLAYBOOKS = {
    "postiz": ("platform/playbooks/seed-postiz-secrets.yml", "services/postiz",
               {"postiz_verify_access_only": True}, "Read-only Postiz access verified", "SEED_X_API_KEY"),
    "openbao-key": ("platform/playbooks/seed-openbao-key.yml", "services/agentgateway",
                    {"bao_verify_access_only": True, "bao_path": "services/agentgateway",
                     "bao_key": "vllm_api_key"}, "Read-only access to secret/services/agentgateway verified",
                    "BAO_VALUE"),
}
# The capability rule's variants are tested directly (test_seed_access_rule.py); each playbook
# keeps one pass and one refusal here, run for real, to prove it wires the rule in.
WIRING = "postiz"


def run_access_check(tmp_path, which, capabilities, provider="", exists=False, foreign=None,
                     override=None, declare=True, env_addr=False, inject=None, declared=None):
    playbook, path, extra_vars, _, own_input = PLAYBOOKS[which]

    class Bao(FakeBao):
        requests = []

        def do_POST(self):  # noqa: N802
            self.record("POST")
            body = self.body()
            if self.path == "/v1/auth/approle/login":
                return self.reply({"auth": {"client_token": "synthetic-login-value"}})
            if self.path == "/v1/sys/capabilities-self":
                assert body == {"paths": [f"secret/data/{path}"]}
                return self.reply({"capabilities": capabilities, f"secret/data/{path}": capabilities})
            return self.reply({}, 403)

        def do_GET(self):  # noqa: N802
            self.record("GET")
            if exists:
                return self.reply({"data": {"data": {"unrelated": "x"}, "metadata": {"version": 1}}})
            return self.reply({}, 404)  # a missing path, or one this token cannot see

        def do_PATCH(self):  # noqa: N802
            self.record("PATCH")
            return self.reply({}, 403)

    with serve(Bao) as address:
        # Only this template's declared input: the seed refuses any other (a leftover).
        inputs = {**({own_input: provider} if provider else {}),
                  **({foreign: "synthetic-leftover-value"} if foreign else {})}
        code, output = run_seed(tmp_path, playbook, address, verbose=True, declare=declare,
                                extra={**extra_vars, **({"openbao_addr": override} if override else {}),
                                       **(inject or {})},
                                inputs=inputs, env_addr=address if env_addr else None, declared=declared,
                                # A templated declaration resolves to the synthetic store.
                                inventory_vars={"openbao_host": address.removeprefix("http://")})
    return code, output, Bao.requests


@pytest.mark.parametrize("which", sorted(PLAYBOOKS))
def test_access_check_passes_and_never_writes_even_with_a_staged_value(tmp_path, which):
    code, output, requests = run_access_check(tmp_path, which, ["read", "create"], "synthetic-provider-value")
    assert code == 0, output
    assert PLAYBOOKS[which][3] in output
    assert ("POST", "/v1/sys/capabilities-self") in requests
    assert writes(requests) == []


@pytest.mark.parametrize("which", sorted(PLAYBOOKS))
def test_access_check_refuses_a_token_the_real_seed_would_be_denied(tmp_path, which):
    code, output, requests = run_access_check(tmp_path, which, ["read", "patch"], exists=False)
    assert code != 0, output
    assert PLAYBOOKS[which][3] not in output
    assert "seeding needs read plus create" in output
    assert writes(requests) == []


@pytest.mark.parametrize("which,foreign", [("postiz", "BAO_VALUE"), ("openbao-key", "SEED_X_API_KEY")])
def test_a_leftover_input_of_another_seed_is_refused_before_the_login(tmp_path, which, foreign):
    # The isolated environment is checked when bound; the run re-checks what actually arrived.
    code, output, requests = run_access_check(tmp_path, which, ["read", "create"], foreign=foreign)
    assert code != 0, output
    assert f"does not declare: {foreign}" in output
    assert requests == []  # not even the AppRole login


@pytest.mark.parametrize("which", sorted(PLAYBOOKS))
def test_an_address_other_than_the_inventorys_is_refused_before_the_login(tmp_path, which):
    # An environment edited after binding (or a -e) overrides the inventory's address.
    code, output, requests = run_access_check(tmp_path, which, ["read", "create"],
                                              override="http://127.0.0.1:9")
    assert code != 0, output
    assert "An extra var overrode it" in output
    assert requests == []  # not even the AppRole login


def test_an_inventory_without_the_address_is_refused_before_the_login(tmp_path):
    # The playbooks fall back to the controller's OPENBAO_ADDR; unverifiable, so refused.
    code, output, requests = run_access_check(tmp_path, WIRING, ["read", "create"], declare=False, env_addr=True)
    assert code != 0, output
    assert "declares no single all.vars.openbao_addr" in output
    assert requests == []


EVIL = "http://127.0.0.1:9"


@pytest.mark.parametrize("inject", [
    # Codex review of PR #256: forge the check's own inputs alongside the override.
    {"openbao_addr": EVIL, "_ba_declared": [EVIL], "_ba_url": EVIL, "_ba_seen": [EVIL]},
    {"_bao_url": EVIL},             # the login URL itself
    {"_bm_url": EVIL},              # the merge target, set later by the playbook
    {"_sa_url": EVIL},              # the access-check target
], ids=["forged-check-inputs", "login-url", "merge-url", "access-url"])
def test_injected_extra_vars_cannot_move_the_login_or_the_write(tmp_path, inject):
    code, output, requests = run_access_check(tmp_path, "openbao-key", ["read", "create"], inject=inject)
    assert code != 0, output
    # The login task must never RUN: a run whose login went to the injected address also shows
    # no request here and fails (nothing listens there), so an empty log alone proves nothing.
    assert "TASK [Refuse an OpenBao address that is not the inventory's]" in output
    assert "TASK [Authenticate to OpenBao (AppRole)]" not in output
    assert requests == []


def test_a_templated_declaration_is_refused_with_its_own_reason(tmp_path):
    code, output, requests = run_access_check(tmp_path, WIRING, ["read", "create"],
                                              declared="http://{{ openbao_host }}")
    assert code != 0, output
    assert "declares a templated all.vars.openbao_addr" in output
    assert requests == []
