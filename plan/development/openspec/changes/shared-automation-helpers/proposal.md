# Shared automation helpers: fold the hand-rolled skeletons into the task library

Author: Joseph A. Wisneski IV <stray@uhstray.io>. Status: **proposal awaiting operator
approval** — nothing here is implemented, and no task may start until the operator approves.

Source: the grounding `/simplify` pass over `dev` at `18534658` (range `df0c2cdf..HEAD`).
Every citation below was re-read against that commit; line numbers are as of `18534658`.

## Why

The composable task library is the platform's answer to "fix the mechanism, not the
symptom", but the last stretch of work re-implemented several skeletons by hand in each
playbook instead of promoting them. Copies drift, and drift in credential and verdict code is
where the costly mistakes live.

Verified evidence at `18534658`:

1. **Step-result verdict/rescue skeleton.** Each of these playbooks hand-builds its own
   block/rescue and verdict before including `tasks/emit-step-result.yml`:
   `provision-vm.yml` (rescue :668, emits :696 and :879), `resize-vm.yml` (:451 / :558),
   `provision-template.yml` (:94 / :245), `harden-ssh.yml` (:277 / :336),
   `distribute-ssh-keys.yml` (:65 / :339), `backup-service-ssh-key.yml` (:216 / :228),
   `backup-credentials-to-site-config.yml` (:233 / :245). Per-host folding is re-explained in
   comments in `deploy-authentik.yml:432`, `manage-caddy-sites.yml:285` and
   `verify-service-persistence.yml:199`; `apply-firewall.yml:806` emits without a shared
   rescue; `renew-internal-certs.yml` (rescue :223, comment :75) and `mount-caddy-certs.yml`
   (rescue :229) carry their own rescue capture.
2. **OpenBao AppRole login.** `auth/approle/login` is hand-rolled at **47** sites across 46
   files (`grep -rn 'auth/approle/login' platform/playbooks | wc -l`), each pairing its own
   transport assertion, `no_log` scoping and token fact.
3. **agentgateway secret read.** Three copies read `secret/data/services/{{ service_name }}`:
   `rollback-inference-route.yml:232` and `:596`, `run-agw-conformance.yml:166`. They have
   already drifted: the rollback copies expose the whole store and enumerate `direct_*` keys
   (:248–:252); the conformance copy derives `client_<name>` and `vllm_api_key` presence flags
   (:180–:183).
4. **Site-config deploy-key fetch.** `backup-credentials-to-site-config.yml:65` and
   `backup-step-ca-to-site-config.yml:60` each read `secret/data/services/ssh/site-config`
   themselves before calling `tasks/site-config-clone.yml`, which only refuses when no key was
   passed (:32).
5. **Internal-leaf issuance.** `tasks/issue-internal-leaf.yml` (284 lines) embeds Python
   inline (:69, :130 shell, :217) alongside its validation.
6. **Certificate renewal** declares per-leaf reload and proof by hand, and DNS records for
   server-profile leaves are listed separately from the leaves.
7. **Compose and agentgateway probing.** `platform/lib/common.sh` derives `COMPOSE_CMD`
   (:43–:53) but has no shared compose-file list or "up only if changed" helper; the
   agentgateway readiness/probe target is restated per consumer.
8. **Shared guards** exist but are not adopted uniformly: `require-reviewed-checkout.yml`
   and `preflight-target-group.yml` (whose header at :34 forbids survey-driven
   `target_service` expressions, so a `target_service` mode is missing).
9. **Test helpers** (`platform/tests/fake_http.py`, `harness_sandbox.py`, `playbook_yaml.py`,
   `ledger_citation_ratchet.txt`) are partly re-implemented per test.
10. **CI/pre-push speed.** `.github/workflows/lint-and-test.yml:285` runs `bats platform/tests/`
    serially; `pip install -r` repeats in three jobs (:218–:223, :258–:259, :278–:279) with no
    cache.
11. **Hygiene.** `check-o11y-liveness.yml` logs in (token at :82) and never revokes;
    `wire-caddy-cloudflare-token.yml:37` defaults `caddy_probe_host` to a literal public
    hostname in a public repo; OPA `launch_branches` lives in
    `platform/services/opa/deployment/policies/agentcloud/data.json` with no test keeping it in
    sync with Semaphore's repository records; Discord webhook-URL validation is repeated
    (`check-o11y-liveness.yml`, `seed-o11y-alert-webhook.yml`, `deploy-uhhcraft.yml`); PVE
    `until:` status waits repeat in `provision-vm.yml`, `resize-vm.yml`,
    `provision-template.yml`, `destroy-vm.yml`.

## What Changes

- New composable tasks: `tasks/capture-failure.yml`, `tasks/bao-login.yml`,
  `tasks/agw-read-secret.yml`, a PVE wait task and a Discord webhook assert task; extend
  `tasks/emit-step-result.yml` to accept per-host errors/evidence and fold them, and
  `tasks/site-config-clone.yml` to fetch its own deploy key.
- Internal-leaf issuance split: validation stays in the task, Python moves to
  `files/internal_leaf.py`, classification becomes a filter plugin.
- Renewal driven by per-leaf reload/proof declarations; DNS records derived from the
  server-profile leaves.
- Shared agentgateway readiness (`gateway-ready.sh`, `vars/agw-probe-target.yml`) and
  `compose_files()` / `compose_up_if_changed` in `platform/lib/common.sh`.
- Adopt the shared guards; add a `target_service` mode to the preflight.
- Shared test helpers; CI parallel BATS and dependency caching; merge duplicate expensive
  tests.
- Every refactor is behaviour-preserving and lands **one helper per PR**, characterization
  tests first.

## Capabilities

### New Capabilities
- `shared-automation-helpers`: one implementation per recurring automation skeleton, with a
  test that refuses a new hand-rolled copy.

## Impact

Playbooks under `platform/playbooks/`, `platform/lib/common.sh`, `platform/tests/`,
`.github/workflows/lint-and-test.yml`, `.githooks/pre-push`. No inventory, secret path,
Semaphore template name or OpenBao policy changes. No site values.

## Rollback Plan

Each helper lands as its own PR into `dev`; revert that merge commit. Characterization tests
written before each refactor pin the prior behaviour, so a revert restores a state the suite
already proves. No data or secret migration is involved.
